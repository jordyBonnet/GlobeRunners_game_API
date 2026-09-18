/* ============================================================ GlobeRunners UI — zones (banners, hand/mana, side rows) */
"use strict";

import { $, toast } from "./utils.mjs";
import { cardEl } from "./cards.mjs";
import { detectPhase, myTurn } from "./phase.mjs";
import { game } from "./game.mjs";
import { sendAction } from "./comm.mjs";
import { checkMana } from "./state.mjs";
import { playedCount, nextSlotCol, orderHint, dispatchPlay, freeCols } from "./actions.mjs";
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

  // mana / discard drop zones
  const ph = detectPhase(game.state);
  const canMana = interactive && (ph === "init-mana" || ph === "mana-pass");
  const dzMana = $("#dz-mana"), dzDisc = $("#dz-discard");
  // waiting-for-mana phase (init: 3 cards / mana: 1 card or pass): the mana drop
  // pulses purple to signal "put a card here" (CSS: #dz-mana.mana-waiting)
  dzMana.classList.toggle("mana-waiting", canMana);
  for (const dz of [dzMana, dzDisc]) {
    dz.ondragover = (e) => {
      if (!game.dragCardId) return;
      e.preventDefault();
      const ok = dz === dzMana ? canMana : true;   // discarding is always allowed
      dz.classList.add(ok ? "over" : "invalid");
    };
    dz.ondragleave = () => dz.classList.remove("over", "invalid");
    dz.ondrop = (e) => {
      e.preventDefault();
      dz.classList.remove("over", "invalid");
      const id = game.dragCardId;
      game.dragCardId = null;
      if (!id || !me.hand.includes(id)) return;
      if (dz === dzMana) {
        if (!canMana) { toast("Cannot put mana in right now"); return; }
        sendAction([id], "mana", "");
      } else {
        sendAction([id], "discard_pile", "");
      }
    };
  }

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

  // tap the dwelling (refinery): draw 1 card, once per turn (free action, play phase)
  const btnTap = $("#btn-tap");   // NOTE: local here - the one in renderAll is NOT in scope
  if (btnTap) btnTap.onclick = () => {
    if (!myTurn(game.state)) return;
    const ph3 = detectPhase(game.state);
    if (!ph3 || ph3.kind !== "play") return;
    const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
    if (!meNow.dwelling || meNow.dwelling_tapped) return;
    sendAction([], "dwelling", "dwelling_activation");
  };

  // discard selection (engine_version 13): the trip chain is paused on
  // "turn N - waiting for NAME to discard K card(s)" — send the chosen cards
  const btnDiscard = $("#btn-discard");
  if (btnDiscard) btnDiscard.onclick = async () => {
    const ph2 = detectPhase(game.state);
    if (!ph2 || ph2.kind !== "discard" || ph2.actor !== game.me) return;
    if (game.selected.size !== ph2.n) { toast(`Select exactly ${ph2.n} card(s) to discard`); return; }
    const resp = await sendAction([...game.selected], "discard_pile", "");
    if (resp && resp.success === false) return;   // rejected: keep the selection
    game.selected = new Set();
  };

  // defend button: the selected card is engaged at 90° on the next slot,
  // it blocks the opponent card placed on the SAME stopover
  $("#btn-defend").onclick = () => {
    const ph2 = detectPhase(game.state);
    if (!ph2 || ph2.kind !== "play" || !myTurn(game.state)) return;
    if (game.selected.size !== 1) return;
    if (freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    const id = [...game.selected][0];
    if (!checkMana(id)) return;
    sendAction([id], `stopover_${nextSlotCol(game.state, game.me)}`, "defend");
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
    // dwelling: 1 card (engine_version 12: the engineers' refinery — tap 1x/turn to draw 1)
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
