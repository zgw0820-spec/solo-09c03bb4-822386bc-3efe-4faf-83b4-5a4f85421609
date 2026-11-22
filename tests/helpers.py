"""Shared helpers for topology/allocation/API tests."""

from __future__ import annotations

import json
from pathlib import Path

from app.topology import Link, Topology


def make_topology(
    switches: list[str], edges: list[tuple[str, str, int, int]]
) -> Topology:
    links = []
    for a, b, cap_ab, cap_ba in edges:
        low, high = (a, b) if a < b else (b, a)
        if a < b:
            links.append(
                Link(f"{low}-{high}", low, high, cap_ab, cap_ba)
            )
        else:
            links.append(
                Link(f"{low}-{high}", low, high, cap_ba, cap_ab)
            )
    return Topology(switches, links)


def write_topology(
    path: Path, switches: list[str], edges: list[tuple[str, str, int, int]]
) -> Path:
    payload = {
        "switches": switches,
        "links": [
            {
                "a": a,
                "b": b,
                "capacity_a_to_b": cap_ab,
                "capacity_b_to_a": cap_ba,
            }
            for a, b, cap_ab, cap_ba in edges
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# A diamond with two disjoint routes S1->S4.
DIAMOND_SWITCHES = ["S1", "S2", "S3", "S4"]
DIAMOND_EDGES = [
    ("S1", "S2", 10, 10),
    ("S1", "S3", 10, 10),
    ("S2", "S4", 10, 10),
    ("S3", "S4", 10, 10),
]

# Three disjoint routes S->A1->T, S->A2->T, S->A3->T (a theta graph).
# Flows on different primaries can share the middle route as standby because
# the corresponding failures are different single-link scenarios.
THETA_SWITCHES = ["S", "A1", "A2", "A3", "T"]
THETA_EDGES = [
    ("S", "A1", 10, 10),
    ("A1", "T", 10, 10),
    ("S", "A2", 10, 10),
    ("A2", "T", 10, 10),
    ("S", "A3", 10, 10),
    ("A3", "T", 10, 10),
]
