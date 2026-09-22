/* ============================================================ GlobeRunners UI — setup page */
"use strict";

import { $, $$, api, toast } from "./utils.mjs";
import { CARDPOOL, loadCardpool, loadSupportCards, cardImg, cardTitle, cardEl, FACTIONS, SUPPORT_FACS, buildSupportDeck, MAIN_DECK_SIZE, SUPPORT_DECK_SIZE, getFactionKey } from "./cards.mjs";
import { enterGame } from "./game.mjs";
import { startBgCards } from "./bg.mjs?v=5";
import { loadFactionThemes, applyFactionTheme } from "./theme.mjs";

/* ------------------------------------------------------------------ deck building */

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
export const setup = {
  deck: [],            // list of main card_ids (20)
  faction: null,       // starter faction key
  support: null,       // support faction key ('engineers' | 'mages' | 'doctors')
  supportDeck: [],     // list of support card_names (10)
  name: "",
  mode: "create",      // 'create' | 'ai' | 'join'
  gameId: null,        // set after create (or typed for join)
};

// theme the setup page with the current faction's colors (faction_themes.json);
// hovering a faction button PRE-VIEWS that palette, mouseleave restores the selection
function setSetupTheme(factionName) {
  applyFactionTheme($("#view-setup"), factionName, "me");
}
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
      setSetupTheme(f.name);
      renderFactions();
      renderDeckPreview();
      updateLaunchBtn();
    };
    btn.onmouseenter = () => setSetupTheme(f.name);   // preview this faction's palette
    btn.onmouseleave = () => {                          // restore the selected faction's palette
      const sel = FACTIONS.find((x) => x.key === setup.faction);
      setSetupTheme(sel ? sel.name : null);
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
    const el = cardEl(id);   // shared render site: hoverId → the delegated 3x hover preview (modals.mjs)
    el.style.cursor = "default";
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
    const el = cardEl(id);   // shared render site: hoverId → the delegated 3x hover preview (modals.mjs)
    el.style.cursor = "default";
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

export function initSetup() {
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
      if (!ids.length) { toast("No card found in this file"); return; }
      if (dupes.length) { toast(`File contains duplicate card(s) — a card can only appear once: ${[...new Set(dupes)].join(", ")}`); return; }
      setup.deck = picked;
      // theme the setup page with the LOADED deck's faction (the majority of the
      // cards — normally all 20 are the same) so the palette follows the file
      const counts = {};
      for (const id of picked) {
        const f = CARDPOOL[id] && CARDPOOL[id].faction;
        if (f) counts[f] = (counts[f] || 0) + 1;
      }
      let top = null, topN = 0;
      for (const [f, n] of Object.entries(counts)) if (n > topN) { top = f; topN = n; }
      if (top) {
        setSetupTheme(top);
        // keep the tracked selection in sync (hover-restore, name-input rebuild)
        const key = getFactionKey(top);
        if (key) setup.faction = key;
      }
      renderDeckPreview();
      updateLaunchBtn();
    } catch (err) {
      toast(`File read error: ${err.message}`);
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

/* ------------------------------------------------------------------ launch & waiting */

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
  btn.disabled = true; btn.textContent = "Starting…";
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
    errEl.textContent = `Error: ${err.message}`;
    btn.disabled = false; btn.textContent = "Start";
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
  btn.disabled = false; btn.textContent = "Start";
}

/* ------------------------------------------------------------------ boot */
export async function boot() {
  initSetup();
  const results = await Promise.allSettled([loadCardpool(), loadSupportCards(), loadFactionThemes()]);
  for (const r of results) if (r.status === "rejected") toast(`Cannot load the cards: ${r.reason.message}`);
  // re-render the support section now that the card data is available
  renderSupportFactions();
  if (setup.support) renderSupportPreview();
  // animated card background behind the setup page (params: background_cards_parameters.json)
  startBgCards();
}
