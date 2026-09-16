/* ============================================================ GlobeRunners UI — actions (stopover ordering + play dispatch) */
"use strict";

import { toast } from "./utils.mjs";
import { isEngineerDrop, isEngineerDwelling } from "./cards.mjs";
import { myTurn } from "./phase.mjs";
import { game, enterCellSelect } from "./game.mjs";
import { sendAction } from "./comm.mjs";
import { checkMana } from "./state.mjs";
import { N_STOPOVERS } from "./board.mjs";

/* -------- ordering rule: stopovers are filled in the order 1, 2, 3, 4, 5 -------- */
// number of cards already played this turn ('move' OR 'defend' actions of the action_chain:
// a card placed on a stopover — even in defense — occupies the turn's slot)
export function playedCount(st, name) {
  const p = (st && st.players) ? st.players[name] : null;
  return ((p && p.action_chain) || []).filter((a) => a && (a.mode === "move" || a.mode === "defend") && a.cards && a.cards.length).length;
}
// next column (0..4) to fill: 1st card -> stopover 1 (column 4, the right-most)
export function nextSlotCol(st, name) {
  const n = Math.min(playedCount(st, name), N_STOPOVERS);
  return N_STOPOVERS - 1 - n;   // 4, 3, 2, 1, 0 -> stopovers 1, 2, 3, 4, 5
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
  if (playedCount(st, game.me) >= N_STOPOVERS) return false;      // all 5 stopovers already full
  return col === nextSlotCol(st, game.me);   // only the next slot in order
}

export function playCardToStopover(cardId, col) {
  game.selected = new Set([cardId]);
  dispatchPlay(cardId, col);
}

// help message if the player drops on a not-yet-accessible slot
export function orderHint() {
  const st = game.state;
  const n = playedCount(st, game.me);
  if (n >= N_STOPOVERS) return "All 5 stopovers are full this turn.";
  return `Stopovers must be filled in order: put your card on stopover ${n + 1}.`;
}

/* -------- play dispatch: the single "play this card" path --------
   Used by BOTH the "Play the card" button (col = null -> the next slot in order)
   and the stopover drop/click targets (col given). Handles the 3 special cases:
   engineer drop (pick a cell), engineer dwelling (refinery), normal move card. */
export function dispatchPlay(cardId, col = null) {
  // engineer drop: needs a target cell -> cell-selection mode
  if (isEngineerDrop(cardId)) { enterCellSelect(cardId); return; }
  // engineer dwelling (refinery): goes to the dwelling zone, not a stopover
  if (isEngineerDwelling(cardId)) {
    if (col == null) {
      const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
      if (meNow.dwelling) { toast("You already have a dwelling — the slot is full"); return; }
      if (playedCount(game.state, game.me) >= N_STOPOVERS) { toast(orderHint()); return; }
    }
    if (!checkMana(cardId)) return;
    sendAction([cardId], "dwelling", "");
    game.selected = new Set();
    return;
  }
  // normal move card onto the stopover (next slot in order when col is absent)
  if (col == null && playedCount(game.state, game.me) >= N_STOPOVERS) { toast(orderHint()); return; }
  if (!checkMana(cardId)) return;
  sendAction([cardId], `stopover_${col != null ? col : nextSlotCol(game.state, game.me)}`, "move");
}
