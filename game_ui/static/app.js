/* ============================================================ GlobeRunners UI */
"use strict";

/* ------------------------------------------------------------------ helpers */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function toast(msg, ms = 2600) {
  let t = $("#toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), ms);
}

// client-side guard: card cost vs available mana (mana zone - mana spent).
// The engine would reject the play ("not enough mana") but that rejection is easy
// to miss (and, before the engine fix, it was even archived as a successful play);
// we flag it well before sending the action.
function checkMana(id) {
  const st = game.state;
  const me = (st && st.players) ? st.players[game.me] : null;
  if (!me) return true;
  const cost = cardCost(id);
  const avail = (me.mana || []).length - (me.mana_spend || 0);
  if (cost > avail) {
    toast(`Not enough mana: ${cardName(id)} costs ${cost}, only ${avail} left`, 5000);
    return false;
  }
  return true;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error((data && (data.detail || data.message)) || `HTTP ${res.status}`);
  return data;
}

/* ------------------------------------------------------------------ card pool */
const CARDPOOL = {};            // card_id -> row
let cardpoolLoaded = false;

async function loadCardpool() {
  if (cardpoolLoaded) return;
  const rows = await api("/cardpool");
  for (const r of rows) CARDPOOL[r.card_id] = r;
  cardpoolLoaded = true;
}

// card data: main faction cards (from /cardpool) or support faction cards (from /support_factions)
function cardInfo(id) {
  return (CARDPOOL && CARDPOOL[id]) || SUPPORT[id] || null;
}

// readable title for a card (main or support)
function cardTitle(id) {
  const c = cardInfo(id);
  if (!c) return id;
  if (c.card_name) return `${c.card_name} (\u2699 ${c.support_faction_name}) — ${c.description}`;   // support card
  return `${c.name} — ${c.faction}`;                                                               // main card
}

function cardImg(id) {
  // art is served by /art/<...>.png; if the file doesn't exist, the onerror handler falls back to the placeholder.
  // support cards use their `card_path` (e.g. Mag_black_hole.png); main cards use <card_id>.png
  const s = SUPPORT[id];
  if (s) return `/art/${encodeURIComponent(s.card_path)}`;
  return `/art/${encodeURIComponent(id)}.png`;
}

// card cost: main cards use `mana`; support cards (engine_version 12+) use `mana_cost`
function cardCost(id) {
  const main = CARDPOOL[id];
  if (main && main.mana != null) return main.mana;
  const sup = SUPPORT[id];
  if (sup && sup.mana_cost != null) return sup.mana_cost;
  return 0;
}
function cardName(id) {
  if (CARDPOOL[id]) return CARDPOOL[id].name;
  if (SUPPORT[id]) return SUPPORT[id].card_name;
  return id;
}

/* ------------------------- Engineers support faction (engine_version 12) ------------------------- */
// 4 drop cards (played in MOVE mode onto a chosen earth cell -> instant token) + 1 dwelling card.
// kind -> board token image (served at /assets/). UI-level constants; the engine is authoritative.
const ENGINEER_DROPS = {
  boost:      { img: "/assets/supfac_eng_boost.png",      label: "boost — +2 advancing" },
  trampoline: { img: "/assets/supfac_eng_trampoline.png", label: "trampoline — +2 jump" },
  gluetrap:   { img: "/assets/supfac_eng_slowingtrap.png",label: "gluetrap — −1 knockback" },
  landmine:   { img: "/assets/supfac_eng_mine.png",       label: "landmine — blocks the arriving player for the turn" },
};
const ENGINEER_DWELLING = "refinery";   // tap once/turn -> draw 1
const isEngineerDrop    = (id) => !!ENGINEER_DROPS[id];
const isEngineerDwelling= (id) => id === ENGINEER_DWELLING;
const isSupportPlay     = (id) => isEngineerDrop(id) || isEngineerDwelling(id);

const FACTIONS = [
  { key: "Dwa", name: "Dwarves", logo: "/assets/logo_dwarves.png" },
  { key: "Dem", name: "Demons",  logo: "/assets/logo_demons.png" },
  { key: "Twi", name: "Twigs",   logo: "/assets/logo_twigs.png" },
  { key: "Mia", name: "Miaous",  logo: "/assets/logo_miaous.png" },
  { key: "Orc", name: "Orcs",    logo: "/assets/logo_orcs.png" },
  { key: "Mum", name: "Mummies", logo: "/assets/logo_mummies.png" },
];
// faction full name → short key ("Dwarves" → "Dwa")
function getFactionKey(factionName) {
  const f = FACTIONS.find(f => f.name === factionName);
  return f ? f.key : null;
}
// placeholder image for the dwelling card (served from /cards_ex/)
function dwellingPlaceholderSrc(factionName) {
  const key = getFactionKey(factionName);
  return key ? `/cards_ex/placeholder_${key}.png` : '/placeholder.svg';
}

/* ------------------------- support factions (engineers / mages / doctors) ------------------------- */
const SUPPORT = {};              // support card_name -> row (from /support_factions)
let supportLoaded = false;

async function loadSupportCards() {
  if (supportLoaded) return;
  const rows = await api("/support_factions");
  for (const r of rows) SUPPORT[r.card_name] = r;
  supportLoaded = true;
}

const SUPPORT_FACS = [
  { key: "engineers", name: "Engineers", banner: "/assets/supfac_eng_banner.png" },
  { key: "mages",     name: "Mages",     banner: "/assets/supfac_mag_banner.png" },
  { key: "doctors",   name: "Doctors",   banner: "/assets/supfac_doc_banner.png" },
];

function buildSupportDeck(key) {
  // 2 copies of each of the 5 cards of the support faction (10 total)
  const cards = Object.values(SUPPORT).filter((r) => r.support_faction_name === key);
  const deck = [];
  for (const c of cards) deck.push(c.card_name, c.card_name);
  return deck;
}

/* ------------------------------------------------------------------ deck building */
const MAIN_DECK_SIZE = 20;      // main faction starter deck (20 cards)
const SUPPORT_DECK_SIZE = 10;   // support faction deck (2 x 5 unique cards)
const DECK_SIZE = MAIN_DECK_SIZE + SUPPORT_DECK_SIZE;   // 30 total: 20 main + 10 support, mixed

function buildStarterDeck(factionKey, seedStr) {
  const pool = Object.values(CARDPOOL).filter((c) => c.faction && c.faction.startsWith(factionKey));
  if (!pool.length) return [];
  // deterministic for the same name: the deck is identical if you reload the page
  let h = 2166136261 >>> 0;
  for (const ch of String(seedStr)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619) >>> 0; }
  const rand = () => { h = (Math.imul(h, 1664525) + 1013904223) >>> 0; return h / 4294967296; };

  // weighting: "rare" cards are less frequent
  const weighted = [];
  for (const c of pool) {
    const w = c.rare ? 1 : 3;
    for (let i = 0; i < w; i++) weighted.push(c);
  }
  // weighted draw without replacement
  const deck = [];
  const bag = [...weighted];
  while (deck.length < MAIN_DECK_SIZE && bag.length) {
    const idx = Math.floor(rand() * bag.length);
    const card = bag.splice(idx, 1)[0];
    if (!deck.includes(card.card_id)) deck.push(card.card_id);
  }
  return deck;
}

function parseCsv(text) {
  // accepts: a card_id column (with or without header), comma/semicolon/tab separated
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const ids = [];
  for (const line of lines) {
    const parts = line.split(/[,;\t]/).map((p) => p.trim().replace(/^"|"$/g, "")).filter(Boolean);
    // if the line contains multiple columns, find the one that looks like a card_id
    let id = null;
    for (const p of parts) {
      if (/^[A-Za-z]{3}\d+_[0-9a-f]{4,}$/.test(p)) { id = p; break; }
    }
    // single-column file: only accept a real card_id (ignore the "card_id" header and junk lines)
    if (!id && parts.length === 1 && /^[A-Za-z]{3}\d+_[0-9a-f]{4,}$/i.test(parts[0])) id = parts[0];
    if (id) ids.push(id);
  }
  return ids;
}

/* ------------------------------------------------------------------ SETUP page */
const setup = {
  deck: [],            // list of main card_ids (20)
  faction: null,       // starter faction key
  support: null,       // support faction key ('engineers' | 'mages' | 'doctors')
  supportDeck: [],     // list of support card_names (10)
  name: "",
  mode: "create",      // 'create' | 'ai' | 'join'
  gameId: null,        // set after create (or typed for join)
};

