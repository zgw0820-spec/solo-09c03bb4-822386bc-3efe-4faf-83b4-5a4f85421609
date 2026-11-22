"""SQLite persistence with a single-writer transaction guarding allocation.

Allocation is a read-compute-write cycle.  To keep the "no scenario may exceed
capacity" invariant under concurrent requests, the whole cycle runs inside a
SQLite ``BEGIN IMMEDIATE`` transaction: the writer lock is taken before the
current reservations are read, so two requests can never allocate against the
same snapshot.  On any error the transaction is rolled back and the previous
reservations are untouched.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .allocator import Allocation, FlowIdConflict, Reservation, allocate, scenario_report
from .topology import Topology

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    flow_id       TEXT PRIMARY KEY,
    src           TEXT NOT NULL,
    dst           TEXT NOT NULL,
    bandwidth     INTEGER NOT NULL CHECK (bandwidth > 0),
    primary_path  TEXT NOT NULL,
    backup_path   TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


class ReservationStore:
    def __init__(self, db_path: str | Path, topology: Topology) -> None:
        self.db_path = str(db_path)
        self.topology = topology
        # A process-wide lock makes the transaction boundary explicit and also
        # serialises threads sharing one connection-pooled file.  BEGIN
        # IMMEDIATE alone protects across processes.
        self._lock = threading.Lock()
        self._initialise()

    # -- connection handling --------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path, timeout=30.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialise(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            # WAL is a database-level, persistent setting; applying it per
            # connection would race with in-flight transactions.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    # -- row conversion --------------------------------------------------
    @staticmethod
    def _row_to_reservation(row: sqlite3.Row) -> Reservation:
        return Reservation(
            flow_id=row["flow_id"],
            src=row["src"],
            dst=row["dst"],
            bandwidth=row["bandwidth"],
            primary_path=tuple(json.loads(row["primary_path"])),
            backup_path=tuple(json.loads(row["backup_path"])),
        )

    def _load_all(self, connection: sqlite3.Connection) -> list[Reservation]:
        rows = connection.execute(
            "SELECT flow_id, src, dst, bandwidth, primary_path, backup_path "
            "FROM reservations ORDER BY flow_id"
        ).fetchall()
        return [self._row_to_reservation(row) for row in rows]

    # -- public API ------------------------------------------------------
    def list(self) -> list[Reservation]:
        with self._read() as connection:
            return self._load_all(connection)

    def get(self, flow_id: str) -> Reservation | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT flow_id, src, dst, bandwidth, primary_path, backup_path "
                "FROM reservations WHERE flow_id = ?",
                (flow_id,),
            ).fetchone()
            return self._row_to_reservation(row) if row else None

    def submit(
        self, flow_id: str, src: str, dst: str, bandwidth: int
    ) -> tuple[Reservation, bool]:
        """Register a flow.  Returns (reservation, created).

        - Same id with identical parameters: returns the stored reservation,
          no reallocation (idempotent retry).
        - Same id with different parameters: raises FlowIdConflict.
        - Otherwise allocates under an exclusive writer transaction; a failure
          rolls back and leaves every existing reservation unchanged.
        """
        with self._write_transaction() as connection:
            existing = self._load_all(connection)
            by_id = {reservation.flow_id: reservation for reservation in existing}

            current = by_id.get(flow_id)
            if current is not None:
                if (
                    current.src == src
                    and current.dst == dst
                    and current.bandwidth == bandwidth
                ):
                    return current, False
                raise FlowIdConflict(
                    f"flow {flow_id!r} already exists with different parameters "
                    f"({current.src}->{current.dst}, {current.bandwidth})"
                )

            allocation: Allocation = allocate(
                self.topology, existing, src, dst, bandwidth
            )
            connection.execute(
                "INSERT INTO reservations "
                "(flow_id, src, dst, bandwidth, primary_path, backup_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    flow_id,
                    src,
                    dst,
                    bandwidth,
                    json.dumps(list(allocation.primary_path)),
                    json.dumps(list(allocation.backup_path)),
                ),
            )
            row = connection.execute(
                "SELECT flow_id, src, dst, bandwidth, primary_path, backup_path "
                "FROM reservations WHERE flow_id = ?",
                (flow_id,),
            ).fetchone()
            return self._row_to_reservation(row), True

    def report(self) -> dict:
        reservations = self.list()
        report = scenario_report(self.topology, reservations)
        report["reservations"] = [
            {
                "flow_id": reservation.flow_id,
                "src": reservation.src,
                "dst": reservation.dst,
                "bandwidth": reservation.bandwidth,
                "primary_path": list(reservation.primary_path),
                "backup_path": list(reservation.backup_path),
            }
            for reservation in reservations
        ]
        return report
