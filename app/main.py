"""FastAPI application: topology info, flow submission, reservations, report."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .allocator import FlowIdConflict, NoFeasiblePaths
from .store import ReservationStore
from .topology import TopologyError, load_topology

DEFAULT_TOPOLOGY = Path(__file__).resolve().parent.parent / "topology.json"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "reservations.db"


class FlowRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "flow_id": "cam-001",
                    "src": "SW1",
                    "dst": "SW8",
                    "bandwidth": 4,
                }
            ]
        },
    )

    flow_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        description="Stable flow identifier; retries with identical parameters are idempotent.",
        examples=["cam-001"],
    )
    src: str = Field(min_length=1, max_length=32, description="Ingress switch.", examples=["SW1"])
    dst: str = Field(min_length=1, max_length=32, description="Egress switch.", examples=["SW8"])
    bandwidth: int = Field(
        gt=0,
        description="Reserved bandwidth per direction, in the same unit as the topology capacities.",
        examples=[4],
    )


class LinkModel(BaseModel):
    link: str
    a: str
    b: str
    capacity_a_to_b: int
    capacity_b_to_a: int


class TopologyResponse(BaseModel):
    switches: list[str]
    max_switches: int
    links: list[LinkModel]


class FlowResponse(BaseModel):
    flow_id: str
    src: str
    dst: str
    bandwidth: int
    primary_path: list[str]
    backup_path: list[str]
    created: bool
    message: str


def create_app(
    topology_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> FastAPI:
    topology_path = Path(
        topology_path or os.environ.get("TOPOLOGY_PATH", DEFAULT_TOPOLOGY)
    )
    db_path = Path(db_path or os.environ.get("DB_PATH", DEFAULT_DB))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        topology = load_topology(topology_path)
        app.state.topology = topology
        app.state.store = ReservationStore(db_path, topology)
        yield

    app = FastAPI(
        title="Camera Backhaul Admission API",
        version="1.0.0",
        description=(
            "Admission control for camera-to-recording-room backhaul flows.\n\n"
            "Each accepted flow gets a link-disjoint primary/backup path pair. "
            "During normal operation only primaries carry traffic; on a single "
            "physical-link failure only flows whose primary uses that link move "
            "to their backup. Every scenario's per-direction usage stays within "
            "capacity. Identical retries return the stored paths; changing "
            "parameters under an existing flow id is rejected."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(TopologyError)
    async def _topology_error(_: Request, exc: TopologyError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    def store(request: Request) -> ReservationStore:
        return request.app.state.store

    @app.get("/health", tags=["ops"])
    def health(request: Request) -> dict:
        return {
            "status": "ok",
            "switches": len(request.app.state.topology.switches),
            "links": len(request.app.state.topology.links),
        }

    @app.get(
        "/topology",
        response_model=TopologyResponse,
        tags=["topology"],
        summary="Fixed switch topology and per-direction link capacities",
    )
    def topology(request: Request) -> TopologyResponse:
        return TopologyResponse(**request.app.state.topology.describe())

    @app.post(
        "/flows",
        response_model=FlowResponse,
        status_code=201,
        tags=["flows"],
        summary="Submit a flow; returns its primary/backup paths",
        responses={
            200: {"description": "Idempotent retry: identical parameters, stored paths returned."},
            409: {"description": "Flow id already exists with different parameters."},
            422: {"description": "Request validation failed or no feasible path exists."},
        },
    )
    def submit_flow(payload: FlowRequest, request: Request) -> JSONResponse:
        topology = request.app.state.topology
        for switch in (payload.src, payload.dst):
            if switch not in topology.switch_set:
                raise HTTPException(
                    status_code=422,
                    detail=f"switch {switch!r} is not part of the fixed topology",
                )
        if payload.src == payload.dst:
            raise HTTPException(
                status_code=422, detail="src and dst must be different switches"
            )
        try:
            reservation, created = store(request).submit(
                payload.flow_id, payload.src, payload.dst, payload.bandwidth
            )
        except FlowIdConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except NoFeasiblePaths as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        body = FlowResponse(
            flow_id=reservation.flow_id,
            src=reservation.src,
            dst=reservation.dst,
            bandwidth=reservation.bandwidth,
            primary_path=list(reservation.primary_path),
            backup_path=list(reservation.backup_path),
            created=created,
            message=(
                "flow registered"
                if created
                else "flow already registered; original paths returned"
            ),
        )
        return JSONResponse(
            status_code=201 if created else 200,
            content=body.model_dump(),
        )

    @app.get(
        "/flows",
        response_model=list[FlowResponse],
        tags=["flows"],
        summary="List registered flows",
    )
    def list_flows(request: Request) -> list[FlowResponse]:
        return [
            FlowResponse(
                flow_id=reservation.flow_id,
                src=reservation.src,
                dst=reservation.dst,
                bandwidth=reservation.bandwidth,
                primary_path=list(reservation.primary_path),
                backup_path=list(reservation.backup_path),
                created=False,
                message="stored reservation",
            )
            for reservation in store(request).list()
        ]

    @app.get(
        "/flows/{flow_id}",
        response_model=FlowResponse,
        tags=["flows"],
        summary="Fetch one registered flow",
    )
    def get_flow(flow_id: str, request: Request) -> FlowResponse:
        reservation = store(request).get(flow_id)
        if reservation is None:
            raise HTTPException(status_code=404, detail=f"flow {flow_id!r} not found")
        return FlowResponse(
            flow_id=reservation.flow_id,
            src=reservation.src,
            dst=reservation.dst,
            bandwidth=reservation.bandwidth,
            primary_path=list(reservation.primary_path),
            backup_path=list(reservation.backup_path),
            created=False,
            message="stored reservation",
        )

    @app.get(
        "/report",
        tags=["report"],
        summary="Per-direction usage in the normal state and each single-link failure",
    )
    def report(request: Request) -> dict:
        return store(request).report()

    return app


app = create_app()
