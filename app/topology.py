"""Fixed switch topology loading and validation.

The topology is a small, undirected graph.  Every physical link is unique per
unordered pair of switches and carries one capacity per direction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

_SWITCH_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
MAX_SWITCHES = 8


class TopologyError(ValueError):
    """Raised when the topology file is invalid."""


@dataclass(frozen=True)
class Link:
    link_id: str
    a: str
    b: str
    cap_ab: int
    cap_ba: int

    def other(self, switch: str) -> str:
        if switch == self.a:
            return self.b
        if switch == self.b:
            return self.a
        raise KeyError(f"{switch!r} is not on link {self.link_id}")

    def capacity(self, src: str, dst: str) -> int:
        if src == self.a and dst == self.b:
            return self.cap_ab
        if src == self.b and dst == self.a:
            return self.cap_ba
        raise KeyError(f"direction {src}->{dst} is not on link {self.link_id}")


class Topology:
    """Validated, immutable view of the fixed topology."""

    def __init__(self, switches: Iterable[str], links: Iterable[Link]) -> None:
        switch_tuple = tuple(switches)
        if not switch_tuple:
            raise TopologyError("topology must contain at least one switch")
        if len(switch_tuple) > MAX_SWITCHES:
            raise TopologyError(f"topology may contain at most {MAX_SWITCHES} switches")
        if len(set(switch_tuple)) != len(switch_tuple):
            raise TopologyError("switch names must be unique")
        for switch in switch_tuple:
            if not _SWITCH_RE.match(switch):
                raise TopologyError(
                    f"invalid switch name {switch!r}; use 1-32 letters, digits or '_'"
                )

        by_id: dict[str, Link] = {}
        by_pair: dict[tuple[str, str], Link] = {}
        directed_capacity: dict[tuple[str, str], int] = {}
        adjacency: dict[str, set[str]] = {switch: set() for switch in switch_tuple}
        known = set(switch_tuple)

        for link in links:
            for endpoint in (link.a, link.b):
                if endpoint not in known:
                    raise TopologyError(
                        f"link {link.link_id!r} references unknown switch {endpoint!r}"
                    )
            if link.a == link.b:
                raise TopologyError(f"link {link.link_id!r} is a self-loop")
            pair = (link.a, link.b)
            if pair in by_pair:
                raise TopologyError(
                    f"duplicate physical link between {link.a!r} and {link.b!r}"
                )
            if link.link_id in by_id:
                raise TopologyError(f"duplicate link id {link.link_id!r}")
            if link.cap_ab <= 0 or link.cap_ba <= 0:
                raise TopologyError(
                    f"link {link.link_id!r} capacities must be positive in both directions"
                )
            by_id[link.link_id] = link
            by_pair[pair] = link
            directed_capacity[(link.a, link.b)] = link.cap_ab
            directed_capacity[(link.b, link.a)] = link.cap_ba
            adjacency[link.a].add(link.b)
            adjacency[link.b].add(link.a)

        self.switches = switch_tuple
        self.switch_set = frozenset(switch_tuple)
        self.links = tuple(by_id[key] for key in sorted(by_id))
        self._links_by_id = by_id
        self._links_by_pair = by_pair
        self._directed_capacity = directed_capacity
        self._adjacency = {
            switch: tuple(sorted(neighbours))
            for switch, neighbours in adjacency.items()
        }

    # -- lookups ---------------------------------------------------------
    @property
    def adjacency(self) -> Mapping[str, tuple[str, ...]]:
        return self._adjacency

    @property
    def directed_capacity(self) -> Mapping[tuple[str, str], int]:
        return self._directed_capacity

    def link_by_id(self, link_id: str) -> Link:
        return self._links_by_id[link_id]

    def link_between(self, src: str, dst: str) -> Link:
        pair = (src, dst) if src < dst else (dst, src)
        try:
            return self._links_by_pair[pair]
        except KeyError as exc:
            raise KeyError(f"no physical link between {src!r} and {dst!r}") from exc

    def capacity(self, src: str, dst: str) -> int:
        try:
            return self._directed_capacity[(src, dst)]
        except KeyError as exc:
            raise KeyError(f"no directed capacity {src!r}->{dst!r}") from exc

    def path_edges(self, path: Iterable[str]) -> tuple[tuple[str, str], ...]:
        nodes = tuple(path)
        edges: list[tuple[str, str]] = []
        for src, dst in zip(nodes, nodes[1:]):
            self.capacity(src, dst)  # validates the hop
            edges.append((src, dst))
        return tuple(edges)

    def path_links(self, path: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            self.link_between(src, dst).link_id
            for src, dst in self.path_edges(path)
        )

    def validate_path(self, path: Iterable[str]) -> tuple[str, ...]:
        nodes = tuple(path)
        if len(nodes) < 2:
            raise TopologyError("a path needs at least two switches")
        if nodes[0] not in self.switch_set or nodes[-1] not in self.switch_set:
            raise TopologyError("path endpoint is not part of the topology")
        self.path_edges(nodes)
        return nodes

    def describe(self) -> dict[str, Any]:
        return {
            "switches": list(self.switches),
            "max_switches": MAX_SWITCHES,
            "links": [
                {
                    "link": link.link_id,
                    "a": link.a,
                    "b": link.b,
                    "capacity_a_to_b": link.cap_ab,
                    "capacity_b_to_a": link.cap_ba,
                }
                for link in self.links
            ],
        }


def _as_positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TopologyError(f"{where} must be a positive integer")
    return value


def parse_topology(payload: Any) -> Topology:
    if not isinstance(payload, dict):
        raise TopologyError("topology must be a JSON object")
    unknown = set(payload) - {"switches", "links"}
    if unknown:
        raise TopologyError(f"unknown topology keys: {sorted(unknown)}")

    switches = payload.get("switches")
    if not isinstance(switches, list) or not switches:
        raise TopologyError("'switches' must be a non-empty list")
    for switch in switches:
        if not isinstance(switch, str) or not _SWITCH_RE.match(switch):
            raise TopologyError(f"invalid switch name {switch!r}")

    raw_links = payload.get("links")
    if not isinstance(raw_links, list):
        raise TopologyError("'links' must be a list")

    links: list[Link] = []
    for index, raw in enumerate(raw_links):
        where = f"links[{index}]"
        if not isinstance(raw, dict):
            raise TopologyError(f"{where} must be an object")
        expected = {"a", "b", "capacity_a_to_b", "capacity_b_to_a"}
        if set(raw) != expected:
            raise TopologyError(f"{where} must have exactly the keys {sorted(expected)}")
        a, b = raw["a"], raw["b"]
        if not isinstance(a, str) or not isinstance(b, str):
            raise TopologyError(f"{where} endpoints must be strings")
        if a == b:
            raise TopologyError(f"{where} is a self-loop")
        # Canonical ordering keeps one physical link per unordered pair.
        low, high = (a, b) if a < b else (b, a)
        cap_low_high = _as_positive_int(
            raw["capacity_a_to_b"] if a < b else raw["capacity_b_to_a"],
            f"{where}.capacity_{a}_to_{b}",
        )
        cap_high_low = _as_positive_int(
            raw["capacity_b_to_a"] if a < b else raw["capacity_a_to_b"],
            f"{where}.capacity_{b}_to_{a}",
        )
        links.append(
            Link(
                link_id=f"{low}-{high}",
                a=low,
                b=high,
                cap_ab=cap_low_high,
                cap_ba=cap_high_low,
            )
        )

    return Topology(switches, links)


def load_topology(path: str | Path) -> Topology:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise TopologyError(f"cannot read topology file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TopologyError(f"topology file {path} is not valid JSON: {exc}") from exc
    return parse_topology(payload)
