/* GlobeRunners - game analysis (frontend) */

const $ = (sel) => document.querySelector(sel);
const content = $("#content");
const gameSelect = $("#gameSelect");

const PLAYER_COLORS = ["var(--p1)", "var(--p2)"];
const WIN_POS = 24; // finishing position (winners remain displayed at 23)

// where a game was read from (see app.py: games.db + the HF backup dataset)
const SOURCE_BADGE = {
  db: `<span class="badge" title="read from the local games.db">💾 local db</span>`,
  hf: `<span class="badge dataset" title="only in the Hugging Face backup dataset (a game played on the hosted deployment)">☁️ HF dataset</span>`,
  both: `<span class="badge" title="read from the local games.db, also backed up on the Hugging Face dataset">💾 local db ☁️ backed up</span>`,
};
const BIOME_NAMES = { OC: "Ocean", MO: "Mountain", DE: "Desert", JU: "Jungle" };

// ---- source filter (the 💾 Local / ☁️ HF tabs next to the game selector) ----
// Client-side view over the ONE merged list `/api/games` (no refetch per click);
// the same values exist server-side as `/api/games?filter=` — tests/_analysis_hf_source.py
// checks that both agree. "local" = games.db holds it, "hf" = the dataset holds it,
// so a game backed up AND stored locally shows in both tabs.
const FILTERS = {
  all: { label: "All", keep: ["db", "both", "hf"], empty: "No game found (local games.db nor the HF dataset)" },
  local: { label: "💾 Local", keep: ["db", "both"], empty: "No game in the local games.db" },
  hf: { label: "☁️ HF", keep: ["hf", "both"], empty: "No game in the HF dataset (unreachable? see the status line above)" },
};
let ALL_GAMES = [];                       // merged list of both sources
// the ?filter= deep-link is honoured on load
let FILTER = (() => {
  const f = new URLSearchParams(location.search).get("filter");
  return FILTERS[f] ? f : "all";
})();

