# Contagion Protocol — Project Spec & Handoff

## 0. Context

This is a solo project for the "Distributed Systems" course (A.Y. 2025/2026, UniBo Cesena, prof. Andrea Omicini / Giovanni Ciatto, tutor Matteo Magnini). The final grade depends on: an oral discussion of the finished project, a GitHub repo evaluated on its `main`/`master` branch, and a report following the course's LaTeX template. The project must genuinely demonstrate distributed systems concepts through real inter-process communication, not a single-process simulation with a UI bolted on.

**Hard constraint: this must actually be distributed.** Each "node" in the simulation is its own OS process (containerized), talking to other nodes only through sockets, with no shared memory and no central authority holding global game state. A dashboard is allowed to observe and issue player commands, but it must not be the source of truth for game logic.

## 1. Elevator pitch

Contagion Protocol is a zombie-outbreak game built as a real distributed system. About 30 independent node processes form a network. One node starts secretly infected. Infection spreads covertly through the network before becoming visible. The player watches a live dashboard and has two irreversible, risky actions available (snipe a suspected zombie, administer a cure to a suspected victim) plus one systemic lever (a global trust threshold). The player wins by ending with as many surviving nodes as possible, and loses outright if the whole network turns before a cure is ready.

## 2. Node lifecycle

States, in order:

`HEALTHY -> INCUBATING (covert) -> OVERT (zombie) -> CURED`
`(any state) -> DEAD` (via snipe or a mis-aimed cure)

- **HEALTHY**: default. Participates honestly in all protocols.
- **INCUBATING**: infected but covert. Behaviorally identical to HEALTHY on the heartbeat/ping channel (always answers normally). The only difference: it silently fails to relay cure-run messages far more often than a healthy node would (see §5). Cannot infect others yet.
- **OVERT**: after a randomized per-node timer elapses while incubating, the node turns. It becomes detectable on the heartbeat channel (non-response, late response, or garbled response) and can now actively transmit infection to healthy neighbors during high-trust interactions.
- **CURED**: infection removed. Behaves like HEALTHY from then on.
- **DEAD**: removed from the network (edges cut, no further participation). Reached only via player action (wrong snipe/cure kills a healthy target; correct snipe kills an infected target). An overt zombie that receives a correct cure becomes CURED instead of DEAD.

Default: incubation-to-overt timer is a per-node random duration (e.g. uniform between 60 and 180 simulated ticks). Exposure-based acceleration (more high-trust interactions while covert shortens the timer) is a stretch goal, not required for v1.

## 3. Communication model (two tiers)

Every pair of connected neighbors can interact in two ways:

1. **Heartbeat gossip** (cheap, frequent, UDP). "Are you okay?" pings. HEALTHY and INCUBATING always answer normally (indistinguishable). OVERT nodes fail to answer correctly (missed, late, or garbled). Carries a small baseline chance of infection transmission on its own (see §4).
2. **High-trust sync** (deliberate, rarer, TCP). A heavier exchange (e.g. simulated state merge). This is the main infection vector, the main way trust score between two nodes increases, and the channel cure-run and alert messages preferentially ride on for reliable delivery.

A global **trust threshold** (0.0–1.0), set by the player and broadcast to all nodes, decides which tier a node uses with a given neighbor: if the edge's accumulated trust score is above the threshold, the pair does high-trust syncs; otherwise they stick to heartbeat gossip. Trust score per edge accrues automatically from interaction history, no player action needed per edge.

## 4. Infection transmission

- Baseline: any contact (even heartbeat-tier) with an INCUBATING or OVERT neighbor carries a small non-zero infection chance (e.g. 1-2% per tick of contact). This means total isolation via a maxed-out trust threshold reduces but never fully eliminates risk.
- High-trust sync with an INCUBATING or OVERT neighbor: a much higher transmission chance (e.g. 15-25% per interaction).
- Only OVERT nodes can *actively* attempt infection (i.e. push harder / retry); INCUBATING nodes transmit passively at the low ambient rate only.

## 5. Cure relay mechanic

- Periodically (e.g. every 10-20 ticks), a random HEALTHY or CURED node auto-originates a **cure run**: a message that must random-walk through a chain of ~5 distinct nodes (never revisiting a node in the same run) to complete.
- At each hop, the current node relays successfully with probability `p_relay`:
  - `p_relay_healthy` ≈ 0.95 (5% baseline drop, models ordinary message loss, applies even to HEALTHY/CURED nodes)
  - `p_relay_infected` ≈ 0.30 (INCUBATING or OVERT)
