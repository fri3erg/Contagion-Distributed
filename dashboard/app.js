// Contagion Protocol dashboard -- debug view.
//
// Pure observer + command sender: it renders whatever the aggregator's
// observability stream says and sends control-plane commands through the
// aggregator's proxy. It never computes game logic. This view shows the
// true state of every node (including covert infections) on purpose: it
// is meant for testing and for following what the protocol is doing.

const SVG_NS = "http://www.w3.org/2000/svg";
const W = 1000, H = 640;
const R = 17;                 // node radius
const MAX_EVENTS = 5000;      // kept in memory for re-filtering
const BASE_TICK_MS = 5000;    // "1×" speed
const MAX_ROWS = 600;         // rendered in the log
const STALE_S = 4;            // node_status older than this = node looks stuck

const CATEGORIES = {
  state:      { label: "state",        color: "var(--accent)",   on: true },
  infection:  { label: "infection",    color: "var(--overt)",    on: true },
  cure:       { label: "cure results", color: "var(--cure)",     on: true },
  hops:       { label: "cure hops",    color: "var(--cure)",     on: false },
  heartbeat:  { label: "heartbeat",    color: "#ff8c42",         on: true },
  alerts:     { label: "alerts",       color: "var(--alert)",    on: true },
  alertrelay: { label: "alert relays", color: "var(--alert)",    on: false },
  trust:      { label: "trust/tiers",  color: "#a371f7",         on: true },
  control:    { label: "player",       color: "#fff",            on: true },
};

const EVENT_CATEGORY = {
  state_change: "state",
  exposure: "infection", exposure_sent: "infection",
  cure_run_result: "cure", cure_run_started: "hops", cure_run_hop: "hops",
  heartbeat_anomaly: "heartbeat", heartbeat_misbehave: "heartbeat",
  neighbor_down: "alerts", neighbor_up: "alerts",
  alert_raised: "alerts", alert_seen: "alertrelay",
  tier_change: "trust", trust_threshold_set: "trust",
  player_action: "control", speed_set: "control",
  outbreak_detected: "cure",
};

const state = {
  topology: null,
  neighbors: {},     // id -> [ids]
  nodeStates: {},    // id -> {state, ts}
  status: {},        // id -> latest node_status event
  debug: { infected_by: {}, death_points: {}, anomalies_about: {}, alerts_about: {}, run_outcomes: {} },
  gameStatus: {},
  positions: {},
  selected: null,
  events: [],
  paused: false,
  filters: Object.fromEntries(Object.entries(CATEGORIES).map(([k, v]) => [k, v.on])),
  dirty: false,
  playerView: false,
  // one-shot pop-ups already shown in this game
  shown: {},
  alertCounts: {},  // alerts seen per node this game, for the headlines
};

const $ = (id) => document.getElementById(id);
const els = {
  svg: $("graph-svg"), graph: $("graph"),
  stateCounts: $("state-counts"), statRuns: $("stat-runs"),
  statAware: $("stat-aware"),
  statAlive: $("stat-alive"), statTotal: $("stat-total"),
  ws: $("ws-indicator"), banner: $("banner"),
  cureFill: $("cure-fill"), cureText: $("cure-text"), cureState: $("cure-state"),
  snipeBudget: $("snipe-budget"), cureBudget: $("cure-budget"),
  trustSlider: $("trust-slider"), trustValue: $("trust-value"),
  startTrustSlider: $("start-trust-slider"), startTrustValue: $("start-trust-value"),
  optPlayerView: $("opt-player-view"), viewName: $("view-name"),
  toasts: $("toasts"),
  endScreen: $("end-screen"), endTitle: $("end-title"), endScore: $("end-score"),
  endChart: $("end-chart"), endReveal: $("end-reveal"),
  strip: {
    survivors: $("strip-survivors"), zombies: $("strip-zombies"), dead: $("strip-dead"),
    cure: $("strip-cure"), tick: $("strip-tick"),
  },
  btnPause: $("btn-pause"), btnRestart: $("btn-restart"), btnStart: $("btn-start"),
  speedButtons: document.querySelectorAll(".speed-group button"),
  speedLabel: $("speed-label"), speedState: $("speed-state"),
  startOverlay: $("start-overlay"), startInfo: $("start-info"),
  tableBody: document.querySelector("#node-table tbody"),
  inspector: $("inspector"),
  log: $("log"), logFilters: $("log-filters"), logCount: $("log-count"),
  logSelectedOnly: $("log-selected-only"), logPause: $("log-pause"), logClear: $("log-clear"),
  optChain: $("opt-chain"), optTraffic: $("opt-traffic"), optTiers: $("opt-tiers"),
};

// everything that ends up in innerHTML goes through esc(): event fields come
// from POST /events, which anyone who can reach the aggregator can call
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (id) => esc(id ? String(id).replace("node-", "") : "?");
const tag = (s) => `<span class="tag ${esc(s)}">${esc(s)}</span>`;
const nodeState = (id) => state.nodeStates[id]?.state || "HEALTHY";
// What the current view may show: in player view a hidden infection looks
// exactly like a healthy node (zombies, cured and dead are visible to all).
const shownState = (id) => {
  const s = nodeState(id);
  return state.playerView && s === "INCUBATING" ? "HEALTHY" : s;
};
const isPZ = (id) => !state.playerView && id === state.topology?.patient_zero;

// -- layout (force-directed, computed once per topology) ------------------

