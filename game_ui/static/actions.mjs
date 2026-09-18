/* ============================================================ GlobeRunners UI — actions (stopover ordering + play dispatch) */
"use strict";

import { toast } from "./utils.mjs";
import { isEngineerDrop, isEngineerDwelling, isDoctorPending, isDoctorDwelling, DOCTOR_PENDING } from "./cards.mjs";
import { myTurn } from "./phase.mjs";
import { game, enterCellSelect } from "./game.mjs";
import { sendAction } from "./comm.mjs";
import { checkMana } from "./state.mjs";
import { N_STOPOVERS } from "./board.mjs";

/* -------- ordering rule: PER-PLAYER stopovers (engine_version 15) --------
   Each player has their OWN 5 positions (1..5, columns 4,3,2,1,0). A player's ROOTED
   cards occupy the leading positions of that player's chain (position 1, 2, …), and
   the player's PLAYS (move OR defend) go AFTER the rooted cards (position R+1, …).
   A player's rooted cards NEVER shift the opponent's positions — the two players'
   position numbers are independent. The engine is the source of truth (it overwrites
   the client's stopover); these functions mirror the rule for DISPLAY + validation. */
// number of positions this player has consumed this turn: their plays ('move' OR
// 'defend' actions of the action_chain — a card placed on a stopover, even in
// defense, occupies a position) PLUS the dwelling placeholder if it was placed this
// turn (p.dwelling_slot is set at placement and cleared in the cleaning phase, so
// it is non-null only while the placeholder is showing). The engine increments
// play_count for BOTH the dwelling and each play, so the frontend must too —
// otherwise the next play lands on the same position as the dwelling placeholder.
export function playedCount(st, name) {
  const p = (st && st.players) ? st.players[name] : null;
  if (!p) return 0;
  let n = ((p.action_chain) || []).filter((a) => a && (a.mode === "move" || a.mode === "defend") && a.cards && a.cards.length).length;
  if (p.dwelling && p.dwelling_slot != null) n += 1;   // the refinery placeholder occupies one position
  // pending placeholders (doctors, v16): ONLY non-null slots occupy a position.
  // Entry shapes: engine_version 19+ = [card, slot] pair (the per-turn placeholder
  // list — NOT parallel to the persistent pendings zone); v18 = bare int or null
  // (the laboratory TAP's 'epo' had a null slot — no position). Count the slot part.
  if (p.pending_slots) n += p.pending_slots.filter((e) => (Array.isArray(e) ? e[1] : e) != null).length;
  return n;
}
// number of ROOTED cards this player has on the board (they occupy the leading
// positions of that player's chain, engine_version 15)
export function rootedCount(st, name) {
  const stb = (st && st.rooted_on_board) || [];
  return stb.filter((r) => r && r.owner === name).length;
}
// the player's next POSITION (1..N): rooted cards first, then plays (v15 per-player)
export function nextPosition(st, name) {
  return rootedCount(st, name) + playedCount(st, name) + 1;
}
// the player's next STOPOVER column (0..4) for their next play (v15 per-player):
// position p -> column 5-p (position 1 -> stopover_4, …, position 5 -> stopover_0)
export function nextSlotCol(st, name) {
  const pos = nextPosition(st, name);
  return N_STOPOVERS - pos;   // may be negative when the player is past position 5
}
// backward-compat: the set of columns this player can still fill (positions 1..5
// after their rooted cards + plays). Kept so board.mjs / game.mjs / zones.mjs
// (which import freeCols) keep working — it is now PER-PLAYER.
export function freeCols(st, name) {
  const me = (st && st.players) ? st.players[name] : null;
  const r = rootedCount(st, name);
  const played = playedCount(st, name);
  const out = [];
  for (let pos = 1; pos <= N_STOPOVERS; pos++) {
    if (pos <= r) continue;               // occupied by a rooted card
    if (pos <= r + played) continue;      // occupied by an earlier play this turn
    out.push(N_STOPOVERS - pos);          // position -> column
  }
  return out;
}
// (kept for board.mjs which imports it; per-player v15 has no shared occupancy)
export function occupiedCols(st, name) {
  const occ = new Set();
  const r = rootedCount(st, name);
  for (let pos = 1; pos <= Math.max(r, N_STOPOVERS); pos++) if (pos <= r) occ.add(N_STOPOVERS - pos);
  if (st && st.players && st.players[name]) {
    const p = st.players[name];
    if (p.dwelling && p.dwelling_slot != null) occ.add(p.dwelling_slot % N_STOPOVERS);
  }
  return occ;
}
// stopover number of an action: "stopover_3" -> 3 (the full number, not the last digit)
export function actionStopoverNum(a) {
  const m = /^stopover_(\d+)/.exec(a.to || "");
  return m ? parseInt(m[1], 10) : null;
}

