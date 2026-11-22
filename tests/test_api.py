from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .helpers import write_topology


@pytest.fixture()
def client(tmp_path):
    topology_path = write_topology(
        tmp_path / "topology.json",
        ["S1", "S2", "S3", "S4"],
        [
            ("S1", "S2", 10, 10),
            ("S1", "S3", 10, 10),
            ("S2", "S4", 10, 10),
            ("S3", "S4", 10, 10),
        ],
    )
    app = create_app(topology_path=topology_path, db_path=tmp_path / "db.sqlite")
    with TestClient(app) as test_client:
        yield test_client


def test_topology_endpoint_exposes_directions(client):
    response = client.get("/topology")
    assert response.status_code == 200
    body = response.json()
    assert body["switches"] == ["S1", "S2", "S3", "S4"]
    assert body["max_switches"] == 8
    link = next(item for item in body["links"] if item["link"] == "S1-S2")
    assert link["capacity_a_to_b"] == 10
    assert link["capacity_b_to_a"] == 10


def test_submit_returns_201_with_disjoint_paths(client):
    response = client.post(
        "/flows", json={"flow_id": "f1", "src": "S1", "dst": "S4", "bandwidth": 4}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    assert body["primary_path"][0] == "S1"
    assert body["primary_path"][-1] == "S4"
    assert set(body["primary_path"][1:-1]).isdisjoint(set(body["backup_path"][1:-1]))


def test_identical_retry_returns_200_and_same_paths(client):
    payload = {"flow_id": "f1", "src": "S1", "dst": "S4", "bandwidth": 4}
    first = client.post("/flows", json=payload)
    second = client.post("/flows", json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["primary_path"] == first.json()["primary_path"]
    assert second.json()["backup_path"] == first.json()["backup_path"]
    assert second.json()["created"] is False
    assert len(client.get("/flows").json()) == 1


def test_changed_parameters_return_409(client):
    client.post("/flows", json={"flow_id": "f1", "src": "S1", "dst": "S4", "bandwidth": 4})
    response = client.post(
        "/flows", json={"flow_id": "f1", "src": "S1", "dst": "S4", "bandwidth": 5}
    )
    assert response.status_code == 409
    stored = client.get("/flows/f1").json()
    assert stored["bandwidth"] == 4


def test_unknown_switch_is_422(client):
    response = client.post(
        "/flows", json={"flow_id": "f1", "src": "S1", "dst": "S9", "bandwidth": 1}
    )
    assert response.status_code == 422
    assert "S9" in response.json()["detail"]


def test_same_endpoint_is_422(client):
    response = client.post(
        "/flows", json={"flow_id": "f1", "src": "S1", "dst": "S1", "bandwidth": 1}
    )
    assert response.status_code == 422


def test_infeasible_capacity_is_422_and_nothing_is_written(client):
    response = client.post(
        "/flows", json={"flow_id": "big", "src": "S1", "dst": "S4", "bandwidth": 11}
    )
    assert response.status_code == 422
    assert client.get("/flows").json() == []
    assert client.get("/flows/big").status_code == 404


def test_missing_flow_is_404(client):
    assert client.get("/flows/nope").status_code == 404


def test_validation_rejects_extra_fields_and_zero_bandwidth(client):
    extra = client.post(
        "/flows",
        json={"flow_id": "f1", "src": "S1", "dst": "S4", "bandwidth": 1, "x": 1},
    )
    assert extra.status_code == 422
    zero = client.post(
        "/flows", json={"flow_id": "f1", "src": "S1", "dst": "S4", "bandwidth": 0}
    )
    assert zero.status_code == 422


def test_report_enumerates_normal_and_all_failures(client):
    client.post("/flows", json={"flow_id": "f1", "src": "S1", "dst": "S4", "bandwidth": 4})
    report = client.get("/report").json()
    assert report["ok"] is True
    assert report["scenario_count"] == 1 + 4
    names = [scenario["name"] for scenario in report["scenarios"]]
    assert names[0] == "normal"
    assert any(name.startswith("failure:") for name in names)
