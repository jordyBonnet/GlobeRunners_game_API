/* ============================================================ GlobeRunners UI — modals, hover preview, messages */
"use strict";

import { $, toast } from "./utils.mjs";
import { cardEl, cardImg } from "./cards.mjs";
import { game } from "./game.mjs";
import { stopPolling } from "./comm.mjs";
import { startBgCards } from "./bg.mjs?v=5";

/* hover preview: the card shows at 3x in the bottom-left (delegation, robust to re-renders) */
const hoverEl = $("#card-hover"), hoverImg = $("#card-hover-img");
export function showCardHover(id) {
  if (!id) return;
  hoverImg.onerror = () => { hoverImg.onerror = null; hoverImg.src = "/placeholder.svg" };
  hoverImg.src = id === "__back__" ? "/assets/GR_cards_back.png" : cardImg(id);
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
  if (game.ws) { try { game.ws.close(); } catch {} game.ws = null; }
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
