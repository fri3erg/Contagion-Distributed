// The little people living in the nodes: names, drawn faces and quips.
// Pure presentation -- nothing here knows anything the event stream
// doesn't already say.

const NAMES = ["Ada", "Bruno", "Carla", "Dario", "Elsa", "Fabio", "Gina", "Ivo",
  "Lea", "Mirko", "Nina", "Otto", "Pia", "Rocco", "Sara", "Teo"];

function nodeIndex(id) {
  return Number(String(id).replace(/\D/g, "")) || 0;
}

function nodeName(id) {
  const i = nodeIndex(id);
  return NAMES[i % NAMES.length] + (i >= NAMES.length ? ` ${Math.floor(i / NAMES.length) + 1}` : "");
}

// -- faces ---------------------------------------------------------------

const FACE_NS = "http://www.w3.org/2000/svg";

function faceEl(name, attrs, parent) {
  const el = document.createElementNS(FACE_NS, name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  parent.appendChild(el);
  return el;
}

// One hand-picked look per node so nobody shares a face:
// [hair style, hair colour, accessories...]
const LOOKS = [
  ["bun", "#c98a52", "glasses"],          // Ada
  ["spiky", "#2b2118", "beard"],          // Bruno
  ["bob", "#d8b04a", "bow"],              // Carla
  ["tuft", "#7a4a26", "hat"],             // Dario
  ["curly", "#b5452c", "earring"],        // Elsa
  ["bald", null, "mustache", "monocle"],  // Fabio
  ["mohawk", "#3f9e5c", "freckles"],      // Gina
  ["tuft", "#e2dcc8", "headphones"],      // Ivo
  ["bob", "#5a3a22", "blush"],            // Lea
  ["spiky", "#d8b04a", "eyepatch"],       // Mirko
  ["bun", "#2b2118", "headphones"],       // Nina
  ["bald", null, "antenna", "glasses"],   // Otto
  ["curly", "#2b2118", "glasses"],        // Pia
  ["mohawk", "#b5452c", "mustache"],      // Rocco
  ["bob", "#e2dcc8", "earring", "blush"], // Sara
  ["tuft", "#3b5ea8", "hat", "freckles"], // Teo
];

function drawFace(group, id, x, y, r) {
  const [hair, hairColor, ...items] = LOOKS[nodeIndex(id) % LOOKS.length];
  const g = faceEl("g", { class: "face" }, group);
  const p = (dx, dy) => `${x + dx * r},${y + dy * r}`;
  const hairAttrs = (attrs) => ({ class: "hair fill", style: `fill: ${hairColor}`, ...attrs });
  const has = (name) => items.includes(name);

  // hair (behind the features)
  if (hair === "tuft") faceEl("path", { class: "hair", style: `stroke: ${hairColor}`, d: `M ${p(-0.25, -0.95)} L ${p(-0.15, -1.25)} M ${p(0, -1)} L ${p(0.05, -1.3)} M ${p(0.25, -0.95)} L ${p(0.3, -1.22)}` }, g);
  if (hair === "bob") faceEl("path", hairAttrs({ d: `M ${p(-0.95, -0.2)} Q ${p(-0.9, -1.15)} ${p(0, -1.1)} Q ${p(0.9, -1.15)} ${p(0.95, -0.2)} Q ${p(0.6, -0.75)} ${p(0, -0.75)} Q ${p(-0.6, -0.75)} ${p(-0.95, -0.2)} Z` }), g);
  if (hair === "spiky") faceEl("path", hairAttrs({ d: `M ${p(-0.7, -0.7)} L ${p(-0.5, -1.25)} L ${p(-0.25, -0.85)} L ${p(0, -1.32)} L ${p(0.25, -0.85)} L ${p(0.5, -1.25)} L ${p(0.7, -0.7)} Z` }), g);
  if (hair === "bun") faceEl("circle", hairAttrs({ cx: x, cy: y - 1.15 * r, r: 0.28 * r }), g);
  if (hair === "curly") {
    for (const [dx, dy] of [[-0.62, -0.72], [-0.32, -0.95], [0, -1.02], [0.32, -0.95], [0.62, -0.72]]) {
      faceEl("circle", hairAttrs({ cx: x + dx * r, cy: y + dy * r, r: 0.25 * r }), g);
    }
  }
  if (hair === "mohawk") faceEl("path", hairAttrs({ d: `M ${p(-0.18, -0.88)} L ${p(-0.14, -1.32)} L ${p(-0.02, -1.08)} L ${p(0.05, -1.42)} L ${p(0.12, -1.08)} L ${p(0.22, -1.3)} L ${p(0.2, -0.88)} Z` }), g);

  // accessories behind the eyes and mouth
  if (has("hat")) faceEl("path", { class: "hat fill", d: `M ${p(-0.8, -0.78)} L ${p(0.8, -0.78)} L ${p(0.5, -1.0)} L ${p(0.45, -1.4)} L ${p(-0.45, -1.4)} L ${p(-0.5, -1.0)} Z` }, g);
  if (has("bow")) faceEl("path", { class: "bow fill", d: `M ${p(0.55, -0.85)} L ${p(0.95, -1.1)} L ${p(0.95, -0.6)} Z M ${p(0.55, -0.85)} L ${p(0.2, -1.1)} L ${p(0.2, -0.6)} Z` }, g);
  if (has("beard")) faceEl("path", { class: "beard fill", d: `M ${p(-0.6, 0.25)} Q ${p(0, 1.15)} ${p(0.6, 0.25)} Q ${p(0, 0.65)} ${p(-0.6, 0.25)} Z` }, g);
  if (has("blush")) {
    for (const dx of [-0.55, 0.55]) faceEl("ellipse", { class: "blush", cx: x + dx * r, cy: y + 0.15 * r, rx: 0.16 * r, ry: 0.09 * r }, g);
  }
  if (has("freckles")) {
    for (const [dx, dy] of [[-0.55, 0.08], [-0.45, 0.2], [-0.62, 0.22], [0.55, 0.08], [0.45, 0.2], [0.62, 0.22]]) {
      faceEl("circle", { class: "freckle", cx: x + dx * r, cy: y + dy * r, r: 0.045 * r }, g);
    }
  }
  if (has("headphones")) {
    faceEl("path", { class: "phones", d: `M ${p(-0.92, -0.05)} Q ${p(0, -1.55)} ${p(0.92, -0.05)}` }, g);
    for (const dx of [-1.02, 0.82]) faceEl("rect", { class: "phones-cup", x: x + dx * r, y: y - 0.25 * r, width: 0.22 * r, height: 0.45 * r, rx: 0.06 * r }, g);
  }
  if (has("antenna")) {
    faceEl("path", { class: "line", d: `M ${p(0, -0.98)} L ${p(0.1, -1.4)}` }, g);
    faceEl("circle", { class: "antenna-tip", cx: x + 0.1 * r, cy: y - 1.45 * r, r: 0.11 * r }, g);
  }
  if (has("earring")) faceEl("circle", { class: "earring", cx: x + 0.98 * r, cy: y + 0.28 * r, r: 0.09 * r }, g);
  if (has("mustache")) faceEl("path", { class: "beard fill", d: `M ${p(-0.38, 0.2)} Q ${p(-0.18, 0.06)} ${p(0, 0.18)} Q ${p(0.18, 0.06)} ${p(0.38, 0.2)} Q ${p(0.18, 0.32)} ${p(0, 0.25)} Q ${p(-0.18, 0.32)} ${p(-0.38, 0.2)} Z` }, g);

  const features = faceEl("g", { class: "features" }, g);

  // accessories over the eyes
  if (has("glasses")) {
    faceEl("circle", { class: "line", cx: x - 0.35 * r, cy: y - 0.15 * r, r: 0.22 * r }, g);
    faceEl("circle", { class: "line", cx: x + 0.35 * r, cy: y - 0.15 * r, r: 0.22 * r }, g);
    faceEl("path", { class: "line", d: `M ${p(-0.13, -0.15)} L ${p(0.13, -0.15)}` }, g);
  }
  if (has("monocle")) {
    faceEl("circle", { class: "line", cx: x + 0.35 * r, cy: y - 0.15 * r, r: 0.24 * r }, g);
    faceEl("path", { class: "chain", d: `M ${p(0.55, -0.05)} Q ${p(0.8, 0.4)} ${p(0.7, 0.75)}` }, g);
  }
  if (has("eyepatch")) {
    faceEl("path", { class: "line", d: `M ${p(-0.9, -0.55)} L ${p(0.95, -0.05)}` }, g);
    faceEl("ellipse", { class: "ink", cx: x + 0.35 * r, cy: y - 0.15 * r, rx: 0.2 * r, ry: 0.17 * r }, g);
  }
  return { features, x, y, r, mood: null };
}

// mood: happy | worried | zombie | dead | cured
function setMood(face, mood) {
  if (face.mood === mood) return;
  face.mood = mood;
  const { features: g, x, y, r } = face;
  g.innerHTML = "";
  g.setAttribute("class", `features ${mood}`);
  const p = (dx, dy) => `${x + dx * r},${y + dy * r}`;
  const eye = (dx, rr = 0.1) => faceEl("circle", { class: "ink", cx: x + dx * r, cy: y - 0.15 * r, r: rr * r }, g);
  const line = (d, cls = "line") => faceEl("path", { class: cls, d }, g);

  if (mood === "dead") {
    for (const dx of [-0.35, 0.35]) {
      line(`M ${p(dx - 0.12, -0.27)} L ${p(dx + 0.12, -0.03)} M ${p(dx + 0.12, -0.27)} L ${p(dx - 0.12, -0.03)}`);
    }
    line(`M ${p(-0.3, 0.38)} L ${p(0.3, 0.38)}`);
  } else if (mood === "zombie") {
    eye(-0.35, 0.14);
    eye(0.35, 0.07);
    line(`M ${p(-0.5, -0.4)} L ${p(-0.2, -0.3)} M ${p(0.5, -0.38)} L ${p(0.2, -0.3)}`);
    line(`M ${p(-0.4, 0.4)} L ${p(-0.25, 0.28)} L ${p(-0.1, 0.42)} L ${p(0.05, 0.28)} L ${p(0.2, 0.42)} L ${p(0.4, 0.3)}`);
  } else if (mood === "worried") {
    eye(-0.35);
    eye(0.35);
    line(`M ${p(-0.5, -0.35)} L ${p(-0.2, -0.45)} M ${p(0.5, -0.35)} L ${p(0.2, -0.45)}`);
    faceEl("ellipse", { class: "ink", cx: x, cy: y + 0.38 * r, rx: 0.1 * r, ry: 0.13 * r }, g);
  } else {
    eye(-0.35);
    eye(0.35);
    line(`M ${p(-0.35, 0.2)} Q ${p(0, 0.55)} ${p(0.35, 0.2)}`);
    if (mood === "cured") {
      line(`M ${p(0.45, 0.05)} L ${p(0.75, 0.35)} M ${p(0.75, 0.05)} L ${p(0.45, 0.35)}`, "line bandage");
    }
  }
}

// -- quips -----------------------------------------------------------------

const pick = (list) => list[Math.floor(Math.random() * list.length)];

const QUIPS = {
  // raised after a few bad heartbeats in a row -- which can just be bad
  // luck on the network, so the lines are suspicion, not facts
  unresponsive: [
    "Something's off with {who}.", "{who} is acting weird.", "Keep an eye on {who}.",
    "{who} seems... different today.", "I don't like {who}'s vibe.", "Did {who} just cough?",
    "{who} looks a bit pale.", "Anyone else worried about {who}?", "Not sure I trust {who}.",
    "{who} is being shady.", "Hmm. {who}.",
  ],
  garbled: [
    "{who} just spoke gibberish!", "{who}, you sound... wrong.", "What did {who} just say??",
    "{who}'s packets smell funny.", "That checksum from {who}... yikes.", "{who} is NOT okay.",
    "{who} growled at me!",
  ],
  killed: ["They shot {who}!!", "Noo, {who}!", "{who} is gone...", "RIP {who}.", "Who's next?!",
    "Did {who} deserve that?"],
  turned: ["{who}?! Your FACE!", "RUN! {who} turned!", "I knew it about {who}...", "{who} is one of THEM!",
    "Somebody shoot {who}!", "{who}, nooo!", "Get away from me, {who}!", "{who} wants my brains!"],
  wrongShot: ["{who} was clean! Murderers!", "Why {who}?! They were fine!", "You shot {who} for nothing!",
    "{who} had a family!", "Are we the baddies?", "Great. Now we're fewer."],
  goodShot: ["Good shot.", "Rest easy, {who}.", "Had to be done.", "Nice aim!", "One less zombie.",
    "Thanks for that."],
  cured: ["{who}, you're back!", "Welcome back, {who}.", "The cure works!", "Look at {who}, good as new!",
    "Science wins!"],
  courierArrived: ["Got {who}'s cure sample!", "Package from {who} arrived!", "Sample secured.",
    "Research thanks you, {who}!", "Delivery complete."],
  courierLost: ["My courier vanished near {who}...", "{who} lost my sample!", "Where's my courier, {who}?",
    "Courier never made it past {who}.", "Suspicious, {who}..."],
  idle: [
    "Quiet day.", "Anyone else hear that?", "Heartbeat steady. Mostly.", "Pass the packets, please.",
    "Did someone say cure?", "Who keeps sending all these alerts?", "I trust nobody.",
    "Lovely weather for gossip.", "I'm feeling a bit off today.", "Stay close, everyone.",
    "Is it me or is it hot in here?", "My TTL is getting low.", "Anyone up for a sync?",
    "I miss the old days.", "Washed my hands. Twice.", "Masks on, folks.",
    "Who ate my snacks?", "Feeling trusted today.", "Keep calm and carry on.",
  ],
  zombieIdle: ["Braaains...", "Hhhrrnn.", "...packets...", "Grrr.", "Truuust meee...", "Sync... with... me...",
    "Mmmh, neighbors."],
};

function quipText(kind, aboutId) {
  return pick(QUIPS[kind]).replace("{who}", aboutId ? nodeName(aboutId) : "");
}