- A completed run (reached full length without dying) adds a fixed increment to global cure progress (e.g. 1/20th, so 20 successful runs = 100%).
- A failed run is logged with the exact node where it died. A single failure there is weak evidence (could be ordinary drop). A node that keeps being the death-point of many runs becomes statistically suspicious. This is the player's primary non-alert signal.
- Once cure progress reaches 100%, the player's **cure action** becomes usable.

## 6. Alert / suspicion broadcast

- Any node can flag a neighbor as suspicious (e.g. after an inconsistent heartbeat response, or after being told by other neighbors that a node behaves oddly). This flag spreads through the network as its own gossip epidemic, independent of infection and cure-run traffic.
- The dashboard surfaces these as a scrolling feed. This is deliberately a second, competing epidemic on the same substrate as infection and cure, for both thematic and technical symmetry (three gossip-disseminated signals sharing one substrate).

## 7. Player actions

- **Snipe** (limited uses, e.g. 1 per cooldown window or small total budget): kill a target node immediately. Correct if target was INCUBATING/OVERT. Kills the target regardless, including if wrongly used on a HEALTHY node.
- **Cure** (unlocked only once cure progress hits 100%; same limited-use cadence as snipe): heal a target node if INCUBATING/OVERT, but kills the target if it was actually HEALTHY or CURED already.
- **Trust threshold** (freely adjustable at any time, no cooldown): the one systemic lever, not a targeted action.

## 8. Scoring and end conditions

- **Score** = number of nodes alive (any non-DEAD state) at game end. Every wrong snipe/cure directly reduces it.
- **Loss condition**: if every node reaches OVERT simultaneously (no HEALTHY/INCUBATING/CURED left), it's an outright loss regardless of score.
- **End of run**: after a fixed tick budget, or when the network stabilizes (no OVERT nodes remain and cure progress is either complete or moot).

## 9. Architecture

Three logical planes, all necessary to keep the game plane genuinely distributed while still being observable and playable:

1. **Data plane (peer-to-peer, the actual distributed systems content)**: node-to-node gossip mesh. UDP for heartbeat, TCP for high-trust sync, cure-run and alert messages ride whichever tier the trust threshold currently permits between that pair. No central coordinator. Each node only knows its direct neighbors (fixed at startup from a topology config, e.g. random graph or small-world, average degree ~4-6).
2. **Control plane**: each node exposes a small local HTTP (or plain TCP+JSON) endpoint accepting exactly three commands: `set_trust_threshold`, `snipe`, `cure`. The dashboard/game-master service calls these directly per node; this is an out-of-band management channel, analogous to a real control plane vs. data plane split (e.g. Kubernetes).
3. **Observability plane**: each node emits structured JSON events (state changes, relay outcomes, alerts sent/received, heartbeat anomalies) to a lightweight aggregator service, which the dashboard subscribes to over WebSocket. This plane is read-only and has no authority over game state; it only reflects it.

## 10. Suggested tech stack

