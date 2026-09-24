/* ============================================================ GlobeRunners UI — modals, hover preview, messages */
"use strict";

import { $, toast } from "./utils.mjs";
import { cardEl, cardImg, CARDPOOL, SUPPORT } from "./cards.mjs";
import { game } from "./game.mjs";
import { stopPolling, teardownWs } from "./comm.mjs";
import { startBgCards } from "./bg.mjs?v=5";

/* hover preview: the card shows at 3x in the bottom-left (delegation, robust to re-renders) */
const hoverEl = $("#card-hover"), hoverImg = $("#card-hover-img"), hoverText = $("#card-hover-text");

/* ---------------- condition / effect legend (hover preview text) ----------------
   Human-readable explanations of the card `condition` / `effect` values, shown
   under the image in the hover preview. "X" in an effect line is the card's
   effect_number (e.g. "Move forward by X cells" → "Move forward by 3 cells").
   Support cards have no condition/effect — they show their pool `description`. */
const COND_TOOLTIPS = {
  "no_condition": "Always met — the effect fires unconditionally.",
  "block": "If card is in defense/block mode.",
  "cataclysm": "This condition is a trigger. The top card of the cataclysm pile (one per biome) strikes its biome: all tokens on it are knocked back to the start of the biome",
  "biome_Dwa": "If you stand on one of the Dwarves' biomes (Mountain/Ocean).",
  "biome_Dem": "If you stand on one of the Demons' biomes (Ocean/Desert).",
  "biome_Twi": "If you stand on one of the Twigs' biomes (Jungle/Ocean).",
  "biome_Mia": "If you stand on one of the Miaous' biomes (Desert/Jungle).",
  "biome_Orc": "If you stand on one of the Orcs' biomes (Mountain/Jungle).",
  "biome_Mum": "If you stand on one of the Mummies' biomes (Desert/Mountain).",
  "dist_ahead_sup_1": "If you are strictly more than 1 cell ahead of the opponent.",
  "dist_ahead_sup_3": "If you are strictly more than 3 cells ahead of the opponent.",
  "dist_behind_sup_1": "If the opponent is strictly more than 1 cell ahead of you.",
  "dist_behind_sup_3": "If the opponent is strictly more than 3 cell ahead of you.",
  "mana_inf_6": "If you have fewer than 6 cards in your mana zone.",
  "mana_sup_5": "If you have more than 5 cards in your mana zone.",
  "mana_inf_6_oppo": "If the opponent has fewer than 6 cards in their mana zone.",
  "mana_sup_5_oppo": "If the opponent has more than 5 cards in their mana zone.",
  "cards_in_hand_inf_4": "If you have fewer than 4 cards in hand.",
  "cards_in_hand_sup_3": "If you have more than 3 cards in hand.",
  "cards_in_hand_inf_4_oppo": "If the opponent has fewer than 4 cards in hand.",
  "cards_in_hand_sup_3_oppo": "If the opponent has more than 3 cards in hand.",
  "temp_inf_6": "If the planet temperature is less than 6.",
  "temp_inf_11": "If the planet temperature is less than 11.",
  "temp_sup_9": "If the planet temperature is greater than 9.",
  "temp_sup_15": "If the planet temperature is greater than 15.",
  "day": "If it is currently day.",
  "night": "If it is currently night.",
  "drop_on_board": "If any drop token (pet_trap) or trap cell exists on the board.",
  "pending": "If you have at least one pending card in your own pending zone (Doctors).",
  "face_point_left": "If the card's token faces left on the board. ⚠️ Condition not available in the online version of the game.",
  "face_point_right": "If the card's token faces right on the board. ⚠️ Condition not available in the online version of the game.",
};
const EFF_TOOLTIPS = {
  "advancing": "Move forward by X cells.",
  "backward": "Move backward by X cells, clamped at cell 0.",
  "advancing_oppo": "Opponent moves forward by X cells.",
  "backward_oppo": "Opponent recoils by X cells, clamped at cell 0.",
  "draw": "Draw X cards.",
  "draw_oppo": "Opponent draws X cards.",
  "discard": "Discard X chosen cards.",
  "discard_oppo": "Opponent discards X chosen cards.",
  "ramp": "Move X cards from your deck into your mana zone.",
  "ramp_oppo": "Opponent moves X cards from their deck into their mana zone.",
  "taxation": "Move X cards from your mana zone to your discard pile.",
  "taxation_oppo": "Opponent moves X cards from their mana zone to their discard pile.",
  "jump": "Jump by the amount of your basic advancing value.",
  "unstoppable": "Ignores any negative effects.",
  "avalanche": "All players tokens on the Mountain (MO) biome are knocked back to MO's first cell, then you apply your basic advancing from there.",
  "grappling_hook": "You advance by your basic value, then copy the net advancing of the opponent's card facing you (same trip-chain position).",
  "effect_canceled": "The effect of the facing card is canceled.",
  "copy_effect": "The effect of the facing card is copied and applied to you.",
  "pet_trap": "Instant, leaves a pet trap token (-1 knockback) on your current cell.",
  "rooted": "The card stays on the board for another turn, move it back to the start of its trip-chain during cleaning phase.",
  "wrecking_ball": "Instant, removes the opponent's dwelling card.",
  "swap_cards": "Instant (optional), swap two cards in your trip-chain.",
};