function renderFactions() {
  const grid = $("#faction-grid");
  grid.innerHTML = "";
  for (const f of FACTIONS) {
    const btn = document.createElement("button");
    btn.className = "faction-btn" + (setup.faction === f.key ? " selected" : "");
    btn.innerHTML = `<img src="${f.logo}" alt=""> <span>${f.name}</span>`;
    btn.onclick = () => {
      setup.faction = f.key;
      setup.deck = buildStarterDeck(f.key, setup.name || f.key);
      renderFactions();
      renderDeckPreview();
      updateLaunchBtn();
    };
    grid.appendChild(btn);
  }
}

function renderDeckPreview() {
  const box = $("#deck-preview");
  const title = $("#deck-preview-title");
  if (!setup.deck.length) { box.classList.add("hidden"); title.classList.add("hidden"); return; }
  box.classList.remove("hidden"); title.classList.remove("hidden");
  $("#deck-count").textContent = setup.deck.length;
  box.innerHTML = "";
  for (const id of setup.deck) {
    const el = document.createElement("div");
    el.className = "card";
    el.style.cursor = "default";
    el.title = cardTitle(id);
    el.innerHTML = `<img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;
    box.appendChild(el);
  }
}

function renderSupportFactions() {
  const grid = $("#support-grid");
  if (!grid) return;
  grid.innerHTML = "";
  for (const f of SUPPORT_FACS) {
    const btn = document.createElement("button");
    btn.className = "supfac-btn" + (setup.support === f.key ? " selected" : "");
    btn.innerHTML = `<img src="${f.banner}" alt="${f.name}"> <span>${f.name}</span>`;
    btn.onclick = () => {
      setup.support = f.key;
      setup.supportDeck = buildSupportDeck(f.key);
      renderSupportFactions();
      renderSupportPreview();
      updateLaunchBtn();
    };
    grid.appendChild(btn);
  }
}

function renderSupportPreview() {
  const box = $("#support-preview");
  const title = $("#support-preview-title");
  if (!box || !setup.supportDeck.length) {
    if (box) box.classList.add("hidden");
    if (title) title.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden"); title.classList.remove("hidden");
  $("#support-count").textContent = setup.supportDeck.length;
  box.innerHTML = "";
  for (const id of setup.supportDeck) {
    const el = document.createElement("div");
    el.className = "card";
    el.style.cursor = "default";
    el.title = cardTitle(id);
    el.innerHTML = `<img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;
    box.appendChild(el);
  }
}

function updateLaunchBtn() {
  const okName = setup.name.trim().length >= 2;
  const okMain = setup.deck.length === MAIN_DECK_SIZE;
  const okSupport = setup.supportDeck.length === SUPPORT_DECK_SIZE;
  const okGame = setup.mode === "join" ? $("#join-game-id").value.trim().length > 0 : true;
  $("#btn-launch").disabled = !(okName && okMain && okSupport && okGame);
}

function initSetup() {
  // tabs deck source
  $("#tab-starter").onclick = () => switchTab("#tab-starter", "#starter-panel");
  $("#tab-csv").onclick = () => switchTab("#tab-csv", "#csv-panel");
  function switchTab(tabSel, panelSel) {
    $$(".deck-source-tabs .tab").forEach((t) => t.classList.remove("active"));
    $(tabSel).classList.add("active");
    $("#starter-panel").classList.toggle("hidden", tabSel !== "#tab-starter");
    $("#csv-panel").classList.toggle("hidden", tabSel !== "#tab-csv");
  }

  // mode tabs (create / AI / join)
  const MODE_TABS = {
    "#tab-create": { mode: "create", panel: "#create-panel" },
    "#tab-ai": { mode: "ai", panel: "#ai-panel" },
    "#tab-join": { mode: "join", panel: "#join-panel" },
  };
  function switchModeTab(tabSel) {
    $$(".mode-tabs .tab").forEach((t) => t.classList.remove("active"));
    $(tabSel).classList.add("active");
    for (const [tab, { panel }] of Object.entries(MODE_TABS)) $(panel).classList.toggle("hidden", tab !== tabSel);
    setup.mode = MODE_TABS[tabSel].mode;
    updateLaunchBtn();
  }
  $("#tab-create").onclick = () => switchModeTab("#tab-create");
  $("#tab-ai").onclick = () => switchModeTab("#tab-ai");
  $("#tab-join").onclick = () => switchModeTab("#tab-join");

  // csv upload
  $("#csv-input").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      await loadCardpool();
      const text = await file.text();
      const ids = parseCsv(text);
      const unknown = ids.filter((id) => !CARDPOOL[id]);
      const picked = ids.slice(0, MAIN_DECK_SIZE);
      const dupes = picked.filter((id, i, arr) => arr.indexOf(id) !== i);
      $("#csv-filename").textContent = `${file.name} — ${ids.length} card(s)` + (unknown.length ? `, ${unknown.length} unknown: ${unknown.slice(0, 3).join(", ")}` : "") + (dupes.length ? `, DUPLICATES: ${[...new Set(dupes)].slice(0, 3).join(", ")}` : "");
      if (!ids.length) { toast("No card found in this CSV"); return; }
      if (dupes.length) { toast(`CSV contains duplicate card(s) — a card can only appear once: ${[...new Set(dupes)].join(", ")}`); return; }
      setup.deck = picked;
      renderDeckPreview();
      updateLaunchBtn();
    } catch (err) {
      toast(`Erreur de lecture du CSV : ${err.message}`);
    }
  };

  // name input
  $("#player-name").oninput = () => {
    setup.name = $("#player-name").value;
    if (setup.faction && !$("#csv-input").files.length) {
      setup.deck = buildStarterDeck(setup.faction, setup.name || setup.faction);
      renderDeckPreview();
    }
    updateLaunchBtn();
  };
  $("#join-game-id").oninput = updateLaunchBtn;

  // copy game id
  $("#copy-gid").onclick = async () => {
    try { await navigator.clipboard.writeText($("#created-game-id").textContent); toast("ID copied!"); }
    catch { toast("Impossible de copier automatiquement"); }
  };

  // launch
  $("#btn-launch").onclick = launch;

  renderFactions();
  renderSupportFactions();
}

