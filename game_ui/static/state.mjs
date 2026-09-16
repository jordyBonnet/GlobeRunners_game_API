/* ============================================================ GlobeRunners UI — state application (renderAll) */
/* The single render funnel: applyState() (comm.mjs) calls renderAll() on every
   new state; renderAll rewrites the topbar, phase label, buttons, zones, board
   and log from scratch (the DOM is a function of game.state). */
"use strict";

import { $, toast } from "./utils.mjs";
import { cardCost, cardName, isEngineerDrop, isEngineerDwelling, isSupportPlay } from "./cards.mjs";
import { detectPhase, myTurn, START_MANA_N } from "./phase.mjs";
import { game, cellSelect } from "./game.mjs";
import { renderOppZone, renderMyZone, renderSideRows } from "./zones.mjs";
import { renderBoard, slotEls, N_STOPOVERS } from "./board.mjs";
import { renderLog } from "./log.mjs";
import { canDropOnStopover } from "./actions.mjs";

// client-side guard: card cost vs available mana (mana zone - mana spent).
// The engine would reject the play ("not enough mana") but that rejection is easy
// to miss (and, before the engine fix, it was even archived as a successful play);
// we flag it well before sending the action.
export function checkMana(id) {
  const st = game.state;
  const me = (st && st.players) ? st.players[game.me] : null;
  if (!me) return true;
  const cost = cardCost(id);
  const avail = (me.mana || []).length - (me.mana_spend || 0);
  if (cost > avail) {
    toast(`Not enough mana: ${cardName(id)} costs ${cost}, only ${avail} left`, 5000);
    return false;
  }
  return true;
}

/* ---------------- central Earth cards: day/night • temperature ---------------- */
function renderEnv(st) {
  $("#env-dn").classList.toggle("dn-night", st.day_night === "night");
  $("#env-temp-val").textContent = (st.temperature != null) ? st.temperature : "?";
}

// highlights the next stopover slot (click fallback) when a card is selected
function highlightValidCells() {
  const st = game.state;
  if (!st || slotEls.me.length === 0) return;
  // in cell-selection mode the engineer drop targets are the Earth cells, not the stopovers
  if (cellSelect.active) {
    for (let i = 0; i < N_STOPOVERS; i++) slotEls.me[i].classList.remove("slot-valid");
    return;
  }
  const me = st.players[game.me];
  // only highlight if the selected card is still in hand (avoids a stale state)
  const sel = [...(game.selected || [])].filter((id) => (me.hand || []).includes(id));
  for (let i = 0; i < N_STOPOVERS; i++) {
    slotEls.me[i].classList.toggle("slot-valid", sel.length && myTurn(st) && canDropOnStopover(sel[0], i));
  }
}

export function renderAll() {
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
  } else if (ph && ph.kind === "discard") {
    // discard selection (engine_version 13): the trip chain is paused
    if (ph.actor === game.me) {
      phaseText = `Discard selection — choose ${ph.n} card(s) to discard`;
      canAct = true;
      hint = `Click ${ph.n} card(s) in your hand, then press the red DISCARD button.`;
    } else {
      phaseText = `Waiting for ${ph.actor} to discard ${ph.n} card(s)…`;
      hint = "";
    }
  } else if (ph === "over") {
    phaseText = "Game over";
  } else {
    phaseText = st.state || "";
  }
  $("#phase-label").textContent = phaseText;
  $("#action-hint").textContent = hint;

  const btnPlay = $("#btn-play"), btnDefend = $("#btn-defend"), btnPass = $("#btn-pass"), btnTap = $("#btn-tap"), btnDiscard = $("#btn-discard");
  const selCard = (game.selected.size === 1) ? [...game.selected][0] : null;
  const selIsDrop = selCard && isEngineerDrop(selCard);
  const selIsDwelling = selCard && isEngineerDwelling(selCard);
  const canDefend = canAct && ph && ph.kind === "play" && !!selCard && !isSupportPlay(selCard);
  // dwelling tap: I have a dwelling card, it's untapped, and it's my play turn
  const canTap = canAct && ph && ph.kind === "play" && !!me.dwelling && !me.dwelling_tapped;
  if (ph === "init-mana") {
    btnPlay.textContent = "Poser en mana";
    btnPlay.disabled = !canAct || game.selected.size === 0;
    btnPass.classList.add("hidden");
    btnDefend.classList.add("hidden");
    btnTap.classList.add("hidden");
    btnDiscard.classList.add("hidden");
  } else if (ph && ph.kind === "discard") {
    // discard selection (engine_version 13): the red DISCARD button is the only action
    btnPlay.classList.add("hidden");
    btnDefend.classList.add("hidden");
    btnPass.classList.add("hidden");
    btnTap.classList.add("hidden");
    if (ph.actor === game.me) {
      btnDiscard.classList.remove("hidden");
      btnDiscard.disabled = game.selected.size !== ph.n;
    } else {
      btnDiscard.classList.add("hidden");
    }
  } else if (ph === "mana-pass" || ph.kind === "play") {
    if (selIsDrop) btnPlay.textContent = "📍 Place drop (pick a cell)";
    else if (selIsDwelling) btnPlay.textContent = "🏠 Place dwelling";
    else btnPlay.textContent = "Play the card";
    btnPlay.disabled = !canAct || game.selected.size !== 1;
    btnDefend.classList.remove("hidden");
    btnDefend.disabled = !canDefend;
    btnPass.classList.remove("hidden");
    btnPass.disabled = !canAct;
    btnTap.classList.toggle("hidden", !canTap);
    if (btnTap) btnTap.disabled = !canTap;
  } else {
    btnPlay.disabled = true;
    btnDefend.disabled = true;
    btnPass.disabled = true;
    btnTap.classList.add("hidden");
    btnDiscard.classList.add("hidden");
  }

  const interactive = canAct && (ph === "init-mana" || ph === "mana-pass"
    || (ph && ph.kind === "play") || (ph && ph.kind === "discard" && ph.actor === game.me));

  if (oppo) renderOppZone(oppo);
  renderMyZone(me, interactive);
  renderSideRows(me, oppo);
  renderEnv(st);
  renderBoard(st, me, oppoName);
  renderLog(st);
  highlightValidCells();
}