/* the {condition, effect} legend lines for a card id (null = no legend: card
   back, unknown id, or pool not loaded yet) */
function hoverCardText(id) {
  if (!id || id === "__back__") return null;
  const main = CARDPOOL[id];
  if (main && main.condition) {
    const effectTxt = EFF_TOOLTIPS[main.effect] || main.effect || "";
    // the pool stores effect_number signed (direction baked in: backward/discard are negative) —
    // the legend lines already carry the direction, so show the magnitude ("Move backward by 1 cells")
    const n = (main.effect_number != null && !Number.isNaN(main.effect_number)) ? Math.abs(main.effect_number) : null;
    return {
      condition: COND_TOOLTIPS[main.condition] || main.condition,
      effect: n != null ? effectTxt.replace(/X/g, n) : effectTxt,
    };
  }
  const sup = SUPPORT[id];
  if (sup && sup.description) return { condition: null, effect: sup.description };
  return null;
}

export function showCardHover(id) {
  if (!id) return;
  hoverImg.onerror = () => { hoverImg.onerror = null; hoverImg.src = "/placeholder.svg" };
  hoverImg.src = id === "__back__" ? "/assets/GR_cards_back.png" : cardImg(id);
  const t = hoverCardText(id);
  if (t) {
    hoverText.innerHTML = "";
    if (t.condition) {
      const row = document.createElement("div"); row.className = "ch-row";
      const lab = document.createElement("span"); lab.className = "ch-label"; lab.textContent = "Condition";
      row.appendChild(lab); row.appendChild(document.createTextNode(t.condition));
      hoverText.appendChild(row);
    }
    if (t.effect) {
      const row = document.createElement("div"); row.className = "ch-row";
      const lab = document.createElement("span"); lab.className = "ch-label"; lab.textContent = "Effect";
      row.appendChild(lab); row.appendChild(document.createTextNode(t.effect));
      hoverText.appendChild(row);
    }
    hoverText.classList.add("show");
  } else {
    hoverText.classList.remove("show");
  }
  hoverEl.classList.add("show");
}
export function hideCardHover() { hoverEl.classList.remove("show"); }
/* the selector also covers the setup-page animated background cards (.bg-card,
   bg.mjs) — they carry dataset.hoverId too */
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest(".card, .bg-card");
  if (!el || !el.dataset.hoverId) return;
  showCardHover(el.dataset.hoverId);
});
document.addEventListener("mouseout", (e) => {
  const el = e.target.closest(".card, .bg-card");
  if (!el) return;
  if (e.relatedTarget && el.contains(e.relatedTarget)) return;   // still over the card
  hideCardHover();
});

/* zoom modal */
export function showCardModal(id) {
  $("#card-modal-img").src = cardImg(id);
  const img = $("#card-modal-img");
  img.onerror = () => { img.onerror = null; img.src = "/placeholder.svg"; };
  $("#card-modal").classList.remove("hidden");
}
$("#card-modal .modal-backdrop").onclick = () => $("#card-modal").classList.add("hidden");
$("#card-modal-img").onclick = () => $("#card-modal").classList.add("hidden");

