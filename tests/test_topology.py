from __future__ import annotations

import pytest

from app.topology import TopologyError, parse_topology


def payload(links, switches=None):
    return {
        "switches": switches or ["S1", "S2", "S3"],
        "links": [
            {"a": a, "b": b, "capacity_a_to_b": cap_ab, "capacity_b_to_a": cap_ba}
            for a, b, cap_ab, cap_ba in links
        ],
    }


def test_parses_and_canonicalises_links():
    topology = parse_topology(
        payload([("S3", "S1", 2, 5)], switches=["S1", "S3"])
    )
    link = topology.links[0]
    assert (link.a, link.b) == ("S1", "S3")
    # a_to_b follows the canonical ordering, not the input ordering.
    assert topology.capacity("S1", "S3") == 5
    assert topology.capacity("S3", "S1") == 2


def test_rejects_more_than_eight_switches():
    switches = [f"S{i}" for i in range(1, 10)]
    with pytest.raises(TopologyError, match="at most 8"):
        parse_topology({"switches": switches, "links": []})


def test_rejects_duplicate_physical_link_in_either_order():
    with pytest.raises(TopologyError, match="duplicate physical link"):
        parse_topology(payload([("S1", "S2", 1, 1), ("S2", "S1", 1, 1)]))


def test_rejects_self_loop():
    with pytest.raises(TopologyError, match="self-loop"):
        parse_topology(payload([("S1", "S1", 1, 1)]))


def test_rejects_non_positive_direction_capacity():
    with pytest.raises(TopologyError, match="positive"):
        parse_topology(payload([("S1", "S2", 0, 1)]))


def test_rejects_unknown_endpoint():
    with pytest.raises(TopologyError, match="unknown switch"):
        parse_topology(payload([("S1", "S9", 1, 1)]))


def test_rejects_duplicate_switch_names():
    with pytest.raises(TopologyError, match="unique"):
        parse_topology({"switches": ["S1", "S1"], "links": []})
