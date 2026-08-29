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
  // l'art est servi par /art/<id>.png ; si le fichier n'existe pas, on bascule sur le placeholder
  return `/art/${encodeURIComponent(id)}.png`;
}

const FACTIONS = [
  { key: "Dwa", name: "Nains",     logo: "/assets/logo_dwarves.png" },
  { key: "Dem", name: "Démons",    logo: "/assets/logo_demons.png" },
  { key: "Twi", name: "Rameaux",   logo: "/assets/logo_twigs.png" },
  { key: "Mia", name: "Miaous",    logo: "/assets/logo_miaous.png" },
  { key: "Orc", name: "Orques",    logo: "/assets/logo_orcs.png" },
  { key: "Mum", name: "Momies",    logo: "/assets/logo_mummies.png" },
];

/* ------------------------------------------------------------------ deck building */
const DECK_SIZE = 30;           // taille standard du jeu (3 mana initiaux + 6 main + le reste en deck)

function buildStarterDeck(factionKey, seedStr) {
  const pool = Object.values(CARDPOOL).filter((c) => c.faction && c.faction.startsWith(factionKey));
  if (!pool.length) return [];
  // déterministe pour un même pseudo : le deck est identique si on recharge la page
  let h = 2166136261 >>> 0;
  for (const ch of String(seedStr)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619) >>> 0; }
  const rand = () => { h = (Math.imul(h, 1664525) + 1013904223) >>> 0; return h / 4294967296; };

  // pondération : les cartes "rare" sont moins fréquentes
  const weighted = [];
  for (const c of pool) {
    const w = c.rare ? 1 : 3;
    for (let i = 0; i < w; i++) weighted.push(c);
  }
  // tirage sans remise pondéré
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
  // accepte : une colonne card_id (avec ou sans en-tête), séparateur virgule/point-virgule/tab
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const ids = [];
  for (const line of lines) {
    const parts = line.split(/[,;\t]/).map((p) => p.trim().replace(/^"|"$/g, "")).filter(Boolean);
    // si la ligne contient plusieurs colonnes, on cherche celle qui ressemble à un card_id
    let id = null;
    for (const p of parts) {
      if (/^[A-Za-z]{3}\d+_[0-9a-f]{4,}$/.test(p)) { id = p; break; }
    }
    // fichier mono-colonne : on n'accepte qu'un vrai card_id (ignore l'en-tête "card_id" et les lignes parasites)
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

  // tabs mode (créer / IA / rejoindre)
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
      $("#csv-filename").textContent = `${file.name} — ${ids.length} carte(s)` + (unknown.length ? `, ${unknown.length} inconnue(s) : ${unknown.slice(0, 3).join(", ")}` : "");
      if (!ids.length) { toast("Aucune carte trouvée dans ce CSV"); return; }
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
    try { await navigator.clipboard.writeText($("#created-game-id").textContent); toast("ID copié !"); }
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
  if (name.length < 2) { errEl.textContent = "Choisis un pseudo (2 caractères min)."; return; }
  if (setup.deck.length !== DECK_SIZE) { errEl.textContent = `Le deck doit contenir exactement ${DECK_SIZE} cartes.`; return; }

  const btn = $("#btn-launch");
  btn.disabled = true; btn.textContent = "Lancement…";
  try {
    await loadCardpool();
    if (setup.mode === "ai") {
      // partie contre l'IA : le robot rejoint immédiatement, on entre dans la partie tout de suite
      const res = await api("/create_game_ai", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, deck: setup.deck }),
      });
      setup.gameId = res.game_id;
      toast(`Partie lancée contre ${res.opponent} !`);
      enterGame();
    } else if (setup.mode === "create") {
      const res = await api("/create_game", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, deck: setup.deck }),
      });
      setup.gameId = res.game_id;
      $("#created-game-id").textContent = res.game_id;
      $("#created-game-box").classList.remove("hidden");
      toast(`Partie créée ! ID : ${res.game_id}`);
      // on attend que l'adversaire rejoigne (polling de l'état)
      startWaitingForOpponent();
    } else {
      const gid = $("#join-game-id").value.trim();
      if (!gid) { errEl.textContent = "Renseigne l'ID de la partie."; return; }
      setup.gameId = gid;
      try {
        await api(`/join_game/${encodeURIComponent(gid)}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, deck: setup.deck }),
        });
      } catch (e) {
        // 409 : le joueur est déjà dans la partie -> on reprend simplement (rechargement de page)
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

/* p1 attend que p2 rejoigne */
let waitTimer = null;
function startWaitingForOpponent() {
  $("#btn-launch").textContent = "En attente de l'adversaire…";
  const poll = async () => {
    try {
      const st = await api(`/game/${encodeURIComponent(setup.gameId)}`);
      if (st.players && Object.keys(st.players).length >= 2) {
        stopWaiting();
        enterGame();
        return;
      }
    } catch { /* pas encore */ }
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
  me: null,              // mon nom de joueur
  ws: null,
  state: null,           // dernier état personnalisé reçu
  selected: new Set(),   // card_id sélectionnés dans la main
  dragCardId: null,      // carte en cours de drag (depuis la main)
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
  startPolling();   // le WS ne reçoit un état que quand ON envoie : on poll aussi pour voir l'adversaire
}

/* ---------------- websocket ---------------- */
function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/${encodeURIComponent(game.id)}/${encodeURIComponent(game.me)}`;
}

let pendingResolvers = [];   // un resolveur par message WS en attente de réponse

function connectWs() {
  if (game.ws && game.ws.readyState <= WebSocket.OPEN) return;
  setConn(false);
  const ws = new WebSocket(wsUrl());
  game.ws = ws;

  ws.onopen = () => setConn(true);
  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    // un refus a la forme {success: false, message}; un état de jeu n'a pas de clé 'success'
    if (data && data.success === false) showServerMsg(data.message || "Action refusée");
    else applyState(data);
    const resolver = pendingResolvers.shift();
    if (resolver) resolver(data);
  };
  ws.onclose = () => setConn(false);
}

function sendAction(cards, to, mode, pendings = []) {
  /* envoie une action et résout avec la réponse du serveur (état ou refus).
     Les envois sont sérialisés : un seul message à la fois sur le WS. */
  return new Promise((resolve) => {
    const doSend = () => {
      if (!game.ws || game.ws.readyState !== WebSocket.OPEN) {
        toast("Connexion perdue — réessai…");
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
  el.textContent = ok ? "● connecté" : "○ déconnecté";
  el.className = ok ? "conn-ok" : "conn-bad";
  if (!ok && game.id) setTimeout(connectWs, 2000);   // reconnexion auto
}

/* ---------------- polling de l'état (voir les actions de l'adversaire) ---------------- */
function startPolling() {
  stopPolling();
  const poll = async () => {
    if (!game.id) return;
    try {
      const st = await api(`/api/state/${encodeURIComponent(game.id)}/${encodeURIComponent(game.me)}`);
      if (st && st.players) applyState(st, true);
    } catch { /* serveur momentanément indisponible */ }
    game.pollTimer = setTimeout(poll, 2500);
  };
  poll();
}
function stopPolling() { if (game.pollTimer) clearTimeout(game.pollTimer); game.pollTimer = null; }

/* ---------------- phase detection ---------------- */
const START_MANA_N = 3;   // cartes à mettre en mana à l'initialisation

function detectPhase(st) {
  const s = st.state || "";
  if (s === "game over") return "over";
  if (s.includes("waiting for both players to put")) return "init-mana";
  if (s.includes("waiting for both players to mana or pass")) return "mana-pass";

  // tour : "turn N - waiting for first/second player (NAME) to play"
  const m = s.match(/turn (\d+) - waiting for (first|second) player \((.+?)\) to play/);
  if (m) {
    return { kind: "play", turn: +m[1], actor: m[3] };
  }
  // entre les deux actions d'un tour, l'état peut être transitoire
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
    showServerMsg(isFailure ? txt : "");   // vide -> effacé après 3 s
  }

  // fin de partie
  const over = detectPhase(st) === "over";
  if (over && !game.lastWinnerShown) {
    game.lastWinnerShown = true;
    stopPolling();
    showEndgame(st);
  } else if (!over && wasOver) {
    // nouvelle partie sur la même page ? on repart du polling
    startPolling();
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

  // phase label + boutons
  const ph = detectPhase(st);
  let phaseText = "", canAct = false, hint = "";
  if (ph === "init-mana") {
    const n = (me.mana || []).length;
    phaseText = `Initialisation : pose ${START_MANA_N} cartes en mana (${n}/${START_MANA_N})`;
    canAct = true;
    hint = "Glisse des cartes de ta main vers la zone ⚡ Mana, puis clique « Jouer la carte ».";
  } else if (ph === "mana-pass") {
    phaseText = "Phase mana : pose 1 carte en mana ou passe";
    canAct = true;
    hint = "Glisse UNE carte vers ⚡ Mana et clique « Jouer la carte », ou clique « Passer ».";
  } else if (ph && ph.kind === "play") {
    phaseText = `Tour ${ph.turn} — au tour de ${ph.actor}`;
    canAct = myTurn(st);
    hint = canAct ? "Pose ta carte sur la prochaine case de stopover (dans l'ordre 1→5), ou « Passer »." : "En attente de l'adversaire…";
  } else if (ph === "over") {
    phaseText = "Partie terminée";
  } else {
    phaseText = st.state || "";
  }
  $("#phase-label").textContent = phaseText;
  $("#action-hint").textContent = hint;

  const btnPlay = $("#btn-play"), btnPass = $("#btn-pass");
  if (ph === "init-mana") {
    btnPlay.textContent = "Poser en mana";
    btnPlay.disabled = !canAct || game.selected.size === 0;
    btnPass.classList.add("hidden");
  } else if (ph === "mana-pass" || ph.kind === "play") {
    btnPlay.textContent = "Jouer la carte";
    btnPlay.disabled = !canAct || game.selected.size !== 1;
    btnPass.classList.remove("hidden");
    btnPass.disabled = !canAct;
  } else {
    btnPlay.disabled = true;
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

/* compteurs publics : l'état personnalisé masque les IDs mais expose *_count */
const publicCount = (p, key) => (p && p[key] != null) ? p[key] : (p && (p[key.replace("_count", "")] || []).length);

/* ---------------- bandeau adverse : [nom + compteurs] [mana à gauche] [main en dos] ---------------- */
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
  // main adverse : N dos de cartes (les cartes elles-mêmes restent cachées)
  const hand = $("#oppo-hand");
  hand.innerHTML = "";
  for (let i = 0; i < handN; i++) {
    const c = document.createElement("div");
    c.className = "card back";
    c.dataset.hoverId = "__back__";
    c.title = `Carte de l'adversaire (cachée) — ${handN} en main`;
    hand.appendChild(c);
  }
  // mana adverse : dos de cartes dans la goutte (engagées = tournées 90°) + compteur dispo/total
  const mc = $("#oppo-mana-cards");
  mc.innerHTML = "";
  for (let i = 0; i < manaN; i++) {
    const c = document.createElement("div");
    c.className = "card back" + (i < manaSpent ? " tapped" : "");
    c.dataset.hoverId = "__back__";
    c.title = i < manaSpent ? `Mana engagé ce tour (${manaSpent})` : `Mana disponible (${manaAvail})`;
    mc.appendChild(c);
  }
  $("#oppo-mana-count").textContent = `${manaAvail}/${manaN}`;
  $("#row-oppo-owner").textContent = oppo.name;
}

/* ---------------- bandeau joueur : [nom] [mana à gauche] [main] [actions] ---------------- */
function renderMyZone(me, interactive) {
  $("#my-name").textContent = me.name;
  const manaAvail = (me.mana || []).length - (me.mana_spend || 0);
  $("#mana-info").innerHTML =
    `<span>⚡ mana ${manaAvail}/${(me.mana || []).length}</span>` +
    `<span>🂠 main ${(me.hand || []).length}</span>`;

  // main (à droite de la zone mana)
  const hand = $("#my-hand");
  hand.innerHTML = "";
  (me.hand || []).forEach((id) => hand.appendChild(makeHandCard(id, interactive, me)));
  $("#row-me-owner").textContent = me.name;

  // mana : dos de cartes dans la goutte (engagées = tournées 90° vers la droite) + compteur dispo/total
  const manaCards = $("#my-mana-cards");
  manaCards.innerHTML = "";
  const nMana = (me.mana || []).length;
  const nSpent = me.mana_spend || 0;
  for (let i = 0; i < nMana; i++) {
    const el = document.createElement("div");
    el.className = "card back" + (i < nSpent ? " tapped" : "");
    el.dataset.hoverId = "__back__";
    el.title = (i < nSpent ? "Mana engagé ce tour" : "Mana disponible") + ` — ${me.name}`;
    manaCards.appendChild(el);
  }
  $("#my-mana-count").textContent = `${Math.max(nMana - nSpent, 0)}/${nMana}`;

  // zones de dépôt mana / défausse
  const ph = detectPhase(game.state);
  const canMana = interactive && (ph === "init-mana" || ph === "mana-pass");
  const dzMana = $("#dz-mana"), dzDisc = $("#dz-discard");
  for (const dz of [dzMana, dzDisc]) {
    dz.ondragover = (e) => {
      if (!game.dragCardId) return;
      e.preventDefault();
      const ok = dz === dzMana ? canMana : true;   // défausser est toujours possible
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
        if (!canMana) { toast("On ne peut pas poser de mana maintenant"); return; }
        sendAction([id], "mana", "");
      } else {
        sendAction([id], "discard_pile", "");
      }
    };
  }

  // boutons d'action
  $("#btn-play").onclick = async () => {
    const ph2 = detectPhase(game.state);
    if (ph2 === "init-mana") {
      // le moteur n'accepte que 1 ou 3 cartes par message : on envoie une à la fois
      for (const id of [...game.selected]) {
        const resp = await sendAction([id], "mana", "");
        if (!resp || resp.success === false) break;   // refus -> on s'arrête, l'état est resynchronisé
      }
      game.selected.clear();
    } else if (ph2 === "mana-pass") {
      if (game.selected.size !== 1) return;
      sendAction([...game.selected], "mana", "");
    } else if (ph2 && ph2.kind === "play") {
      const id = [...game.selected][0];
      if (!id) return;
      if (playedCount(game.state, game.me) >= N_STOPOVERS) { toast(orderHint()); return; }
      // règle d'ordre : la carte part toujours sur la prochaine case (1, 2, 3, 4, 5)
      sendAction([id], `stopover_${nextSlotCol(game.state, game.me)}`, "move");
    }
  };

  $("#btn-pass").onclick = () => {
    if (!myTurn(game.state)) return;
    const ph3 = detectPhase(game.state);
    if (ph3 === "mana-pass") sendAction([], "", "pass");
    else if (ph3 && ph3.kind === "play") sendAction([], "", "pass");
  };
}

/* ---------------- rangées latérales : dwelling / pending / deck / défausse ---------------- */
function renderSideRows(me, oppo) {
  const side = (p, pref) => {
    // dwelling : 1 carte (quand implémentée)
    const dw = $(`#${pref}-dwelling`);
    dw.innerHTML = "";
    if (p.dwelling) dw.appendChild(makeStaticCard(p.dwelling, p.name));
    // pending : petites cartes
    const pe = $(`#${pref}-pendings`);
    pe.innerHTML = "";
    (p.pendings || []).slice(0, 5).forEach((id) => pe.appendChild(makeStaticCard(id, p.name)));
    // deck : compteur (public via deck_count même si les cartes sont cachées)
    const deckN = publicCount(p, "deck_count");
    $(`#${pref}-deck-count`).textContent = deckN ? deckN : "–";
    // défausse : dernière carte + compteur
    const di = $(`#${pref}-discard`);
    di.innerHTML = "";
    const last = (p.discard || []).slice(-1)[0];
    if (last) di.appendChild(makeStaticCard(last, p.name));
    $(`#${pref}-discard-count`).textContent = (p.discard || []).length;
  };
  side(me, "my");
  if (oppo) side(oppo, "oppo");
}

/* carte statique (non interactive) pour les panneaux latéraux et les stopovers */
function makeStaticCard(id, owner) {
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.hoverId = id;
  const c = CARDPOOL[id];
  el.title = c ? `${c.name} — ${c.faction}` : id;
  el.innerHTML = `<img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;
  return el;
}

/* ---------------- 3 cartes centrales de la Terre : jour/nuit • température • cataclysmes ---------------- */
function renderEnv(st) {
  $("#env-dn").classList.toggle("dn-night", st.day_night === "night");
  $("#env-temp-val").textContent = (st.temperature != null) ? st.temperature : "?";
}

// surligne la prochaine case de stopover (repli clic) quand une carte est sélectionnée
function highlightValidCells() {
  const st = game.state;
  if (!st || slotEls.me.length === 0) return;
  const me = st.players[game.me];
  // on ne surligne que si la carte sélectionnée est encore dans la main (évite un état périmé)
  const sel = [...(game.selected || [])].filter((id) => (me.hand || []).includes(id));
  for (let i = 0; i < N_STOPOVERS; i++) {
    slotEls.me[i].classList.toggle("slot-valid", sel.length && myTurn(st) && canDropOnStopover(sel[0], i));
  }
}

/* ---------------- board (plateau radial) ---------------- */
const BIOME_NAMES = { OC: "Océan", MO: "Montagne", DE: "Désert", JU: "Jungle" };
const N_CELLS = 24;        // 24 positions radiales (invisibles) autour de la Terre
const N_STOPOVERS = 5;     // 5 stopovers par rangée (2 rangées : adverse + joueur)
const slotEls = { me: [], oppo: [] };   // slotEls[row][col] -> element
const POS24 = [];          // index 0..23 -> {x: %, y: %} relatif à #board-stage

// rayon des pions (% de la taille du carré du plateau) : entre les traits radiaux,
// juste à l'extérieur du globe (rayon du globe ≈ 44 %)
const POS_RADIUS = 49;

function polar(angleDeg, radiusPct) {
  const a = (angleDeg * Math.PI) / 180;
  return { x: 50 + radiusPct * Math.cos(a), y: 50 + radiusPct * Math.sin(a) };
}

function buildBoard() {
  if (slotEls.me.length) return;

  // 2 rangées × 5 cases : rangée adverse (haut) + rangée du joueur (bas), numérotées 5..1
  for (const row of ["oppo", "me"]) {
    const rowEl = document.getElementById(`row-${row}`);
    const label = rowEl.querySelector(".row-label");
    for (let col = 0; col < N_STOPOVERS; col++) {
      const el = document.createElement("div");
      el.className = "slot";
      el.dataset.row = row;
      el.dataset.col = col;
      el.innerHTML = `<span class="slot-num">${N_STOPOVERS - col}</span>`;

      if (row === "me") {   // seule la rangée du joueur est une cible de dépôt
        el.title = `Stopover ${N_STOPOVERS - col} — dépose une carte ici pour la jouer`;
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
        // repli clic : carte sélectionnée + clic sur la case suivante -> jouer ici
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

  // 24 positions des pions : centrées ENTRE les traits radiaux (demi-pas de 7,5°)
  for (let i = 0; i < N_CELLS; i++) {
    POS24[i] = polar(-90 + (i + 0.5) * (360 / N_CELLS), POS_RADIUS);   // position 0 en haut, sens horaire
  }
}

/* -------- règle d'ordre : les stopovers se remplissent dans l'ordre 1, 2, 3, 4, 5 -------- */
// nombre de cartes déjà jouées ce tour (actions 'move' de la action_chain)
function playedCount(st, name) {
  const p = (st && st.players) ? st.players[name] : null;
  return ((p && p.action_chain) || []).filter((a) => a && a.mode === "move" && a.cards && a.cards.length).length;
}
// prochaine colonne (0..4) à remplir : 1ère carte -> stopover 1 (colonne 4, la plus à droite)
function nextSlotCol(st, name) {
  const n = Math.min(playedCount(st, name), N_STOPOVERS);
  return N_STOPOVERS - 1 - n;   // 4, 3, 2, 1, 0 -> stopovers 1, 2, 3, 4, 5
}

function canDropOnStopover(cardId, col) {
  const st = game.state;
  if (!st || !myTurn(st)) return false;
  const me = st.players[game.me];
  if (!cardId || !(me.hand || []).includes(cardId)) return false;
  if (playedCount(st, game.me) >= N_STOPOVERS) return false;      // 5 stopovers déjà pleins
  return col === nextSlotCol(st, game.me);   // uniquement la case suivante dans l'ordre
}

function playCardToStopover(cardId, col) {
  game.selected = new Set([cardId]);
  sendAction([cardId], `stopover_${col}`, "move");
}

// message d'aide si le joueur dépose sur une case non encore accessible
function orderHint() {
  const st = game.state;
  const n = playedCount(st, game.me);
  if (n >= N_STOPOVERS) return "Les 5 stopovers sont pleins pour ce tour.";
  return `Stopovers à remplir dans l'ordre : pose ta carte sur le stopover ${n + 1}.`;
}

function renderBoard(st, me, oppoName) {
  buildBoard();
  const earth = st.earth || [];

  // pions d'avancement des joueurs, entre les traits radiaux de la Terre (animés via transition CSS)
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
      // décalage pour éviter le chevauchement des deux pions sur la même position
      const shared = Object.entries(st.players).some(([n, q]) => n !== name && Math.min(q.current_position || 0, N_CELLS - 1) === pos);
      if (shared) el.style.marginTop = "-16px";
    }
    layer.appendChild(el);
  }

  // cartes jouées par chacun, posées dans SA rangée de stopovers
  // (récupérées depuis l'action_chain du tour : to = "stopover_X")
  for (const [name, p] of Object.entries(st.players)) {
    const row = (name === game.me) ? "me" : "oppo";
    for (let col = 0; col < N_STOPOVERS; col++) {
      const slot = slotEls[row][col];
      slot.querySelectorAll(".played").forEach((e) => e.remove());
    }
    for (const a of (p.action_chain || [])) {
      if (!a || a.mode !== "move" || !a.cards || !a.cards.length) continue;
      const m = /^stopover_(\d+)/.exec(a.to || "");
      const col = m ? (parseInt(m[1], 10) % N_STOPOVERS) : 0;
      const el = makeStaticCard(a.cards[0], name);
      el.classList.add("played");
      el.title = `Jouée par ${name}` + (el.title ? " — " + el.title : "");
      slotEls[row][col].appendChild(el);
    }
    // états visuels : cases pleines / prochaine case (ordre 1->5) / cases non encore accessibles
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
// carte de main (interactive) — utilisée dans le bandeau bas, de part et d'autre de la goutte mana
function makeHandCard(id, interactive, me) {
  const el = document.createElement("div");
  el.className = "card" + (game.selected.has(id) ? " selected" : "") + (!interactive ? " disabled" : "");
  el.dataset.hoverId = id;
  const c = CARDPOOL[id];
  // carte jouable : coût en mana ≤ mana disponible du tour -> contour vert/bleu
  const cost = (c && c.mana != null) ? c.mana : 0;
  const avail = (me && (me.mana || []).length) - ((me && me.mana_spend) || 0);
  if (interactive && cost <= avail) el.classList.add("playable");
  el.title = c ? `${c.name} — ${c.faction} (coût mana : ${cost})` : id;
  el.innerHTML = `<img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;

  // clic : sélection (1 carte pour jouer/mana-pass, jusqu'à 3 en init)
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

    // drag & drop natif
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

    // clic droit : zoom
    el.oncontextmenu = (e) => { e.preventDefault(); showCardModal(id); };
  } else {
    el.oncontextmenu = (e) => { e.preventDefault(); showCardModal(id); };
  }
  return el;
}

/* aperçu au survol : la carte s'affiche en 3x en bas à gauche (délégation, robuste aux re-rendus) */
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
  if (e.relatedTarget && el.contains(e.relatedTarget)) return;   // on reste sur la carte
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
  el._clearTimer = setTimeout(() => { el.textContent = ""; }, 3000);   // les succès s'effacent seuls
}

function showEndgame(st) {
  const winner = st.winner;
  const title = winner ? `🏆 ${winner} gagne !` : "🤝 Match nul";
  $("#endgame-title").textContent = title;
  $("#endgame-sub").textContent = st.state === "game over" && String(st.message?.message || "").startsWith("Deadlock")
    ? "Impasse — plus aucune carte jouable pour les deux joueurs."
    : `La course s'arrête au tour ${st.turn}.`;
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
  if (confirm("Quitter la partie ? L'état reste sauvegardé sur le serveur.")) leaveGame();
};

/* ------------------------------------------------------------------ boot */
(async function boot() {
  initSetup();
  try { await loadCardpool(); } catch (e) { toast(`Impossible de charger les cartes : ${e.message}`); }
})();
