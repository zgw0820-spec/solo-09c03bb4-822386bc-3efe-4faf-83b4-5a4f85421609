from __future__ import annotations

import threading

import pytest

from app.allocator import FlowIdConflict, NoFeasiblePaths
from app.store import ReservationStore

from .helpers import DIAMOND_EDGES, DIAMOND_SWITCHES, make_topology


def store(tmp_path, edges=None, switches=None):
    topology = make_topology(
        switches or DIAMOND_SWITCHES, edges or DIAMOND_EDGES
    )
    return ReservationStore(tmp_path / "reservations.db", topology)


def test_identical_retry_returns_stored_paths(tmp_path):
    db = store(tmp_path)
    first, created = db.submit("f1", "S1", "S4", 4)
    assert created is True
    second, created_again = db.submit("f1", "S1", "S4", 4)
    assert created_again is False
    assert second == first
    assert len(db.list()) == 1


def test_changed_parameters_for_existing_id_are_rejected(tmp_path):
    db = store(tmp_path)
    original, _ = db.submit("f1", "S1", "S4", 4)
    with pytest.raises(FlowIdConflict):
        db.submit("f1", "S1", "S4", 5)
    with pytest.raises(FlowIdConflict):
        db.submit("f1", "S4", "S1", 4)
    # The stored reservation is untouched.
    assert db.get("f1") == original


def test_failed_allocation_leaves_previous_reservations_intact(tmp_path):
    db = store(tmp_path, edges=[("S1", "S2", 4, 4), ("S2", "S3", 4, 4), ("S1", "S3", 4, 4)])
    first, _ = db.submit("a", "S1", "S3", 2)
    with pytest.raises(NoFeasiblePaths):
        db.submit("b", "S1", "S3", 100)
    assert db.list() == [first]


def test_concurrent_submissions_never_oversubscribe(tmp_path):
    """Many threads race for capacity that only fits N flows."""
    # Three parallel S->T routes of capacity 10 each; each flow needs a
    # disjoint backup.  Only three 10-unit flows fit per scenario (=one per
    # route, with backups rotating).  Fire 12 at once and check the final
    # report.
    switches = ["S", "A1", "A2", "A3", "T"]
    edges = [
        ("S", "A1", 10, 10),
        ("A1", "T", 10, 10),
        ("S", "A2", 10, 10),
        ("A2", "T", 10, 10),
        ("S", "A3", 10, 10),
        ("A3", "T", 10, 10),
    ]
    db = store(tmp_path, edges=edges, switches=switches)

    results: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        try:
            db.submit(f"f{index}", "S", "T", 4)
            outcome = "ok"
        except NoFeasiblePaths:
            outcome = "rejected"
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            outcome = f"error:{type(exc).__name__}:{exc}"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert "rejected" in results
    assert all(not item.startswith("error") for item in results), results
    report = db.report()
    assert report["ok"], report["violations"]
    accepted = sum(1 for item in results if item == "ok")
    assert accepted == len(db.list())
    # Capacity is the real limit: 3 persistent primary routes of 10 / 4 = 7
    # flows on primaries, but backups constrain further; the invariant is what
    # matters, not a specific count.
    assert accepted < 12


def test_report_counts_every_scenario(tmp_path):
    db = store(tmp_path)
    db.submit("f1", "S1", "S4", 4)
    report = db.report()
    # normal + one per physical link
    assert report["scenario_count"] == 1 + len(DIAMOND_EDGES)
    assert report["ok"] is True