function visibleGames() {
  const keep = FILTERS[FILTER].keep;
  return ALL_GAMES.filter((g) => keep.includes(g.source));
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function playerColor(players, name) {
  const i = players.indexOf(name);
  return PLAYER_COLORS[i >= 0 ? i % 2 : 0];
}

function cardImg(card, sizeClass = "card-img") {
  if (!card || !card.img) return `<img class="${sizeClass}" alt="?" />`;
  // never display a card id: image + name only
  return `<img class="${sizeClass} card-hoverable" src="${esc(card.img)}" alt="${esc(card.name)}" data-img="${esc(card.img)}" data-name="${esc(card.name)}" loading="lazy" onerror="this.style.opacity=.25" />`;
}

function cardBack() {
  // card back: unknown identity (drawn card, random deck order)
  return `<div class="card-back card-hoverable" title="Drawn card (unknown identity)">?</div>`;
}

function cardBlock(card) {
  if (!card || !card.img) return null;
  return `
    <div class="card-info">
      <div class="card-name">${esc(card.name)}</div>
      ${card.faction ? `<div class="card-faction">${esc(card.faction)}</div>` : ""}
    </div>`;
}

function deltaHtml(delta) {
  if (delta == null) return "";
  const cls = delta > 0 ? "pos" : delta < 0 ? "neg" : "zero";
  const sign = delta > 0 ? "+" : "";
  return `<span class="delta ${cls}">${sign}${delta}</span>`;
}

function actionCellHtml(name, act, players) {
  if (!act || (act.error && !act.card)) {
    return `<div class="action-cell empty">—</div>`;
  }
  const isDefend = act.mode === "defend";
  const met = act.condition_met;
  const defendTag = isDefend
    ? `<span class="cond-tag cond-defend" title="played in defense mode (card engaged at 90°)">🛡 defense</span>`
    : "";
  const condTag = act.blocked
    ? `<span class="cond-tag cond-blocked" title="blocked by the opponent's defense card(s) on the same stopover">🛡 blocked</span>`
    : act.cancelled
      ? `<span class="cond-tag cond-blocked" title="canceled (a landmine block, or the opponent's effect_canceled card)">⚡ canceled</span>`
      : met
        ? `<span class="cond-tag cond-met" title="condition met">✓ condition</span>`
        : `<span class="cond-tag cond-notmet" title="condition not met">✗ condition</span>`;
  const posLine = act.blocked
    ? `blocked at cell ${act.pos_before ?? "?"} — no effect, no advancing`
    : act.cancelled
      ? `canceled at cell ${act.pos_before ?? "?"} — no effect, no advancing`
      : `cell ${act.pos_before ?? "?"} → <b>${act.pos_after ?? "?"}</b>`;
  // defense card: image (card + frame) engaged at 90° in a landscape box
  const imgHtml = isDefend
    ? `<span class="card-defend-wrap" title="Card played in defense (90°)">${cardImg(act.card, "card-defend")}</span>`
    : cardImg(act.card);
  return `
    <div class="action-cell ${players.indexOf(name) === 0 ? "p1" : "p2"}${isDefend ? " cell-defend" : ""}">
      ${imgHtml}
      ${cardBlock(act.card)}
      <div style="flex:1;min-width:0">
        <div class="effect-text">${esc(act.effect_text || "")} ${defendTag}${condTag}</div>
        <div style="font-size:12px;color:var(--muted);margin-top:4px">
          ${posLine}
        </div>
      </div>
      ${act.blocked ? "" : deltaHtml(act.delta)}
    </div>`;
}

function posTrack(pos, color) {
  const pct = Math.max(0, Math.min(100, (pos / WIN_POS) * 100));
  return `<span class="pos-track"><span class="pos-fill" style="width:${pct}%;background:${color}"></span></span>`;
}

function renderTurn(t, players) {
  const [n1, n2] = players;
  const c1 = playerColor(players, n1);
  const c2 = playerColor(players, n2);

  // mana put (turn setup) - list of cards (3 on turn 1, 1 afterwards)
  let manaHtml = "";
  const mp = t.mana_puts || {};
  if (Object.values(mp).some((c) => Array.isArray(c) && c.length)) {
    manaHtml = `<div class="mana-row">
      <span style="color:var(--muted)">Mana put:</span>
      ${players.map((n) => {
        const cards = mp[n] || [];
        return `<span class="mana-item"><span class="dot" style="background:${playerColor(players, n)}"></span>${esc(n)} : ${
          cards.length ? cards.map((c) => cardImg(c)).join("") : "<i>pass</i>"
        }</span>`;
      }).join("")}
    </div>`;
  }

  // starting hand of the turn (before the cards are played)
  const hs = t.hands_start || {};
  let handsHtml = "";
  if (Object.values(hs).some((h) => h && ((h.cards || []).length || (h.unknown_count || 0)))) {
    handsHtml = `<div class="hands-row">
      ${players.map((n) => {
        const h = hs[n] || { cards: [], unknown_count: 0 };
        const backs = Array.from({ length: h.unknown_count || 0 }, () => cardBack()).join("");
        return `<span class="hand-item"><span class="dot" style="background:${playerColor(players, n)}"></span>${esc(n)} : ${
          (h.cards || []).map((c) => cardImg(c, "card-img hand-card")).join("") + backs
        }</span>`;
      }).join("")}
    </div>`;
  }

  // board / dwelling events (engineers drops, dwelling place / tap) - play-time
  // actions that are NOT part of the trip chain
  const evs = t.events || [];
  let eventsHtml = "";
  if (evs.length) {
    const biome = (c) => BIOME_NAMES[c] || c;
    const nm = (e) => (e.card && e.card.name ? e.card.name : "?");
    const EV_LABEL = {
      drop_place: (e) => `dropped a ${nm(e)} token on cell ${e.cell ?? "?"}`,
      dwelling_place: (e) => `placed the ${nm(e)} dwelling on the board`,
      dwelling_tap: (e) => `tapped the ${nm(e)} (drew a card)`,
      laboratory_tap: (e) => `tapped the laboratory (added a ${nm(e)} pending card)`,
      black_hole_tap: (e) => `tapped the black_hole — earth rotated ${e.rotation === "ccw" ? "3 cells CCW" : "3 cells CW"}`,
      pending_place: (e) => `placed the ${nm(e)} pending card in the pending zone`,
      celestial_reversal: (e) => `fixed day/night to ${e.day_night === "night" ? "🌙 night" : "☀️ day"} for the rest of the game`,
      thermic_flux: (e) => `changed the planet temperature ${e.from ?? "?"} → ${e.to ?? "?"} °C`,
      nobodymoves: (e) => `locked every player's movement for the rest of the turn`,
      apocalypticritual: (e) => `set the cataclysm order: ${(e.order || []).map(biome).join(" → ")}`,
      swap_cards: (e) => `swapped with the ${e.kind || "?"} at position ${e.swap_with ?? "?"}`,
    };
    eventsHtml = `<div class="events-row">` + evs.map((e) => {
      const label = (EV_LABEL[e.type] || (() => e.type))(e);
      const art = e.card && e.card.img
        ? `<img class="event-card" src="${esc(e.card.img)}" alt="${esc(e.card.name || "")}" onerror="this.style.display='none'">`
        : "";
      return `<span class="event-chip"><span class="dot" style="background:${playerColor(players, e.player)}"></span>${art}${esc(e.player)} ${esc(label)}</span>`;
    }).join("") + `</div>`;
  }

  // parallel actions (index by index)
  const rows = (t.actions || []).map((act) => `
    <div class="action-row">
      ${actionCellHtml(n1, act[n1], players)}
      <div class="vs">VS</div>
      ${actionCellHtml(n2, act[n2], players)}
    </div>`).join("");

  const pb = t.positions_before || {};
  const pa = t.positions_after || {};

  return `
    <details class="turn" id="turn-${t.turn}">
      <summary>
        <span class="chev">▶</span>
        <span class="turn-num">Turn ${t.turn}</span>
        <span class="dn">${t.day_night === "day" ? "☀️ day" : "🌙 night"} · first: ${esc(t.order[0])}</span>
        <span class="pos-summary">
          <span class="p"><span class="dot" style="background:${c1}"></span>${esc(n1)}: ${pb[n1] ?? "?"} → <b>${pa[n1] ?? "?"}</b></span>
          <span class="p"><span class="dot" style="background:${c2}"></span>${esc(n2)}: ${pb[n2] ?? "?"} → <b>${pa[n2] ?? "?"}</b></span>
        </span>
      </summary>
      <div class="turn-body">
        ${manaHtml}
        ${handsHtml}
        ${eventsHtml}
        ${rows || `<div style="color:var(--muted);font-size:13px">No card played this turn.</div>`}
        <div class="final-pos">
          <span class="p"><span class="dot" style="background:${c1}"></span>${esc(n1)}: cell <b>${pa[n1] ?? "?"}</b> ${posTrack(pa[n1] ?? 0, c1)}</span>
          <span class="p"><span class="dot" style="background:${c2}"></span>${esc(n2)}: cell <b>${pa[n2] ?? "?"}</b> ${posTrack(pa[n2] ?? 0, c2)}</span>
        </div>
      </div>
    </details>`;
}

function renderGame(g) {
  const players = g.players || [];
  const winnerBadge = g.winner
    ? `<span class="badge winner">🏆 Winner: ${esc(g.winner)}</span>`
    : `<span class="badge">Tie</span>`;
  const sourceBadge = SOURCE_BADGE[g.source] || "";
  const verifBadge = g.verified
    ? `<span class="badge verified" title="the replay reproduces the stored final state exactly">✓ replay verified</span>`
    : `<span class="badge unverified" title="slight drift from the stored final state (engine version)">⚠ approximate replay</span>`;

  const ended = g.ended || {};
  let endText = "";
  if (ended.type === "finish") endText = `Ended at turn ${ended.turn} — reached the finish line.`;
  else if (ended.type === "deadlock") endText = `Ended at turn ${ended.turn} — deadlock (no playable card left).`;

  const warnings = (g.warnings || []).length
    ? `<div class="warnings">⚠ Replay notes:<ul>${g.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`
    : "";

  return `
    <section class="game-head">
      <h1>Game ${esc(g.date_label)} ${winnerBadge} ${sourceBadge} ${verifBadge}</h1>
      <div class="meta-row">
        <span>Players: <b>${players.map(esc).join(" vs ")}</b></span>
        <span>Planet temperature: <b>${g.temperature ?? "?"}</b></span>
        ${endText ? `<span>${esc(endText)}</span>` : ""}
      </div>
      ${warnings}
    </section>

    <div class="legend">
      ${players.map((n) => `<span class="chip"><span class="dot" style="background:${playerColor(players, n)}"></span>${esc(n)}</span>`).join("")}
    </div>

    ${(g.turns || []).map((t) => renderTurn(t, players)).join("")}

    <footer class="foot">Positions after each trip-chain resolution phase · finish at cell ${WIN_POS}</footer>`;
}

async function loadGames(refresh = false) {
  try {
    const [gamesRes, statusRes] = await Promise.allSettled([
      fetch(`/api/games${refresh ? "?refresh=1" : ""}`).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status)))),
      fetch(`/api/hf_status${refresh ? "?refresh=1" : ""}`).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status)))),
    ]);
    if (gamesRes.status !== "fulfilled") throw gamesRes.reason;
    ALL_GAMES = gamesRes.value;
    renderSrcStatus(statusRes.status === "fulfilled" ? statusRes.value : { enabled: false, error: "status unavailable" });
    // deep-link: ?game=<id> (or #<id>) selects a specific game, else the first one
    const params = new URLSearchParams(location.search);
    const wanted = params.get("game") || location.hash.replace("#", "");
    await showGames(wanted);
  } catch (e) {
    content.innerHTML = `<p class="error">Loading error: ${esc(e.message)}</p>`;
  }
}

