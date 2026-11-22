"""Topology binding: one reservation store serves exactly one fixed topology."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import ReservationStore, TopologyBindingError
from app.topology import (
    canonical_description,
    describe_differences,
    load_topology,
    topology_fingerprint,
)

from .helpers import DIAMOND_EDGES, DIAMOND_SWITCHES, make_topology, write_topology

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def shuffled_edges(edges):
    """Same links, reversed file order with a/b labels swapped."""
    return [(b, a, cap_ba, cap_ab) for a, b, cap_ab, cap_ba in reversed(edges)]


def read_binding(db_path) -> sqlite3.Row | None:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        try:
            return connection.execute(
                "SELECT fingerprint, canonical_json FROM topology_binding WHERE id = 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # a refused startup leaves even the table uncreated
    finally:
        connection.close()


def read_reservations(db_path) -> list[tuple]:
    connection = sqlite3.connect(str(db_path))
    try:
        return connection.execute(
            "SELECT flow_id, src, dst, bandwidth, primary_path, backup_path "
            "FROM reservations ORDER BY flow_id"
        ).fetchall()
    finally:
        connection.close()


def make_legacy_db(db_path, topology, flows):
    """A database as written before topology bindings existed."""
    store = ReservationStore(db_path, topology)
    for flow_id, src, dst, bandwidth in flows:
        store.submit(flow_id, src, dst, bandwidth)
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("DROP TABLE topology_binding")
        connection.commit()
    finally:
        connection.close()
    return db_path


# -- fingerprint semantics ----------------------------------------------------


def test_fingerprint_ignores_file_layout():
    """Same switches/links/per-direction capacities => same fingerprint."""
    topology = make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES)
    rearranged = make_topology(
        list(reversed(DIAMOND_SWITCHES)), shuffled_edges(DIAMOND_EDGES)
    )
    assert canonical_description(topology) == canonical_description(rearranged)
    assert topology_fingerprint(topology) == topology_fingerprint(rearranged)


def test_fingerprint_changes_with_any_direction_capacity():
    topology = make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES)
    edges = DIAMOND_EDGES.copy()
    a, b, cap_ab, cap_ba = edges[0]
    edges[0] = (a, b, cap_ab, cap_ba - 1)
    assert topology_fingerprint(topology) != topology_fingerprint(
        make_topology(DIAMOND_SWITCHES, edges)
    )


def test_describe_differences_pinpoints_direction_capacity():
    stored = canonical_description(make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES))
    edges = DIAMOND_EDGES.copy()
    edges[0] = ("S1", "S2", 10, 7)
    current = canonical_description(make_topology(DIAMOND_SWITCHES, edges))
    differences = describe_differences(stored, current)
    assert differences == ["link S1-S2 capacity S2->S1: bound 10, configured 7"]


# -- first bind and equivalent configs ----------------------------------------


def test_first_process_binds_and_equivalent_config_shares(tmp_path):
    db_path = tmp_path / "reservations.db"
    first = ReservationStore(db_path, make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES))
    first.submit("f1", "S1", "S4", 4)

    # A second process with a textually different but semantically identical
    # configuration opens the same store without complaint.
    second = ReservationStore(
        db_path,
        make_topology(list(reversed(DIAMOND_SWITCHES)), shuffled_edges(DIAMOND_EDGES)),
    )
    assert second.fingerprint == first.fingerprint
    reservation = second.get("f1")
    assert reservation is not None and reservation.bandwidth == 4
    assert read_binding(db_path)["fingerprint"] == first.fingerprint


def test_binding_row_written_once(tmp_path):
    db_path = tmp_path / "reservations.db"
    topology = make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES)
    ReservationStore(db_path, topology)
    ReservationStore(db_path, topology)
    connection = sqlite3.connect(str(db_path))
    try:
        count = connection.execute("SELECT COUNT(*) FROM topology_binding").fetchone()[0]
    finally:
        connection.close()
    assert count == 1


# -- conflicting configs are refused ------------------------------------------


def test_conflicting_capacity_refused_and_database_untouched(tmp_path):
    db_path = tmp_path / "reservations.db"
    first = ReservationStore(db_path, make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES))
    first.submit("f1", "S1", "S4", 4)
    before_reservations = read_reservations(db_path)
    before_binding = read_binding(db_path)["fingerprint"]

    edges = DIAMOND_EDGES.copy()
    edges[0] = ("S1", "S2", 4, 10)  # shrink one direction
    with pytest.raises(TopologyBindingError) as excinfo:
        ReservationStore(db_path, make_topology(DIAMOND_SWITCHES, edges))
    message = str(excinfo.value)
    assert "already bound" in message
    assert "S1-S2" in message and "S1->S2" in message
    assert "bound 10, configured 4" in message

    # Reservations and the existing binding are preserved.
    assert read_reservations(db_path) == before_reservations
    assert read_binding(db_path)["fingerprint"] == before_binding
    # The originally bound process keeps working.
    assert first.report()["ok"] is True


def test_missing_or_extra_links_refused(tmp_path):
    db_path = tmp_path / "reservations.db"
    ReservationStore(db_path, make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES))

    with pytest.raises(TopologyBindingError, match=r"missing from the configuration"):
        ReservationStore(
            db_path, make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES[:-1])
        )
    with pytest.raises(TopologyBindingError, match=r"not in the bound topology"):
        ReservationStore(
            db_path,
            make_topology(
                DIAMOND_SWITCHES + ["S5"],
                DIAMOND_EDGES + [("S4", "S5", 10, 10)],
            ),
        )


# -- legacy database adoption --------------------------------------------------


def test_legacy_database_adopted_when_reservations_honourable(tmp_path):
    topology = make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES)
    db_path = make_legacy_db(
        tmp_path / "reservations.db", topology, [("f1", "S1", "S4", 4)]
    )
    before = read_reservations(db_path)

    store = ReservationStore(db_path, topology)
    assert read_reservations(db_path) == before  # reservations preserved
    assert read_binding(db_path)["fingerprint"] == store.fingerprint
    # And the store is fully usable afterwards.
    store.submit("f2", "S1", "S4", 4)
    assert store.report()["ok"] is True


def test_legacy_database_refused_when_normal_state_exceeds_capacity(tmp_path):
    topology = make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES)
    db_path = make_legacy_db(
        tmp_path / "reservations.db", topology, [("f1", "S1", "S4", 6)]
    )
    before = read_reservations(db_path)

    # f1's primary is S1-S2-S4; shrinking S1->S2 to 5 breaks the normal state.
    edges = [("S1", "S2", 5, 10)] + DIAMOND_EDGES[1:]
    with pytest.raises(TopologyBindingError) as excinfo:
        ReservationStore(db_path, make_topology(DIAMOND_SWITCHES, edges))
    message = str(excinfo.value)
    assert "cannot be honoured" in message
    assert "scenario normal: S1->S2 carries 6, capacity is 5" in message

    # Nothing was written: no binding, reservations untouched.
    assert read_binding(db_path) is None
    assert read_reservations(db_path) == before


def test_legacy_database_refused_when_failure_scenario_exceeds_capacity(tmp_path):
    topology = make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES)
    db_path = make_legacy_db(
        tmp_path / "reservations.db", topology, [("f1", "S1", "S4", 6)]
    )
    before = read_reservations(db_path)

    # f1's backup is S1-S3-S4; shrinking S3->S4 only breaks failure scenarios.
    edges = DIAMOND_EDGES.copy()
    edges[3] = ("S3", "S4", 5, 10)
    with pytest.raises(TopologyBindingError) as excinfo:
        ReservationStore(db_path, make_topology(DIAMOND_SWITCHES, edges))
    message = str(excinfo.value)
    assert "scenario failure:S1-S2: S3->S4 carries 6, capacity is 5" in message
    assert read_binding(db_path) is None
    assert read_reservations(db_path) == before


def test_legacy_database_refused_when_stored_path_left_the_topology(tmp_path):
    topology = make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES)
    db_path = make_legacy_db(
        tmp_path / "reservations.db", topology, [("f1", "S1", "S4", 4)]
    )
    before = read_reservations(db_path)

    # The new topology reaches S4 only via S3; f1's primary used S1-S2(-S4).
    edges = [edge for edge in DIAMOND_EDGES if edge[:2] != ("S1", "S2")]
    edges = [edge for edge in edges if edge[:2] != ("S2", "S4")]
    with pytest.raises(TopologyBindingError) as excinfo:
        ReservationStore(db_path, make_topology(DIAMOND_SWITCHES, edges))
    assert "'f1' primary path" in str(excinfo.value)
    assert read_binding(db_path) is None
    assert read_reservations(db_path) == before


# -- concurrent startup ---------------------------------------------------------


def test_concurrent_starts_with_identical_topology_all_bind(tmp_path):
    db_path = tmp_path / "reservations.db"
    topology = make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES)
    errors: list[BaseException] = []

    def start():
        try:
            ReservationStore(db_path, topology)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert read_binding(db_path)["fingerprint"] == topology_fingerprint(topology)


def test_concurrent_starts_bind_exactly_one_topology(tmp_path):
    db_path = tmp_path / "reservations.db"
    topology_a = make_topology(DIAMOND_SWITCHES, DIAMOND_EDGES)
    edges = DIAMOND_EDGES.copy()
    edges[0] = ("S1", "S2", 9, 10)
    topology_b = make_topology(DIAMOND_SWITCHES, edges)

    outcomes: list[str] = []
    lock = threading.Lock()

    def start(topology, label):
        try:
            ReservationStore(db_path, topology)
            outcome = f"bound:{label}"
        except TopologyBindingError:
            outcome = f"refused:{label}"
        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=start, args=(topology_a if i % 2 == 0 else topology_b, "ab"[i % 2]))
        for i in range(10)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    bound_labels = {outcome.split(":")[1] for outcome in outcomes if outcome.startswith("bound")}
    refused = [outcome for outcome in outcomes if outcome.startswith("refused")]
    # Exactly one topology won the race; every process of the other was refused.
    assert len(bound_labels) == 1
    assert refused, outcomes
    winner = topology_a if bound_labels == {"a"} else topology_b
    assert read_binding(db_path)["fingerprint"] == topology_fingerprint(winner)


def test_concurrent_processes_bind_exactly_one_topology(tmp_path):
    """Two real processes race; only one topology can bind the store."""
    db_path = tmp_path / "reservations.db"
    path_a = write_topology(tmp_path / "a.json", DIAMOND_SWITCHES, DIAMOND_EDGES)
    edges = DIAMOND_EDGES.copy()
    edges[0] = ("S1", "S2", 9, 10)
    path_b = write_topology(tmp_path / "b.json", DIAMOND_SWITCHES, edges)

    starter = (
        "import sys\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        "from app.store import ReservationStore, TopologyBindingError\n"
        "from app.topology import load_topology\n"
        "try:\n"
        "    ReservationStore(sys.argv[1], load_topology(sys.argv[2]))\n"
        "except TopologyBindingError as exc:\n"
        "    print(f'REFUSED: {exc}')\n"
        "    sys.exit(3)\n"
        "print('BOUND')\n"
    )
    launcher = tmp_path / "starter.py"
    launcher.write_text(starter, encoding="utf-8")

    def spawn(topology_path):
        return subprocess.Popen(
            [sys.executable, str(launcher), str(db_path), str(topology_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    proc_a = spawn(path_a)
    proc_b = spawn(path_b)
    out_a, _ = proc_a.communicate(timeout=60)
    out_b, _ = proc_b.communicate(timeout=60)

    results = {
        "a": (proc_a.returncode, out_a.strip()),
        "b": (proc_b.returncode, out_b.strip()),
    }
    outcomes = sorted(result[0] for result in results.values())
    assert outcomes == [0, 3], results  # exactly one process bound the store
    loser_output = results["a"][1] if proc_a.returncode else results["b"][1]
    assert "already bound to a different topology" in loser_output

    winner_path = path_a if proc_a.returncode == 0 else path_b
    winner_fingerprint = topology_fingerprint(load_topology(winner_path))
    assert read_binding(db_path)["fingerprint"] == winner_fingerprint


# -- API surface -----------------------------------------------------------------


def test_app_startup_refused_on_conflicting_topology(tmp_path):
    db_path = tmp_path / "db.sqlite"
    topology_path = write_topology(
        tmp_path / "topology.json", DIAMOND_SWITCHES, DIAMOND_EDGES
    )
    app = create_app(topology_path=topology_path, db_path=db_path)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    edges = DIAMOND_EDGES.copy()
    edges[0] = ("S1", "S2", 4, 10)
    conflicting_path = write_topology(tmp_path / "other.json", DIAMOND_SWITCHES, edges)
    conflicting_app = create_app(topology_path=conflicting_path, db_path=db_path)
    with pytest.raises(TopologyBindingError, match="already bound"):
        with TestClient(conflicting_app):
            pass


def test_equivalent_configs_share_store_and_fingerprint(tmp_path):
    db_path = tmp_path / "db.sqlite"
    path_a = write_topology(tmp_path / "a.json", DIAMOND_SWITCHES, DIAMOND_EDGES)
    path_b = write_topology(
        tmp_path / "b.json",
        list(reversed(DIAMOND_SWITCHES)),
        shuffled_edges(DIAMOND_EDGES),
    )
    app_a = create_app(topology_path=path_a, db_path=db_path)
    app_b = create_app(topology_path=path_b, db_path=db_path)
    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        health_a = client_a.get("/health").json()
        health_b = client_b.get("/health").json()
        assert health_a["topology_fingerprint"] == health_b["topology_fingerprint"]
        topology_body = client_a.get("/topology").json()
        assert topology_body["fingerprint"].startswith(health_a["topology_fingerprint"])
        # Both processes see the same reservations through the shared store.
        client_a.post(
            "/flows", json={"flow_id": "f1", "src": "S1", "dst": "S4", "bandwidth": 4}
        )
        assert client_b.get("/flows/f1").json()["bandwidth"] == 4
        assert client_b.get("/report").json()["ok"] is True