export function canDropOnStopover(cardId, col) {
  const st = game.state;
  if (!st || !myTurn(st)) return false;
  const me = st.players[game.me];
  if (!cardId || !(me.hand || []).includes(cardId)) return false;
  if (freeCols(st, game.me).length === 0) return false;   // all 5 positions already taken
  return col === nextSlotCol(st, game.me);   // only the player's next position
}

export function playCardToStopover(cardId, col) {
  game.selected = new Set([cardId]);
  dispatchPlay(cardId, col);
}

// help message if the player drops on a not-yet-accessible slot (per-player v15)
export function orderHint() {
  const st = game.state;
  if (freeCols(st, game.me).length === 0) return "All 5 of your stopover positions are full this turn.";
  const pos = nextPosition(st, game.me);
  const col = N_STOPOVERS - pos;
  return `Your card goes on your own position ${pos} (stopover ${col + 1}). Rooted cards and earlier plays occupy the leading positions of YOUR chain — they never shift the opponent's positions.`;
}

/* -------- play dispatch: the single "play this card" path --------
   Used by BOTH the "Play the card" button (col = null -> the next slot in order)
   and the stopover drop/click targets (col given). Handles the special cases:
   engineer drop (pick a cell), engineer dwelling (refinery), doctor pending
   (pending zone), doctor dwelling (laboratory), normal move card (stopover,
   with pending card popup if applicable). */
export function dispatchPlay(cardId, col = null) {
  // engineer drop: needs a target cell -> cell-selection mode
  if (isEngineerDrop(cardId)) { enterCellSelect(cardId); return; }
  // engineer dwelling (refinery): goes to the dwelling zone, not a stopover
  if (isEngineerDwelling(cardId)) {
    if (col == null) {
      const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
      if (meNow.dwelling) { toast("You already have a dwelling — the slot is full"); return; }
      if (freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    }
    if (!checkMana(cardId)) return;
    sendAction([cardId], "dwelling", "");
    game.selected = new Set();
    return;
  }
  // doctor pending card (epo/virus/bloodtest/mercurochrome): goes to the pending zone
  if (isDoctorPending(cardId)) {
    if (col == null) {
      if (freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    }
    if (!checkMana(cardId)) return;
    sendAction([cardId], "pending_zone", "");
    game.selected = new Set();
    return;
  }
  // doctor dwelling (laboratory): goes to the dwelling zone, not a stopover
  if (isDoctorDwelling(cardId)) {
    if (col == null) {
      const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
      if (meNow.dwelling) { toast("You already have a dwelling — the slot is full"); return; }
      if (freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    }
    if (!checkMana(cardId)) return;
    sendAction([cardId], "dwelling", "");
    game.selected = new Set();
    return;
  }
  // normal move card onto the stopover (the player's next position when col is absent)
  if (col == null && freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
  if (!checkMana(cardId)) return;
  const slotCol = col != null ? col : nextSlotCol(game.state, game.me);
  // Doctors (engine_version 16): if the player has pending cards, show a popup
  // to attach one (or none) to the main card before sending the action.
  const meP = (game.state && game.state.players) ? game.state.players[game.me] : null;
  const myPendings = (meP && meP.pendings) || [];
  if (myPendings.length > 0) {
    showPendingPopup(cardId, slotCol, myPendings);
  } else {
    sendAction([cardId], `stopover_${slotCol}`, "move");
  }
}

/* popup: choose a pending card to attach to the main card (or none) */
function showPendingPopup(cardId, col, pendings) {
  const modal = document.createElement("div");
  modal.className = "modal pending-popup";
  const options = ['<button class="pending-opt" data-pending="">⏭ None</button>'];
  for (const p of pendings) {
    const label = DOCTOR_PENDING[p] ? DOCTOR_PENDING[p].label : p;
    options.push(`<button class="pending-opt" data-pending="${p}">📎 ${label}</button>`);
  }
  modal.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-box">
      <h3>Attach a pending card?</h3>
      <p class="hint">Choose a pending card to attach to <b>${cardId}</b>, or skip.</p>
      <div class="pending-options">${options.join("")}</div>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelectorAll(".pending-opt").forEach(btn => {
    btn.onclick = () => {
      const pending = btn.dataset.pending;
      const pendingsArr = pending ? [pending] : [];
      sendAction([cardId], `stopover_${col}`, "move", pendingsArr);
      game.selected = new Set();
      modal.remove();
    };
  });
  modal.querySelector(".modal-backdrop").onclick = () => modal.remove();
}
