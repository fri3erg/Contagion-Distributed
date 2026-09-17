// Contagion Protocol dashboard.
//
// This is a pure observer + command sender: it renders whatever the
// aggregator's observability stream tells it and issues control-plane
// commands through the aggregator's proxy. It never computes game logic
// itself -- every color, every progress bar, every "who died" comes
// straight from events the nodes themselves emitted.

const SVG_NS = "http://www.w3.org/2000/svg";

const state = {
  topology: null,
  nodeStates: {},   // id -> {state, ts}
  positions: {},    // id -> {x, y}
  selected: null,
  gameStatus: {},
};

const els = {
  svg: document.getElementById("graph-svg"),
  graphContainer: document.getElementById("graph"),
  statAlive: document.getElementById("stat-alive"),
  statTotal: document.getElementById("stat-total"),
  statOvert: document.getElementById("stat-overt"),
  statCure: document.getElementById("stat-cure"),
  wsIndicator: document.getElementById("ws-indicator"),
  banner: document.getElementById("banner"),
  cureFill: document.getElementById("cure-fill"),
  snipeBudget: document.getElementById("snipe-budget"),
  cureBudget: document.getElementById("cure-budget"),
  trustSlider: document.getElementById("trust-slider"),
  trustValue: document.getElementById("trust-value"),
  selectedNode: document.getElementById("selected-node"),
  btnSnipe: document.getElementById("btn-snipe"),
  btnCure: document.getElementById("btn-cure"),
  feed: document.getElementById("feed"),
};

// -- force-directed layout (computed once per topology, then static) -----

function layout(topology) {
  const ids = topology.nodes.map((n) => n.id);
  const n = ids.length;
  const pos = {};
  const idx = {};
  ids.forEach((id, i) => {
    idx[id] = i;
    const angle = (2 * Math.PI * i) / n;
    pos[id] = { x: 500 + 380 * Math.cos(angle), y: 380 + 380 * Math.sin(angle) };
  });

  const edges = topology.edges.map(([a, b]) => [idx[a], idx[b]]);
  const arr = ids.map((id) => ({ x: pos[id].x, y: pos[id].y, vx: 0, vy: 0 }));

  const REPULSION = 26000;
  const SPRING = 0.02;
  const SPRING_LEN = 90;
  const CENTER = 0.002;
  const CX = 500, CY = 380;

  for (let iter = 0; iter < 260; iter++) {
    for (let i = 0; i < n; i++) {
      let fx = 0, fy = 0;
      for (let j = 0; j < n; j++) {
        if (i === j) continue;
        const dx = arr[i].x - arr[j].x;
        const dy = arr[i].y - arr[j].y;
        const d2 = Math.max(dx * dx + dy * dy, 40);
        const f = REPULSION / d2;
        const d = Math.sqrt(d2);
        fx += (dx / d) * f;
        fy += (dy / d) * f;
      }
      fx += (CX - arr[i].x) * CENTER;
      fy += (CY - arr[i].y) * CENTER;
      arr[i].vx = (arr[i].vx + fx) * 0.6;
      arr[i].vy = (arr[i].vy + fy) * 0.6;
    }
    for (const [a, b] of edges) {
      const dx = arr[b].x - arr[a].x;
      const dy = arr[b].y - arr[a].y;
      const d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const f = (d - SPRING_LEN) * SPRING;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      arr[a].vx += fx; arr[a].vy += fy;
      arr[b].vx -= fx; arr[b].vy -= fy;
    }
    for (let i = 0; i < n; i++) {
      arr[i].x += arr[i].vx;
      arr[i].y += arr[i].vy;
    }
  }

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  arr.forEach((p) => {
    minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
    minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
  });
  const pad = 40;
  const w = Math.max(maxX - minX, 1), h = Math.max(maxY - minY, 1);
  ids.forEach((id, i) => {
    pos[id] = {
      x: ((arr[i].x - minX) / w) * (1000 - 2 * pad) + pad,
      y: ((arr[i].y - minY) / h) * (760 - 2 * pad) + pad,
    };
  });
  return pos;
}

// -- rendering -------------------------------------------------------------

function renderGraph() {
  const svg = els.svg;
  svg.setAttribute("viewBox", "0 0 1000 760");
  svg.innerHTML = "";

  const edgeLayer = document.createElementNS(SVG_NS, "g");
  const nodeLayer = document.createElementNS(SVG_NS, "g");

  for (const [a, b] of state.topology.edges) {
    const pa = state.positions[a], pb = state.positions[b];
    if (!pa || !pb) continue;
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("class", "edge");
    line.setAttribute("x1", pa.x); line.setAttribute("y1", pa.y);
    line.setAttribute("x2", pb.x); line.setAttribute("y2", pb.y);
    edgeLayer.appendChild(line);
  }

  for (const node of state.topology.nodes) {
    const p = state.positions[node.id];
    if (!p) continue;
    const g = document.createElementNS(SVG_NS, "g");

    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("cx", p.x);
    circle.setAttribute("cy", p.y);
    circle.setAttribute("r", node.id === state.topology.patient_zero ? 9 : 7);
    circle.setAttribute("class", "node-dot " + nodeClass(node.id));
    circle.dataset.id = node.id;
    circle.addEventListener("click", () => selectNode(node.id));

    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", p.x);
    label.setAttribute("y", p.y - 12);
    label.setAttribute("class", "node-label");
    label.textContent = node.id.replace("node-", "");

    g.appendChild(circle);
    g.appendChild(label);
    nodeLayer.appendChild(g);
  }

  svg.appendChild(edgeLayer);
  svg.appendChild(nodeLayer);
}

function nodeClass(id) {
  const s = state.nodeStates[id]?.state || "HEALTHY";
  const sel = state.selected === id ? " selected" : "";
  return s + sel;
}