- **Python 3.11+ with asyncio** for node processes (matches the language used in the course's own Module 2 exercises).
- **Raw `socket` / `asyncio` UDP and TCP** for the data plane (this is the part that should NOT be hidden behind a high-level framework, since it's the actual course-relevant content).
- **JSON** for all message serialization (`json` stdlib is enough).
- **aiohttp** (or plain `http.server`) for each node's small control-plane endpoint.
- **A lightweight aggregator** (Flask or FastAPI + WebSocket, or aiohttp again) collecting observability events and serving them to the dashboard.
- **Dashboard**: a single static HTML page with vanilla JS (or a small framework if preferred) using a graph-drawing library (e.g. vis-network or d3-force) fed over WebSocket. Keep this simple; it is not the graded core of the project.
- **Docker Compose** to launch ~30 node containers, 1 aggregator, 1 dashboard (or serve the dashboard as a static file from the aggregator to avoid a 4th service).

## 11. Suggested repo structure

```
contagion-protocol/
  node/
    main.py            # node process entrypoint
    state.py           # lifecycle state machine
    gossip.py          # heartbeat + high-trust sync handling
    cure_relay.py       # cure-run origination and relay logic
    alerts.py           # suspicion broadcast logic
    control_api.py       # snipe/cure/threshold HTTP endpoint
    events.py           # emits observability events to aggregator
  aggregator/
    main.py             # collects events, serves WebSocket + static dashboard
  dashboard/
    index.html
    app.js
  topology/
    generate_topology.py # produces the fixed neighbor graph + docker-compose service list
  docker-compose.yml
  report/               # LaTeX report using the course template
  README.md
```

## 12. Message schemas (draft, JSON over the wire)

```jsonc
// heartbeat (UDP, data plane)
{"type": "heartbeat", "from": "node-07", "ts": 12345}
{"type": "heartbeat_ack", "from": "node-12", "ts": 12345, "status": "ok"} // or garbled/missing if OVERT

// high-trust sync (TCP, data plane)
{"type": "trust_sync", "from": "node-07", "to": "node-12", "payload": {...}}

// cure run (rides current tier per pair, data plane)
{"type": "cure_run", "run_id": "uuid", "path": ["node-03"], "hops_remaining": 4}
// forwarded node appends itself to "path" and decrements hops_remaining, or drops it

// alert (data plane, gossip)
{"type": "alert", "about": "node-22", "reason": "heartbeat_anomaly", "from": "node-19", "ttl": 5}

// control plane (HTTP, dashboard -> node)
POST /control/snipe
POST /control/cure
POST /control/set_trust_threshold {"value": 0.6}

// observability event (node -> aggregator, one-way)
{"type": "state_change", "node": "node-22", "from": "HEALTHY", "to": "INCUBATING", "ts": ...}
{"type": "cure_run_result", "run_id": "uuid", "success": false, "path": [...], "died_at": "node-14"}
{"type": "alert_seen", "about": "node-22", "from": "node-19"}
```

## 13. Suggested build order (phases)

1. **Skeleton**: fixed topology generator, Docker Compose spinning up N trivial node containers that just exchange heartbeats over UDP and log receipt. Prove the data plane works before anything else.
2. **Lifecycle**: add node states, patient-zero seeding at startup, the incubation-to-overt random timer, and infection transmission probabilities on both tiers.
3. **Trust system**: per-edge trust score accrual, global trust-threshold control-plane command, tier selection logic per pair.
4. **Cure relay**: cure-run origination, random-walk relay with drop probabilities, progress aggregation, failure logging with death-point.
5. **Alerts**: suspicion broadcast as a third gossip message type.
6. **Control + observability planes**: per-node HTTP control endpoint, event emission, aggregator service, minimal dashboard (graph + feed + cure bar + threshold slider + snipe/cure buttons).
7. **Scoring & end conditions**: track alive count, detect full-outbreak loss, detect stable/cured end state, surface final score.
8. **Polish & report**: tune constants by playtesting, write the LaTeX report (include an Analysis section: vary node count, drop rates, trust-threshold defaults, and measure cure convergence time / detection accuracy), record a demo.

## 14. Tunable parameters (defaults to start from, expect to retune by playtesting)

| Parameter | Default |
|---|---|
| Node count | 30 |
| Average node degree | 4-6 |
| Incubation duration | uniform random, 60-180 ticks |
| Cure run length | 5 hops |
| Runs needed for 100% cure | 20 |
| Relay success, healthy/cured | 0.95 |
| Relay success, infected | 0.30 |
| Baseline infection chance (heartbeat contact) | 0.01-0.02 per tick |
| Infection chance (high-trust sync with infected) | 0.15-0.25 per interaction |
| Snipe/cure cooldown | e.g. 1 use per 15-20 ticks, or a small total budget (tune by playtesting) |

## 15. Explicit non-goals (keep scope sane for a solo project)

- No authentication, no persistence beyond a single in-memory run.
- No matchmaking/lobby, no multiple concurrent games.
- No mobile client; a single browser dashboard is enough.
- The "high-trust necessity" mechanic (nodes periodically forced to do a risky sync) and exposure-accelerated turning are stretch goals only, add them after the core loop works end to end.
- Don't over-build the dashboard visuals; a functional force-directed graph with color-coded nodes is enough. The grade rides on the distributed protocol, not the UI polish.

## 16. Deliverables checklist (per course requirements)

- [ ] GitHub repo, `main` branch, all members (n/a, solo) added as collaborators, `gciatto` and `matteomagnini` given admin.
- [ ] Working Docker Compose demo (`docker compose up` should just work).
- [ ] Final report in the course's LaTeX template, including the Analysis section described above.
- [ ] Short demo recording or live demo ready for the 12-15 minute presentation + Q&A with prof. Omicini.
