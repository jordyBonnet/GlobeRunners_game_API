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
  const c = CARDPOOL[id];
  if (!me || !c || c.mana == null) return true;
  const avail = (me.mana || []).length - (me.mana_spend || 0);
  if (c.mana > avail) {
    toast(`Not enough mana: ${c.name} costs ${c.mana}, only ${avail} left`, 5000);
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

function cardImg(id) {
  // art is served by /art/<id>.png; if the file doesn't exist, the onerror handler falls back to the placeholder
  return `/art/${encodeURIComponent(id)}.png`;
}

const FACTIONS = [
  { key: "Dwa", name: "Dwarves", logo: "/assets/logo_dwarves.png" },
  { key: "Dem", name: "Demons",  logo: "/assets/logo_demons.png" },
  { key: "Twi", name: "Twigs",   logo: "/assets/logo_twigs.png" },
  { key: "Mia", name: "Miaous",  logo: "/assets/logo_miaous.png" },
  { key: "Orc", name: "Orcs",    logo: "/assets/logo_orcs.png" },
  { key: "Mum", name: "Mummies", logo: "/assets/logo_mummies.png" },
];

/* ------------------------------------------------------------------ deck building */
const DECK_SIZE = 30;           // standard deck size (3 initial mana + 6 in hand + the rest in deck)

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
  while (deck.length < DECK_SIZE && bag.length) {
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
  deck: [],            // list of card_id
  faction: null,       // starter faction key
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
    el.title = CARDPOOL[id] ? `${CARDPOOL[id].name} — ${CARDPOOL[id].faction}` : id;
    el.innerHTML = `<img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;
    box.appendChild(el);
  }
}

function updateLaunchBtn() {
  const okName = setup.name.trim().length >= 2;
  const okDeck = setup.deck.length === DECK_SIZE;
  const okGame = setup.mode === "join" ? $("#join-game-id").value.trim().length > 0 : true;
  $("#btn-launch").disabled = !(okName && okDeck && okGame);
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
      $("#csv-filename").textContent = `${file.name} — ${ids.length} card(s)` + (unknown.length ? `, ${unknown.length} unknown: ${unknown.slice(0, 3).join(", ")}` : "");
      if (!ids.length) { toast("No card found in this CSV"); return; }
      setup.deck = ids.slice(0, DECK_SIZE);
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
}

async function launch() {
  const errEl = $("#setup-error");
  errEl.textContent = "";
  const name = setup.name.trim();
  if (name.length < 2) { errEl.textContent = "Pick a nickname (2 characters min)."; return; }
  if (setup.deck.length !== DECK_SIZE) { errEl.textContent = `The deck must contain exactly ${DECK_SIZE} cards.`; return; }

  const btn = $("#btn-launch");
  btn.disabled = true; btn.textContent = "Lancement…";
  try {
    await loadCardpool();
    if (setup.mode === "ai") {
      // game vs AI: the robot joins immediately, we go straight into the game
      const res = await api("/create_game_ai", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, deck: setup.deck }),
      });
      setup.gameId = res.game_id;
      toast(`Game started against ${res.opponent}!`);
      enterGame();
    } else if (setup.mode === "create") {
      const res = await api("/create_game", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, deck: setup.deck }),
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
          body: JSON.stringify({ name, deck: setup.deck }),
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
};

function enterGame() {
  $("#view-setup").classList.add("hidden");
  $("#view-game").classList.remove("hidden");
  game.id = setup.gameId;
  game.me = setup.name.trim();
  game.lastWinnerShown = false;
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

function sendAction(cards, to, mode, pendings = []) {
  /* sends an action and resolves with the server's reply (state or rejection).
     Sends are serialized: one WS message at a time. */
  return new Promise((resolve) => {
    const doSend = () => {
      if (!game.ws || game.ws.readyState !== WebSocket.OPEN) {
        toast("Connection lost — retrying…");
        connectWs();
        setTimeout(doSend, 1500);
        return;
      }
      pendingResolvers.push(resolve);
      game.ws.send(JSON.stringify({ cards: cards || [], to: to || "", mode: mode || "", pendings }));
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
  } else if (ph === "over") {
    phaseText = "Game over";
  } else {
    phaseText = st.state || "";
  }
  $("#phase-label").textContent = phaseText;
  $("#action-hint").textContent = hint;

  const btnPlay = $("#btn-play"), btnDefend = $("#btn-defend"), btnPass = $("#btn-pass");
  const canDefend = canAct && ph && ph.kind === "play" && game.selected.size === 1;
  if (ph === "init-mana") {
    btnPlay.textContent = "Poser en mana";
    btnPlay.disabled = !canAct || game.selected.size === 0;
    btnPass.classList.add("hidden");
    btnDefend.classList.add("hidden");
  } else if (ph === "mana-pass" || ph.kind === "play") {
    btnPlay.textContent = "Play the card";
    btnPlay.disabled = !canAct || game.selected.size !== 1;
    btnDefend.classList.remove("hidden");
    btnDefend.disabled = !canDefend;
    btnPass.classList.remove("hidden");
    btnPass.disabled = !canAct;
  } else {
    btnPlay.disabled = true;
    btnDefend.disabled = true;
    btnPass.disabled = true;
  }

  const interactive = canAct && (ph === "init-mana" || ph === "mana-pass" || (ph && ph.kind === "play"));

  if (oppo) renderOppZone(oppo);
  renderMyZone(me, interactive);
  renderSideRows(me, oppo);
  renderEnv(st);
  renderBoard(st, me, oppoName);
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
  $("#row-oppo-owner").textContent = oppo.name;
}

/* ---------------- player banner: [name] [mana on the left] [hand] [actions] ---------------- */
function renderMyZone(me, interactive) {
  $("#my-name").textContent = me.name;
  const manaAvail = (me.mana || []).length - (me.mana_spend || 0);
  $("#mana-info").innerHTML =
    `<span>⚡ mana ${manaAvail}/${(me.mana || []).length}</span>` +
    `<span>🂠 main ${(me.hand || []).length}</span>`;

  // hand (to the right of the mana zone)
  const hand = $("#my-hand");
  hand.innerHTML = "";
  (me.hand || []).forEach((id) => hand.appendChild(makeHandCard(id, interactive, me)));
  $("#row-me-owner").textContent = me.name;

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
      if (playedCount(game.state, game.me) >= N_STOPOVERS) { toast(orderHint()); return; }
      if (!checkMana(id)) return;
      // ordering rule: the card always goes to the next slot (1, 2, 3, 4, 5)
      sendAction([id], `stopover_${nextSlotCol(game.state, game.me)}`, "move");
    }
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
    if (!myTurn(game.state)) return;
    const ph3 = detectPhase(game.state);
    if (ph3 === "mana-pass") sendAction([], "", "pass");
    else if (ph3 && ph3.kind === "play") sendAction([], "", "pass");
  };
}

/* ---------------- side rows: dwelling / pending / deck / discard ---------------- */
function renderSideRows(me, oppo) {
  const side = (p, pref) => {
    // dwelling: 1 card (when implemented)
    const dw = $(`#${pref}-dwelling`);
    dw.innerHTML = "";
    if (p.dwelling) dw.appendChild(makeStaticCard(p.dwelling, p.name));
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
  const c = CARDPOOL[id];
  el.title = c ? `${c.name} — ${c.faction}` : id;
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
    const label = rowEl.querySelector(".row-label");
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
      rowEl.insertBefore(el, label);
      slotEls[row][col] = el;
    }
  }

  // 24 token positions: centered BETWEEN the radial lines (half-step of 7.5°)
  for (let i = 0; i < N_CELLS; i++) {
    POS24[i] = polar(-90 + (i + 0.5) * (360 / N_CELLS), POS_RADIUS);   // position 0 at the top, clockwise
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

  // cards played by each player, placed in THEIR OWN stopover row
  // (retrieved from the turn's action_chain: to = "stopover_X")
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
        slotEls[row][col].appendChild(el);
      }
    }
    // visual states: filled slots / next slot (order 1->5) / not-yet-accessible slots
    const n = Math.min(playedCount(st, name), N_STOPOVERS);
    const next = n < N_STOPOVERS ? N_STOPOVERS - 1 - n : -1;
    for (let col = 0; col < N_STOPOVERS; col++) {
      const slot = slotEls[row][col];
      slot.classList.toggle("filled", slot.querySelector(".played") !== null);
      slot.classList.toggle("slot-next", row === "me" && col === next);
    }
  }
}