// Fisher-Yates: the 20 main cards + 10 support cards are mixed into one 30-card deck
function shuffleDeck(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

async function launch() {
  const errEl = $("#setup-error");
  errEl.textContent = "";
  const name = setup.name.trim();
  if (name.length < 2) { errEl.textContent = "Pick a nickname (2 characters min)."; return; }
  if (setup.deck.length !== MAIN_DECK_SIZE) { errEl.textContent = `The main deck must contain exactly ${MAIN_DECK_SIZE} cards.`; return; }
  if (setup.supportDeck.length !== SUPPORT_DECK_SIZE) { errEl.textContent = "Pick a support faction (its 10 cards are mixed into your main deck)."; return; }
  // the 20 main cards + 10 support cards are mixed (shuffled) into one 30-card deck
  const deck = shuffleDeck([...setup.deck, ...setup.supportDeck]);

  const btn = $("#btn-launch");
  btn.disabled = true; btn.textContent = "Lancement…";
  try {
    await loadCardpool();
    if (setup.mode === "ai") {
      // game vs AI: the robot joins immediately, we go straight into the game
      const res = await api("/create_game_ai", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, deck }),
      });
      setup.gameId = res.game_id;
      toast(`Game started against ${res.opponent}!`);
      enterGame();
    } else if (setup.mode === "create") {
      const res = await api("/create_game", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, deck }),
      });
      setup.gameId = res.game_id;
      $("#created-game-id").textContent = res.game_id;
      $("#created-game-box").classList.remove("hidden");
      toast(`Game created! ID: ${res.game_id}`);
      // wait for the opponent to join (state polling)
      startWaitingForOpponent();
    } else {
      const gid = $("#join-game-id").value.trim();
      if (!gid) { errEl.textContent = "Enter the game ID."; return; }
      setup.gameId = gid;
      try {
        await api(`/join_game/${encodeURIComponent(gid)}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, deck }),
        });
      } catch (e) {
        // 409: the player is already in the game -> simply resume (page reload)
        let st = null;
        try { st = await api(`/game/${encodeURIComponent(gid)}`); } catch { throw e; }
        if (!st.players || !Object.keys(st.players).includes(name)) throw e;
      }
      enterGame();
    }
  } catch (err) {
    errEl.textContent = `Erreur : ${err.message}`;
    btn.disabled = false; btn.textContent = "Lancer";
  }
}

/* p1 waits for p2 to join */
let waitTimer = null;
function startWaitingForOpponent() {
  $("#btn-launch").textContent = "Waiting for the opponent…";
  const poll = async () => {
    try {
      const st = await api(`/game/${encodeURIComponent(setup.gameId)}`);
      if (st.players && Object.keys(st.players).length >= 2) {
        stopWaiting();
        enterGame();
        return;
      }
    } catch { /* not yet */ }
    waitTimer = setTimeout(poll, 1500);
  };
  poll();
}
function stopWaiting() {
  if (waitTimer) clearTimeout(waitTimer);
  waitTimer = null;
  const btn = $("#btn-launch");
  btn.disabled = false; btn.textContent = "Lancer";
}

/* ------------------------------------------------------------------ GAME page */
const game = {
  id: null,
  me: null,              // my player name
  ws: null,
  state: null,           // last personalized state received
  selected: new Set(),   // card_ids selected in the hand
  dragCardId: null,      // card currently being dragged (from the hand)
  pollTimer: null,
  lastWinnerShown: false,
  logTurnsRendered: 0,   // how many turns of state.log are already in the DOM (incremental render)
};

/* ---------------- cell-selection mode (engineer drop placement, engine_version 12) ---------------- */
// When an engineer drop card is being played, the player must choose the earth cell the
// token lands on. The 24 cells are not DOM elements (the Earth is one image + positioned
// markers), so we overlay 24 invisible circular targets at the POS24 points, shown only
// while in this mode. Clicking one places the drop on that cell.
const cellSelect = { active: false, cardId: null };

function enterCellSelect(cardId) {
  if (!isEngineerDrop(cardId)) return;
  cellSelect.active = true;
  cellSelect.cardId = cardId;
  document.body.classList.add("cell-selecting");   // CSS shows the 24 cell targets
  const info = ENGINEER_DROPS[cardId];
  toast(`Choose a cell on the Earth for the ${cardId} (${info ? info.label : ""})`, 4200);
}

function exitCellSelect() {
  if (!cellSelect.active) return;
  cellSelect.active = false;
  cellSelect.cardId = null;
  document.body.classList.remove("cell-selecting");
}

// lightweight reset (used on state change, without a re-render)
function _resetCellSelect() {
  if (cellSelect.active) {
    cellSelect.active = false;
    cellSelect.cardId = null;
    document.body.classList.remove("cell-selecting");
  }
}

function confirmCell(cellIndex) {
  const id = cellSelect.cardId;
  if (!id) return;
  exitCellSelect();
  const st = game.state;
  if (playedCount(st, game.me) >= N_STOPOVERS) { toast(orderHint()); return; }
  if (!checkMana(id)) return;
  // the drop occupies the next stopover slot (move mode) + carries its target cell
  sendAction([id], `stopover_${nextSlotCol(st, game.me)}`, "move", [], cellIndex);
  game.selected = new Set();
}

function enterGame() {
  $("#view-setup").classList.add("hidden");
  $("#view-game").classList.remove("hidden");
  game.id = setup.gameId;
  game.me = setup.name.trim();
  game.lastWinnerShown = false;
  game.logTurnsRendered = 0;
  $("#log-body").innerHTML = "";
  connectWs();
  startPolling();   // the WS only receives a state when WE send: we also poll to see the opponent
}

/* ---------------- websocket ---------------- */
function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/${encodeURIComponent(game.id)}/${encodeURIComponent(game.me)}`;
}

let pendingResolvers = [];   // one resolver per WS message waiting for a reply

function connectWs() {
  if (game.ws && game.ws.readyState <= WebSocket.OPEN) return;
  setConn(false);
  const ws = new WebSocket(wsUrl());
  game.ws = ws;

  ws.onopen = () => setConn(true);
  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    // a rejection has the shape {success: false, message}; a game state has no 'success' key
    if (data && data.success === false) showServerMsg(data.message || "Action refused");
    else applyState(data);
    const resolver = pendingResolvers.shift();
    if (resolver) resolver(data);
  };
  ws.onclose = () => setConn(false);
}

function sendAction(cards, to, mode, pendings = [], cell = null) {
  /* sends an action and resolves with the server's reply (state or rejection).
     Sends are serialized: one WS message at a time.
     `cell` (0-23): target earth cell for an engineer drop placement (engine_version 12). */
  return new Promise((resolve) => {
    const doSend = () => {
      if (!game.ws || game.ws.readyState !== WebSocket.OPEN) {
        toast("Connection lost — retrying…");
        connectWs();
        setTimeout(doSend, 1500);
        return;
      }
      pendingResolvers.push(resolve);
      const msg = { cards: cards || [], to: to || "", mode: mode || "", pendings };
      if (cell != null) msg.cell = cell;
      game.ws.send(JSON.stringify(msg));
    };
    doSend();
  });
}

function setConn(ok) {
  const el = $("#conn-state");
  el.textContent = ok ? "● connected" : "○ disconnected";
  el.className = ok ? "conn-ok" : "conn-bad";
  if (!ok && game.id) setTimeout(connectWs, 2000);   // auto-reconnect
}

/* ---------------- state polling (see the opponent's actions) ---------------- */
function startPolling() {
  stopPolling();
  const poll = async () => {
    if (!game.id) return;
    try {
      const st = await api(`/api/state/${encodeURIComponent(game.id)}/${encodeURIComponent(game.me)}`);
      if (st && st.players) applyState(st, true);
    } catch { /* server temporarily unavailable */ }
    game.pollTimer = setTimeout(poll, 2500);
  };
  poll();
}
function stopPolling() { if (game.pollTimer) clearTimeout(game.pollTimer); game.pollTimer = null; }

/* ---------------- phase detection ---------------- */
const START_MANA_N = 3;   // cards to put in mana at initialization

function detectPhase(st) {
  const s = st.state || "";
  if (s === "game over") return "over";
  if (s.includes("waiting for both players to put")) return "init-mana";
  if (s.includes("waiting for both players to mana or pass")) return "mana-pass";

  // discard selection (engine_version 13): the trip chain paused so the discarding
  // player can choose which cards to discard — "turn N - waiting for NAME to discard K card(s)"
  const md = s.match(/turn (\d+) - waiting for (.+?) to discard (\d+) card\(s\)/);
  if (md) return { kind: "discard", turn: +md[1], actor: md[2], n: +md[3] };

  // play phase: "turn N - waiting for first/second player (NAME) to play"
  const m = s.match(/turn (\d+) - waiting for (first|second) player \((.+?)\) to play/);
  if (m) {
    return { kind: "play", turn: +m[1], actor: m[3] };
  }
  // between the two actions of a turn, the state may be transient
  const t = s.match(/turn (\d+)/);
  if (t) return { kind: "play-waiting", turn: +t[1] };
  return "unknown";
}

function myTurn(st) {
  const ph = detectPhase(st);
  if (!ph || typeof ph !== "object") return false;
  return ph.kind === "play" && ph.actor === game.me;
}

/* ---------------- state application & rendering ---------------- */

function applyState(st, fromPolling) {
  const wasOver = game.state && detectPhase(game.state) === "over";
  _resetCellSelect();   // any new state ends cell-selection mode (a placement / opponent move happened)
  game.state = st;
  renderAll();
  if (st && st.message && typeof st.message === "object") {
    const txt = st.message.message || "";
    const isFailure = !st.message.success || /tried|must select|not enough/i.test(txt);
    showServerMsg(isFailure ? txt : "");   // empty -> cleared after 3 s
  }

  // end of game
  const over = detectPhase(st) === "over";
  if (over && !game.lastWinnerShown) {
    game.lastWinnerShown = true;
    stopPolling();
    showEndgame(st);
  } else if (!over && wasOver) {
    // new game on the same page? restart polling
    startPolling();   // new game on the same page? restart polling
  }
}

function renderAll() {
  const st = game.state;
  if (!st || !st.players) return;
  const me = st.players[game.me];
  const oppoName = Object.keys(st.players).find((n) => n !== game.me);
  const oppo = oppoName ? st.players[oppoName] : null;

  // topbar
  $("#turn-number").textContent = `Tour ${st.turn || "?"}`;
  $("#temp-badge").textContent = st.temperature != null ? `🌡️ ${st.temperature}` : "🌡️ ?";
  const dn = st.day_night === "night" ? "🌙 Nuit" : "☀️ Jour";
  $("#daynight-badge").textContent = dn;

  // phase label + buttons
  const ph = detectPhase(st);
  let phaseText = "", canAct = false, hint = "";
  if (ph === "init-mana") {
    const n = (me.mana || []).length;
    phaseText = `Initialization: put ${START_MANA_N} cards in mana (${n}/${START_MANA_N})`;
    canAct = true;
    hint = "Drag cards from your hand to the ⚡ Mana zone, then click “Play the card”.";
  } else if (ph === "mana-pass") {
    phaseText = "Mana phase: put 1 card in mana or pass";
    canAct = true;
    hint = "Drag ONE card to ⚡ Mana and click “Play the card”, or click “Pass”.";
  } else if (ph && ph.kind === "play") {
    phaseText = `Turn ${ph.turn} — ${ph.actor}'s turn`;
    canAct = myTurn(st);
    hint = canAct ? "Put your card on the next stopover cell (in order 1→5), or “Pass”." : "Waiting for the opponent…";
  } else if (ph && ph.kind === "discard") {
    // discard selection (engine_version 13): the trip chain is paused
    if (ph.actor === game.me) {
      phaseText = `Discard selection — choose ${ph.n} card(s) to discard`;
      canAct = true;
      hint = `Click ${ph.n} card(s) in your hand, then press the red DISCARD button.`;
    } else {
      phaseText = `Waiting for ${ph.actor} to discard ${ph.n} card(s)…`;
      hint = "";
    }
  } else if (ph === "over") {
    phaseText = "Game over";
  } else {
    phaseText = st.state || "";
  }
  $("#phase-label").textContent = phaseText;
  $("#action-hint").textContent = hint;

  const btnPlay = $("#btn-play"), btnDefend = $("#btn-defend"), btnPass = $("#btn-pass"), btnTap = $("#btn-tap"), btnDiscard = $("#btn-discard");
  const selCard = (game.selected.size === 1) ? [...game.selected][0] : null;
  const selIsDrop = selCard && isEngineerDrop(selCard);
  const selIsDwelling = selCard && isEngineerDwelling(selCard);
  const canDefend = canAct && ph && ph.kind === "play" && !!selCard && !isSupportPlay(selCard);
  // dwelling tap: I have a dwelling card, it's untapped, and it's my play turn
  const canTap = canAct && ph && ph.kind === "play" && !!me.dwelling && !me.dwelling_tapped;
  if (ph === "init-mana") {
    btnPlay.textContent = "Poser en mana";
    btnPlay.disabled = !canAct || game.selected.size === 0;
    btnPass.classList.add("hidden");
    btnDefend.classList.add("hidden");
    btnTap.classList.add("hidden");
    btnDiscard.classList.add("hidden");
  } else if (ph && ph.kind === "discard") {
    // discard selection (engine_version 13): the red DISCARD button is the only action
    btnPlay.classList.add("hidden");
    btnDefend.classList.add("hidden");
    btnPass.classList.add("hidden");
    btnTap.classList.add("hidden");
    if (ph.actor === game.me) {
      btnDiscard.classList.remove("hidden");
      btnDiscard.disabled = game.selected.size !== ph.n;
    } else {
      btnDiscard.classList.add("hidden");
    }
  } else if (ph === "mana-pass" || ph.kind === "play") {
    if (selIsDrop) btnPlay.textContent = "📍 Place drop (pick a cell)";
    else if (selIsDwelling) btnPlay.textContent = "🏠 Place dwelling";
    else btnPlay.textContent = "Play the card";
    btnPlay.disabled = !canAct || game.selected.size !== 1;
    btnDefend.classList.remove("hidden");
    btnDefend.disabled = !canDefend;
    btnPass.classList.remove("hidden");
    btnPass.disabled = !canAct;
    btnTap.classList.toggle("hidden", !canTap);
    if (btnTap) btnTap.disabled = !canTap;
  } else {
    btnPlay.disabled = true;
    btnDefend.disabled = true;
    btnPass.disabled = true;
    btnTap.classList.add("hidden");
    btnDiscard.classList.add("hidden");
  }

  const interactive = canAct && (ph === "init-mana" || ph === "mana-pass"
    || (ph && ph.kind === "play") || (ph && ph.kind === "discard" && ph.actor === game.me));

  if (oppo) renderOppZone(oppo);
  renderMyZone(me, interactive);
  renderSideRows(me, oppo);
  renderEnv(st);
  renderBoard(st, me, oppoName);
  renderLog(st);
  highlightValidCells();
}

/* public counters: the personalized state masks the ids but exposes *_count */
const publicCount = (p, key) => (p && p[key] != null) ? p[key] : (p && (p[key.replace("_count", "")] || []).length);

/* ---------------- opponent banner: [name + counters] [mana on the left] [hand face-down] ---------------- */
function renderOppZone(oppo) {
  $("#oppo-name").textContent = oppo.name;
  const handN = publicCount(oppo, "hand_count");
  const manaN = publicCount(oppo, "mana_count");
  const deckN = publicCount(oppo, "deck_count");
  const manaSpent = oppo.mana_spend || 0;
  const manaAvail = Math.max(manaN - manaSpent, 0);
  $("#oppo-counts").innerHTML =
    (oppo.landmine_blocked ? `<span class="landmine-badge" title="Blocked by a landmine — move cards are canceled until the end of the turn">💣 blocked</span>` : "") +
    `<span>📍 case ${oppo.current_position ?? 0}</span>` +
    `<span>🂠 main : ${handN}</span>` +
    `<span>⚡ mana : ${manaAvail}/${manaN}</span>` +
    `<span>📦 deck : ${deckN}</span>` +
    `<span>🗑️ ${(oppo.discard || []).length}</span>`;
  // opponent hand: N card backs (the cards themselves stay hidden)
  const hand = $("#oppo-hand");
  hand.innerHTML = "";
  for (let i = 0; i < handN; i++) {
    const c = document.createElement("div");
    c.className = "card back";
    c.dataset.hoverId = "__back__";
    c.title = `Opponent's card (hidden) — ${handN} in hand`;
    hand.appendChild(c);
  }
  // opponent mana: card backs in the drop (spent = turned 90°) + available/total counter
  const mc = $("#oppo-mana-cards");
  mc.innerHTML = "";
  for (let i = 0; i < manaN; i++) {
    const c = document.createElement("div");
    c.className = "card back" + (i < manaSpent ? " tapped" : "");
    c.dataset.hoverId = "__back__";
    c.title = i < manaSpent ? `Mana spent this turn (${manaSpent})` : `Mana available (${manaAvail})`;
    mc.appendChild(c);
  }
  $("#oppo-mana-count").textContent = `${manaAvail}/${manaN}`;
}

/* ---------------- player banner: [name] [mana on the left] [hand] [actions] ---------------- */
function renderMyZone(me, interactive) {
  $("#my-name").textContent = me.name;
  const manaAvail = (me.mana || []).length - (me.mana_spend || 0);
  $("#mana-info").innerHTML =
    (me.landmine_blocked ? `<span class="landmine-badge" title="Blocked by a landmine — move cards are canceled until the end of the turn">💣 blocked</span>` : "") +
    `<span>⚡ mana ${manaAvail}/${(me.mana || []).length}</span>` +
    `<span>🂠 main ${(me.hand || []).length}</span>`;

  // hand (to the right of the mana zone)
  const hand = $("#my-hand");
  hand.innerHTML = "";
  (me.hand || []).forEach((id) => hand.appendChild(makeHandCard(id, interactive, me)));

  // mana: card backs in the drop (spent = turned 90° to the right) + available/total counter
  const manaCards = $("#my-mana-cards");
  manaCards.innerHTML = "";
  const nMana = (me.mana || []).length;
  const nSpent = me.mana_spend || 0;
  for (let i = 0; i < nMana; i++) {
    const el = document.createElement("div");
    el.className = "card back" + (i < nSpent ? " tapped" : "");
    el.dataset.hoverId = "__back__";
    el.title = (i < nSpent ? "Mana spent this turn" : "Mana available") + ` — ${me.name}`;
    manaCards.appendChild(el);
  }
  $("#my-mana-count").textContent = `${Math.max(nMana - nSpent, 0)}/${nMana}`;

  // mana / discard drop zones
  const ph = detectPhase(game.state);
  const canMana = interactive && (ph === "init-mana" || ph === "mana-pass");
  const dzMana = $("#dz-mana"), dzDisc = $("#dz-discard");
  for (const dz of [dzMana, dzDisc]) {
    dz.ondragover = (e) => {
      if (!game.dragCardId) return;
      e.preventDefault();
      const ok = dz === dzMana ? canMana : true;   // discarding is always allowed
      dz.classList.add(ok ? "over" : "invalid");
    };
    dz.ondragleave = () => dz.classList.remove("over", "invalid");
    dz.ondrop = (e) => {
      e.preventDefault();
      dz.classList.remove("over", "invalid");
      const id = game.dragCardId;
      game.dragCardId = null;
      if (!id || !me.hand.includes(id)) return;
      if (dz === dzMana) {
        if (!canMana) { toast("Cannot put mana in right now"); return; }
        sendAction([id], "mana", "");
      } else {
        sendAction([id], "discard_pile", "");
      }
    };
  }

  // action buttons
  $("#btn-play").onclick = async () => {
    const ph2 = detectPhase(game.state);
    if (ph2 === "init-mana") {
      // the engine only accepts 1 or 3 cards per message: we send them one at a time
      for (const id of [...game.selected]) {
        const resp = await sendAction([id], "mana", "");
        if (!resp || resp.success === false) break;   // rejection -> stop, state is resynchronized
      }
      game.selected.clear();
    } else if (ph2 === "mana-pass") {
      if (game.selected.size !== 1) return;
      sendAction([...game.selected], "mana", "");
    } else if (ph2 && ph2.kind === "play") {
      const id = [...game.selected][0];
      if (!id) return;
      // engineer drop: pick a target cell on the Earth, then place it
      if (isEngineerDrop(id)) { enterCellSelect(id); return; }
      // engineer dwelling (refinery): place it in the dwelling zone
      if (isEngineerDwelling(id)) {
        const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
        if (meNow.dwelling) { toast("You already have a dwelling — the slot is full"); return; }
        if (playedCount(game.state, game.me) >= N_STOPOVERS) { toast(orderHint()); return; }
        if (!checkMana(id)) return;
        sendAction([id], "dwelling", "");
        game.selected = new Set();
        return;
      }
      if (playedCount(game.state, game.me) >= N_STOPOVERS) { toast(orderHint()); return; }
      if (!checkMana(id)) return;
      // ordering rule: the card always goes to the next slot (1, 2, 3, 4, 5)
      sendAction([id], `stopover_${nextSlotCol(game.state, game.me)}`, "move");
    }
  };

  // tap the dwelling (refinery): draw 1 card, once per turn (free action, play phase)
  const btnTap = $("#btn-tap");   // NOTE: local here - the one in renderAll is NOT in scope
  if (btnTap) btnTap.onclick = () => {
    if (!myTurn(game.state)) return;
    const ph3 = detectPhase(game.state);
    if (!ph3 || ph3.kind !== "play") return;
    const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
    if (!meNow.dwelling || meNow.dwelling_tapped) return;
    sendAction([], "dwelling", "dwelling_activation");
  };

  // discard selection (engine_version 13): the trip chain is paused on
  // "turn N - waiting for NAME to discard K card(s)" — send the chosen cards
  const btnDiscard = $("#btn-discard");
  if (btnDiscard) btnDiscard.onclick = async () => {
    const ph2 = detectPhase(game.state);
    if (!ph2 || ph2.kind !== "discard" || ph2.actor !== game.me) return;
    if (game.selected.size !== ph2.n) { toast(`Select exactly ${ph2.n} card(s) to discard`); return; }
    const resp = await sendAction([...game.selected], "discard_pile", "");
    if (resp && resp.success === false) return;   // rejected: keep the selection
    game.selected = new Set();
  };

  // defend button: the selected card is engaged at 90° on the next slot,
  // it blocks the opponent card placed on the SAME stopover
  $("#btn-defend").onclick = () => {
    const ph2 = detectPhase(game.state);
    if (!ph2 || ph2.kind !== "play" || !myTurn(game.state)) return;
    if (game.selected.size !== 1) return;
    if (playedCount(game.state, game.me) >= N_STOPOVERS) { toast(orderHint()); return; }
    const id = [...game.selected][0];
    if (!checkMana(id)) return;
    sendAction([id], `stopover_${nextSlotCol(game.state, game.me)}`, "defend");
  };

  $("#btn-pass").onclick = () => {
    const ph3 = detectPhase(game.state);
    // mana phase: pass is always allowed (the engine decides whose pass it is)
    if (ph3 === "mana-pass") sendAction([], "", "pass");
    // play phase: only when it is MY action (myTurn() is false in the mana phase —
    // detectPhase returns the string "mana-pass" there, so it must NOT gate the pass)
    else if (ph3 && ph3.kind === "play" && myTurn(game.state)) sendAction([], "", "pass");
  };
}

/* ---------------- side rows: dwelling / pending / deck / discard ---------------- */
function renderSideRows(me, oppo) {
  const side = (p, pref) => {
    // dwelling: 1 card (engine_version 12: the engineers' refinery — tap 1x/turn to draw 1)
    const dw = $(`#${pref}-dwelling`);
    dw.innerHTML = "";
    if (p.dwelling) {
      const card = makeStaticCard(p.dwelling, p.name);
      if (p.dwelling_tapped) card.classList.add("dwelling-tapped");   // dimmed while tapped this turn
      dw.appendChild(card);
    }
    // pending: small cards
    const pe = $(`#${pref}-pendings`);
    pe.innerHTML = "";
    (p.pendings || []).slice(0, 5).forEach((id) => pe.appendChild(makeStaticCard(id, p.name)));
    // deck: counter (public via deck_count even though the cards are hidden)
    const deckN = publicCount(p, "deck_count");
    $(`#${pref}-deck-count`).textContent = deckN ? deckN : "–";
    // discard: last card + counter
    const di = $(`#${pref}-discard`);
    di.innerHTML = "";
    const last = (p.discard || []).slice(-1)[0];
    if (last) di.appendChild(makeStaticCard(last, p.name));
    $(`#${pref}-discard-count`).textContent = (p.discard || []).length;
  };
  side(me, "my");
  if (oppo) side(oppo, "oppo");
}

/* static (non-interactive) card for the side panels and the stopovers */
function makeStaticCard(id, owner) {
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.hoverId = id;
  el.title = cardTitle(id);
  el.innerHTML = `<img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;
  return el;
}

/* ---------------- central Earth cards: day/night • temperature • cataclysms ---------------- */
function renderEnv(st) {
  $("#env-dn").classList.toggle("dn-night", st.day_night === "night");
  $("#env-temp-val").textContent = (st.temperature != null) ? st.temperature : "?";
}

// highlights the next stopover slot (click fallback) when a card is selected
function highlightValidCells() {
  const st = game.state;
  if (!st || slotEls.me.length === 0) return;
  // in cell-selection mode the engineer drop targets are the Earth cells, not the stopovers
  if (cellSelect.active) {
    for (let i = 0; i < N_STOPOVERS; i++) slotEls.me[i].classList.remove("slot-valid");
    return;
  }
  const me = st.players[game.me];
  // only highlight if the selected card is still in hand (avoids a stale state)
  const sel = [...(game.selected || [])].filter((id) => (me.hand || []).includes(id));
  for (let i = 0; i < N_STOPOVERS; i++) {
    slotEls.me[i].classList.toggle("slot-valid", sel.length && myTurn(st) && canDropOnStopover(sel[0], i));
  }
}

/* ---------------- board (radial board) ---------------- */
const BIOME_NAMES = { OC: "Ocean", MO: "Mountain", DE: "Desert", JU: "Jungle" };
const N_CELLS = 24;        // 24 radial positions (invisible) around the Earth
const N_STOPOVERS = 5;     // 5 stopovers per row (2 rows: opponent + player)
const slotEls = { me: [], oppo: [] };   // slotEls[row][col] -> element
const POS24 = [];          // index 0..23 -> {x: %, y: %} relative to #board-stage

// token radius (% of the board square size): between the radial lines,
// just outside the globe (globe radius ≈ 44%)
const POS_RADIUS = 49;

function polar(angleDeg, radiusPct) {
  const a = (angleDeg * Math.PI) / 180;
  return { x: 50 + radiusPct * Math.cos(a), y: 50 + radiusPct * Math.sin(a) };
}

function buildBoard() {
  if (slotEls.me.length) return;

  // 2 rows × 5 slots: opponent row (top) + player row (bottom), numbered 5..1
  for (const row of ["oppo", "me"]) {
    const rowEl = document.getElementById(`row-${row}`);
    for (let col = 0; col < N_STOPOVERS; col++) {
      const el = document.createElement("div");
      el.className = "slot";
      el.dataset.row = row;
      el.dataset.col = col;
      el.innerHTML = `<span class="slot-num">${N_STOPOVERS - col}</span>`;

      if (row === "me") {   // only the player row is a drop target
        el.title = `Stopover ${N_STOPOVERS - col} — drop a card here to play it`;
        el.addEventListener("dragover", (e) => {
          if (!game.dragCardId) return;
          e.preventDefault();
          el.classList.add(canDropOnStopover(game.dragCardId, col) ? "drop-target" : "drop-target-invalid");
        });
        el.addEventListener("dragleave", () => el.classList.remove("drop-target", "drop-target-invalid"));
        el.addEventListener("drop", (e) => {
          e.preventDefault();
          el.classList.remove("drop-target", "drop-target-invalid");
          if (!game.dragCardId) return;
          const id = game.dragCardId;
          game.dragCardId = null;
          game.justDropped = true;
          setTimeout(() => { game.justDropped = false; }, 400);
          if (canDropOnStopover(id, col)) playCardToStopover(id, col);
          else if (myTurn(game.state) && (game.state.players[game.me].hand || []).includes(id)) toast(orderHint());
        });
        // click fallback: card selected + click on the next slot -> play here
        el.addEventListener("click", () => {
          if (game.justDropped) return;
          const sel = [...(game.selected || [])];
          if (!sel.length) return;
          if (canDropOnStopover(sel[0], col)) playCardToStopover(sel[0], col);
          else if (myTurn(game.state) && sel[0]) toast(orderHint());
        });
      }
      rowEl.appendChild(el);
      slotEls[row][col] = el;
    }
  }

  // 24 token positions: centered BETWEEN the radial lines (half-step of 7.5°)
  for (let i = 0; i < N_CELLS; i++) {
    POS24[i] = polar(-90 + (i + 0.5) * (360 / N_CELLS), POS_RADIUS);   // position 0 at the top, clockwise
  }

  // 24 invisible click targets on the Earth (shown only in cell-selection mode):
  // engineer drop placement picks a cell. Sized to the token ring, centered on POS24.
  const targets = document.getElementById("cell-targets");
  if (targets) {
    targets.innerHTML = "";
    for (let i = 0; i < N_CELLS; i++) {
      const pt = POS24[i];
      if (!pt) continue;
      const el = document.createElement("div");
      el.className = "cell-target";
      el.dataset.cell = i;
      el.title = `Cell ${i}`;
      el.style.left = pt.x + "%";
      el.style.top = pt.y + "%";
      el.addEventListener("click", () => { if (cellSelect.active) confirmCell(i); });
      targets.appendChild(el);
    }
  }
}

/* -------- ordering rule: stopovers are filled in the order 1, 2, 3, 4, 5 -------- */
// number of cards already played this turn ('move' OR 'defend' actions of the action_chain:
// a card placed on a stopover — even in defense — occupies the turn's slot)
function playedCount(st, name) {
  const p = (st && st.players) ? st.players[name] : null;
  return ((p && p.action_chain) || []).filter((a) => a && (a.mode === "move" || a.mode === "defend") && a.cards && a.cards.length).length;
}
// next column (0..4) to fill: 1st card -> stopover 1 (column 4, the right-most)
function nextSlotCol(st, name) {
  const n = Math.min(playedCount(st, name), N_STOPOVERS);
  return N_STOPOVERS - 1 - n;   // 4, 3, 2, 1, 0 -> stopovers 1, 2, 3, 4, 5
}
// stopover number of an action: "stopover_3" -> 3 (the full number, not the last digit)
function actionStopoverNum(a) {
  const m = /^stopover_(\d+)/.exec(a.to || "");
  return m ? parseInt(m[1], 10) : null;
}

function canDropOnStopover(cardId, col) {
  const st = game.state;
  if (!st || !myTurn(st)) return false;
  const me = st.players[game.me];
  if (!cardId || !(me.hand || []).includes(cardId)) return false;
  if (playedCount(st, game.me) >= N_STOPOVERS) return false;      // all 5 stopovers already full
  return col === nextSlotCol(st, game.me);   // only the next slot in order
}

function playCardToStopover(cardId, col) {
  game.selected = new Set([cardId]);
  // engineer drop: needs a target cell -> cell-selection mode
  if (isEngineerDrop(cardId)) { enterCellSelect(cardId); return; }
  // engineer dwelling (refinery): goes to the dwelling zone, not a stopover
  if (isEngineerDwelling(cardId)) {
    if (!checkMana(cardId)) return;
    sendAction([cardId], "dwelling", "");
    game.selected = new Set();
    return;
  }
  if (!checkMana(cardId)) return;
  sendAction([cardId], `stopover_${col}`, "move");
}

// help message if the player drops on a not-yet-accessible slot
function orderHint() {
  const st = game.state;
  const n = playedCount(st, game.me);
  if (n >= N_STOPOVERS) return "All 5 stopovers are full this turn.";
  return `Stopovers must be filled in order: put your card on stopover ${n + 1}.`;
}

function renderBoard(st, me, oppoName) {
  buildBoard();
  const earth = st.earth || [];

  // players' advance tokens, between the radial lines of the Earth (animated via CSS transition)
  const layer = $("#markers-layer");
  layer.innerHTML = "";
  for (const [name, p] of Object.entries(st.players)) {
    const pos = Math.min(p.current_position || 0, N_CELLS - 1);
    const el = document.createElement("div");
    el.className = "marker " + (name === game.me ? "me" : "oppo");
    el.textContent = name.slice(0, 1).toUpperCase();
    const tokens = earth[pos] || [];
    const biome = tokens.find((t) => ["OC", "MO", "DE", "JU"].includes(t));
    el.title = `${name} — position ${p.current_position}` + (biome ? ` (${BIOME_NAMES[biome]})` : "");
    const pt = POS24[pos];
    if (pt) {
      el.style.left = pt.x + "%";
      el.style.top = pt.y + "%";
      // offset to avoid the two tokens overlapping on the same position
      const shared = Object.entries(st.players).some(([n, q]) => n !== name && Math.min(q.current_position || 0, N_CELLS - 1) === pos);
      if (shared) el.style.marginTop = "-16px";
    }
    layer.appendChild(el);
  }

  // pet_trap drop tokens on the Earth (public board info: cell -> number of traps;
  //  the next token ARRIVING on that cell is knocked back by -count, then consumed).
  //  Each trap is drawn as its own marker in a slightly offset fan (deliberate
  //  overlap), so a stack of N traps reads as N tokens; the top one carries the
  //  exact count badge.
  const drops = st.drop_tokens || {};
  const MAX_FAN = 6;                 // cap on visible tokens per cell (badge shows the true count)
  const FAN_STEP_PX = 5;             // per-token up-right offset
  for (const [cell, count] of Object.entries(drops)) {
    const pos = Math.min(parseInt(cell, 10) || 0, N_CELLS - 1);
    const pt = POS24[pos];
    if (!pt) continue;
    const n = Math.min(count, MAX_FAN);
    for (let k = 0; k < n; k++) {
      const el = document.createElement("div");
      el.className = "drop-token";
      el.title = `Trap ${k + 1}/${count} on cell ${pos} — the next token arriving here is knocked back by −${count} (then the traps are consumed)`;
      el.innerHTML = `<img src="/assets/effect_pettrap.png" alt="trap" onerror="this.onerror=null;this.src='/placeholder.svg'">`
        + (k === n - 1 && count > 1 ? `<b>${count}</b>` : "");
      el.style.left = `calc(${pt.x}% + ${k * FAN_STEP_PX}px)`;
      el.style.top = `calc(${pt.y}% - ${k * FAN_STEP_PX}px)`;
      el.style.zIndex = 5 + k;       // upper-right tokens on top
      layer.appendChild(el);
    }
  }

  // engineer drop tokens on the Earth (public board info: [{cell, kind, owner}];
  //  each fires ONCE when any token arrives on its cell, then is consumed).
  //  Multiple drops on the same cell fan out up-right (same pattern as pet traps).
  const bdrops = st.board_drops || [];
  const byCell = {};
  for (const d of bdrops) {
    const c = (d && d.cell != null) ? d.cell : -1;
    (byCell[c] = byCell[c] || []).push(d);
  }
  for (const [cellStr, list] of Object.entries(byCell)) {
    const pos = Math.min(parseInt(cellStr, 10) || 0, N_CELLS - 1);
    const pt = POS24[pos];
    if (!pt) continue;
    const n = Math.min(list.length, MAX_FAN);
    for (let k = 0; k < n; k++) {
      const d = list[k];
      const info = ENGINEER_DROPS[d.kind];
      if (!info) continue;
      const el = document.createElement("div");
      el.className = "drop-token eng-drop";
      el.title = `${d.kind} (${info.label}) on cell ${pos}, placed by ${d.owner} — fires when any token arrives`;
      el.innerHTML = `<img src="${info.img}" alt="${d.kind}" onerror="this.onerror=null;this.src='/placeholder.svg'">`
        + (k === n - 1 && list.length > 1 ? `<b>${list.length}</b>` : "");
      el.style.left = `calc(${pt.x}% + ${k * FAN_STEP_PX}px)`;
      el.style.top = `calc(${pt.y}% - ${k * FAN_STEP_PX}px)`;
      el.style.zIndex = 5 + k;
      layer.appendChild(el);
    }
  }

  // cards played by each player, placed in THEIR OWN stopover row
  // (retrieved from the turn's action_chain: to = "stopover_X")
  const slotFill = {};   // row:col -> cards already placed there (offsets overlaps)
  for (const [name, p] of Object.entries(st.players)) {
    const row = (name === game.me) ? "me" : "oppo";
    for (let col = 0; col < N_STOPOVERS; col++) {
      const slot = slotEls[row][col];
      slot.querySelectorAll(".played").forEach((e) => e.remove());
    }
    for (const a of (p.action_chain || [])) {
      if (!a || !a.cards || !a.cards.length) continue;
      if (a.mode !== "move" && a.mode !== "defend") continue;
      const num = actionStopoverNum(a);
      if (num == null) continue;
      const col = num % N_STOPOVERS;
      for (const id of a.cards) {     // a 'defend' action may group several cards
        const el = makeStaticCard(id, name);
        el.classList.add("played");
        if (a.mode === "defend") {
          el.classList.add("defend");   // engaged at 90° to the right
          el.title = `Defense by ${name} (blocks the card facing it)` + (el.title ? " — " + el.title : "");
        } else {
          el.title = `Played by ${name}` + (el.title ? " — " + el.title : "");
        }
        // several cards on the SAME slot (only possible in old games): nudge each
        // additional one up so they all stay visible
        const key = row + ":" + col;
        const k = (slotFill[key] = (slotFill[key] || 0) + 1);
        if (k > 1) el.style.marginTop = `${-(k - 1) * 8}px`;
        slotEls[row][col].appendChild(el);
      }
    }
    // dwelling placeholder: a faction-specific placeholder image in the stopover
    // slot that the refinery would have occupied (stored in p.dwelling_slot)
    // — purely visual, fills the slot to show the dwelling card is on the board
    // (engine_version 12+). The engine sets it at placement time and CLEARS it in
    // the cleaning phase, so the placeholder only shows during the placement turn
    // and must not reappear at every new turn.
    // ALWAYS remove any stale placeholder first (it would otherwise stick in the
    // DOM once the engine clears the slot - the slots are built once, not rebuilt
    // on each render).
    slotEls[row].forEach(s => s.querySelectorAll(".dwelling-placeholder").forEach(e => e.remove()));
    if (p.dwelling && p.dwelling_slot !== null && p.dwelling_slot !== undefined) {
      const phCol = p.dwelling_slot;
      const slot = slotEls[row][phCol];
      const el = document.createElement("div");
      el.className = "card dwelling-placeholder";
      el.title = `⚗ Dwelling: ${p.dwelling} (${name})`;
      el.innerHTML = `<img src="${dwellingPlaceholderSrc(p.faction)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;
      slot.appendChild(el);
    }
    // visual states: filled slots / next slot (order 1->5) / not-yet-accessible slots
    const n = Math.min(playedCount(st, name), N_STOPOVERS);
    const next = n < N_STOPOVERS ? N_STOPOVERS - 1 - n : -1;
    for (let col = 0; col < N_STOPOVERS; col++) {
      const slot = slotEls[row][col];
      slot.classList.toggle("filled", slot.querySelector(".played") !== null || slot.querySelector(".dwelling-placeholder") !== null);
      slot.classList.toggle("slot-next", row === "me" && col === next);
    }
  }
}

