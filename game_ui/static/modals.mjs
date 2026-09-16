/* ============================================================ GlobeRunners UI — modals, hover preview, messages */
"use strict";

import { $ } from "./utils.mjs";
import { cardImg } from "./cards.mjs";
import { game } from "./game.mjs";
import { stopPolling } from "./comm.mjs";

/* hover preview: the card shows at 3x in the bottom-left (delegation, robust to re-renders) */
const hoverEl = $("#card-hover"), hoverImg = $("#card-hover-img");
export function showCardHover(id) {
  if (!id) return;
  hoverImg.onerror = () => { hoverImg.onerror = null; hoverImg.src = "/placeholder.svg" };
  hoverImg.src = id === "__back__" ? "/assets/GR_cards_back.png" : cardImg(id);
  hoverEl.classList.add("show");
}
export function hideCardHover() { hoverEl.classList.remove("show"); }
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest(".card");
  if (!el || !el.dataset.hoverId) return;
  showCardHover(el.dataset.hoverId);
});
document.addEventListener("mouseout", (e) => {
  const el = e.target.closest(".card");
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
  if (game.ws) { try { game.ws.close(); } catch {} game.ws = null; }
  game.state = null; game.id = null; game.selected.clear();
  game.logTurnsRendered = 0;
  $("#log-body").innerHTML = "";
  $("#log-panel").classList.add("hidden");
  $("#view-game").classList.add("hidden");
  $("#view-setup").classList.remove("hidden");
}

$("#btn-leave").onclick = () => {
  if (confirm("Leave the game? The state stays saved on the server.")) leaveGame();
};
