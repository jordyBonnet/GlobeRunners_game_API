/* ============================================================ GlobeRunners UI — zones (banners, hand/mana, side rows) */
"use strict";

import { $, toast } from "./utils.mjs";
import { cardEl, isMageBlackHole } from "./cards.mjs";
import { detectPhase, myTurn } from "./phase.mjs";
import { game } from "./game.mjs";
import { sendAction } from "./comm.mjs";
import { checkMana } from "./state.mjs";
import { playedCount, nextSlotCol, orderHint, dispatchPlay, freeCols, showBlackHoleRotationPopup, showDefendPopup } from "./actions.mjs";
import { N_STOPOVERS } from "./board.mjs";
import { makeHandCard } from "./interaction.mjs";

/* public counters: the personalized state masks the ids but exposes *_count */
export const publicCount = (p, key) => (p && p[key] != null) ? p[key] : (p && (p[key.replace("_count", "")] || []).length);

/* ---------------- opponent banner: [name + counters] [mana on the left] [hand face-down] ---------------- */
export function renderOppZone(oppo) {
  $("#oppo-name").textContent = oppo.name;
  const handN = publicCount(oppo, "hand_count");
  const manaN = publicCount(oppo, "mana_count");
  const deckN = publicCount(oppo, "deck_count");
  const manaSpent = oppo.mana_spend || 0;
  const manaAvail = Math.max(manaN - manaSpent, 0);
  $("#oppo-counts").innerHTML =
    (oppo.landmine_blocked ? `<span class="landmine-badge" title="Blocked by a landmine — move cards are canceled until the end of the turn">💣 blocked</span>` : "") +
    `<span>📍 case ${oppo.current_position ?? 0}</span>` +
    `<span>🂠 main : ${handN}</span>` +
    `<span>⚡ mana : ${manaAvail}/${manaN}</span>` +
    `<span>📦 deck : ${deckN}</span>` +
    `<span>🗑️ ${(oppo.discard || []).length}</span>`;
  // opponent hand: N card backs (the cards themselves stay hidden)
  const hand = $("#oppo-hand");
  hand.innerHTML = "";
  for (let i = 0; i < handN; i++) {
    const c = document.createElement("div");
    c.className = "card back";
    c.dataset.hoverId = "__back__";
    c.title = `Opponent's card (hidden) — ${handN} in hand`;
    hand.appendChild(c);
  }
  // opponent mana: card backs in the drop (spent = turned 90°) + available/total counter
  const mc = $("#oppo-mana-cards");
  mc.innerHTML = "";
  for (let i = 0; i < manaN; i++) {
    const c = document.createElement("div");
    c.className = "card back" + (i < manaSpent ? " tapped" : "");
    c.dataset.hoverId = "__back__";
    c.title = i < manaSpent ? `Mana spent this turn (${manaSpent})` : `Mana available (${manaAvail})`;
    mc.appendChild(c);
  }
  $("#oppo-mana-count").textContent = `${manaAvail}/${manaN}`;
}