/* ---------------- game log (turn -> stopover -> both players' lines) ---------------- */
// readable label for a card's effect (uses the card pool: effect + effect_number)
function effectLabel(id) {
  const row = CARDPOOL[id];
  if (!row || !row.effect) return null;
  const n = (row.effect_number != null) ? (Math.abs(parseInt(row.effect_number, 10)) || 0) : 0;
  switch (row.effect) {
    case 'advancing': return `advancing +${n}`;
    case 'backward': return `recoil ${n}`;
    case 'advancing_oppo': return `opponent +${n}`;
    case 'backward_oppo': return `opponent −${n}`;
    case 'draw': return `draw ${n}`;
    case 'draw_oppo': return `opponent draws ${n}`;
    case 'discard': return `discard ${n}`;
    case 'discard_oppo': return `opponent discards ${n}`;
    case 'ramp': return `ramp ${n}`;
    case 'ramp_oppo': return `opponent +${n} mana`;
    case 'taxation': return `taxation ${n}`;
    case 'taxation_oppo': return `opponent −${n} mana`;
    case 'pet_trap': return 'trap — drop token here';
    default: return row.effect;
  }
}

// "stopover_4" (1st slot of the turn) -> "stopover 1" (the UI's numbering 1..5)
function stopoverLabel(sv) {
  const m = /^stopover_(\d+)/.exec(sv || '');
  if (!m) return String(sv || 'stopover');
  const num = parseInt(m[1], 10);
  return `stopover ${((5 - (num % 5)) % 5) || 5}`;
}

