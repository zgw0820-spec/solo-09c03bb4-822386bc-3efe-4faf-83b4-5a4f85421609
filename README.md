# Camera backhaul admission API

FastAPI + SQLite admission control for camera-to-recording-room backhaul.
Every accepted flow gets a link-disjoint primary/backup path pair on a fixed
topology of at most 8 switches. Capacity is per physical link **per direction**,
and is checked in the normal state and in every single-physical-link failure
scenario.

A reservation store (SQLite file) is **bound to exactly one topology**: the
first process to open a database records the topology fingerprint, and every
other process must present a semantically identical configuration (same
switches, same physical links, same per-direction capacities — file ordering
and `a`/`b` labelling are irrelevant) or it refuses to start.

## Run with Docker Compose

```bash
docker compose up --build
```

- Swagger UI: <http://localhost:8000/docs>
- Health: `GET /health` (includes the bound topology fingerprint)
- Topology: `GET /topology`
- SQLite file: named volume `reservations` mounted at `/srv/app/data`

The "Try it out" example for `POST /flows` uses `SW1 -> SW4`; on the shipped
topology use e.g. `{"flow_id":"cam-001","src":"SW1","dst":"SW8","bandwidth":4}`.
After admitting a flow, open `GET /report` in Swagger and expand a
`failure:<link>` scenario to see the per-direction usage with that physical
link down.

To run a **second process on the same store** (both bind the identical
topology, so both may serve the same volume):

```bash
docker compose --profile shared-store up --build
# A on :8000, B on :8001 — compare GET /health fingerprints, then submit via
# A and read /flows and /report back from B.
```

A process started with a different topology against the same volume now
refuses to start, e.g.:

```bash
docker compose run --rm --no-deps \
  -v "$PWD/topology-other.json:/srv/app/topology.json:ro" \
  backhaul-api
# ... TopologyBindingError: this reservation store is already bound to a
# different topology ...: link SW1-SW2 capacity SW1->SW2: bound 10, configured 4
```

## Run locally without Docker

```bash
python3 -m pip install --user --break-system-packages -r requirements-dev.txt
TOPOLOGY_PATH=./topology.json DB_PATH=./data/reservations.db \
  python3 -m uvicorn app.main:app --reload
python3 -m pytest -q
```

## API

| Method | Path             | Behaviour |
|--------|------------------|-----------|
| POST   | `/flows`         | Register a flow. `201` created, `200` idempotent retry (identical parameters, stored paths returned unchanged), `409` same id with different parameters, `422` validation error or no feasible pair. |
| GET    | `/flows`         | List reservations. |
| GET    | `/flows/{id}`    | One reservation, `404` if absent. |
| GET    | `/report`        | Per-direction usage for the normal state and every single-link failure, plus a `violations` list (must be empty). |
| GET    | `/topology`      | Fixed topology and per-direction capacities. |

Request body:

```json
{"flow_id": "cam-001", "src": "SW1", "dst": "SW8", "bandwidth": 4}
```

## Rules implemented

1. **Fixed topology, ≤ 8 switches, one physical link per unordered pair.**
   `Topology` rejects duplicate links (in either order), unknown endpoints,
   self-loops, non-positive capacities and more than 8 switches.
2. **Two directions per link.** The primary/backup search and the capacity
   checks use directed capacities, so `A->B` and `B->A` are independent.
3. **Link-disjoint primary and backup.** Disjointness is over physical links,
   not switches: a backup may revisit a switch as long as it does not reuse a
   primary link.
4. **Normal-state capacity.** Only primaries carry traffic when nothing is
   broken; every directed edge is checked against capacity.
5. **Single-link failure model.** A failure takes out both directions of one
   physical link. Only flows whose primary uses that link move to their backup;
   all other flows stay on their primary. Each such scenario is checked against
   every directed capacity.
6. **Standby sharing across different failures.** Backups are charged only in
   scenarios where they are actually active, so different flows may share the
   same standby link when their primaries fail on different links. Within any
   one scenario the capacity bound still holds.
7. **Concurrency.** Allocation + insert runs inside one SQLite
   `BEGIN IMMEDIATE` transaction (plus an in-process lock), so two requests
   never allocate against the same snapshot. A rejected request or a failed
   write rolls back and leaves all existing reservations untouched.
