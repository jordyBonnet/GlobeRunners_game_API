/* ============================================================ GlobeRunners UI — interaction (hand cards) */
"use strict";

import { $$, toast } from "./utils.mjs";
import { cardImg, cardInfo, cardCost, cardTitle } from "./cards.mjs";
import { detectPhase, myTurn, START_MANA_N } from "./phase.mjs";
import { game } from "./game.mjs";
import { renderAll } from "./state.mjs";
import { showCardModal } from "./modals.mjs";

export function makeHandCard(id, interactive, me) {
  const el = document.createElement("div");
  el.className = "card" + (game.selected.has(id) ? " selected" : "") + (!interactive ? " disabled" : "");
  el.dataset.hoverId = id;
  const c = cardInfo(id);
  // playable card: cost ≤ available mana this turn -> green/blue outline
  const cost = cardCost(id);
  const avail = (me && (me.mana || []).length) - ((me && me.mana_spend) || 0);
  if (interactive && cost <= avail) el.classList.add("playable");
  let title;
  if (c && c.card_name) title = cardTitle(id) + ` (mana cost: ${cost})`;
  else if (c && c.name) title = `${c.name} — ${c.faction} (mana cost: ${cost})`;
  else title = id;
  el.title = title;
  el.innerHTML = `<img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;

  // click: selection (1 card for play/mana-pass, up to 3 in init, exactly N in discard selection)
  if (interactive) {
    el.onclick = () => {
      const ph = detectPhase(game.state);
      const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
      const isDiscardSel = ph && ph.kind === "discard" && ph.actor === game.me;
      const maxSel = ph === "init-mana" ? START_MANA_N - ((meNow.mana || []).length)
                   : isDiscardSel ? ph.n : 1;
      if (game.selected.has(id)) game.selected.delete(id);
      else {
        if (ph !== "init-mana" && !isDiscardSel) game.selected.clear();
        if (game.selected.size < maxSel) game.selected.add(id);
        else if (isDiscardSel) toast(`Select exactly ${maxSel} card(s) to discard`);
      }
      renderAll();
    };

    // native drag & drop
    el.draggable = true;
    el.addEventListener("dragstart", (e) => {
      if (!myTurn(game.state) && detectPhase(game.state) !== "init-mana" && detectPhase(game.state) !== "mana-pass") {
        e.preventDefault(); return;
      }
      game.dragCardId = id;
      el.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", id); } catch {}
    });
    el.addEventListener("dragend", () => {
      el.classList.remove("dragging");
      game.dragCardId = null;
      $$(".slot").forEach((s) => s.classList.remove("drop-target", "drop-target-invalid"));
    });

    // right click: zoom
    el.oncontextmenu = (e) => { e.preventDefault(); showCardModal(id); };
  } else {
    el.oncontextmenu = (e) => { e.preventDefault(); showCardModal(id); };
  }
  return el;
}