// mini card (hover = 3x preview, click = zoom modal)
function makeLogCardEl(id) {
  const el = document.createElement('span');
  el.className = 'log-card';
  el.title = (CARDPOOL[id] && CARDPOOL[id].name) || id;
  el.innerHTML = `<img src="${cardImg(id)}" alt="" loading="lazy" onerror="this.onerror=null;this.src='/placeholder.svg'">`;
  el.addEventListener('mouseenter', () => showCardHover(id));
  el.addEventListener('mouseleave', hideCardHover);
  el.addEventListener('click', () => showCardModal(id));
  return el;
}

function makeTag(cls, text) {
  const t = document.createElement('span');
  t.className = 'log-tag ' + cls;
  t.textContent = text;
  return t;
}

// one player's line: [order] [name] [pos] [cards] [condition tag] [effect] [negatives] -> [final pos]
function buildLogEntryEl(e) {
  const div = document.createElement('div');
  div.className = 'log-entry';
  const flow = document.createElement('div');
  flow.className = 'log-flow';

  const ord = document.createElement('span');
  ord.className = 'log-ord';
  ord.textContent = e.order === 1 ? '1st' : (e.order === 2 ? '2nd' : '');
  const name = document.createElement('span');
  name.className = 'log-name';
  name.textContent = e.player;
  flow.append(ord, name);

  const p0 = document.createElement('span');
  p0.className = 'log-pos';
  p0.textContent = `pos ${e.pos_before}`;
  flow.append(p0);

  for (const id of (e.cards || [])) flow.append(makeLogCardEl(id));

  const negatives = e.negatives || [];
  const blocked = negatives.some((s) => /blocked/.test(s));

  if (e.mode === 'defend') {
    // defend line: no condition is ever evaluated -> shield tag instead
    flow.append(makeTag('shield', `⛨ shield ${e.shield ?? 0}`));
  } else {
    // condition tag: green = met, red = not met, gray = no condition / not evaluated
    if (e.condition_met === true) flow.append(makeTag('met', '✓ condition'));
    else if (e.condition_met === false) flow.append(makeTag('unmet', '✗ condition'));
    else if (!blocked) flow.append(makeTag('shield', 'no condition'));
    // effect tag (dimmed when the effect never fired: blocked or canceled)
    const eff = effectLabel((e.cards || [])[0]);
    if (eff) {
      const dim = /blocked|canceled/.test(negatives.join(' '));
      flow.append(makeTag('effect' + (dim ? ' dim' : ''), eff));
    }
    for (const s of negatives) flow.append(makeTag('neg', s));
  }

  const pf = document.createElement('span');
  pf.className = 'log-pos final';
  pf.textContent = `→ ${e.pos_after != null ? e.pos_after : e.pos_before}`;
  flow.append(pf);

  div.append(flow);
  for (const note of (e.notes || [])) {
    const n = document.createElement('div');
    n.className = 'log-note';
    n.textContent = note;
    div.append(n);
  }
  return div;
}

