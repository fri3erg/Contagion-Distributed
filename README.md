# Contagion Protocol

A zombie-outbreak game built as a real distributed system, for the
Distributed Systems course (A.Y. 2025/2026, UniBo Cesena — prof. Andrea
Omicini / Giovanni Ciatto, tutor Matteo Magnini). Solo project.

> **Status:** work in progress, built ahead of formal project approval by
> the professor. The full spec this implementation follows lives at
> [`docs/SPEC.md`](docs/SPEC.md). Balance constants will need retuning by
> playtesting; see [Tunables](#tunables) below.

## What it is

About 30 independent OS processes (one Docker container each) form a
gossip network. One node starts secretly infected. Infection spreads
covertly before becoming visible. You watch a live dashboard and have two
irreversible, risky actions (snipe a suspected zombie, cure a suspected
victim) plus one systemic lever (a global trust threshold). You win by
maximizing survivors, and lose outright if the whole reachable network
turns before a cure is ready.

**Hard constraint honored throughout:** every node is a separate process
talking to its neighbors only through sockets. There is no shared memory
and no central authority holding game state. The dashboard/aggregator can
observe and issue commands, but every bit of actual game logic — who's
infected, whether a cure-relay hop succeeds, whether an alert has already
been seen — lives and is decided *inside* the node process it concerns.

## Quickstart

Requires Docker + Docker Compose.

```bash
docker compose up --build
```

(or `./scripts/play.sh` from the repo root, which also does a clean
`down -v` first). Then open **http://localhost:8000/** for the dashboard.
The default topology (30 nodes, avg degree 4, committed at
[`topology/topology.json`](topology/topology.json)) spins up automatically
— no extra setup needed.

To change node count, average degree, or any balance constant, edit
[`topology/generate_topology.py`](topology/generate_topology.py) and
re-run it (it regenerates both `topology/topology.json` *and*
`docker-compose.yml` from one source of truth, so they can't drift apart):

```bash
python topology/generate_topology.py --nodes 40 --degree 6 --seed 7
docker compose up --build
```

### Running tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

Unit tests cover the lifecycle state machine, trust-tier selection, the
cure-relay random walk (with networking mocked out), and the topology
generator (connectivity, degree, determinism). The system has also been
smoke-tested end to end in real Docker containers (heartbeat gossip,
infection spread, cure relay, the control-plane proxy, and the loss
condition all observed firing correctly).

## Architecture

Three planes, per the spec (`docs/SPEC.md` sec. 9):

1. **Data plane** — the actual distributed-systems content. Raw UDP for
   heartbeat gossip, raw TCP for high-trust sync. Cure-run and alert
   messages ride whichever tier each node's own *local* trust score for
   that neighbor currently selects (no shared/synced trust state — see
   [`node/trust.py`](node/trust.py)). Each node only ever knows its direct
   neighbors, fixed at startup from `topology/topology.json`.
2. **Control plane** — each node exposes a tiny HTTP endpoint
   (`node/control_api.py`) accepting exactly `snipe`, `cure`, and
   `set_trust_threshold`. The aggregator proxies dashboard commands to the
   right node over Docker's internal network; it applies them
   unconditionally and holds no game logic of its own.
3. **Observability plane** — nodes emit one-way JSON events to the
   aggregator (`node/events.py` → `aggregator/main.py`), which fans them
   out to the dashboard over WebSocket. Read-only: nothing here can ever
   change what a node actually does.

```
contagion node (per container)
├── state.py        lifecycle state machine (HEALTHY → INCUBATING → OVERT → CURED, → DEAD)
├── trust.py         per-neighbor local trust score + tier selection
├── infection.py     transmission rolls, attached to outgoing messages
├── gossip.py         UDP heartbeat + TCP high-trust sync transports
├── cure_relay.py     cure-run origination / random-walk relay
├── alerts.py          suspicion-gossip flood (independent epidemic #2)
├── control_api.py     snipe / cure / set_trust_threshold HTTP endpoint
├── events.py           fire-and-forget event emission to the aggregator
└── main.py               wires it all together, drives the tick loop
```

### Design decisions worth knowing about (deviations / fill-ins where the spec left room)

The spec (rightly) leaves several mechanics as "roughly this" rather than
fully pinned down. Choices made here, and why:

- **Infection is sender-attached, not receiver-inferred.** An infected
  node is the only party that ever knows it's infected, so *it* rolls the
  transmission chance and attaches a hidden `_exposure` marker to a
  message it was sending anyway (a heartbeat or trust-sync payload). The
  receiver just reacts to a field on a message it received — no peeking
  at a neighbor's true state, no shared memory. See `node/infection.py`.
- **Trust is local, not synced.** Each node keeps its own estimate of
  every neighbor's trustworthiness from its own interaction history. The
  two ends of an edge can legitimately disagree about which tier to use
  for a given interaction — that's a real consequence of "no shared
  memory," not a bug.
- **Cure-readiness gating and snipe/cure budgets live in the aggregator,
  not in nodes.** A node's control endpoint applies `snipe`/`cure`
  unconditionally, no questions asked — enforcing *"only after 100% cure
  progress"* or *"only N uses total"* would require every node to track
  global aggregate state, which breaks the no-central-authority
  constraint for actual game logic. Session/UI policy (a player's action
  budget, whether the cure button should even be clickable) isn't game
  logic though — it's meta state about *this playthrough*, and the
  aggregator is the natural, harmless place for it, exactly like a
  real control-plane rate limiter doesn't hold any worker's state.
- **Cure-run completion is reported by whoever observes it**, not by a
  callback to the originator. The random walk is genuinely one-way; the
  node that either drops a run or completes the final hop is the one
  that emits `cure_run_result`. Global cure progress is a display-only
  aggregate the aggregator derives by counting unique successful
  `run_id`s — no node needs to know it.
- **End-condition / score detection (loss, stabilization, final score)
  is computed by the aggregator from observed states**, not decided by
  any node. This is intentionally just a read-only derived summary for
  the player's banner — it has no authority and nothing acts on it other
  than displaying text.

### Node lifecycle

```
HEALTHY --infect--> INCUBATING --timer--> OVERT
   |                    |                    |
   +----cure (wrong)----+----cure (wrong)-----+----> DEAD
                         \--cure (correct)--> CURED
(any state) --snipe--> DEAD
```

INCUBATING is indistinguishable from HEALTHY on the heartbeat channel by
design (`node/state.py: answers_heartbeat_normally`); OVERT nodes fail
heartbeats (missed / late / garbled) and can actively push infection.

## Tunables

All balance constants live in one place —
[`node/config.py`](node/config.py) (defaults + env var names) and
[`topology/generate_topology.py`](topology/generate_topology.py)
(`TUNABLES`, which is what actually populates each node container's
environment in the generated `docker-compose.yml`). Defaults mirror the
spec's suggested starting point (sec. 14); expect to retune by
playtesting and record the process in the report's Analysis section.

## Repo structure

```
node/            node process (one container each)
aggregator/      observability sink + control-plane proxy + static dashboard host
dashboard/       single-page vanilla-JS dashboard (no CDN dependency — force-directed
                 graph is a small hand-rolled layout, so `docker compose up` works offline)
topology/        graph generator; also the only place that writes docker-compose.yml
tests/           unit tests (state machine, trust, cure relay, topology generator)
scripts/         small dev convenience wrappers
docs/SPEC.md     the full project spec this implementation follows
```

The LaTeX report lives in a local `report/` folder that is deliberately
git-ignored: it's submitted separately (as a PDF attached to the
Projects Forum post per the course rules), not through this code repo,
so it isn't part of the graded repo's history.

## Build status against the spec's phased plan (`docs/SPEC.md` sec. 13)

- [x] 1. Skeleton — topology generator, Compose, UDP heartbeat data plane.
- [x] 2. Lifecycle — states, patient-zero seeding, incubation timer, transmission.
- [x] 3. Trust system — per-edge local trust accrual, threshold control command, tier selection.
- [x] 4. Cure relay — origination, random-walk relay, progress aggregation, failure logging.
- [x] 5. Alerts — suspicion broadcast as a third gossip type.
- [x] 6. Control + observability planes — control API, event emission, aggregator, dashboard.
- [x] 7. Scoring & end conditions — alive count, loss detection, stable-end detection, score banner.
- [ ] 8. Polish & report — playtesting/retuning constants, LaTeX report + Analysis section, demo recording.

Everything through phase 7 has been exercised end to end in real Docker
containers (not just unit tests): heartbeat gossip, infection spread,
cure-relay traffic, the control-plane proxy (snipe / cure / threshold
broadcast), and loss-condition detection were all observed working
correctly on a live cluster during development.

## Deliverables checklist (course requirements, `docs/SPEC.md` sec. 16)

- [ ] GitHub repo on `main`, `gciatto` and `matteomagnini` added as admins.
- [x] Working Docker Compose demo (`docker compose up` — verified working).
- [ ] Final report in the course's LaTeX template, with Analysis section.
- [ ] Demo recording / live-demo readiness for the presentation + Q&A.
