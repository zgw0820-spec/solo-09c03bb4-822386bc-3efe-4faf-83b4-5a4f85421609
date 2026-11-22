"""Path allocation for primary/backup pairs under per-scenario capacity.

Capacity model
--------------
Every reservation is active on its primary path while nothing is broken.  A
failure is a single physical link (both directions).  A reservation whose
primary path uses the failed physical link moves to its backup path; all other
reservations stay on their primary.  For the normal state and for every single
physical-link failure we sum the demand per directed edge and require it to stay
within that direction's capacity.

Because standby paths are only charged in the scenarios that actually invoke
them, two reservations may share standby bandwidth when their primaries fail on
different links.  They may never exceed a direction's capacity *within one
scenario*.

The search is exhaustive over simple primary paths and, for each feasible
primary, an exhaustive backup search restricted to edges that remain usable in
every scenario triggered by a primary-link failure.  Cycles can always be
removed without increasing per-edge usage, so considering only simple paths
does not lose solutions.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable, Mapping

from .topology import Topology

Edge = tuple[str, str]


class AllocationError(Exception):
    """Base class for allocation failures."""


class NoFeasiblePaths(AllocationError):
    """No primary/backup pair satisfies disjointness and capacity rules."""


class FlowIdConflict(AllocationError):
    """The flow id exists with different request parameters."""


@dataclass(frozen=True)
class Reservation:
    flow_id: str
    src: str
    dst: str
    bandwidth: int
    primary_path: tuple[str, ...]
    backup_path: tuple[str, ...]


@dataclass(frozen=True)
class Allocation:
    primary_path: tuple[str, ...]
    backup_path: tuple[str, ...]


def enumerate_simple_paths(
    topology: Topology, src: str, dst: str
) -> tuple[tuple[str, ...], ...]:
    """All simple paths from src to dst, shortest (then lexicographic) first."""
    if src == dst:
        return ()
    paths: list[tuple[str, ...]] = []
    path: list[str] = [src]
    visited: set[str] = {src}

    def walk(node: str) -> None:
        if node == dst:
            paths.append(tuple(path))
            return
        for nxt in topology.adjacency[node]:
            if nxt in visited:
                continue
            visited.add(nxt)
            path.append(nxt)
            walk(nxt)
            path.pop()
            visited.remove(nxt)

    walk(src)
    paths.sort(key=lambda candidate: (len(candidate), candidate))
    return tuple(paths)


def _shortest_path_over_edges(
    topology: Topology,
    src: str,
    dst: str,
    allowed_edges: set[Edge],
    forbidden_links: set[str],
) -> tuple[str, ...] | None:
    """BFS shortest simple path using only allowed directed edges/links.

    Any simple path inside ``allowed_edges`` satisfies every scenario bound the
    set was built from, so returning the shortest one is both complete and
    cheap.  BFS never revisits a node, hence the result is simple.
    """
    adjacency: dict[str, list[str]] = defaultdict(list)
    for a, b in sorted(allowed_edges):
        if topology.link_between(a, b).link_id in forbidden_links:
            continue
        adjacency[a].append(b)
    for targets in adjacency.values():
        targets.sort()

    queue: deque[tuple[str, tuple[str, ...]]] = deque([(src, (src,))])
    seen: set[str] = {src}
    while queue:
        node, path = queue.popleft()
        if node == dst:
            return path
        for nxt in adjacency.get(node, ()):
            if nxt in seen:
                continue
            seen.add(nxt)
            queue.append((nxt, path + (nxt,)))
    return None


def _backup_for(
    topology: Topology,
    usage: ScenarioUsage,
    src: str,
    dst: str,
    primary_links: frozenset[str],
    bandwidth: int,
) -> tuple[str, ...] | None:
    """Shortest simple backup disjoint from the primary and feasible in every
    scenario where one of the primary links has failed."""
    capacity = topology.directed_capacity
    allowed: set[Edge] | None = None
    for link_id in primary_links:
        scenario = usage.failure[link_id]
        edge_set = {
            edge
            for edge, cap in capacity.items()
            if scenario[edge] + bandwidth <= cap
        }
        allowed = edge_set if allowed is None else allowed & edge_set
        if not allowed:
            return None
    if allowed is None:
        return None
    return _shortest_path_over_edges(
        topology,
        src,
        dst,
        allowed_edges=allowed,
        forbidden_links=set(primary_links),
    )


class ScenarioUsage:
    """Capacity accounting for the normal state and every single link failure."""

    def __init__(self, topology: Topology, reservations: Iterable[Reservation]):
        self.topology = topology
        self.reservations = tuple(reservations)
        self.normal: dict[Edge, int] = defaultdict(int)
        self.failure: dict[str, dict[Edge, int]] = {
            link.link_id: defaultdict(int) for link in topology.links
        }

        for reservation in self.reservations:
            primary_edges = topology.path_edges(reservation.primary_path)
            backup_edges = topology.path_edges(reservation.backup_path)
            primary_links = frozenset(
                topology.path_links(reservation.primary_path)
            )
            demand = reservation.bandwidth
            for edge in primary_edges:
                self.normal[edge] += demand
            for link in topology.links:
                usage = self.failure[link.link_id]
                if link.link_id in primary_links:
                    for edge in backup_edges:
                        usage[edge] += demand
                else:
                    for edge in primary_edges:
                        usage[edge] += demand

    def usage(self, failed_link: str | None) -> Mapping[Edge, int]:
        if failed_link is None:
            return self.normal
        return self.failure[failed_link]


def _primary_feasible(
    topology: Topology,
    usage: ScenarioUsage,
    primary_edges: tuple[Edge, ...],
    primary_links: frozenset[str],
    bandwidth: int,
) -> bool:
    """Check the new flow's primary on every scenario where it stays primary."""
    capacity = topology.directed_capacity
    for edge in primary_edges:
        if usage.normal[edge] + bandwidth > capacity[edge]:
            return False
    for link in topology.links:
        if link.link_id in primary_links:
            continue
        scenario = usage.failure[link.link_id]
        for edge in primary_edges:
            if scenario[edge] + bandwidth > capacity[edge]:
                return False
    return True