function buildLogTurnEl(t, open) {
  const det = document.createElement('details');
  det.className = 'log-turn';
  det.open = !!open;
  const sum = document.createElement('summary');
  sum.textContent = `Turn ${t.turn}`;
  det.append(sum);
  for (const sv of (t.stopovers || [])) {
    const sd = document.createElement('details');
    sd.className = 'log-sv';
    sd.open = true;
    const ss = document.createElement('summary');
    ss.textContent = stopoverLabel(sv.stopover);
    sd.append(ss);
    for (const e of (sv.entries || [])) sd.append(buildLogEntryEl(e));
    det.append(sd);
  }
  return det;
}

// The log panel is pinned to the RIGHT edge of the row (absolute, see CSS) while the
// board + trip-chain are centered — measure the free space between them and cap the
// panel's width so the centered group is never overlapped, whatever the window width.
function fitLogWidth() {
  const panel = $('#log-panel');
  if (!panel || panel.classList.contains('hidden')) return;
  const row = $('#board-wrap');
  const rr = row && row.getBoundingClientRect();
  if (!rr || !rr.width) return;
  let groupRight = 0;
  for (const el of [$('#board-stage'), $('#stopovers')]) {
    if (!el) continue;
    const b = el.getBoundingClientRect();
    if (b.width) groupRight = Math.max(groupRight, b.right);
  }
  // rr.right is the border-box edge; the panel (absolute, right:0) sits inside the
  // 18px right padding -> reserve that padding + a 20px visual gap before the panel.
  const avail = rr.right - 18 - 20 - groupRight;
  panel.style.maxWidth = Math.max(170, Math.min(avail, 340)) + 'px';
}
window.addEventListener('resize', () => fitLogWidth());

