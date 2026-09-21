/* ============================================================ GlobeRunners UI — game state & entry */
/* The game-page global state, the engineer drop cell-selection mode, and enterGame(). */
"use strict";

import { $, toast } from "./utils.mjs";
import { ENGINEER_DROPS, isEngineerDrop } from "./cards.mjs";
import { sendAction, connectWs, startPolling } from "./comm.mjs";
import { checkMana } from "./state.mjs";
import { playedCount, nextSlotCol, orderHint, freeCols } from "./actions.mjs";
import { N_STOPOVERS } from "./board.mjs";
import { setup } from "./setup.mjs";

export const game = {
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

/* ---------------- cell-selection mode (engineer drop placement) ---------------- */
// When an engineer drop card is being played, the player must choose the earth cell the
// token lands on. The 24 cells are not DOM elements (the Earth is one image + positioned
// markers), so we overlay 24 invisible circular targets at the POS24 points, shown only
// while in this mode. Clicking one places the drop on that cell.
export const cellSelect = { active: false, cardId: null };

export function enterCellSelect(cardId) {
  if (!isEngineerDrop(cardId)) return;
  cellSelect.active = true;
  cellSelect.cardId = cardId;
  document.body.classList.add("cell-selecting");   // CSS shows the 24 cell targets
  const info = ENGINEER_DROPS[cardId];
  toast(`Choose a cell on the Earth for the ${cardId} (${info ? info.label : ""})`, 4200);
}

export function exitCellSelect() {
  if (!cellSelect.active) return;
  cellSelect.active = false;
  cellSelect.cardId = null;
  document.body.classList.remove("cell-selecting");
}

export function confirmCell(cellIndex) {
  const id = cellSelect.cardId;
  if (!id) return;
  exitCellSelect();
  const st = game.state;
  if (freeCols(st, game.me).length === 0) { toast(orderHint()); return; }
  if (!checkMana(id)) return;
  // the drop occupies the player's next position (move mode, per-player) + carries its target cell
  sendAction([id], `stopover_${nextSlotCol(st, game.me)}`, "move", [], cellIndex);
  game.selected = new Set();
}

export function enterGame() {
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
