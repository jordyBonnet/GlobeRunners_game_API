/* GlobeRunners - game analysis (frontend) */

const $ = (sel) => document.querySelector(sel);
const content = $("#content");
const gameSelect = $("#gameSelect");

const PLAYER_COLORS = ["var(--p1)", "var(--p2)"];
const WIN_POS = 24; // finishing position (winners remain displayed at 23)

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
    : met
      ? `<span class="cond-tag cond-met" title="condition met">✓ condition</span>`
      : `<span class="cond-tag cond-notmet" title="condition not met">✗ condition</span>`;
  const posLine = act.blocked
    ? `blocked at cell ${act.pos_before ?? "?"} — no effect, no advancing`
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
      <h1>Game ${esc(g.date_label)} ${winnerBadge} ${verifBadge}</h1>
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

async function loadGames() {
  try {
    const res = await fetch("/api/games");
    if (!res.ok) throw new Error(res.status);
    const games = await res.json();
    gameSelect.innerHTML = "";
    for (const g of games) {
      const opt = document.createElement("option");
      opt.value = g.id;
      opt.textContent = g.label;
      gameSelect.appendChild(opt);
    }
    if (!games.length) {
      content.innerHTML = `<p class="error">No game found in games.db</p>`;
      return;
    }
    await selectGame(games[0].id);
  } catch (e) {
    content.innerHTML = `<p class="error">Loading error: ${esc(e.message)}</p>`;
  }
}

async function selectGame(id) {
  content.innerHTML = `<p class="loading">Analyzing the game…</p>`;
  try {
    const res = await fetch(`/api/game/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(res.status);
    const g = await res.json();
    content.innerHTML = renderGame(g);
  } catch (e) {
    content.innerHTML = `<p class="error">Error: ${esc(e.message)}</p>`;
  }
}

gameSelect.addEventListener("change", () => selectGame(gameSelect.value));

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