// incremental: only newly-resolved turns are appended (existing collapsed state is
// preserved across the 2.5s polling re-renders); on the first render (or a rejoin)
// every turn appears, all collapsed except the latest one
function renderLog(st) {
  const log = (st && st.log) || [];
  const panel = $('#log-panel');
  if (!log.length) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  const body = $('#log-body');
  const start = game.logTurnsRendered;
  let added = false;
  while (game.logTurnsRendered < log.length) {
    body.append(buildLogTurnEl(log[game.logTurnsRendered], true));
    game.logTurnsRendered++;
    added = true;
  }
  fitLogWidth();   // cap the (right-pinned) panel to the free space left of the centered board + trip-chain
  if (start === 0 && game.logTurnsRendered > 1) {
    // first render with several turns: collapse all but the latest
    body.querySelectorAll(':scope > .log-turn').forEach((d, i) => {
      if (i < game.logTurnsRendered - 1) d.open = false;
    });
  }
  // auto-reveal the latest turn only when it's new — otherwise keep the
  // user's scroll position (the panel scrolls internally, see #board-wrap)
  if (added) body.scrollTop = body.scrollHeight;
}

/* ---------------- hand & drag/drop ---------------- */
// hand card (interactive) — used in the bottom banner, on both sides of the mana drop
function makeHandCard(id, interactive, me) {
  const el = document.createElement("div");
  el.className = "card" + (game.selected.has(id) ? " selected" : "") + (!interactive ? " disabled" : "");
  el.dataset.hoverId = id;
  const c = cardInfo(id);
  // playable card: cost ≤ available mana this turn -> green/blue outline
  const cost = cardCost(id);
  const avail = (me && (me.mana || []).length) - ((me && me.mana_spend) || 0);
  if (interactive && cost <= avail) el.classList.add("playable");
  let title;
  if (c && c.card_name) title = cardTitle(id) + ` (mana cost: ${cost})`;
  else if (c && c.name) title = `${c.name} — ${c.faction} (mana cost: ${cost})`;
  else title = id;
  el.title = title;
  el.innerHTML = `<img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;

  // click: selection (1 card for play/mana-pass, up to 3 in init, exactly N in discard selection)
  if (interactive) {
    el.onclick = () => {
      const ph = detectPhase(game.state);
      const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
      const isDiscardSel = ph && ph.kind === "discard" && ph.actor === game.me;
      const maxSel = ph === "init-mana" ? START_MANA_N - ((meNow.mana || []).length)
                   : isDiscardSel ? ph.n : 1;
      if (game.selected.has(id)) game.selected.delete(id);
      else {
        if (ph !== "init-mana" && !isDiscardSel) game.selected.clear();
        if (game.selected.size < maxSel) game.selected.add(id);
        else if (isDiscardSel) toast(`Select exactly ${maxSel} card(s) to discard`);
      }
      renderAll();
    };

    // native drag & drop
    el.draggable = true;
    el.addEventListener("dragstart", (e) => {
      if (!myTurn(game.state) && detectPhase(game.state) !== "init-mana" && detectPhase(game.state) !== "mana-pass") {
        e.preventDefault(); return;
      }
      game.dragCardId = id;
      el.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", id); } catch {}
    });
    el.addEventListener("dragend", () => {
      el.classList.remove("dragging");
      game.dragCardId = null;
      $$(".slot").forEach((s) => s.classList.remove("drop-target", "drop-target-invalid"));
    });

    // right click: zoom
    el.oncontextmenu = (e) => { e.preventDefault(); showCardModal(id); };
  } else {
    el.oncontextmenu = (e) => { e.preventDefault(); showCardModal(id); };
  }
  return el;
}

/* hover preview: the card shows at 3x in the bottom-left (delegation, robust to re-renders) */
const hoverEl = $("#card-hover"), hoverImg = $("#card-hover-img");
function showCardHover(id) {
  if (!id) return;
  hoverImg.onerror = () => { hoverImg.onerror = null; hoverImg.src = "/placeholder.svg" };
  hoverImg.src = id === "__back__" ? "/assets/GR_cards_back.png" : cardImg(id);
  hoverEl.classList.add("show");
}
function hideCardHover() { hoverEl.classList.remove("show"); }
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest(".card");
  if (!el || !el.dataset.hoverId) return;
  showCardHover(el.dataset.hoverId);
});
document.addEventListener("mouseout", (e) => {
  const el = e.target.closest(".card");
  if (!el) return;
  if (e.relatedTarget && el.contains(e.relatedTarget)) return;   // still over the card
  hideCardHover();
});

/* ---------------- modales & messages ---------------- */
function showCardModal(id) {
  $("#card-modal-img").src = cardImg(id);
  const img = $("#card-modal-img");
  img.onerror = () => { img.onerror = null; img.src = "/placeholder.svg"; };
  $("#card-modal").classList.remove("hidden");
}
$("#card-modal .modal-backdrop").onclick = () => $("#card-modal").classList.add("hidden");
$("#card-modal-img").onclick = () => $("#card-modal").classList.add("hidden");

function showServerMsg(msg) {
  const el = $("#server-msg");
  clearTimeout(el._clearTimer);
  if (msg) { el.textContent = msg; return; }
  el._clearTimer = setTimeout(() => { el.textContent = ""; }, 3000);   // successes clear themselves
}

function showEndgame(st) {
  const winner = st.winner;
  const title = winner ? `🏆 ${winner} gagne !` : "🤝 Match nul";
  $("#endgame-title").textContent = title;
  $("#endgame-sub").textContent = st.state === "game over" && String(st.message?.message || "").startsWith("Deadlock")
    ? "Deadlock — no playable card left for either player."
    : `The race ends at turn ${st.turn}.`;
  $("#endgame-modal").classList.remove("hidden");
}

$("#btn-endgame-ok").onclick = () => {
  $("#endgame-modal").classList.add("hidden");
  leaveGame();
};

function leaveGame() {
  stopPolling();
  if (game.ws) { try { game.ws.close(); } catch {} game.ws = null; }
  game.state = null; game.id = null; game.selected.clear();
  game.logTurnsRendered = 0;
  $("#log-body").innerHTML = "";
  $("#log-panel").classList.add("hidden");
  $("#view-game").classList.add("hidden");
  $("#view-setup").classList.remove("hidden");
}

$("#btn-leave").onclick = () => {
  if (confirm("Leave the game? The state stays saved on the server.")) leaveGame();
};

/* ------------------------------------------------------------------ boot */
(async function boot() {
  initSetup();
  const results = await Promise.allSettled([loadCardpool(), loadSupportCards()]);
  for (const r of results) if (r.status === "rejected") toast(`Cannot load the cards: ${r.reason.message}`);
  // re-render the support section now that the card data is available
  renderSupportFactions();
  if (setup.support) renderSupportPreview();
})();