/* ---------------- discard pile viewer (MY pile + OPPONENT pile) ----------------
   Clicking a side-row discard panel (my row or the opponent's row) opens a
   read-only popup listing ALL of that player's discarded cards (most recent
   first, hover preview via data-hover-id). No message is sent — the discard
   pile is public information (only hand/mana/deck are masked by the engine).
   renderAll (state.mjs) refreshes the open popup on every state, so it never
   shows a stale pile. */
export function showDiscardPilePopup(name) {
  const st = game.state;
  const p = (st && st.players) ? st.players[name] : null;
  const pile = (p && p.discard) || [];
  document.querySelectorAll(".discard-pile-popup").forEach((m) => m.remove());
  if (!pile.length) { toast(`${name}: the discard pile is empty`); return; }
  const modal = document.createElement("div");
  modal.className = "modal discard-pile-popup";
  modal.dataset.player = name;
  modal.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-box">
      <h3></h3>
      <div class="discard-pile-grid"></div>
      <div class="pending-options"><button class="discard-pile-close">✕ Close</button></div>
    </div>`;
  modal.querySelector("h3").textContent = `🗑️ ${name} — discard pile (${pile.length})`;
  const grid = modal.querySelector(".discard-pile-grid");
  [...pile].reverse().forEach((id) => grid.appendChild(cardEl(id)));   // most recent first
  const close = () => modal.remove();
  modal.querySelector(".modal-backdrop").onclick = close;
  modal.querySelector(".discard-pile-close").onclick = close;
  document.body.appendChild(modal);
}

/* called from renderAll (state.mjs) on every state: rebuild the open pile popup
   in place so its contents track the live discard pile (a card discarded between
   polls appears without a manual reopen) */
export function refreshDiscardPilePopup() {
  const modal = document.querySelector(".discard-pile-popup");
  if (modal) showDiscardPilePopup(modal.dataset.player);
}

/* wiring: the two discard side-panels are STATIC DOM (only their inner cards are
   rewritten by renderSideRows), so direct listeners are safe and survive re-renders */
for (const sel of ["#oppo-side .side-panel.discard", "#my-side .side-panel.discard"]) {
  const panel = document.querySelector(sel);
  if (!panel) continue;
  panel.style.cursor = "pointer";
  panel.title = panel.title + " — click to see all discarded cards";
  panel.addEventListener("click", () => {
    const st = game.state;
    if (!st || !st.players) return;
    if (panel.closest("#oppo-side")) {
      const oppoName = Object.keys(st.players).find((n) => n !== game.me);
      if (oppoName) showDiscardPilePopup(oppoName);
    } else {
      showDiscardPilePopup(game.me);
    }
  });
}

/* server message bar */
export function showServerMsg(msg) {
  const el = $("#server-msg");
  clearTimeout(el._clearTimer);
  if (msg) { el.textContent = msg; return; }
  el._clearTimer = setTimeout(() => { el.textContent = ""; }, 3000);   // successes clear themselves
}

/* end of game */
export function showEndgame(st) {
  const winner = st.winner;
  const title = winner ? `🏆 ${winner} gagne !` : "🤝 Match nul";
  $("#endgame-title").textContent = title;
  $("#endgame-sub").textContent = st.state === "game over" && String(st.message?.message || "").startsWith("Deadlock")
    ? "Deadlock — no playable card left for either player."
    : `The race ends at turn ${st.turn}.`;
  $("#endgame-modal").classList.remove("hidden");
}

$("#btn-endgame-ok").onclick = () => {
  $("#endgame-modal").classList.add("hidden");
  leaveGame();
};

function leaveGame() {
  stopPolling();
  document.querySelectorAll(".discard-pile-popup").forEach((m) => m.remove());
  teardownWs();   // close the socket, kill any pending reconnect, settle in-flight action promises
  game.state = null; game.id = null; game.selected.clear();
  game.logTurnsRendered = 0;
  $("#log-body").innerHTML = "";
  $("#log-panel").classList.add("hidden");
  $("#view-game").classList.add("hidden");
  $("#view-setup").classList.remove("hidden");
  startBgCards();   // restart the setup-page card background (stopped by enterGame; boot() does not re-run)
}

$("#btn-leave").onclick = () => {
  if (confirm("Leave the game? The state stays saved on the server.")) leaveGame();
};
