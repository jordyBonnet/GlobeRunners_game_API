/* ============================================================ GlobeRunners UI — trip-chain resolution animation */
/* When a new turn just resolved (new turn log entry), the stopover slots pulse
   in the EXACT resolution order: stopover by stopover, first player's card then
   second player's card. Gold = first player of the turn, silver = second; a
   glowing thread connects the two facing cards of the stopover that is
   resolving. Purely visual — driven by the public turn log (st.log): the log's
   stopovers are appended by the engine in resolution order and each entry
   carries `order` (1 = first player of the turn). The generation counter
   (chainGen) invalidates pending timers, so a new state/turn kills a stale run. */
"use strict";

import { game } from "./game.mjs";
import { N_STOPOVERS, slotEls } from "./board.mjs";
import { makeStaticCard } from "./zones.mjs";

let chainGen = 0;
/* Tutorial hold mode: while the in-game tutorial is running, the automatic pulse run is
   replaced by a step-by-step replay the tutorial drives (`pulseStep`) — the clones are
   still placed on the slots (so the cards of the turn are visible while each card is
   explained) but nothing self-advances. Cleared by `clearChainAnim()`. */
let hold = false;

export function setChainAnimHold(on) {
  hold = !!on;
  if (!hold) clearChainAnim();        // leaving hold mode: hand the board back to the game
}

export function clearChainAnim() {
  chainGen++;                                   // kills any pending step() timer
  unlight();
  document.querySelectorAll(".slot .chain-anim-card").forEach((e) => e.remove());
}

// glowing vertical thread between the two slots of one stopover column
// (gold end on the first player's side, silver end on the second player's side)
function placeThread(col, firstRow) {
  const host = document.getElementById("stopovers");
  const a = slotEls.oppo[col], b = slotEls.me[col];
  if (!host || !a || !b) return;
  const hr = host.getBoundingClientRect();
  const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
  const el = document.createElement("div");
  el.className = "chain-thread" + (firstRow === "oppo" ? " first-top" : " first-bottom");
  // span the GAP between the two slots: from the upper slot's bottom edge to
  // the lower slot's top edge
  const topEdge = Math.min(ra.bottom, rb.bottom) - hr.top;
  const bottomEdge = Math.max(ra.top, rb.top) - hr.top;
  el.style.left = (Math.min(ra.left, rb.left) + ra.width / 2 - hr.left - 2) + "px";
  el.style.top = topEdge + "px";
  el.style.height = Math.max(0, bottomEdge - topEdge) + "px";
  host.appendChild(el);
}

/* Put each card of a resolved turn back on its slot: the cleaning phase already emptied
   them, so the turn would otherwise be explained over empty slots. Idempotent (a clone of
   that card already on that slot is left alone), so the tutorial can call it on every step
   of its replay. Torn down by `clearChainAnim()`. */
function placeChainClones(t) {
  if (!t || !Array.isArray(t.stopovers) || !slotEls.me.length) return 0;
  let n = 0;
  for (const sv of t.stopovers) {
    const m = /^stopover_(\d+)/.exec(sv.stopover || "");
    if (!m) continue;
    const col = parseInt(m[1], 10) % N_STOPOVERS;
    for (const e of (sv.entries || [])) {
      const slot = slotEls[(e.player === game.me) ? "me" : "oppo"][col];
      if (!slot || !(e.cards || []).length) continue;
      const id = e.cards[0];
      if (slot.querySelector(`.chain-anim-card[data-card="${CSS.escape(id)}"]`)) continue;
      const c = makeStaticCard(id, e.player);
      c.classList.add("played", "chain-anim-card");
      c.dataset.card = id;
      slot.appendChild(c);
      n++;
    }
  }
  return n;
}

export function playChainAnim(t) {
  if (!t || !Array.isArray(t.stopovers) || !t.stopovers.length) return;
  if (!slotEls.me.length) return;               // board not built yet
  clearChainAnim();
  const g = chainGen;
  // flatten the turn log into (row, col, first) steps — already in resolution order
  const steps = [];
  for (const sv of t.stopovers) {
    const m = /^stopover_(\d+)/.exec(sv.stopover || "");
    if (!m) continue;
    const col = parseInt(m[1], 10) % N_STOPOVERS;
    for (const e of (sv.entries || [])) {
      steps.push({ row: (e.player === game.me) ? "me" : "oppo", col, first: e.order === 1 });
    }
  }
  if (!steps.length) return;
  // the action_chain is already cleared in the new state (cleaning phase), so the
  // slots are empty — clone each card onto its slot for the duration of the run
  placeChainClones(t);

  const kill = () => document.querySelectorAll(".slot .chain-anim-card").forEach((e) => e.remove());
  if (hold) return;                             // tutorial replay: the clones stay, no timer
  const STEP_MS = 460;
  let i = 0, lastSlot = null;
  const step = () => {
    if (g !== chainGen) { kill(); return; }     // a newer state killed this run
    if (lastSlot) lastSlot.classList.remove("chain-pulsing", "chain-first", "chain-second");
    document.querySelectorAll(".chain-thread").forEach((el) => el.remove());
    if (i >= steps.length) { kill(); return; }  // final cleanup done — stop
    const s = steps[i];
    const slot = slotEls[s.row][s.col];
    if (slot) {
      slot.classList.add("chain-pulsing", s.first ? "chain-first" : "chain-second");
      lastSlot = slot;
    }
    // thread while a stopover has BOTH players' cards (a facing pair resolving);
    // `prev` is the first player's step (the log lists order 1 before order 2)
    const prev = steps[i - 1];
    if (prev && prev.col === s.col && prev.row !== s.row) placeThread(s.col, prev.row);
    i++;
    setTimeout(step, STEP_MS);
  };
  step();
}

/* ---- tutorial step API: light ONE facing pair of a stopover, and KEEP it lit ----
   (the automatic run above pulses each slot for 460 ms; a tip stays on screen for as
   long as the player reads, so this uses the sustained `.chain-live` styling) */
export function pulseStep(row, col, first, pairRow) {
  if (!slotEls.me.length) return;
  unlight();
  const slot = slotEls[row] && slotEls[row][col];
  if (slot) slot.classList.add("chain-pulsing", "chain-live", first ? "chain-first" : "chain-second");
  // the thread spans the gap between the two facing slots; `pairRow` = the FIRST player's row
  if (pairRow && pairRow !== row) {
    placeThread(col, pairRow);              // all threads were removed above → exactly one
    const th = document.querySelector(".chain-thread");
    if (th) th.classList.add("chain-live");
  }
}

/* clear the lit slot(s) + the thread. Selecting BOTH classes matters: a slot that lost
   `chain-pulsing` another way must not keep a stale `.chain-live` glow forever */
function unlight() {
  document.querySelectorAll(".slot.chain-live, .slot.chain-pulsing").forEach((s) =>
    s.classList.remove("chain-pulsing", "chain-first", "chain-second", "chain-live"));
  document.querySelectorAll(".chain-thread").forEach((e) => e.remove());
}

export function clearPulse() { unlight(); }