// Fill the selector from ALL_GAMES according to the active tab, then analyze one
// game (`want`, when the current tab holds it) or the newest visible one.
async function showGames(want) {
  const games = visibleGames();
  renderFilterButtons();
  gameSelect.innerHTML = "";
  for (const g of games) {
    const opt = document.createElement("option");
    opt.value = g.id;
    // ☁️ = only in the HF backup dataset (a game played on the hosted deployment)
    opt.textContent = `${g.source === "hf" ? "☁️ " : ""}${g.label}`;
    opt.title = g.source === "hf" ? "Hugging Face backup dataset (not in local games.db)" : g.source;
    gameSelect.appendChild(opt);
  }
  if (!games.length) {
    content.innerHTML = `<p class="error">${esc(FILTERS[FILTER].empty)}</p>`;
    return;
  }
  const target = (want && games.some((g) => g.id === want)) ? want : games[0].id;
  gameSelect.value = target;
  await selectGame(target);
}

// The tab buttons, with a live count per source
function renderFilterButtons() {
  const box = $("#srcFilter");
  if (!box) return;
  for (const btn of box.querySelectorAll("button")) {
    const f = btn.dataset.filter;
    const n = ALL_GAMES.filter((g) => FILTERS[f].keep.includes(g.source)).length;
    btn.textContent = `${FILTERS[f].label} (${n})`;
    btn.classList.toggle("active", f === FILTER);
    btn.setAttribute("aria-pressed", f === FILTER ? "true" : "false");
  }
}

