/* ============================================================ GlobeRunners UI — board (radial Earth + stopovers) */
"use strict";

import { $, toast } from "./utils.mjs";
import { ENGINEER_DROPS, dwellingPlaceholderSrc } from "./cards.mjs";
import { myTurn } from "./phase.mjs";
import { game, cellSelect, confirmCell } from "./game.mjs";
import { canDropOnStopover, playCardToStopover, orderHint, actionStopoverNum, playedCount } from "./actions.mjs";
import { makeStaticCard } from "./zones.mjs";

export const BIOME_NAMES = { OC: "Ocean", MO: "Mountain", DE: "Desert", JU: "Jungle" };
export const N_CELLS = 24;        // 24 radial positions (invisible) around the Earth
export const N_STOPOVERS = 5;     // 5 stopovers per row (2 rows: opponent + player)
export const slotEls = { me: [], oppo: [] };   // slotEls[row][col] -> element
export const POS24 = [];          // index 0..23 -> {x: %, y: %} relative to #board-stage

// token radius (% of the board square size): between the radial lines,
// just outside the globe (globe radius ≈ 44%)
const POS_RADIUS = 49;

function polar(angleDeg, radiusPct) {
  const a = (angleDeg * Math.PI) / 180;
  return { x: 50 + radiusPct * Math.cos(a), y: 50 + radiusPct * Math.sin(a) };
}

export function buildBoard() {
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

/* ---------------- render the board from state ---------------- */
export function renderBoard(st, me, oppoName) {
  buildBoard();
  const earth = st.earth || [];

  // players' advance tokens, between the radial lines of the Earth.
  // The marker ELEMENTS are persisted across renders (repositioned, not recreated) so the
  // CSS left/top transition actually animates — the token visibly slides along the ring when
  // a player advances/recoils (re-creating them on every render would kill the transition).
  const layer = $("#markers-layer");
  const seen = new Set();
  for (const [name, p] of Object.entries(st.players)) {
    seen.add(name);
    let el = layer.querySelector(`.marker[data-player="${CSS.escape(name)}"]`);
    if (!el) {
      el = document.createElement("div");
      el.className = "marker";
      el.dataset.player = name;
      layer.appendChild(el);
    }
    el.classList.toggle("me", name === game.me);
    el.classList.toggle("oppo", name !== game.me);
    el.textContent = name.slice(0, 1).toUpperCase();
    const pos = Math.min(p.current_position || 0, N_CELLS - 1);
    const tokens = earth[pos] || [];
    const biome = tokens.find((t) => ["OC", "MO", "DE", "JU"].includes(t));
    el.title = `${name} — position ${p.current_position}` + (biome ? ` (${BIOME_NAMES[biome]})` : "");
    const pt = POS24[pos];
    if (pt) {
      el.style.left = pt.x + "%";
      el.style.top = pt.y + "%";
      // offset to avoid the two tokens overlapping on the same position
      const shared = Object.entries(st.players).some(([n, q]) => n !== name && Math.min(q.current_position || 0, N_CELLS - 1) === pos);
      el.style.marginTop = shared ? "-16px" : "";
    }
  }
  layer.querySelectorAll(".marker").forEach((m) => { if (!seen.has(m.dataset.player)) m.remove(); });

  // pet_trap drop tokens on the Earth (public board info: cell -> number of traps;
  //  the next token ARRIVING on that cell is knocked back by -count, then consumed).
  //  Each trap is drawn as its own marker in a slightly offset fan (deliberate
  //  overlap), so a stack of N traps reads as N tokens; the top one carries the
  //  exact count badge.
  const MAX_FAN = 6;                 // cap on visible tokens per cell (badge shows the true count)
  const FAN_STEP_PX = 5;             // per-token up-right offset
  const drops = st.drop_tokens || {};
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
      // :not(.chain-anim-card) — the trip-chain animation's clones survive
      // mid-run re-renders (they are removed by clearChainAnim / the run's end)
      slot.querySelectorAll(".played:not(.chain-anim-card)").forEach((e) => e.remove());
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
