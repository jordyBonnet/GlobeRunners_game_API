/* ============================================================ GlobeRunners UI — setup page */
"use strict";

import { $, $$, api, toast } from "./utils.mjs";
import { CARDPOOL, loadCardpool, loadSupportCards, cardImg, cardTitle, cardEl, FACTIONS, SUPPORT_FACS, buildSupportDeck, MAIN_DECK_SIZE, SUPPORT_DECK_SIZE, getFactionKey, checkMainDeck, condFamily } from "./cards.mjs";
import { enterGame } from "./game.mjs";
import { startBgCards } from "./bg.mjs?v=5";
import { loadFactionThemes, applyFactionTheme, factionTheme, THEME_DEFAULTS } from "./theme.mjs";

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
  // weighted draw without replacement — the starter deck respects the deck rules
  // too (single-copy + max 5 per condition family / effect, see checkMainDeck)
  const deck = [];
  const cc = {}; const ec = {};
  const bag = [...weighted];
  while (deck.length < MAIN_DECK_SIZE && bag.length) {
    const idx = Math.floor(rand() * bag.length);
    const card = bag.splice(idx, 1)[0];
    if (deck.includes(card.card_id)) continue;
    const cf = condFamily(card.condition);
    if (cf && (cc[cf] || 0) >= 5) continue;
    if (card.effect && (ec[card.effect] || 0) >= 5) continue;
    if (cf) cc[cf] = (cc[cf] || 0) + 1;
    if (card.effect) ec[card.effect] = (ec[card.effect] || 0) + 1;
    deck.push(card.card_id);
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

/* ---------------- faction tooltips (setup page) ----------------
   A short role description on every main + support faction button, revealed
   on hover. The tooltip background follows the faction's OWN color:
   main factions read faction_themes.json (re-painted once the async JSON
   lands), support factions use their banner colors (SUPPORT_TIP_COLORS). */
const FACTION_TIPS = {
  Dwarves: "High shield, high mana curve (2–5): the natural blocker — the only faction rewarded for blocking, with Unstoppable cards. Signature: Avalanche (sends tokens back to the biome start).",
  Demons: "Control & attrition on a low curve (1–4): pushes the opponent back and makes them discard, but the low shield makes full blocks hard. Signature: Effect cancelled.",
  Twigs: "Speed through generosity, low curve (1–4): making the opponent draw or ramp is what makes you advance fast. Signature: Rooted (the card stays on the board one more turn).",
  Miaous: "The deck-builder's faction: access to every condition/effect combo, but little raw advancing — choose your combinations carefully. Signature: Pet trap.",
  Orcs: "Speed through sacrifice: discard mana or hand cards to accelerate. High curve (2–5), low defense. Signature: Unstoppable — and their Swap Cards keep plans unpredictable.",
  Mummies: "Attrition & control, low curve (1–4): the only faction to exploit the opponent's current state, and their blocks actively penalize the opponent. Signature: Copy effect.",
  Engineers: "Instant drops placed onto the map — they can affect BOTH players, so place them carefully. Dwelling: the refinery (tap once per turn to draw a card).",
  Doctors: "Stack cards in the pending zone, then attach them later to a main card to add their effect to that play. Dwelling: the laboratory (tap to gain an epo pending).",
  Mages: "Masters of nature: change the planet temperature, fix day/night, choose the cataclysm order, and rotate the Earth to keep their preferred biome.",
};
/* banner colors for the 3 support factions (no faction_themes.json entry) */
const SUPPORT_TIP_COLORS = {
  engineers: { bg: "#a36244", fg: "#fdf1e7" },
  mages:     { bg: "#3d9df0", fg: "#f2f9ff" },
  doctors:   { bg: "#e8555f", fg: "#fff0f0" },
};
function addFactionTip(btn, name, supportKey = null) {
  const tip = document.createElement("span");
  tip.className = "faction-tip";
  tip.textContent = FACTION_TIPS[name] || "";
  const paint = () => {
    const c = supportKey
      ? SUPPORT_TIP_COLORS[supportKey]
      : { bg: (factionTheme(name) || THEME_DEFAULTS).accent, fg: (factionTheme(name) || THEME_DEFAULTS).onAccent };
    tip.style.setProperty("--tip-bg", c.bg);
    tip.style.color = c.fg;
  };
  paint();
  if (!supportKey) loadFactionThemes().then(paint).catch(() => {});   // re-paint once the themes JSON lands
  btn.appendChild(tip);
}

function renderFactions() {
  const grid = $("#faction-grid");
  grid.innerHTML = "";
  for (const f of FACTIONS) {
    const btn = document.createElement("button");
    btn.className = "faction-btn" + (setup.faction === f.key ? " selected" : "");
    btn.innerHTML = `<img src="${f.logo}" alt=""> <span>${f.name}</span>`;
    addFactionTip(btn, f.name);
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
  if (!setup.deck.length) {
    box.classList.add("hidden"); title.classList.add("hidden");
    renderDeckIssues();
    return;
  }
  box.classList.remove("hidden"); title.classList.remove("hidden");
  $("#deck-count").textContent = setup.deck.length;
  box.innerHTML = "";
  for (const id of setup.deck) {
    const el = cardEl(id);   // shared render site: hoverId → the delegated 3x hover preview (modals.mjs)
    el.style.cursor = "default";
    box.appendChild(el);
  }
  renderDeckIssues();
}

// the deck-rules verdict (checkMainDeck — mirror of deck_rules.py): a green ✓ when
// the deck is valid, a red list of violations otherwise (the server re-checks and
// is the hard gate at launch)
function renderDeckIssues() {
  const el = $("#deck-issues");
  if (!el) return;
  if (!setup.deck.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  const problems = checkMainDeck(setup.deck);
  el.classList.remove("hidden");
  el.classList.toggle("deck-issues-ok", problems.length === 0);
  if (!problems.length) {
    el.innerHTML = '<div class="deck-issues-line ok">✓ Deck is valid — it follows the deck rules (single faction, single-copy, max 5 per condition/effect).</div>';
  } else {
    el.innerHTML = problems.map((p) => `<div class="deck-issues-line bad">✗ ${p}</div>`).join("");
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
    addFactionTip(btn, f.name, f.key);
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
  // WHY is Start disabled? — a vibrant-red list rendered just ABOVE the button
  // (one ✗ line per missing piece), so the user sees exactly what to fix.
  const blockers = [];
  // order: 1. main faction  2. support faction  3. nickname
  if (!setup.deck.length) blockers.push("Select a main faction");
  else {
    const problems = checkMainDeck(setup.deck);
    if (problems.length) blockers.push(`Main deck invalid — ${problems.length} deck-rule violation(s), see the ✗ list above`);
  }
  if (setup.supportDeck.length !== SUPPORT_DECK_SIZE) blockers.push("Select a support faction");
  if (setup.name.trim().length < 2) blockers.push("Write your nickname");
  if (setup.mode === "join" && !$("#join-game-id").value.trim()) blockers.push("Write the game ID to join");

  const el = $("#launch-blockers");
  if (el) {
    el.classList.toggle("hidden", blockers.length === 0);
    el.innerHTML = blockers.map((b) => `<div class="launch-blocker">✗ ${b}</div>`).join("");
  }
  $("#btn-launch").disabled = blockers.length > 0;
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
      if (!ids.length) { toast("No card found in this file"); return; }
      const picked = ids.slice(0, MAIN_DECK_SIZE);
      $("#csv-filename").textContent = `${file.name} — ${ids.length} card(s)` + (ids.length > MAIN_DECK_SIZE ? ` (first ${MAIN_DECK_SIZE} used)` : "");
      setup.deck = picked;   // rule violations (duplicates, unknowns, max-5, …) show in the issues panel below the preview
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
  updateLaunchBtn();   // paint the "why Start is disabled" list on load
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
  const deckProblems = checkMainDeck(setup.deck);
  if (deckProblems.length) { errEl.textContent = deckProblems.join(" • "); return; }
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