function updateNodeVisual(id) {
  const circle = els.svg.querySelector(`circle[data-id="${CSS.escape(id)}"]`);
  if (circle) circle.setAttribute("class", "node-dot " + nodeClass(id));
}

function selectNode(id) {
  const prev = state.selected;
  state.selected = id;
  if (prev) updateNodeVisual(prev);
  updateNodeVisual(id);
  renderSelected();
}

function renderSelected() {
  const id = state.selected;
  if (!id) {
    els.selectedNode.innerHTML = `<div class="row"><span>Click a node</span></div>`;
    els.btnSnipe.disabled = true;
    els.btnCure.disabled = true;
    return;
  }
  const s = state.nodeStates[id]?.state || "HEALTHY";
  const isPZ = id === state.topology.patient_zero;
  els.selectedNode.innerHTML = `
    <div class="row"><span>ID</span><b>${id}${isPZ ? " (patient zero, if revealed)" : ""}</b></div>
    <div class="row"><span>State</span><b>${s}</b></div>
  `;
  const alive = s !== "DEAD";
  const cureReady = (state.gameStatus.cure_progress || 0) >= 1.0;
  els.btnSnipe.disabled = !alive || (state.gameStatus.snipe_remaining ?? 1) <= 0;
  els.btnCure.disabled = !alive || !cureReady || (state.gameStatus.cure_remaining ?? 1) <= 0;
}

function renderStatus() {
  const g = state.gameStatus || {};
  els.statAlive.textContent = g.alive ?? "-";
  els.statTotal.textContent = g.total ?? "-";
  els.statOvert.textContent = g.overt ?? "-";
  const pct = Math.round((g.cure_progress || 0) * 100);
  els.statCure.textContent = pct + "%";
  els.cureFill.style.width = pct + "%";
  els.snipeBudget.textContent = g.snipe_remaining ?? "-";
  els.cureBudget.textContent = g.cure_remaining ?? "-";

  if (g.loss) {
    els.banner.className = "loss";
    els.banner.textContent = "OUTBREAK COMPLETE — every reachable node has turned. You lost.";
  } else if (g.stable && (g.overt ?? 0) === 0 && g.alive) {
    els.banner.className = "win";
    els.banner.textContent = `Network stabilized. Final score: ${g.score} / ${g.total} nodes alive.`;
  } else {
    els.banner.className = "";
    els.banner.textContent = "";
  }
  renderSelected();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function addFeedItem(event) {
  const div = document.createElement("div");
  const t = event.type;
  const isAlert = t === "alert_raised" || t === "alert_seen";
  div.className = "feed-item" + (isAlert ? " alert" : "");
  const time = new Date((event.ts || Date.now() / 1000) * 1000).toLocaleTimeString();
  const e = {}; // escaped copies of every field we interpolate
  for (const k of ["node", "from_state", "to", "died_at", "reason", "about", "action", "value"]) {
    e[k] = escapeHtml(event[k]);
  }
  let text = "";
  if (t === "state_change") text = `<b>${e.node}</b> ${e.from_state || "?"} → ${e.to}`;
  else if (t === "cure_run_result") text = event.success
    ? `cure run completed via ${e.node}`
    : `cure run died at <b>${e.died_at}</b> (${e.reason || "drop"})`;
  else if (t === "alert_raised") text = `<b>${e.node}</b> raised alert: ${e.about} (${e.reason})`;
  else if (t === "alert_seen") text = `alert about <b>${e.about}</b> reached ${e.node}`;
  else if (t === "player_action") text = `<b>${e.node}</b> action ${escapeHtml(event.action)}: ${event.correct ? "correct" : "WRONG"}`;
  else if (t === "trust_threshold_set") text = `${e.node} threshold → ${e.value}`;
  else text = `${e.node || ""} ${escapeHtml(t)}`;
  div.innerHTML = `<span class="ts">${time}</span>${text}`;
  els.feed.prepend(div);
  while (els.feed.children.length > 300) els.feed.removeChild(els.feed.lastChild);
}

// -- websocket ---------------------------------------------------------

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => { els.wsIndicator.textContent = "live"; };
  ws.onclose = () => {
    els.wsIndicator.textContent = "disconnected — retrying…";
    setTimeout(connect, 1500);
  };
  ws.onerror = () => ws.close();

  ws.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.type === "snapshot") {
      state.topology = data.topology;
      state.nodeStates = data.node_states;
      state.gameStatus = data.game_status;
      state.positions = layout(data.topology);
      renderGraph();
      renderStatus();
      els.trustSlider.value = data.topology.generated_with ? els.trustSlider.value : els.trustSlider.value;
      for (const ev of data.recent_events) addFeedItem(ev);
      return;
    }
    if (data.type === "event") {
      const ev = data.event;
      if (ev.type === "state_change" && ev.node) {
        state.nodeStates[ev.node] = { state: ev.to, ts: ev.ts };
        updateNodeVisual(ev.node);
      }
      state.gameStatus = data.game_status;
      renderStatus();
      addFeedItem(ev);
    }
  };
}

// -- controls ------------------------------------------------------------

els.trustSlider.addEventListener("input", () => {
  els.trustValue.textContent = Number(els.trustSlider.value).toFixed(2);
});
els.trustSlider.addEventListener("change", async () => {
  const value = Number(els.trustSlider.value);
  await fetch("/control/trust_threshold", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
});

els.btnSnipe.addEventListener("click", async () => {
  if (!state.selected) return;
  await fetch(`/control/${state.selected}/snipe`, { method: "POST" });
});
els.btnCure.addEventListener("click", async () => {
  if (!state.selected) return;
  await fetch(`/control/${state.selected}/cure`, { method: "POST" });
});

connect();