/* ---------------- hand & drag/drop ---------------- */
// hand card (interactive) — used in the bottom banner, on both sides of the mana drop
function makeHandCard(id, interactive, me) {
  const el = document.createElement("div");
  el.className = "card" + (game.selected.has(id) ? " selected" : "") + (!interactive ? " disabled" : "");
  el.dataset.hoverId = id;
  const c = CARDPOOL[id];
  // playable card: mana cost ≤ available mana this turn -> green/blue outline
  const cost = (c && c.mana != null) ? c.mana : 0;
  const avail = (me && (me.mana || []).length) - ((me && me.mana_spend) || 0);
  if (interactive && cost <= avail) el.classList.add("playable");
  el.title = c ? `${c.name} — ${c.faction} (mana cost: ${cost})` : id;
  el.innerHTML = `<img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;

  // click: selection (1 card for play/mana-pass, up to 3 in init)
  if (interactive) {
    el.onclick = () => {
      const ph = detectPhase(game.state);
      const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
      const maxSel = ph === "init-mana" ? START_MANA_N - ((meNow.mana || []).length) : 1;
      if (game.selected.has(id)) game.selected.delete(id);
      else {
        if (ph !== "init-mana") game.selected.clear();
        if (game.selected.size < maxSel) game.selected.add(id);
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
  $("#view-game").classList.add("hidden");
  $("#view-setup").classList.remove("hidden");
}

$("#btn-leave").onclick = () => {
  if (confirm("Leave the game? The state stays saved on the server.")) leaveGame();
};

/* ------------------------------------------------------------------ boot */
(async function boot() {
  initSetup();
  try { await loadCardpool(); } catch (e) { toast(`Cannot load the cards: ${e.message}`); }
})();