/* ---------------- player banner: [name] [mana on the left] [hand] [actions] ---------------- */
export function renderMyZone(me, interactive) {
  $("#my-name").textContent = me.name;
  const manaAvail = (me.mana || []).length - (me.mana_spend || 0);
  $("#mana-info").innerHTML =
    (me.landmine_blocked ? `<span class="landmine-badge" title="Blocked by a landmine — move cards are canceled until the end of the turn">💣 blocked</span>` : "") +
    `<span>⚡ mana ${manaAvail}/${(me.mana || []).length}</span>` +
    `<span>🂠 main ${(me.hand || []).length}</span>`;

  // hand (to the right of the mana zone)
  const hand = $("#my-hand");
  hand.innerHTML = "";
  (me.hand || []).forEach((id) => hand.appendChild(makeHandCard(id, interactive, me)));

  // mana: card backs in the drop (spent = turned 90° to the right) + available/total counter
  const manaCards = $("#my-mana-cards");
  manaCards.innerHTML = "";
  const nMana = (me.mana || []).length;
  const nSpent = me.mana_spend || 0;
  for (let i = 0; i < nMana; i++) {
    const el = document.createElement("div");
    el.className = "card back" + (i < nSpent ? " tapped" : "");
    el.dataset.hoverId = "__back__";
    el.title = (i < nSpent ? "Mana spent this turn" : "Mana available") + ` — ${me.name}`;
    manaCards.appendChild(el);
  }
  $("#my-mana-count").textContent = `${Math.max(nMana - nSpent, 0)}/${nMana}`;

  // mana drop zone (the discard pile panel is DISPLAY ONLY since the discard
  // popup — showDiscardPopup, actions.mjs — replaced the drag-to-discard target)
  const ph = detectPhase(game.state);
  const canMana = interactive && (ph === "init-mana" || ph === "mana-pass");
  const dzMana = $("#dz-mana");
  // waiting-for-mana phase (init: 3 cards / mana: 1 card or pass): the mana drop
  // pulses purple to signal "put a card here" (CSS: #dz-mana.mana-waiting)
  dzMana.classList.toggle("mana-waiting", canMana);
  dzMana.ondragover = (e) => {
    if (!game.dragCardId) return;
    e.preventDefault();
    dzMana.classList.add(canMana ? "over" : "invalid");
  };
  dzMana.ondragleave = () => dzMana.classList.remove("over", "invalid");
  dzMana.ondrop = (e) => {
    e.preventDefault();
    dzMana.classList.remove("over", "invalid");
    const id = game.dragCardId;
    game.dragCardId = null;
    if (!id || !me.hand.includes(id)) return;
    if (!canMana) { toast("Cannot put mana in right now"); return; }
    sendAction([id], "mana", "");
  };

  // action buttons
  $("#btn-play").onclick = async () => {
    const ph2 = detectPhase(game.state);
    if (ph2 === "init-mana") {
      // the engine only accepts 1 or 3 cards per message: we send them one at a time
      for (const id of [...game.selected]) {
        const resp = await sendAction([id], "mana", "");
        if (!resp || resp.success === false) break;   // rejection -> stop, state is resynchronized
      }
      game.selected.clear();
    } else if (ph2 === "mana-pass") {
      if (game.selected.size !== 1) return;
      sendAction([...game.selected], "mana", "");
    } else if (ph2 && ph2.kind === "play") {
      const id = [...game.selected][0];
      if (!id) return;
      dispatchPlay(id);   // single play path: drop (cell select) / dwelling / stopover
    }
  };

  // tap the dwelling: refinery draws 1 / laboratory adds an 'epo' pending / black_hole
  // rotates the earth 3 cells. Once per turn (free action, play phase). The black_hole
  // needs a direction CHOICE (cw/ccw) -> a popup (Mages);
  // the other dwellings tap directly.
  const btnTap = $("#btn-tap");   // NOTE: local here - the one in renderAll is NOT in scope
  if (btnTap) btnTap.onclick = () => {
    if (!myTurn(game.state)) return;
    const ph3 = detectPhase(game.state);
    if (!ph3 || ph3.kind !== "play") return;
    const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
    if (!meNow.dwelling || meNow.dwelling_tapped) return;
    if (isMageBlackHole(meNow.dwelling)) {
      showBlackHoleRotationPopup();
    } else {
      sendAction([], "dwelling", "dwelling_activation");
    }
  };

  // discard selection: the popup (showDiscardPopup, actions.mjs)
  // is the interface — auto-opened by renderAll; no action-bar button any more.

  // defend button: the selected card is engaged at 90° on the next slot,
  // it blocks the opponent card placed on the SAME stopover
  // "Play in defense" opens the multi-card defense popup (showDefendPopup,
  // actions.mjs): the player toggles 1–5 main cards there (cost = Σ mana,
  // shield = Σ shields) and confirms — no pre-selection in hand is required.
  $("#btn-defend").onclick = () => {
    const ph2 = detectPhase(game.state);
    if (!ph2 || ph2.kind !== "play" || !myTurn(game.state)) return;
    showDefendPopup();
  };

  $("#btn-pass").onclick = () => {
    const ph3 = detectPhase(game.state);
    // mana phase: pass is always allowed (the engine decides whose pass it is)
    if (ph3 === "mana-pass") sendAction([], "", "pass");
    // play phase: only when it is MY action (myTurn() is false in the mana phase —
    // detectPhase returns the string "mana-pass" there, so it must NOT gate the pass)
    else if (ph3 && ph3.kind === "play" && myTurn(game.state)) sendAction([], "", "pass");
  };
}

/* ---------------- side rows: dwelling / pending / deck / discard ---------------- */
export function renderSideRows(me, oppo) {
  const side = (p, pref) => {
    // dwelling: 1 card (the engineers' refinery — tap 1x/turn to draw 1)
    const dw = $(`#${pref}-dwelling`);
    dw.innerHTML = "";
    if (p.dwelling) {
      const card = makeStaticCard(p.dwelling, p.name);
      if (p.dwelling_tapped) card.classList.add("dwelling-tapped");   // dimmed while tapped this turn
      dw.appendChild(card);
    }
    // pending: small cards
    const pe = $(`#${pref}-pendings`);
    pe.innerHTML = "";
    (p.pendings || []).slice(0, 5).forEach((id) => pe.appendChild(makeStaticCard(id, p.name)));
    // deck: counter (public via deck_count even though the cards are hidden)
    const deckN = publicCount(p, "deck_count");
    $(`#${pref}-deck-count`).textContent = deckN ? deckN : "–";
    // discard: last card + counter
    const di = $(`#${pref}-discard`);
    di.innerHTML = "";
    const last = (p.discard || []).slice(-1)[0];
    if (last) di.appendChild(makeStaticCard(last, p.name));
    $(`#${pref}-discard-count`).textContent = (p.discard || []).length;
  };
  side(me, "my");
  if (oppo) side(oppo, "oppo");
}

/* static (non-interactive) card for the side panels and the stopovers */
export function makeStaticCard(id, owner) {
  return cardEl(id);
}