def allocate(
    topology: Topology,
    existing: Iterable[Reservation],
    src: str,
    dst: str,
    bandwidth: int,
) -> Allocation:
    """Return a feasible primary/backup pair or raise NoFeasiblePaths."""
    if src == dst:
        raise NoFeasiblePaths("source and destination must be different switches")

    usage = ScenarioUsage(topology, existing)

    for primary in enumerate_simple_paths(topology, src, dst):
        primary_edges = topology.path_edges(primary)
        primary_links = frozenset(topology.path_links(primary))
        if not _primary_feasible(
            topology, usage, primary_edges, primary_links, bandwidth
        ):
            continue
        backup = _backup_for(topology, usage, src, dst, primary_links, bandwidth)
        if backup is not None:
            return Allocation(primary_path=primary, backup_path=backup)

    raise NoFeasiblePaths(
        f"no link-disjoint primary/backup pair for {src}->{dst} "
        f"at {bandwidth} bandwidth within the per-scenario capacity limits"
    )


# -- reporting ---------------------------------------------------------------


def scenario_report(
    topology: Topology, reservations: Iterable[Reservation]
) -> dict:
    """Per-direction usage for the normal state and each single-link failure."""
    reservations = tuple(reservations)
    usage = ScenarioUsage(topology, reservations)
    capacity = topology.directed_capacity
    edges = sorted(capacity)
    primary_links = {
        reservation.flow_id: frozenset(
            topology.path_links(reservation.primary_path)
        )
        for reservation in reservations
    }

    def edge_rows(
        scenario: Mapping[Edge, int], failed_link: str | None
    ) -> list[dict]:
        rows = []
        for edge in edges:
            link_id = topology.link_between(*edge).link_id
            used = scenario.get(edge, 0)
            rows.append(
                {
                    "from": edge[0],
                    "to": edge[1],
                    "link": link_id,
                    "available": failed_link != link_id,
                    "used": used,
                    "capacity": capacity[edge],
                    "within_capacity": used <= capacity[edge],
                }
            )
        return rows

    scenarios = [
        {
            "name": "normal",
            "failed_link": None,
            "flow_state": {r.flow_id: "primary" for r in reservations},
            "edges": edge_rows(usage.normal, None),
        }
    ]
    violations: list[dict] = []

    def collect(scenario_name: str, rows: list[dict]) -> None:
        for row in rows:
            if row["available"] and not row["within_capacity"]:
                violations.append(
                    {
                        "scenario": scenario_name,
                        "from": row["from"],
                        "to": row["to"],
                        "used": row["used"],
                        "capacity": row["capacity"],
                    }
                )

    collect("normal", scenarios[0]["edges"])
    for link in topology.links:
        scenario = usage.failure[link.link_id]
        flow_state = {
            reservation.flow_id: (
                "backup" if link.link_id in primary_links[reservation.flow_id]
                else "primary"
            )
            for reservation in reservations
        }
        rows = edge_rows(scenario, link.link_id)
        name = f"failure:{link.link_id}"
        scenarios.append(
            {
                "name": name,
                "failed_link": link.link_id,
                "flow_state": flow_state,
                "edges": rows,
            }
        )
        collect(name, rows)

    return {
        "ok": not violations,
        "flow_count": len(reservations),
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
        "violations": violations,
    }