function layout(topology) {
  const ids = topology.nodes.map((n) => n.id);
  const n = ids.length;
  const idx = Object.fromEntries(ids.map((id, i) => [id, i]));
  const arr = ids.map((_, i) => {
    const a = (2 * Math.PI * i) / n;
    return { x: W / 2 + 250 * Math.cos(a), y: H / 2 + 250 * Math.sin(a), vx: 0, vy: 0 };
  });
  const edges = topology.edges.map(([a, b]) => [idx[a], idx[b]]);

  for (let iter = 0; iter < 300; iter++) {
    for (let i = 0; i < n; i++) {
      let fx = 0, fy = 0;
      for (let j = 0; j < n; j++) {
        if (i === j) continue;
        const dx = arr[i].x - arr[j].x, dy = arr[i].y - arr[j].y;
        const d2 = Math.max(dx * dx + dy * dy, 40), d = Math.sqrt(d2);
        fx += (dx / d) * (40000 / d2);
        fy += (dy / d) * (40000 / d2);
      }
      arr[i].vx = (arr[i].vx + fx + (W / 2 - arr[i].x) * 0.002) * 0.6;
      arr[i].vy = (arr[i].vy + fy + (H / 2 - arr[i].y) * 0.002) * 0.6;
    }
    for (const [a, b] of edges) {
      const dx = arr[b].x - arr[a].x, dy = arr[b].y - arr[a].y;
      const d = Math.max(Math.hypot(dx, dy), 1), f = (d - 140) * 0.02;
      arr[a].vx += (dx / d) * f; arr[a].vy += (dy / d) * f;
      arr[b].vx -= (dx / d) * f; arr[b].vy -= (dy / d) * f;
    }
    arr.forEach((p) => { p.x += p.vx; p.y += p.vy; });
  }

  const xs = arr.map((p) => p.x), ys = arr.map((p) => p.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const padX = 70, padTop = 70, padBottom = 80;
  const pos = {};
  ids.forEach((id, i) => {
    pos[id] = {
      x: ((arr[i].x - minX) / Math.max(maxX - minX, 1)) * (W - 2 * padX) + padX,
      y: ((arr[i].y - minY) / Math.max(maxY - minY, 1)) * (H - padTop - padBottom) + padTop,
    };
  });
  return pos;
}

// -- graph ------------------------------------------------------------------

const layers = {};
const nodeEls = {};   // id -> {circle, sub, badges, ring}
const halfEls = {};   // "a|b" -> line from a towards the midpoint with b

function svgEl(name, attrs, parent) {
  const el = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

function renderGraph() {
  const svg = els.svg;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = "";
  const defs = svgEl("defs", {}, svg);
  const marker = svgEl("marker", { id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5,
    markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, defs);
  svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#f85149" }, marker);

  for (const name of ["edges", "chain", "paths", "fx", "nodes"]) layers[name] = svgEl("g", {}, svg);

  for (const [a, b] of state.topology.edges) {
    const pa = state.positions[a], pb = state.positions[b];
    const mx = (pa.x + pb.x) / 2, my = (pa.y + pb.y) / 2;
    halfEls[`${a}|${b}`] = svgEl("line", { class: "half unknown", x1: pa.x, y1: pa.y, x2: mx, y2: my }, layers.edges);
    halfEls[`${b}|${a}`] = svgEl("line", { class: "half unknown", x1: pb.x, y1: pb.y, x2: mx, y2: my }, layers.edges);
  }

  for (const node of state.topology.nodes) {
    const id = node.id, p = state.positions[id];
    const g = svgEl("g", {}, layers.nodes);
    let ring = null;
    if (id === state.topology.patient_zero) {
      ring = svgEl("circle", { class: "pz-ring debug-only", cx: p.x, cy: p.y, r: R + 5 }, g);
    }
    const circle = svgEl("circle", { cx: p.x, cy: p.y, r: R, class: "node-dot" }, g);
    circle.addEventListener("click", () => selectNode(id));
    const label = svgEl("text", { class: "node-id", x: p.x, y: p.y }, g);
    label.textContent = short(id);
    const sub = svgEl("text", { class: "node-sub", x: p.x, y: p.y + R + 14 }, g);
    const badges = svgEl("text", { class: "node-badges", x: p.x + R + 4, y: p.y - R + 2 }, g);
    const title = svgEl("title", {}, circle);
    nodeEls[id] = { circle, sub, badges, ring, title };
  }
  for (const node of state.topology.nodes) updateNode(node.id);
  renderChain();
}

function updateNode(id) {
  const ne = nodeEls[id];
  if (!ne) return;
  const s = shownState(id);
  const st = state.status[id];
  const stale = st && (Date.now() / 1000 - st.ts) > STALE_S;
  ne.circle.setAttribute("class", `node-dot ${s}${state.selected === id ? " selected" : ""}${stale ? " stale" : ""}`);

  let sub = s === "OVERT" ? "zombie" : s.toLowerCase();
  if (s === "INCUBATING" && st?.ticks_to_overt != null) sub += ` · overt in ${st.ticks_to_overt}t`;
  if (stale && !state.playerView) sub += " · no status!";
  ne.sub.textContent = sub;

  const d = state.debug;
  const parts = [];
  if (d.death_points[id]) parts.push(`✗${d.death_points[id]}`);
  if (d.alerts_about[id]) parts.push(`!${d.alerts_about[id]}`);
  ne.badges.textContent = parts.join(" ");
  ne.title.textContent = `${id} — ${s}${isPZ(id) ? " (patient zero)" : ""}`;

  // this node's own view of each link: its half of every edge
  if (st?.tiers) {
    for (const [nb, tier] of Object.entries(st.tiers)) {
      const half = halfEls[`${id}|${nb}`];
      if (!half) continue;
      half.setAttribute("class", `half ${s === "DEAD" ? "unknown" : tier}`);
      half.style.strokeOpacity = 0.35 + 0.65 * (st.trust?.[nb] ?? 0);
    }
  }
}

function renderChain() {
  if (!layers.chain) return;
  layers.chain.innerHTML = "";
  if (state.playerView) return;
  for (const [victim, info] of Object.entries(state.debug.infected_by)) {
    if (!info.by) continue;
    const a = state.positions[info.by], b = state.positions[victim];
    if (!a || !b) continue;
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.max(Math.hypot(dx, dy), 1);
    // slight curve so the arrow doesn't sit exactly on top of the edge
    const mx = (a.x + b.x) / 2 - (dy / d) * 18, my = (a.y + b.y) / 2 + (dx / d) * 18;
    const sx = a.x + (dx / d) * (R + 2), sy = a.y + (dy / d) * (R + 2);
    const ex = b.x - (dx / d) * (R + 4), ey = b.y - (dy / d) * (R + 4);
    const path = svgEl("path", { class: "infect-arrow", d: `M ${sx} ${sy} Q ${mx} ${my} ${ex} ${ey}`,
      "marker-end": "url(#arrow)" }, layers.chain);
    svgEl("title", {}, path).textContent = `${info.by} infected ${victim} via ${info.tier} at its tick ${info.tick}`;
  }
}

function pulseNode(id) {
  if (!els.optTraffic.checked) return;
  const p = state.positions[id];
  if (!p) return;
  const c = svgEl("circle", { class: "pulse", cx: p.x, cy: p.y, r: R }, layers.fx);
  setTimeout(() => c.remove(), 850);
}

function highlightPath(path, failed) {
  const pts = path.map((id) => state.positions[id]).filter(Boolean);
  if (pts.length < 2) return;
  const pl = svgEl("polyline", { class: `path-hl${failed ? " failed" : ""}`,
    points: pts.map((p) => `${p.x},${p.y}`).join(" ") }, layers.paths);
  setTimeout(() => pl.remove(), 5000);
}

function selectNode(id) {
  const prev = state.selected;
  state.selected = prev === id ? null : id;
  if (prev) updateNode(prev);
  if (state.selected) updateNode(state.selected);
  renderTable();
  renderInspector();
  if (els.logSelectedOnly.checked) rebuildLog();
}

// -- node table ---------------------------------------------------------------

function renderTable() {
  if (!state.topology) return;
  const d = state.debug;
  const rows = state.topology.nodes.map(({ id }) => {
    const s = shownState(id), st = state.status[id] || {};
    const by = d.infected_by[id];
    const byText = by ? (by.by ? short(by.by) : by.cause === "patient_zero" ? "P0" : "?") : "";
    return `<tr data-id="${id}" class="${state.selected === id ? "selected" : ""}">
      <td><b>${short(id)}</b>${isPZ(id) ? " <span class='muted'>P0</span>" : ""}</td>
      <td>${tag(s)}</td>
      <td class="debug-only">${st.outbreak_known ? "✓" : ""}</td>
      <td>${esc(st.tick ?? "-")}</td>
      <td class="debug-only">${s === "INCUBATING" && st.ticks_to_overt != null ? esc(st.ticks_to_overt) : ""}</td>
      <td class="debug-only">${byText}</td>
      <td>${d.death_points[id] || ""}</td>
      <td>${d.alerts_about[id] || ""}</td>
    </tr>`;
  });
  els.tableBody.innerHTML = rows.join("");
}

els.tableBody.addEventListener("click", (e) => {
  const tr = e.target.closest("tr");
  if (tr) selectNode(tr.dataset.id);
});

// -- inspector ----------------------------------------------------------------

function row(label, value) {
  return `<div class="row"><span>${label}</span><b>${value}</b></div>`;
}

function renderInspector() {
  const id = state.selected;
  if (!id) {
    els.inspector.innerHTML = `<p class="muted">Click a node in the graph or table.</p>`;
    return;
  }
  const s = nodeState(id);
  const st = state.status[id] || {};
  const d = state.debug;
  const age = st.ts ? Date.now() / 1000 - Number(st.ts) : null;
  const by = d.infected_by[id];
  const g = state.gameStatus;

  let infection = "not infected";
  if (by) {
    infection = by.cause === "patient_zero" ? "patient zero (seeded at start)"
      : `by <b>${esc(by.by)}</b> via ${esc(by.tier)} at its tick ${esc(by.tick ?? "?")}`;
  } else if (id === state.topology.patient_zero) infection = "patient zero";

  let overt = "-";
  if (s === "INCUBATING" && st.incubation_end_tick != null) {
    overt = `at tick ${esc(st.incubation_end_tick)} (in ${esc(st.ticks_to_overt)} ticks)`;
  } else if (s === "OVERT") overt = "already overt";

  const pv = state.playerView;
  const neighborRows = (state.neighbors[id] || []).map((nb) => {
    const score = Number(st.trust?.[nb] ?? 0) || 0;
    const tier = st.tiers?.[nb] || "?";
    const thr = Number(st.threshold ?? 0.5) || 0;
    if (pv) {
      return `<tr><td><a href="#" data-select="${nb}">${short(nb)}</a></td><td>${tag(shownState(nb))}</td></tr>`;
    }
    return `<tr>
      <td><a href="#" data-select="${nb}">${short(nb)}</a></td>
      <td>${tag(nodeState(nb))}</td>
      <td><span class="trust-bar"><i style="width:${score * 100}%"></i><u style="left:${thr * 100}%"></u></span> ${score.toFixed(2)}</td>
      <td>${esc(tier.toUpperCase())}${(st.down || []).includes(nb) ? " <span class='bad' title='failure detector: considered down'>down</span>" : ""}</td>
      <td>${esc(st.anomalies?.[nb] || "")}</td>
    </tr>`;
  }).join("");

  const c = st.stats || {};
  const counter = (label, key) => row(label, esc(c[key] || 0));

  const cureReady = (g.cure_progress || 0) >= 1;
  const alive = s !== "DEAD" && !!g.started;
  const actions = `
    <div class="actions">
      <button id="btn-snipe" ${alive && !g.over && (g.snipe_remaining ?? 1) > 0 ? "" : "disabled"}>Snipe</button>
      <button id="btn-cure" class="danger" ${alive && !g.over && cureReady && (g.cure_remaining ?? 1) > 0 ? "" : "disabled"}
        title="${cureReady ? "" : "cure research not at 100% yet"}">Cure</button>
    </div>
    <div id="action-msg">${esc(state.actionMsg || "")}</div>`;

  if (pv) {
    // player view: only what someone watching the network could know
    els.inspector.innerHTML = `
      <div class="title">${esc(id)} ${tag(shownState(id))}</div>
      <h3>Evidence against it</h3>
      ${row("✗ cure runs lost here", d.death_points[id] || 0)}
      ${row("! suspicion (alerts about it)", d.alerts_about[id] || 0)}
      <p class="muted small">Healthy nodes lose the odd courier and miss the odd heartbeat too —
        look for nodes that keep piling up evidence.</p>
      <h3>Neighbors</h3>
      <table><tbody>${neighborRows}</tbody></table>
      ${actions}`;
    bindInspector();
    return;
  }

  els.inspector.innerHTML = `
    <div class="title">${esc(id)} ${tag(s)} ${isPZ(id) ? "<span class='muted'>patient zero</span>" : ""}</div>
    ${row("local tick", esc(st.tick ?? "-"))}
    ${row("last status", age == null ? "none yet" : age > STALE_S
      ? `<span class="stale-warn">${age.toFixed(1)}s ago — node stuck or down?</span>` : `${age.toFixed(1)}s ago`)}
    ${row("infection", infection)}
    ${row("turns overt", overt)}
    ${row("knows of outbreak", !st.outbreak_known ? "no — won't start cure runs yet"
      : st.outbreak_known.how === "always_on" ? "n/a (CURE_AFTER_OUTBREAK off)"
      : `since tick ${esc(st.outbreak_known.tick)} (${st.outbreak_known.how === "alert" ? "alert from" : "bad heartbeat from"} ${short(st.outbreak_known.source)})`)}
    ${row("trust threshold", st.threshold != null ? Number(st.threshold).toFixed(2) : "-")}
    ${row("next cure run it starts", st.next_cure_run_tick != null ? `tick ${esc(st.next_cure_run_tick)}` : "-")}
    ${row("clock", st.tick_ms != null ? `${esc(st.tick_ms)} ms/tick${st.paused ? " · <b>paused</b>" : ""}` : "-")}

    <h3>Neighbors (this node's local view)</h3>
    <table>
      <thead><tr><th>node</th><th>state</th><th>trust</th><th>tier</th><th title="heartbeat anomalies this node saw from it">⚠ seen</th></tr></thead>
      <tbody>${neighborRows}</tbody>
    </table>

    <h3>What others observed about it</h3>
    ${row("cure runs that died here", d.death_points[id] || 0)}
    ${row("heartbeat anomalies reported about it", d.anomalies_about[id] || 0)}
    ${row("alerts raised about it", d.alerts_about[id] || 0)}

    <h3>Its own counters</h3>
    <div class="counters">
      ${counter("heartbeats sent", "heartbeats_sent")}
      ${counter("hb anomalies seen", "heartbeat_anomalies")}
      ${counter("trust syncs sent", "trust_syncs_sent")}
      ${row("pending pings", esc(st.pending_heartbeats ?? 0))}
      ${counter("exposures sent", "exposures_sent")}
      ${counter("exposures received", "exposures_received")}
      ${counter("runs originated", "runs_originated")}
      ${counter("runs relayed", "runs_relayed")}
      ${counter("runs dropped", "runs_dropped")}
      ${counter("runs completed", "runs_completed")}
      ${counter("alerts raised", "alerts_raised")}
      ${counter("alerts seen", "alerts_seen")}
    </div>
    ${actions}
  `;
  bindInspector();
}

function bindInspector() {
  $("btn-snipe").onclick = () => doAction("snipe");
  $("btn-cure").onclick = () => doAction("cure");
  els.inspector.querySelectorAll("[data-select]").forEach((a) =>
    a.addEventListener("click", (e) => { e.preventDefault(); selectNode(a.dataset.select); }));
}

async function doAction(action) {
  const id = state.selected;
  if (!id) return;
  try {
    const resp = await fetch(`/control/${id}/${action}`, { method: "POST" });
    const body = await resp.json().catch(() => ({}));
    state.actionMsg = resp.ok
      ? `${action} ${id}: ${body.correct ? "correct" : "WRONG"} → ${body.state}`
      : `${action} refused: ${body.error || resp.status}`;
  } catch (err) {
    state.actionMsg = `${action} failed: ${err}`;
  }
  renderInspector();
}

// -- header / status ------------------------------------------------------------

function renderStatus() {
  const g = state.gameStatus || {};
  const counts = g.state_counts || {};
  const chips = state.playerView
    ? [["HEALTHY", "looks healthy", (counts.HEALTHY ?? 0) + (counts.INCUBATING ?? 0)],
       ["OVERT", "zombies", counts.OVERT], ["CURED", "cured", counts.CURED], ["DEAD", "dead", counts.DEAD]]
    : [["HEALTHY", "healthy", counts.HEALTHY], ["INCUBATING", "incubating", counts.INCUBATING],
       ["OVERT", "zombies", counts.OVERT], ["CURED", "cured", counts.CURED], ["DEAD", "dead", counts.DEAD]];
  els.stateCounts.innerHTML = chips
    .map(([cls, label, n]) => `<span class="chip ${cls}">${label} ${n ?? 0}</span>`).join("");
  const reported = Object.values(state.status);
  els.statAware.textContent = `${reported.filter((st) => st.outbreak_known).length}/${g.total ?? "-"}`;
  renderCure(g);
  renderStrip(g);
  checkMilestones(g);
  els.statRuns.textContent = `${g.runs_started ?? 0} started · ${g.runs_completed ?? 0} ok · ${g.runs_failed ?? 0} failed`;
  els.statRuns.title = Object.entries(state.debug.run_outcomes || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
  renderSpeed();
  els.statAlive.textContent = g.alive ?? "-";
  els.statTotal.textContent = g.total ?? "-";
  els.snipeBudget.textContent = g.snipe_remaining ?? "-";
  els.cureBudget.textContent = g.cure_remaining ?? "-";

  // g.over is decided once by the aggregator and sticks until restart
  if (g.over === "loss") {
    els.banner.className = "loss";
    els.banner.textContent = "OUTBREAK COMPLETE — every remaining node has turned. You lost. Press restart to play again.";
  } else if (g.over === "stable") {
    els.banner.className = "win";
    els.banner.textContent = `Infection wiped out! Final score: ${g.score} / ${g.total} nodes alive.`;
  } else if (g.over === "time_up") {
    els.banner.className = "paused";
    els.banner.textContent = `Time's up (${g.max_ticks} ticks). Score: ${g.score} / ${g.total} nodes alive, `
      + `${g.overt} still zombies, ${g.state_counts?.INCUBATING ?? 0} still infected.`;
  } else if (g.started && g.speed?.paused) {
    els.banner.className = "paused";
    els.banner.textContent = "PAUSED";
  } else {
    els.banner.className = "";
    els.banner.textContent = "";
  }
}

function setStat(el, value) {
  const text = String(value);
  if (el.textContent === text) return;
  el.textContent = text;
  el.classList.remove("bump");
  void el.offsetWidth;  // restart the animation
  el.classList.add("bump");
}

function renderStrip(g) {
  const c = g.state_counts || {};
  setStat(els.strip.survivors, (g.alive ?? 0) - (c.OVERT ?? 0));
  setStat(els.strip.zombies, c.OVERT ?? 0);
  setStat(els.strip.dead, c.DEAD ?? 0);
  setStat(els.strip.cure, `${Math.round(Math.min(1, g.cure_progress || 0) * 100)}%`);
  setStat(els.strip.tick, `${g.tick ?? 0} / ${g.max_ticks ?? "?"}`);
}

// Non-blocking pop-up; the game keeps running underneath.
function popup(text, kind = "info") {
  const div = document.createElement("div");
  div.className = `toast ${kind}`;
  div.textContent = text;
  els.toasts.appendChild(div);
  setTimeout(() => div.remove(), 3200);
}

function once(key, fn) {
  if (state.shown[key]) return;
  state.shown[key] = true;
  fn();
}

function checkMilestones(g) {
  if (!g.started) return;
  if ((g.cure_progress || 0) >= 1) once("cure", () => popup("CURE READY", "good"));
  if (g.over === "loss") once("over", () => popup("OUTBREAK COMPLETE — YOU LOST", "danger"));
  if (g.over === "stable") once("over", () => popup("INFECTION WIPED OUT", "good"));
  if (g.over === "time_up") once("over", () => popup("TIME'S UP", "info"));
  if (g.over) once("end_screen", () => setTimeout(showEndScreen, 1500));
}

// -- end screen: score, infection curve, and who was secretly infected -----

const CURVE_ORDER = ["DEAD", "OVERT", "INCUBATING", "CURED", "HEALTHY"];
const CURVE_COLOR = { HEALTHY: "#3fb950", INCUBATING: "#d29922", OVERT: "#f85149", CURED: "#58a6ff", DEAD: "#45505c" };

function curveSvg(series, total) {
  const Wc = 600, Hc = 200;
  if (series.length < 2 || !total) return "<p class='muted'>Not enough data for a curve.</p>";
  const t0 = series[0].tick, t1 = series[series.length - 1].tick || 1;
  const x = (tick) => ((tick - t0) / Math.max(t1 - t0, 1)) * Wc;
  const y = (n) => Hc - (n / total) * Hc;
  const below = series.map(() => 0);
  let paths = "";
  for (const s of CURVE_ORDER) {
    const top = series.map((p, i) => below[i] + (Number(p[s]) || 0));
    const pts = series.map((p, i) => `${x(p.tick)},${y(top[i])}`)
      .concat(series.map((p, i) => `${x(p.tick)},${y(below[i])}`).reverse());
    paths += `<polygon points="${pts.join(" ")}" fill="${CURVE_COLOR[s]}" />`;
    top.forEach((v, i) => { below[i] = v; });
  }
  return `<svg viewBox="0 0 ${Wc} ${Hc}" preserveAspectRatio="none">${paths}</svg>`
    + `<div class="end-legend"><span>tick ${esc(t0)}</span><span style="margin-left:auto">tick ${esc(t1)}</span></div>`;
}

async function showEndScreen() {
  let summary;
  try {
    summary = await (await fetch("/game/summary")).json();
  } catch (_) {
    return;
  }
  const g = summary.game_status || {};
  const titles = { stable: ["Infection wiped out!", "win"], loss: ["The network fell", "loss"],
    time_up: ["Time's up", "time_up"] };
  const [title, cls] = titles[g.over] || ["Game over", ""];
  els.endTitle.textContent = title;
  els.endTitle.className = cls;
  const c = g.state_counts || {};
  els.endScore.textContent = `Score ${g.score ?? 0} / ${g.total ?? 0} nodes alive · ${c.DEAD ?? 0} dead · `
    + `${c.CURED ?? 0} cured · ${c.OVERT ?? 0} zombies · lasted ${g.tick ?? 0} ticks`;
  els.endChart.innerHTML = curveSvg(summary.series || [], g.total);

  const infections = Object.entries(summary.infected_by || {})
    .sort((a, b) => (a[1].tick ?? 0) - (b[1].tick ?? 0));
  els.endReveal.innerHTML = infections.map(([node, info]) => {
    const how = info.cause === "patient_zero" ? "<b>patient zero</b>"
      : `infected by <b>${short(info.by)}</b> via ${esc(String(info.tier || "").replace("_", " "))}`;
    return `<li><b>node ${short(node)}</b> — ${how} at tick ${esc(info.tick ?? 0)} `
      + `<span class="muted">→ ended ${tag(nodeState(node))}</span></li>`;
  }).join("") || "<li class='muted'>Nobody else was infected.</li>";
  els.endScreen.hidden = false;
}

$("end-close").addEventListener("click", () => { els.endScreen.hidden = true; });
$("end-restart").addEventListener("click", async () => {
  els.endScreen.hidden = true;
  await fetch("/control/restart", { method: "POST" });
});

function eventPopup(ev) {
  if (ev.type === "state_change" && ev.to === "OVERT") {
    once("first_zombie", () => popup("FIRST ZOMBIE SIGHTED", "danger"));
  } else if (ev.type === "player_action") {
    if (ev.correct) popup(ev.action === "snipe" ? "TARGET WAS INFECTED" : "PATIENT CURED", "good");
    else popup("A SURVIVOR WAS KILLED", "danger");
  }
}

function renderCure(g) {
  const progress = Math.min(1, g.cure_progress || 0);
  const pct = Math.round(progress * 100);
  const done = Math.min(g.runs_completed ?? 0, g.runs_needed ?? 0);
  const ready = progress >= 1;
  els.cureFill.style.width = pct + "%";
  els.cureFill.classList.toggle("ready", ready);
  els.cureText.textContent = `${pct}% · ${done}/${g.runs_needed ?? "?"} cure runs completed`;
  let label;
  if (!g.started) label = "not started";
  else if (ready) label = `READY — cure unlocked (${g.cure_remaining ?? 0} left)`;
  else if (!g.runs_started) label = "waiting for the outbreak to be noticed";
  else label = "researching…";
  els.cureState.textContent = label;
  els.cureState.classList.toggle("ready", ready);
}

// -- event log -----------------------------------------------------------------

function describe(ev) {
  const n = `<b>${short(ev.node)}</b>`;
  const b = (id) => `<b>${short(id)}</b>`;
  switch (ev.type) {
    case "state_change": {
      if (state.playerView) {
        // don't leak the hidden state the node came from
        if (ev.to === "OVERT") return `${n} turned into a <b>ZOMBIE</b>`;
        return `${n} → ${tag(ev.to)}${ev.cause === "snipe" ? " (sniped)" : ev.cause === "cure" ? " (cure applied)" : ""}`;
      }
      let why = esc(ev.cause || "");
      if (ev.cause === "exposure") why = `infected by ${b(ev.by)} via ${esc(ev.tier)}, will turn overt at tick ${esc(ev.overt_at_tick)}`;
      else if (ev.cause === "incubation_timer") why = "incubation timer ran out";
      return `${n} ${ev.from_state ? tag(ev.from_state) + " → " : ""}${tag(ev.to)} <span>${why}</span>`;
    }
    case "exposure":
      return ev.infected
        ? `${n} <b>INFECTED</b> by ${b(ev.source)} over ${esc(ev.tier)}`
        : `${n} got exposure from ${b(ev.source)} over ${esc(ev.tier)} — no effect (was ${tag(ev.state_before)})`;
    case "exposure_sent":
      return `${n} ${tag(ev.sender_state)} pushed infection at ${b(ev.to)} over ${esc(ev.tier)}`;
    case "cure_run_started":
      return `run <b>${esc(ev.run_id).slice(0, 6)}</b> started at ${n} → ${b(ev.next_hop)} (${esc(ev.tier)})`;
    case "cure_run_hop":
      return `run <b>${esc(ev.run_id).slice(0, 6)}</b> ${n} relayed → ${b(ev.next_hop)} (${esc(ev.tier)}, ${esc(ev.hops_remaining)} hops left)`;
    case "cure_run_result": {
      const path = (Array.isArray(ev.path) ? ev.path : []).map(short).join("→");
      if (ev.success) return `run <b>${esc(ev.run_id).slice(0, 6)}</b> <span class="ok">COMPLETED</span> ${path}`;
      if (ev.reason === "send_failed") return `run <b>${esc(ev.run_id).slice(0, 6)}</b> <span class="bad">LOST</span> in transit ${n} → ${b(ev.lost_towards)} (tcp send failed) ${path}`;
      return `run <b>${esc(ev.run_id).slice(0, 6)}</b> <span class="bad">DIED</span> at ${b(ev.died_at)} (${esc(ev.reason)}) ${path}`;
    }
    case "heartbeat_anomaly":
      return `${n} saw <b>${esc(ev.kind)}</b> heartbeat from ${b(ev.about)} (#${esc(ev.count)} from it)${ev.alerted ? " → raising alert" : ""}`;
    case "neighbor_down":
      if (ev.reason === "left") return `${n} got a goodbye from ${b(ev.neighbor)} (it was shot) — marks it <b>DOWN</b>`;
      return `${n} considers ${b(ev.neighbor)} <b>DOWN</b> (${esc(ev.misses)} heartbeats in a row unanswered) — no more cure runs through it`;
    case "neighbor_up":
      return `${n} hears from ${b(ev.neighbor)} again — considers it <b>up</b>`;
    case "heartbeat_misbehave":
      return `${n} ${tag("OVERT")} ${esc(ev.behavior)} its heartbeat reply to ${b(ev.to)}${ev.delay_s ? ` (+${esc(ev.delay_s)}s)` : ""}`;
    case "alert_raised":
      return `${n} <b>ALERT</b> about ${b(ev.about)} (${esc(ev.reason)})`;
    case "alert_seen":
      return `${n} received alert about ${b(ev.about)} from ${b(ev.origin)}${ev.via ? ` via ${b(ev.via)}` : ""} (ttl ${esc(ev.ttl)})`;
    case "tier_change":
      return `${n} → ${b(ev.neighbor)} now <b>${esc(ev.tier).toUpperCase()}</b> (trust ${esc(ev.score)} vs threshold ${esc(ev.threshold)})`;
    case "trust_threshold_set": {
      const flipped = Object.entries(ev.flipped || {}).map(([k, v]) => `${short(k)}→${esc(v)}`).join(", ");
      return `${n} threshold set to ${esc(ev.value)}${flipped ? ` (flipped: ${flipped})` : ""}`;
    }
    case "outbreak_detected":
      if (ev.how === "neighbor_killed") return `${n} saw neighbor ${b(ev.source)} get shot → the outbreak is real, starts cure runs (first at tick ${esc(ev.first_run_at_tick)})`;
      return ev.how === "alert"
        ? `${n} learned of the outbreak from an alert by ${b(ev.source)} → starts cure runs (first at tick ${esc(ev.first_run_at_tick)})`
        : `${n} <b>noticed the outbreak</b> itself (bad heartbeat from ${b(ev.source)}) → starts cure runs (first at tick ${esc(ev.first_run_at_tick)})`;
    case "speed_set":
      return `${n} clock set to ${esc(ev.tick_ms)} ms/tick${ev.paused ? " — <b>paused</b>" : ""}`;
    case "player_action":
      return `<b>${esc(ev.action).toUpperCase()}</b> on ${n}: <span class="${ev.correct ? "ok" : "bad"}">${ev.correct ? "correct" : "WRONG"}</span> (${state.playerView ? "" : tag(ev.prev_state || "?") + " → "}${tag(ev.new_state)})`;
    default:
      return `${n} ${esc(ev.type)}`;
  }
}

function involves(ev, id) {
  if (!id) return true;
  return ev.node === id || ev.about === id || ev.to === id || ev.source === id || ev.by === id
    || ev.neighbor === id || ev.next_hop === id || ev.died_at === id || ev.origin === id
    || ev.lost_towards === id || (ev.path || []).includes(id);
}

// Player view: news-style headlines for the few things a player could know
// (what they did, what became visibly true, alerts and cure couriers).
// null = not news; the event stays out of the feed.
function headline(ev) {
  const n = `<b>node ${short(ev.node)}</b>`;
  switch (ev.type) {
    case "alert_raised":
      // one story per node, not one per alert: first report, then every 5th
      if (ev._nth === 1) return `First reports of strange behaviour around <b>node ${short(ev.about)}</b>`;
      return ev._nth % 5 === 0 ? `<b>Node ${short(ev.about)}</b>: ${ev._nth} reports of strange behaviour so far` : null;
    case "cure_run_result":
      if (ev.success) return "Cure courier arrived — research advances";
      return ev.died_at ? `A cure courier went missing near <b>node ${short(ev.died_at)}</b>` : null;
    case "state_change":
      return ev.to === "OVERT" ? `<span class="bad">ZOMBIE sighted at ${n}!</span>` : null;
    case "player_action":
      if (ev.action === "snipe") {
        return ev.correct ? `<span class="ok">${n} neutralised — it was infected</span>`
          : `<span class="bad">${n} was healthy. A survivor is dead.</span>`;
      }
      return ev.correct ? `<span class="ok">${n} cured</span>`
        : `<span class="bad">The cure killed healthy ${n}</span>`;
    default:
      return null;
  }
}

function visible(ev) {
  if (state.playerView && headline(ev) === null) return false;
  if (state.playerView) return !els.logSelectedOnly.checked || involves(ev, state.selected);
  const cat = EVENT_CATEGORY[ev.type] || "state";
  if (!state.filters[cat]) return false;
  if (els.logSelectedOnly.checked && !involves(ev, state.selected)) return false;
  return true;
}

function makeRow(ev) {
  const div = document.createElement("div");
  const cat = EVENT_CATEGORY[ev.type] || "state";
  div.className = `log-row cat-${cat}`;
  const t = new Date((ev.ts || Date.now() / 1000) * 1000);
  const time = t.toLocaleTimeString([], { hour12: false }) + "." + String(t.getMilliseconds()).padStart(3, "0");
  div.innerHTML = `<span class="ts">${time}</span><span class="tick">t${esc(ev.tick ?? "?")}</span>`
    + `<span class="who">${esc(ev.node || "")}</span><span class="msg">${state.playerView ? headline(ev) : describe(ev)}</span>`;
  if (ev.path || ev.node) {
    div.classList.add("clickable");
    div.title = ev.path ? "click: select node and show the run's path" : "click: select node";
    div.addEventListener("click", () => {
      if (ev.path) highlightPath(ev.path, ev.type === "cure_run_result" && !ev.success);
      const target = ev.died_at || ev.node;
      if (target && target !== state.selected) selectNode(target);
    });
  }
  return div;
}

function rebuildLog() {
  els.log.innerHTML = "";
  const frag = document.createDocumentFragment();
  let shown = 0;
  for (let i = state.events.length - 1; i >= 0 && shown < MAX_ROWS; i--) {
    if (!visible(state.events[i])) continue;
    frag.appendChild(makeRow(state.events[i]));
    shown++;
  }
  els.log.appendChild(frag);
  els.logCount.textContent = `${state.events.length} events`;
}

function pushEvent(ev) {
  if (ev.type === "alert_raised") {
    state.alertCounts[ev.about] = (state.alertCounts[ev.about] || 0) + 1;
    ev._nth = state.alertCounts[ev.about];
  }
  state.events.push(ev);
  if (state.events.length > MAX_EVENTS) state.events.splice(0, state.events.length - MAX_EVENTS);
  els.logCount.textContent = `${state.events.length} events${state.paused ? " (paused)" : ""}`;
  if (state.paused || !visible(ev)) return;
  els.log.prepend(makeRow(ev));
  while (els.log.children.length > MAX_ROWS) els.log.lastChild.remove();
}

// -- moving packets --------------------------------------------------------

const MAX_PACKETS = 60;  // keeps a weak PC responsive at 8× speed
const PACKET_MS = 650;
const packets = [];
let packetLoop = null;

// A dot travelling along an edge for one real message (cure courier,
// alert, infection attempt). Purely visual; drawn from observed events.
function spawnPacket(from, to, kind) {
  // alerts come in floods: leave room for the cure couriers
  const limit = kind === "alert" ? MAX_PACKETS - 20 : MAX_PACKETS;
  if (!els.optTraffic.checked || !layers.fx || packets.length >= limit) return;
  const a = state.positions[from], b = state.positions[to];
  if (!a || !b) return;
  const el = svgEl("circle", { class: `packet ${kind}`, r: kind === "cure" ? 6 : 4, cx: a.x, cy: a.y }, layers.fx);
  packets.push({ el, a, b, start: performance.now() });
  if (!packetLoop) packetLoop = requestAnimationFrame(stepPackets);
}

function stepPackets(now) {
  for (let i = packets.length - 1; i >= 0; i--) {
    const p = packets[i];
    const t = Math.min(1, (now - p.start) / PACKET_MS);
    const e = t < 0.5 ? 2 * t * t : 1 - (2 - 2 * t) ** 2 / 2;  // ease in-out
    p.el.setAttribute("cx", p.a.x + (p.b.x - p.a.x) * e);
    p.el.setAttribute("cy", p.a.y + (p.b.y - p.a.y) * e);
    if (t >= 1) { p.el.remove(); packets.splice(i, 1); }
  }
  packetLoop = packets.length ? requestAnimationFrame(stepPackets) : null;
}

function animate(ev) {
  switch (ev.type) {
    case "exposure":
      if (ev.infected && !state.playerView) spawnPacket(ev.source, ev.node, "infect");
      break;
    case "cure_run_started":
    case "cure_run_hop":
      spawnPacket(ev.node, ev.next_hop, "cure");
      break;
    case "cure_run_result":
      if (ev.success) highlightPath(ev.path || [], false);
      else if (ev.died_at) pulseNode(ev.died_at);
      break;
    case "alert_raised":
      for (const nb of state.neighbors[ev.node] || []) spawnPacket(ev.node, nb, "alert");
      break;
    case "alert_seen":
      // the gossip hopping onwards, one neighbor at a time
      if (ev.via) spawnPacket(ev.via, ev.node, "alert");
      break;
  }
}

function buildFilters() {
  els.logFilters.innerHTML = Object.entries(CATEGORIES).map(([k, v]) =>
    `<label><input type="checkbox" data-cat="${k}" ${state.filters[k] ? "checked" : ""}/>`
    + `<i style="background:${v.color}"></i>${v.label}</label>`).join("");
  els.logFilters.querySelectorAll("input").forEach((cb) => cb.addEventListener("change", () => {
    state.filters[cb.dataset.cat] = cb.checked;
    rebuildLog();
  }));
}

// -- websocket -------------------------------------------------------------------

function applyDebug(debug) {
  if (!debug) return;
  for (const k of Object.keys(state.debug)) if (debug[k]) state.debug[k] = debug[k];
}

function markDirty() {
  if (state.dirty) return;
  state.dirty = true;
  // coalesce the many messages per second into one repaint
  setTimeout(() => {
    state.dirty = false;
    renderStatus();
    renderTable();
    renderChain();
    if (state.topology) state.topology.nodes.forEach(({ id }) => updateNode(id));
    // don't rebuild the inspector under the cursor while a button is being clicked
    if (state.selected && !document.querySelector("#inspector .actions:hover")) renderInspector();
  }, 250);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => { els.ws.textContent = "live"; };
  ws.onclose = () => {
    els.ws.textContent = "disconnected — retrying…";
    setTimeout(connect, 1500);
  };
  ws.onerror = () => ws.close();

  ws.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.type === "snapshot") {
      state.topology = data.topology;
      state.neighbors = {};
      for (const { id } of data.topology.nodes) state.neighbors[id] = [];
      for (const [a, b] of data.topology.edges) { state.neighbors[a].push(b); state.neighbors[b].push(a); }
      state.nodeStates = data.node_states;
      state.gameStatus = data.game_status;
      state.status = data.debug?.node_status || {};
      applyDebug(data.debug);
      state.positions = layout(data.topology);
      const anyStatus = Object.values(state.status)[0];
      if (anyStatus) {
        els.trustSlider.value = anyStatus.threshold;
        els.trustValue.textContent = Number(anyStatus.threshold).toFixed(2);
      }
      state.events = [];
      state.alertCounts = {};
      for (const ev of data.recent_events) pushEvent(ev);
      if (state.gameStatus.game !== state.shownForGame) {
        state.shown = {};
        state.shownForGame = state.gameStatus.game;
        els.endScreen.hidden = true;
      }
      renderGraph();
      rebuildLog();
      markDirty();
      // e.g. http://localhost:8000/#node-04 opens with that node selected
      const fromHash = decodeURIComponent(location.hash.slice(1));
      if (fromHash && state.neighbors[fromHash] && state.selected !== fromHash) selectNode(fromHash);
      return;
    }
    if (data.type === "status") {
      const st = data.status;
      state.status[st.node] = st;
      if (st.state && nodeState(st.node) !== st.state) state.nodeStates[st.node] = { state: st.state, ts: st.ts };
      state.gameStatus = data.game_status;
      applyDebug(data.debug);
      markDirty();
      return;
    }
    if (data.type === "event") {
      const ev = data.event;
      if (ev.type === "state_change" && ev.node) state.nodeStates[ev.node] = { state: ev.to, ts: ev.ts };
      state.gameStatus = data.game_status;
      pushEvent(ev);
      animate(ev);
      eventPopup(ev);
      markDirty();
    }
  };
}

// -- controls -----------------------------------------------------------------------

function renderSpeed() {
  const g = state.gameStatus;
  const speed = g.speed;
  if (!speed) return;
  els.btnPause.textContent = g.over ? "game over" : !g.started ? "▶ start" : speed.paused ? "▶ resume" : "⏸ pause";
  els.btnPause.disabled = !!g.over;
  els.speedLabel.title = `game ends at tick ${g.max_ticks ?? "?"}`;
  els.btnPause.classList.toggle("on", speed.paused);
  els.speedButtons.forEach((b) =>
    b.classList.toggle("active", Math.round(BASE_TICK_MS / Number(b.dataset.mult)) === speed.tick_ms));
  els.speedLabel.textContent = `1 tick = ${(speed.tick_ms / 1000).toFixed(2).replace(/\.?0+$/, "")} s`;
  // nodes are told one by one; show if any of them disagrees with the request
  const reported = Object.values(state.status);
  const lagging = reported.filter((st) => st.tick_ms !== speed.tick_ms || st.paused !== speed.paused).length;
  els.speedState.textContent = lagging ? `· ${lagging} node(s) not updated yet` : "";

  els.startOverlay.hidden = !!g.started;
  els.startInfo.textContent = `Game ${g.game ?? 1} is set up and waiting.`
    + (state.playerView ? " One node is already secretly infected." : ` Patient zero: ${g.patient_zero || "?"}`);
}

async function setThreshold(value) {
  await fetch("/control/trust_threshold", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
  els.trustSlider.value = value;
  els.trustValue.textContent = Number(value).toFixed(2);
}

async function startGame() {
  // the threshold picked on the start screen goes out before the clocks run
  await setThreshold(Number(els.startTrustSlider.value));
  await setSpeed({ paused: false });
}

async function setSpeed(update) {
  const resp = await fetch("/control/speed", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  const body = await resp.json().catch(() => null);
  if (body?.ok) {
    state.gameStatus.speed = { tick_ms: body.tick_ms, paused: body.paused };
    state.gameStatus.started = body.started;
    renderStatus();
  }
}

async function restartGame() {
  if (!confirm("Throw away this game and start a new one?")) return;
  state.actionMsg = "";
  await fetch("/control/restart", { method: "POST" });
  // the aggregator pushes a fresh snapshot over the websocket
}

els.btnPause.addEventListener("click", () =>
  state.gameStatus.started ? setSpeed({ paused: !state.gameStatus.speed?.paused }) : startGame());
els.btnStart.addEventListener("click", startGame);
els.startTrustSlider.addEventListener("input", () => {
  els.startTrustValue.textContent = Number(els.startTrustSlider.value).toFixed(2);
});
els.btnRestart.addEventListener("click", restartGame);
els.speedButtons.forEach((b) => b.addEventListener("click", () =>
  setSpeed({ tick_ms: Math.round(BASE_TICK_MS / Number(b.dataset.mult)) })));

els.trustSlider.addEventListener("input", () => {
  els.trustValue.textContent = Number(els.trustSlider.value).toFixed(2);
});
els.trustSlider.addEventListener("change", () => setThreshold(Number(els.trustSlider.value)));

els.logSelectedOnly.addEventListener("change", rebuildLog);
els.logPause.addEventListener("click", () => {
  state.paused = !state.paused;
  els.logPause.textContent = state.paused ? "resume" : "pause";
  els.logPause.classList.toggle("on", state.paused);
  if (!state.paused) rebuildLog();
});
els.logClear.addEventListener("click", () => { state.events = []; rebuildLog(); });

els.optChain.addEventListener("change", () => els.graph.classList.toggle("no-chain", !els.optChain.checked));
els.optTiers.addEventListener("change", applyView);

function applyView() {
  state.playerView = els.optPlayerView.checked;
  try { localStorage.setItem("playerView", state.playerView ? "1" : "0"); } catch (_) { /* private mode */ }
  document.body.classList.toggle("player-view", state.playerView);
  els.viewName.textContent = state.playerView ? "player view" : "debug view";
  // per-link trust/tier is node-internal knowledge
  els.graph.classList.toggle("no-tiers", state.playerView || !els.optTiers.checked);
  if (!state.topology) return;
  state.topology.nodes.forEach(({ id }) => updateNode(id));
  renderChain();
  renderTable();
  renderInspector();
  renderStatus();
  rebuildLog();
}

window.addEventListener("hashchange", () => {
  const id = decodeURIComponent(location.hash.slice(1));
  if (state.neighbors[id] && state.selected !== id) selectNode(id);
});

try { els.optPlayerView.checked = localStorage.getItem("playerView") === "1"; } catch (_) { /* ignore */ }
els.optPlayerView.addEventListener("change", applyView);
applyView();

buildFilters();
connect();