function setFilter(f) {
  if (!FILTERS[f] || f === FILTER) return;
  FILTER = f;
  const keep = new URLSearchParams(location.search).get("game") || "";
  history.replaceState(null, "", `?filter=${f}${keep ? `&game=${encodeURIComponent(keep)}` : ""}`);
  showGames(keep);
}

async function selectGame(id) {
  content.innerHTML = `<p class="loading">Analyzing the game…</p>`;
  try {
    const res = await fetch(`/api/game/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(res.status);
    const g = await res.json();
    content.innerHTML = renderGame(g);
    if (new URLSearchParams(location.search).has("expand")) {
      document.querySelectorAll("details.turn").forEach((d) => (d.open = true));
    }
    if (location.search !== `?game=${encodeURIComponent(id)}`) {
      const f = FILTER === "all" ? "" : `&filter=${FILTER}`;
      history.replaceState(null, "", `?game=${encodeURIComponent(id)}${f}`);
    }
  } catch (e) {
    content.innerHTML = `<p class="error">Error: ${esc(e.message)}</p>`;
  }
}

gameSelect.addEventListener("change", () => selectGame(gameSelect.value));

// the 💾 Local / ☁️ HF tabs
$("#srcFilter")?.querySelectorAll("button").forEach((btn) => {
  btn.addEventListener("click", () => setFilter(btn.dataset.filter));
});

// ---- data-source status line (games.db + the Hugging Face backup dataset) ----
function renderSrcStatus(s) {
  const box = $("#srcStatus");
  if (!box) return;
  const refreshBtn = `<button id="refreshSrc" class="mini" title="Re-read the dataset file listing">↻ dataset</button>`;
  if (!s.enabled) {
    box.innerHTML = `<span class="src local">💾 source: local games.db only</span>`
      + `<span class="src muted">☁️ HF dataset source disabled${s.error ? ` (${esc(s.error)})` : ""}</span>`
      + refreshBtn;
  } else if (s.error && !s.listed) {
    box.innerHTML = `<span class="src local">💾 source: local games.db</span>`
      + `<span class="src bad" title="${esc(s.error)}">⚠️ HF dataset unreachable — ${esc(s.error)}${s.n_mirrored ? ` (serving the ${s.n_mirrored} mirrored game(s) only)` : ""}</span>`
      + refreshBtn;
  } else {
    const only = Math.max(0, (s.n_remote || 0) - (s.n_in_db || 0));
    box.innerHTML = `<span class="src local">💾 local games.db: ${esc(s.n_local ?? "?")}</span>`
      + `<a class="src cloud" href="${esc(s.url || "#")}" target="_blank" rel="noopener"` 
      + ` title="${esc(s.repo || "")} — open the dataset">☁️ HF dataset: ${s.n_remote ?? "?"} game(s)`
      + `${only ? `, ${only} not in local db` : ""} · ${s.n_mirrored ?? 0} mirrored</a>`
      + (s.error ? `<span class="src bad" title="${esc(s.error)}">⚠️ stale listing</span>` : "")
      + refreshBtn;
  }
  const btn = $("#refreshSrc");
  if (btn) btn.addEventListener("click", () => {
    btn.disabled = true;
    loadGames(true).finally(() => { btn.disabled = false; });
  });
}

$("#expandAll").addEventListener("click", () => {
  document.querySelectorAll("details.turn").forEach((d) => (d.open = true));
});
$("#collapseAll").addEventListener("click", () => {
  document.querySelectorAll("details.turn").forEach((d) => (d.open = false));
});

// ---- big card tooltip, bottom left of the screen ----
const hoverBox = document.createElement("div");
hoverBox.id = "cardHover";
document.body.appendChild(hoverBox);
let hoverTimer = null;

document.addEventListener("mouseover", (e) => {
  const el = e.target.closest(".card-hoverable");
  if (!el || !el.dataset.img) return;
  clearTimeout(hoverTimer);
  hoverBox.innerHTML = `<img src="${esc(el.dataset.img)}" alt="" /><div class="hover-name">${esc(el.dataset.name || "")}</div>`;
  hoverBox.classList.add("show");
});
document.addEventListener("mouseout", (e) => {
  const el = e.target.closest(".card-hoverable");
  if (!el) return;
  // only hide when leaving toward a non-card element
  const to = e.relatedTarget && e.relatedTarget.closest ? e.relatedTarget.closest(".card-hoverable") : null;
  if (to === el) return;
  hoverTimer = setTimeout(() => hoverBox.classList.remove("show"), 120);
});

loadGames();