8. **Idempotent retries and conflicts.** Same `flow_id` with identical
   `src`/`dst`/`bandwidth` returns the stored paths; any parameter change under
   an existing id is `409` and the stored reservation is unchanged.
9. **One topology per store.** At startup the process opens the database in a
   single `BEGIN IMMEDIATE` transaction that creates the schema and verifies
   or records the topology binding, so even concurrent starters bind exactly
   one topology. A process whose configuration differs in switches, physical
   links or any per-direction capacity refuses to start and logs the specific
   differences; the database — reservations and any existing binding — is left
   untouched. A database written before bindings existed is adopted only after
   every stored reservation is re-validated: primary/backup paths must still
   be valid link-disjoint pairs, and the original failover rules must keep
   every direction within capacity in the normal state and under every
   single-link failure; otherwise startup is refused with the list of
   violations.

The allocator enumerates simple primary paths shortest-first and, for each,
finds the shortest link-disjoint backup whose edges are feasible in every
scenario triggered by a failed primary link. Cycles never help (removing one
cannot increase per-edge usage), so restricting to simple paths is complete for
this model.

## Verification performed

- `python3 -m pytest -q` → **48 passed** (topology parsing, allocator, store,
  API, topology binding).
- Topology binding (real uvicorn processes, one shared SQLite file):
  - process A binds the store; process B with one direction's capacity
    changed **refuses to start** (exit 3) and logs
    `link SW1-SW2 capacity SW1->SW2: bound 10, configured 4`; A keeps serving
    and the database is unchanged.
  - process C with a textually different but semantically identical topology
    (reordered file, swapped `a`/`b` labels) starts fine and shows the same
    `/health` fingerprint; flows submitted via A are listed by C and C's
    `/report` is clean (10 scenarios, no violations).
  - legacy database (pre-binding schema, one 6-unit reservation): a process
    whose topology cannot honour the stored backup path refuses to start and
    names the failing scenarios (`failure:SW1-SW2: SW6->SW7 carries 6,
    capacity is 5`); the legacy file is left byte-identical (no binding
    written, reservation intact). The same file with the matching topology is
    adopted: binding recorded, reservation preserved, `/report` clean.
  - concurrent startup races: 10 threads with two different topologies bind
    exactly one; two real processes racing on a fresh database end with
    exactly one bound (exit codes 0 and 3) and the binding matches the
    winner.
- Real uvicorn process, shipped topology:
  - submit → `201`; identical retry → `200` with identical paths; changed
    bandwidth/topology → `409`; unknown switch / same endpoint / zero
    bandwidth / over-capacity → `422`.
  - two camera sites each with two 4-unit flows: `SW6-SW7` and `SW7-SW8` are
    used by **all four backups**, yet every failure scenario activates at most
    two of them; `/report` shows `ok: true`, 10 scenarios, no violations.
  - 40 concurrent submissions (20 parallel) → 4 accepted, 36 rejected, 0
    duplicates, 0 5xx, report clean.
  - 30 concurrent identical submissions → 1 × `201`, 29 × `200`, one DB row,
    all returning the same paths.
  - 4 uvicorn workers, 60 concurrent submissions → 4 accepted, 0 lock errors,
    report clean.
  - rejection test: after 4 + 4 units filled a pair, a third 4-unit flow was
    rejected while a 2-unit flow fit exactly at 10; existing reservations
    unchanged.

## Not done / limitations

- **Docker Compose was not actually started:** this environment has no Docker
  CLI or daemon. `Dockerfile` and `docker-compose.yml` are written and the
  Compose YAML parses, but `docker compose up` and the image build were not
  executed. All runtime checks used uvicorn directly.
- **No multi-link failure scenarios.** Only single physical-link failures are
  modelled, as specified.
- **No reallocation or path migration.** Once accepted, a flow's paths never
  change; there is no delete/update endpoint and no re-optimisation. A
  topology change therefore requires a new (empty) store or manual migration —
  by design the service refuses to start against a store bound to a different
  topology.
- **At-most-one fresh reservation at a time.** The writer lock serialises
  admission; throughput is fine for this scale but not parallelised.
- **No authentication, rate limiting, metrics or structured logging.**
- **No automated tests against Compose/container.** Test suite runs against
  the app in-process; topology is fixed by `topology.json` (the 8-switch sample
  has three disjoint S1/S3 -> SW8 lanes through SW6/SW7 so standby sharing is
  observable).
