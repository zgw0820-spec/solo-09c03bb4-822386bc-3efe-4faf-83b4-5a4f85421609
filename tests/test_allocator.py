from __future__ import annotations

import pytest

from app.allocator import (
    NoFeasiblePaths,
    Reservation,
    allocate,
    scenario_report,
)

from .helpers import (
    DIAMOND_EDGES,
    DIAMOND_SWITCHES,
    THETA_EDGES,
    THETA_SWITCHES,
    make_topology,
)


def diamond():
    return make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES)


def theta():
    return make_topology(THETA_SWITCHES, THETA_EDGES)


def reserve(flow_id, src, dst, bw, primary, backup):
    return Reservation(flow_id, src, dst, bw, tuple(primary), tuple(backup))


def test_allocates_link_disjoint_pair():
    allocation = allocate(diamond(), [], "S1", "S4", 4)
    primary_links = {
        frozenset(edge) for edge in zip(allocation.primary_path, allocation.primary_path[1:])
    }
    backup_links = {
        frozenset(edge) for edge in zip(allocation.backup_path, allocation.backup_path[1:])
    }
    assert primary_links.isdisjoint(backup_links)
    assert allocation.primary_path[0] == "S1"
    assert allocation.primary_path[-1] == "S4"


def test_primary_capacity_is_enforced():
    topology = diamond()
    existing = [reserve("a", "S1", "S4", 8, ["S1", "S2", "S4"], ["S1", "S3", "S4"])]
    # S1->S2 and S1->S3 each have 10; only 2 left on each, so 3 cannot fit.
    with pytest.raises(NoFeasiblePaths):
        allocate(topology, existing, "S1", "S4", 3)


def test_backup_bandwidth_is_shared_across_different_failures():
    """Two flows whose primaries fail on different links may share standby."""
    topology = theta()
    # Flow a: primary S-A1-T, backup S-A2-T.  Flow b: primary S-A3-T,
    # backup S-A2-T.  Only one of them is on the A2 route in any single-link
    # failure scenario, so 8 + 8 on A2 fits in 10 per scenario.
    existing = [
        reserve("a", "S", "T", 8, ["S", "A1", "T"], ["S", "A2", "T"]),
        reserve("b", "S", "T", 8, ["S", "A3", "T"], ["S", "A2", "T"]),
    ]
    report = scenario_report(topology, existing)
    assert report["ok"], report["violations"]

    a_failure = next(
        scenario for scenario in report["scenarios"] if scenario["failed_link"] == "A1-S"
    )
    assert a_failure["flow_state"] == {"a": "backup", "b": "primary"}
    b_failure = next(
        scenario for scenario in report["scenarios"] if scenario["failed_link"] == "A3-S"
    )
    assert b_failure["flow_state"] == {"a": "primary", "b": "backup"}


def test_shared_backup_capacity_is_still_bounded_within_one_scenario():
    topology = theta()
    # Three flows of 4 all primary on S-A1-T and standby on S-A2-T: under the
    # A1-S failure all three backups are active, 12 > 10.
    existing = [
        reserve("a", "S", "T", 4, ["S", "A1", "T"], ["S", "A2", "T"]),
        reserve("b", "S", "T", 4, ["S", "A1", "T"], ["S", "A2", "T"]),
        reserve("c", "S", "T", 4, ["S", "A1", "T"], ["S", "A2", "T"]),
    ]
    report = scenario_report(topology, existing)
    assert not report["ok"]
    assert any("A1-S" in violation["scenario"] for violation in report["violations"])


def test_allocator_lets_new_flow_share_a_standby_route():
    topology = theta()
    existing = [
        reserve("a", "S", "T", 8, ["S", "A1", "T"], ["S", "A2", "T"]),
    ]
    # The new 2-unit flow keeps the A1 primary and shares A2 as standby.  In
    # the A1 failure scenario the A2 route carries 8 + 2 = 10, exactly the
    # capacity, so this is allowed.
    allocation = allocate(topology, existing, "S", "T", 2)
    assert allocation.primary_path == ("S", "A1", "T")
    assert allocation.backup_path == ("S", "A2", "T")
    reservations = existing + [
        reserve(
            "new",
            "S",
            "T",
            2,
            list(allocation.primary_path),
            list(allocation.backup_path),
        )
    ]
    report = scenario_report(topology, reservations)
    assert report["ok"], report["violations"]
    failure = next(
        scenario for scenario in report["scenarios"] if scenario["failed_link"] == "A1-S"
    )
    assert failure["flow_state"] == {"a": "backup", "new": "backup"}


def test_allocator_refuses_when_shared_standby_would_overflow():
    topology = diamond()
    existing = [
        reserve("a", "S1", "S4", 6, ["S1", "S2", "S4"], ["S1", "S3", "S4"]),
    ]
    # The new flow can only take the same half as primary (the other half has
    # no disjoint backup left), so its backup shares S1-S3-S4 with a in the
    # S1-S2 failure scenario: 6 + 5 = 11.
    with pytest.raises(NoFeasiblePaths):
        allocate(topology, existing, "S1", "S4", 6)
    # At 4 units the shared standby route is exactly 10 and fits.
    allocation = allocate(topology, existing, "S1", "S4", 4)
    reservations = existing + [
        reserve(
            "new",
            "S1",
            "S4",
            4,
            list(allocation.primary_path),
            list(allocation.backup_path),
        )
    ]
    report = scenario_report(topology, reservations)
    assert report["ok"], report["violations"]
    failure = next(
        scenario for scenario in report["scenarios"] if scenario["failed_link"] == "S1-S2"
    )
    assert failure["flow_state"] == {"a": "backup", "new": "backup"}


def test_direction_is_respected():
    topology = make_topology(
        ["S1", "S2", "S3", "S4"],
        [
            ("S1", "S2", 1, 100),
            ("S1", "S3", 100, 100),
            ("S3", "S4", 100, 100),
            ("S4", "S2", 100, 100),
        ],
    )
    # The direct S1->S2 link carries only 1 unit, so S1->S2 at 5 cannot use it;
    # the long way round is available, but then the only disjoint backup is the
    # 1-unit direct link, so no pair fits.
    allocate(topology, [], "S2", "S1", 5)
    with pytest.raises(NoFeasiblePaths):
        allocate(topology, [], "S1", "S2", 5)


def test_requires_two_link_disjoint_routes():
    topology = make_topology(["S1", "S2", "S3"], [("S1", "S2", 10, 10), ("S2", "S3", 10, 10)])
    with pytest.raises(NoFeasiblePaths, match="link-disjoint"):
        allocate(topology, [], "S1", "S3", 1)


def test_prefers_shortest_primary():
    allocation = allocate(diamond(), [], "S1", "S4", 1)
    assert allocation.primary_path in (("S1", "S2", "S4"), ("S1", "S3", "S4"))


def test_rejects_same_endpoint():
    with pytest.raises(NoFeasiblePaths):
        allocate(diamond(), [], "S1", "S1", 1)
