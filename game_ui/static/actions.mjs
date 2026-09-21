/* ============================================================ GlobeRunners UI — actions (stopover ordering + play dispatch) */
"use strict";

import { toast } from "./utils.mjs";
import { cardImg, cardTitle, cardInfo, cardCost, isSupportPlay, isEngineerDrop, isEngineerDwelling, isDoctorPending, isDoctorDwelling, DOCTOR_PENDING, isMageCelestial, isMageThermicFlux, isMageApocalypticritual, isMageBlackHole, isSwapCards } from "./cards.mjs";
import { myTurn } from "./phase.mjs";
import { game, enterCellSelect } from "./game.mjs";
import { sendAction } from "./comm.mjs";
import { checkMana } from "./state.mjs";
import { N_STOPOVERS, BIOME_NAMES } from "./board.mjs";

/* -------- ordering rule: PER-PLAYER stopovers (engine_version 15) --------
   Each player has their OWN 5 positions (1..5, columns 4,3,2,1,0). A player's ROOTED
   cards occupy the leading positions of that player's chain (position 1, 2, …), and
   the player's PLAYS (move OR defend) go AFTER the rooted cards (position R+1, …).
   A player's rooted cards NEVER shift the opponent's positions — the two players'
   position numbers are independent. The engine is the source of truth (it overwrites
   the client's stopover); these functions mirror the rule for DISPLAY + validation. */
// number of positions this player has consumed this turn: their plays ('move' OR
// 'defend' actions of the action_chain — a card placed on a stopover, even in
// defense, occupies a position) PLUS the dwelling placeholder if it was placed this
// turn (p.dwelling_slot is set at placement and cleared in the cleaning phase, so
// it is non-null only while the placeholder is showing). The engine increments
// play_count for BOTH the dwelling and each play, so the frontend must too —
// otherwise the next play lands on the same position as the dwelling placeholder.
export function playedCount(st, name) {
  const p = (st && st.players) ? st.players[name] : null;
  if (!p) return 0;
  let n = ((p.action_chain) || []).filter((a) => a && (a.mode === "move" || a.mode === "defend") && a.cards && a.cards.length).length;
  // the dwelling placeholder occupies one position. engine_version 28: it STAYS after
  // the dwelling card is wrecked (only the card goes to the discard) — so the gate is
  // on p.dwelling_slot ALONE (no longer requiring p.dwelling). In games < 28 the engine
  // cleared dwelling_slot on the wreck, so a slot-without-card can only occur in v28+.
  if (p.dwelling_slot != null) n += 1;
  // pending placeholders (doctors, v16): ONLY non-null slots occupy a position.
  // Entry shapes: engine_version 19+ = [card, slot] pair (the per-turn placeholder
  // list — NOT parallel to the persistent pendings zone); v18 = bare int or null
  // (the laboratory TAP's 'epo' had a null slot — no position). Count the slot part.
  if (p.pending_slots) n += p.pending_slots.filter((e) => (Array.isArray(e) ? e[1] : e) != null).length;
  return n;
}
// number of ROOTED cards this player has on the board (they occupy the leading
// positions of that player's chain, engine_version 15)
export function rootedCount(st, name) {
  const stb = (st && st.rooted_on_board) || [];
  return stb.filter((r) => r && r.owner === name).length;
}
// the player's next POSITION (1..N): rooted cards first, then plays (v15 per-player)
export function nextPosition(st, name) {
  return rootedCount(st, name) + playedCount(st, name) + 1;
}
// the player's next STOPOVER column (0..4) for their next play (v15 per-player):
// position p -> column 5-p (position 1 -> stopover_4, …, position 5 -> stopover_0)
export function nextSlotCol(st, name) {
  const pos = nextPosition(st, name);
  return N_STOPOVERS - pos;   // may be negative when the player is past position 5
}
// backward-compat: the set of columns this player can still fill (positions 1..5
// after their rooted cards + plays). Kept so board.mjs / game.mjs / zones.mjs
// (which import freeCols) keep working — it is now PER-PLAYER.
export function freeCols(st, name) {
  const me = (st && st.players) ? st.players[name] : null;
  const r = rootedCount(st, name);
  const played = playedCount(st, name);
  const out = [];
  for (let pos = 1; pos <= N_STOPOVERS; pos++) {
    if (pos <= r) continue;               // occupied by a rooted card
    if (pos <= r + played) continue;      // occupied by an earlier play this turn
    out.push(N_STOPOVERS - pos);          // position -> column
  }
  return out;
}
// (kept for board.mjs which imports it; per-player v15 has no shared occupancy)
export function occupiedCols(st, name) {
  const occ = new Set();
  const r = rootedCount(st, name);
  for (let pos = 1; pos <= Math.max(r, N_STOPOVERS); pos++) if (pos <= r) occ.add(N_STOPOVERS - pos);
  if (st && st.players && st.players[name]) {
    const p = st.players[name];
    if (p.dwelling_slot != null) occ.add(p.dwelling_slot % N_STOPOVERS);   // v28: also after a wreck (placeholder stays)
  }
  return occ;
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
  if (freeCols(st, game.me).length === 0) return false;   // all 5 positions already taken
  return col === nextSlotCol(st, game.me);   // only the player's next position
}

export function playCardToStopover(cardId, col) {
  game.selected = new Set([cardId]);
  dispatchPlay(cardId, col);
}

// help message if the player drops on a not-yet-accessible slot (per-player v15)
export function orderHint() {
  const st = game.state;
  if (freeCols(st, game.me).length === 0) return "All 5 of your stopover positions are full this turn.";
  const pos = nextPosition(st, game.me);
  const col = N_STOPOVERS - pos;
  return `Your card goes on your own position ${pos} (stopover ${col + 1}). Rooted cards and earlier plays occupy the leading positions of YOUR chain — they never shift the opponent's positions.`;
}

/* -------- play dispatch: the single "play this card" path --------
   Used by BOTH the "Play the card" button (col = null -> the next slot in order)
   and the stopover drop/click targets (col given). Handles the special cases:
   engineer drop (pick a cell), engineer dwelling (refinery), doctor pending
   (pending zone), doctor dwelling (laboratory), normal move card (stopover,
   with pending card popup if applicable). */
export function dispatchPlay(cardId, col = null) {
  // engineer drop: needs a target cell -> cell-selection mode
  if (isEngineerDrop(cardId)) { enterCellSelect(cardId); return; }
  // engineer dwelling (refinery): goes to the dwelling zone, not a stopover
  if (isEngineerDwelling(cardId)) {
    if (col == null) {
      const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
      if (meNow.dwelling) { toast("You already have a dwelling — the slot is full"); return; }
      if (freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    }
    if (!checkMana(cardId)) return;
    sendAction([cardId], "dwelling", "");
    game.selected = new Set();
    return;
  }
  // doctor pending card (epo/virus/bloodtest/mercurochrome): goes to the pending zone
  if (isDoctorPending(cardId)) {
    if (col == null) {
      if (freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    }
    if (!checkMana(cardId)) return;
    sendAction([cardId], "pending_zone", "");
    game.selected = new Set();
    return;
  }
  // doctor dwelling (laboratory): goes to the dwelling zone, not a stopover
  if (isDoctorDwelling(cardId)) {
    if (col == null) {
      const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
      if (meNow.dwelling) { toast("You already have a dwelling — the slot is full"); return; }
      if (freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    }
    if (!checkMana(cardId)) return;
    sendAction([cardId], "dwelling", "");
    game.selected = new Set();
    return;
  }
  // Mages black_hole (engine_version 26): a DWELLING card (like refinery/laboratory) —
  // goes to the dwelling zone, not a stopover. Its TAP (the #btn-tap direction popup)
  // rotates the earth 3 cells; placement is identical to the other dwellings.
  if (isMageBlackHole(cardId)) {
    if (col == null) {
      const meNow = (game.state && game.state.players) ? game.state.players[game.me] : {};
      if (meNow.dwelling) { toast("You already have a dwelling — the slot is full"); return; }
      if (freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    }
    if (!checkMana(cardId)) return;
    sendAction([cardId], "dwelling", "");
    game.selected = new Set();
    return;
  }
  // Mages Celestial_reversal (engine_version 22): a MOVE play onto a stopover, but it
  // needs a day/night CHOICE (the INSTANT effect fixes the day/night for the rest of
  // the game). The player picks day or night in a popup; the card is a no-op chain.
  if (isMageCelestial(cardId)) {
    if (col == null && freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    if (!checkMana(cardId)) return;
    const slotCol = col != null ? col : nextSlotCol(game.state, game.me);
    showDayNightPopup(cardId, slotCol);
    return;
  }
  // Mages thermic_flux (engine_version 24): a MOVE play onto a stopover, but it needs
  // a +4/−4 °C CHOICE (the INSTANT effect changes the planet temperature, clamped
  // 1..20, permanently). The player picks +4 or −4 in a popup; the card is a no-op
  // chain (like Celestial_reversal).
  if (isMageThermicFlux(cardId)) {
    if (col == null && freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    if (!checkMana(cardId)) return;
    const slotCol = col != null ? col : nextSlotCol(game.state, game.me);
    showThermicFluxPopup(cardId, slotCol);
    return;
  }
  // Mages Apocalypticritual (engine_version 25): a MOVE play onto a stopover, but it
  // needs an ORDER CHOICE (the INSTANT effect sets the cataclysm pile to the chosen
  // permutation of the 4 biomes — 1st = strikes next). The player arranges the 4
  // biomes in a popup; the card is a no-op chain (like Celestial_reversal).
  if (isMageApocalypticritual(cardId)) {
    if (col == null && freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    if (!checkMana(cardId)) return;
    const slotCol = col != null ? col : nextSlotCol(game.state, game.me);
    showApocalypticritualPopup(cardId, slotCol);
    return;
  }
  // swap_cards (engine_version 29): a MOVE play onto a stopover with an OPTIONAL
  // position swap — the card SWAPS its trip-chain position with one of the
  // player's OWN chain entries (a play, a board placeholder, a rooted card). The
  // player sees their own stopover mirror and drag-and-drops the card onto the
  // target entry (or plays without a swap). The target position is sent as the
  // message `swap_with` field. If the player has pending cards, the attach popup
  // comes FIRST (the pending choice is part of the same action message).
  if (isSwapCards(cardId)) {
    if (col == null && freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
    if (!checkMana(cardId)) return;
    const slotCol = col != null ? col : nextSlotCol(game.state, game.me);
    const meP = (game.state && game.state.players) ? game.state.players[game.me] : null;
    const myPendings = (meP && meP.pendings) || [];
    if (myPendings.length > 0) {
      // chain: pick the pending card first, then the swap target (one action message)
      showPendingPopup(cardId, slotCol, myPendings, (cid, c, pArr) => showSwapPopup(cid, c, pArr));
    } else {
      showSwapPopup(cardId, slotCol);
    }
    return;
  }
  // normal move card onto the stopover (the player's next position when col is absent)
  if (col == null && freeCols(game.state, game.me).length === 0) { toast(orderHint()); return; }
  if (!checkMana(cardId)) return;
  const slotCol = col != null ? col : nextSlotCol(game.state, game.me);
  // Doctors (engine_version 16): if the player has pending cards, show a popup
  // to attach one (or none) to the main card before sending the action.
  const meP = (game.state && game.state.players) ? game.state.players[game.me] : null;
  const myPendings = (meP && meP.pendings) || [];
  if (myPendings.length > 0) {
    showPendingPopup(cardId, slotCol, myPendings);
  } else {
    sendAction([cardId], `stopover_${slotCol}`, "move");
  }
}

/* popup: choose day or night for Celestial_reversal (Mages, engine_version 22).
   The choice is sent with the action message (the `day_night` field) and applied
   INSTANTLY at play time — it fixes the day/night for the rest of the game. */
function showDayNightPopup(cardId, col) {
  const modal = document.createElement("div");
  modal.className = "modal pending-popup";
  const cur = (game.state && game.state.day_night) === "day" ? "day" : "night";
  const options = [
    `<button class="pending-opt" data-dn="day">☀️ Day</button>`,
    `<button class="pending-opt" data-dn="night">🌙 Night</button>`,
  ];
  modal.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-box">
      <h3>Celestial reversal</h3>
      <p class="hint">Pick the day/night for the rest of the game (currently <b>${cur}</b> — it will stop flipping).</p>
      <div class="pending-options">${options.join("")}</div>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelectorAll(".pending-opt").forEach(btn => {
    btn.onclick = () => {
      sendAction([cardId], `stopover_${col}`, "move", [], null, btn.dataset.dn);
      game.selected = new Set();
      modal.remove();
    };
  });
  modal.querySelector(".modal-backdrop").onclick = () => modal.remove();
}

/* popup: choose +4 °C or −4 °C for thermic_flux (Mages, engine_version 24).
   The choice is sent with the action message (the `temp_change` field: "up"|"down")
   and applied INSTANTLY at play time — the planet temperature changes by ±4 °C,
   clamped to 1..20, permanently. */
function showThermicFluxPopup(cardId, col) {
  const modal = document.createElement("div");
  modal.className = "modal pending-popup";
  const cur = (game.state && game.state.temperature != null) ? game.state.temperature : "?";
  const options = [
    `<button class="pending-opt" data-dir="up">🔥 +4 °C</button>`,
    `<button class="pending-opt" data-dir="down">❄️ −4 °C</button>`,
  ];
  modal.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-box">
      <h3>Thermic flux</h3>
      <p class="hint">Change the planet temperature by ±4 °C (currently <b>${cur} °C</b>, clamped 1–20) — permanent.</p>
      <div class="pending-options">${options.join("")}</div>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelectorAll(".pending-opt").forEach(btn => {
    btn.onclick = () => {
      // sendAction(cards, to, mode, pendings, cell, dayNight, tempChange)
      sendAction([cardId], `stopover_${col}`, "move", [], null, null, btn.dataset.dir);
      game.selected = new Set();
      modal.remove();
    };
  });
  modal.querySelector(".modal-backdrop").onclick = () => modal.remove();
}

/* popup: arrange the 4 cataclysm biomes for Apocalypticritual (Mages, engine_version 25).
   The order is sent with the action message (the `cataclysm_order` field: a permutation
   of ["OC","MO","DE","JU"], index 0 = strikes next) and applied INSTANTLY at play time
   — the cataclysm pile is set to that order, permanently (until the next ritual).
   Uniqueness: picking a biome already used in another row SWAPS the two rows, so the
   selection is always a valid permutation. Pre-filled with the current pile order. */
function showApocalypticritualPopup(cardId, col) {
  const BIOMES = ["OC", "MO", "DE", "JU"];
  const pile = (game.state && Array.isArray(game.state.cataclysm_pile)
      && game.state.cataclysm_pile.length === 4) ? game.state.cataclysm_pile : BIOMES;
  const values = [...pile];   // source of truth (slot order)
  const modal = document.createElement("div");
  modal.className = "modal pending-popup";
  const rowHtml = (i) => `
    <div class="cata-order-row">
      <span class="cata-order-pos">${i + 1}.</span>
      <select data-slot="${i}">
        ${BIOMES.map(x => `<option value="${x}"${x === values[i] ? " selected" : ""}>${BIOME_NAMES[x] || x}</option>`).join("")}
      </select>
    </div>`;
  modal.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-box">
      <h3>☄️ Apocalyptic ritual</h3>
      <p class="hint">Choose the order the 4 cataclysm biomes will strike (1st = next to strike) — permanent until the next ritual. Currently: <b>${values.map(b => BIOME_NAMES[b] || b).join(" → ")}</b></p>
      <div class="cata-order">${[0, 1, 2, 3].map(rowHtml).join("")}</div>
      <div class="pending-options"><button class="pending-opt" data-confirm="1">☄️ Set this order</button></div>
    </div>`;
  document.body.appendChild(modal);
  const selects = [...modal.querySelectorAll(".cata-order-row select")];
  selects.forEach((sel, i) => {
    sel.onchange = () => {
      const old = values[i];
      values[i] = sel.value;
      const dup = values.indexOf(sel.value);
      if (dup !== -1 && dup !== i) { values[dup] = old; selects[dup].value = old; }   // swap
    };
  });
  modal.querySelector("[data-confirm]").onclick = () => {
    // sendAction(cards, to, mode, pendings, cell, dayNight, tempChange, cataclysmOrder)
    sendAction([cardId], `stopover_${col}`, "move", [], null, null, null, [...values]);
    game.selected = new Set();
    modal.remove();
  };
  modal.querySelector(".modal-backdrop").onclick = () => modal.remove();
}

/* popup: choose a pending card to attach to the main card (or none) */
/* showPendingPopup: pick a pending card to attach to the play (or none). With an
   `onSend` callback (swap_cards, engine_version 29) the choice is passed to it
   (cardId, col, pendingsArr) INSTEAD of sending — the callback opens the next
   popup in the chain (the swap target), which sends the single action message. */
function showPendingPopup(cardId, col, pendings, onSend = null) {
  const modal = document.createElement("div");
  modal.className = "modal pending-popup";
  const options = ['<button class="pending-opt" data-pending="">⏭ None</button>'];
  for (const p of pendings) {
    const label = DOCTOR_PENDING[p] ? DOCTOR_PENDING[p].label : p;
    options.push(`<button class="pending-opt" data-pending="${p}">📎 ${label}</button>`);
  }
  modal.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-box">
      <h3>Attach a pending card?</h3>
      <p class="hint">Choose a pending card to attach to <b>${cardId}</b>, or skip.</p>
      <div class="pending-options">${options.join("")}</div>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelectorAll(".pending-opt").forEach(btn => {
    btn.onclick = () => {
      const pending = btn.dataset.pending;
      const pendingsArr = pending ? [pending] : [];
      game.selected = new Set();
      modal.remove();
      if (onSend) {
        onSend(cardId, col, pendingsArr);
      } else {
        sendAction([cardId], `stopover_${col}`, "move", pendingsArr);
      }
    };
  });
  modal.querySelector(".modal-backdrop").onclick = () => modal.remove();
}

/* showSwapPopup: swap_cards position swap (engine_version 29). The card being
   played SWAPS its trip-chain position with ONE of the player's OWN chain
   entries (a play, a board placeholder — pending/dwelling — or a rooted card).
   The popup shows the player's own stopover mirror (positions 1..5): the card
   being played (draggable, at its own position) and the other entries. The
   player DRAG-AND-DROPS the card onto the target entry (or clicks the entry) to
   exchange places — the target POSITION is sent as the message `swap_with`
   field (the engine rewrites both entries' stopover columns at play time).
   "Play without swap" plays the card normally (no `swap_with` field — the card
   simply advances as usual). Idempotent: renderAll re-runs on every 2.5 s poll
   — the .swap-popup guard prevents a second instance. Backdrop click closes the
   popup (no action is sent). */
function showSwapPopup(cardId, col, pendingsArr = []) {
  if (document.querySelector(".swap-popup")) return;   // already open (polling re-render)
  const st = game.state;
  const me = (st && st.players) ? st.players[game.me] : {};
  const ownPos = 5 - col;   // the card's own position (column col -> position 5-col)
  // the player's OWN chain entries by position (1..5) — plays, placeholders, rooted
  const cells = {};
  for (const a of (me.action_chain || [])) {
    const m = /^stopover_(\d+)$/.exec(a.to || '');
    if (!m || !a.cards) continue;
    cells[5 - (+m[1])] = { kind: a.mode === 'defend' ? 'defend' : 'play', label: a.cards[0] };
  }
  for (const e of (me.pending_slots || [])) {
    const slot = Array.isArray(e) ? e[1] : e;
    if (slot == null) continue;
    cells[5 - (+slot)] = { kind: 'pending', label: (Array.isArray(e) ? e[0] : '?') };
  }
  if (me.dwelling_slot != null) {
    cells[5 - (+me.dwelling_slot)] = { kind: 'dwelling', label: me.dwelling || 'dwelling' };
  }
  for (const r of ((st && st.rooted_on_board) || [])) {
    if (!r || r.owner !== game.me) continue;
    const m = /^stopover_(\d+)$/.exec(r.stopover || '');
    if (!m) continue;
    cells[5 - (+m[1])] = { kind: 'rooted', label: r.card_id || r.card || 'rooted' };
  }
  const targets = [1,2,3,4,5].filter(p => p !== ownPos && cells[p]);
  const kindLabel = { play: 'card', defend: 'defense', pending: 'pending', dwelling: 'dwelling', rooted: 'rooted' };
  const kindIcon = { play: '🎴', defend: '🛡', pending: '⏳', dwelling: '🏠', rooted: '🌿' };
  const nameOf = (id) => (cardTitle(id) || id).split(' — ')[0];
  const chips = [1,2,3,4,5].map(pos => {
    const entry = cells[pos];
    const isOwn = pos === ownPos;
    const isTarget = targets.includes(pos);
    let inner = `<span class="swap-pos">stopover ${pos}</span>`;
    if (isOwn) inner += `<span class="swap-card" draggable="true">🎴 ${nameOf(cardId)} <i>(this card — drag me)</i></span>`;
    else if (entry) inner += `${kindIcon[entry.kind] || ''} ${nameOf(entry.label)} <i>(${kindLabel[entry.kind]})</i>`;
    else inner += '<i>empty</i>';
    return `<div class="swap-cell${isOwn ? ' swap-own' : ''}${isTarget ? ' swap-target' : ''}"${isTarget ? ` data-pos="${pos}"` : ''}>${inner}</div>`;
  }).join('');
  const modal = document.createElement("div");
  modal.className = "modal pending-popup swap-popup";
  modal.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-box">
      <h3>🔀 Swap positions?</h3>
      <p class="hint">Drag <b>${nameOf(cardId)}</b> onto one of your own chain entries to exchange trip-chain positions (or click the entry). Or play it without swapping.</p>
      <div class="swap-grid">${chips}</div>
      <div class="pending-options"><button class="pending-opt swap-noswap">▶ Play without swap</button></div>
    </div>`;
  document.body.appendChild(modal);
  const doSend = (swapPos) => {
    if (swapPos) {
      sendAction([cardId], `stopover_${col}`, "move", pendingsArr, null, null, null, null, null, swapPos);
    } else {
      sendAction([cardId], `stopover_${col}`, "move", pendingsArr);
    }
    game.selected = new Set();
    modal.remove();
  };
  // drag-and-drop: the card chip (draggable) onto a target cell
  const dragCard = modal.querySelector(".swap-card");
  if (dragCard) {
    dragCard.addEventListener('dragstart', (ev) => { ev.dataTransfer.setData('text/plain', cardId); ev.dataTransfer.effectAllowed = 'move'; });
  }
  modal.querySelectorAll('.swap-target').forEach(cellEl => {
    cellEl.addEventListener('dragover', (ev) => { ev.preventDefault(); ev.dataTransfer.dropEffect = 'move'; cellEl.classList.add('dragover'); });
    cellEl.addEventListener('dragleave', () => cellEl.classList.remove('dragover'));
    cellEl.addEventListener('drop', (ev) => { ev.preventDefault(); cellEl.classList.remove('dragover'); doSend(+cellEl.dataset.pos); });
    cellEl.addEventListener('click', () => doSend(+cellEl.dataset.pos));   // click fallback (touch / no DnD)
  });
  modal.querySelector('.swap-noswap').onclick = () => doSend(null);
  modal.querySelector(".modal-backdrop").onclick = () => modal.remove();
}

/* popup: DISCARD SELECTION (engine_version ≥ 13). When the engine pauses the trip
   chain on "waiting for NAME to discard K card(s)", this modal lists ALL of the player's
   hand cards; the player toggles exactly N of them (cap N, toast on overflow) and presses
   the red DISCARD button at the bottom, which sends {cards:[…N…], to:"discard_pile",
   mode:""}. The selection is LOCAL to the popup (not game.selected) — the hand behind
   the backdrop is untouched. Idempotent: renderAll runs on every 2.5 s poll, so the
   .discard-popup guard prevents a second instance. Backdrop click closes it (the engine
   stays paused — a hand-card click reopens it, and renderAll re-opens it on the next
   state too). A rejected answer (wrong count / not in hand / not my turn) toasts and
   keeps the popup + selection open; on success the modal is removed (the state change
   also sweeps it in renderAll). */
export function showDiscardPopup(n) {
  if (document.querySelector(".discard-popup")) return;   // already open (polling re-render)
  const st = game.state;
  if (!st || !st.players) return;
  const me = st.players[game.me];
  const hand = (me && me.hand) || [];
  const selected = new Set();
  const modal = document.createElement("div");
  modal.className = "modal pending-popup discard-popup";
  modal.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-box">
      <h3>🗑 Discard ${n} card(s)</h3>
      <p class="hint">Select exactly ${n} card(s) from your hand, then press DISCARD.</p>
      <div class="discard-cards"></div>
      <div class="discard-count">0 / ${n} selected</div>
      <div class="pending-options"><button class="discard-btn" disabled>🗑 DISCARD</button></div>
    </div>`;
  const grid = modal.querySelector(".discard-cards");
  const countEl = modal.querySelector(".discard-count");
  const confirm = modal.querySelector(".discard-btn");
  for (const id of hand) {
    const el = document.createElement("div");
    el.className = "card";
    el.dataset.hoverId = id;   // delegated 3x hover preview (modals.mjs)
    el.title = cardTitle(id);
    el.innerHTML = `<img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;
    el.onclick = () => {
      if (selected.has(id)) selected.delete(id);
      else {
        if (selected.size >= n) { toast(`Select exactly ${n} card(s) to discard`); return; }
        selected.add(id);
      }
      el.classList.toggle("selected", selected.has(id));
      countEl.textContent = `${selected.size} / ${n} selected`;
      confirm.disabled = selected.size !== n;
    };
    grid.appendChild(el);
  }
  confirm.onclick = async () => {
    if (selected.size !== n) return;
    confirm.disabled = true;   // in flight (sends are serialized anyway)
    const resp = await sendAction([...selected], "discard_pile", "");
    if (resp && resp.success === false) {
      toast(resp.message || "Action refused");   // rejected: keep popup + selection
      confirm.disabled = false;
      return;
    }
    modal.remove();   // success (the state change's renderAll sweep double-covers)
  };
  document.body.appendChild(modal);
  modal.querySelector(".modal-backdrop").onclick = () => modal.remove();
}

/* Popup: DEFENSE SELECTION. When the player clicks "Play in defense", this modal
   lists all cards in the player's hand; the player toggles 1–5 MAIN cards (max 5,
   toast on overflow) and presses the ⛨ DEFEND button at the bottom, which sends
   {cards:[…1..5…], to:"stopover_N", mode:"defend"} to the player's NEXT stopover
   position. Rules (engine): the cost is the SUM of the selected cards' mana, and
   the SHIELD is the SUM of their shields — the block on the opponent's card on
   that stopover only holds if Σ shield ≥ the opponent card's cost. Support cards
   have no shield in the pool and CANNOT be defended — they are listed but marked
   unselectable. The selection is LOCAL to the popup (not game.selected, so it does
   not interfere with move/mana selection). Idempotent (.defend-popup guard).
   Backdrop click closes; a rejected answer toasts and keeps the popup + selection
   open; on success the modal is removed (the polling re-render also sweeps it). */
export function showDefendPopup() {
  if (document.querySelector(".defend-popup")) return;   // already open
  const st = game.state;
  if (!st || !st.players) return;
  const me = st.players[game.me];
  const hand = (me && me.hand) || [];
  const main = hand.filter((id) => !isSupportPlay(id));
  if (!main.length) { toast("No main-faction card in hand to defend with"); return; }
  if (freeCols(st, game.me).length === 0) { toast(orderHint()); return; }
  const avail = (me.mana || []).length - (me.mana_spend || 0);
  const MAX_DEFEND = 5;
  const selected = new Set();
  const costOf  = (id) => cardCost(id) || 0;
  const shieldOf = (id) => { const c = cardInfo(id); return c && c.shield != null ? c.shield : 0; };
  const modal = document.createElement("div");
  modal.className = "modal pending-popup defend-popup";
  modal.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-box">
      <h3>⛨ Play in defense</h3>
      <p class="hint">Pick 1–${MAX_DEFEND} card(s) to engage sideways (90°) on your next stopover. They block the opponent's card on that stopover: your <b>total shield</b> must be ≥ the cost of their card. Cost = the sum of the selected cards' mana (you have <b>${avail}</b>).</p>
      <div class="defend-cards"></div>
      <div class="defend-summary"><span class="defend-count">0 / ${MAX_DEFEND} selected</span> • <span class="defend-cost">cost 0</span> • <span class="defend-shield">shield 0</span></div>
      <div class="pending-options"><button class="defend-confirm" disabled>⛨ DEFEND</button></div>
    </div>`;
  const grid = modal.querySelector(".defend-cards");
  const countEl = modal.querySelector(".defend-count");
  const costEl = modal.querySelector(".defend-cost");
  const shieldEl = modal.querySelector(".defend-shield");
  const confirm = modal.querySelector(".defend-confirm");
  const refresh = () => {
    let cost = 0, shield = 0;
    for (const id of selected) { cost += costOf(id); shield += shieldOf(id); }
    countEl.textContent = `${selected.size} / ${MAX_DEFEND} selected`;
    costEl.textContent = `cost ${cost}`;
    shieldEl.textContent = `shield ${shield}`;
    confirm.disabled = selected.size === 0 || cost > avail;
    confirm.title = cost > avail ? `Not enough mana: cost ${cost}, only ${avail} left` : "";
  };
  for (const id of hand) {
    const support = isSupportPlay(id);   // support cards cannot be defended
    const el = document.createElement("div");
    el.className = "card" + (support ? " defend-disabled" : "");
    el.dataset.hoverId = id;
    let title = cardTitle(id);
    if (support) title += " — support card (cannot be defended)";
    else title += ` (cost ${cardCost(id)}, shield ${shieldOf(id)})`;
    el.title = title;
    el.innerHTML = `<img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">`;
    if (!support) {
      el.onclick = () => {
        if (selected.has(id)) { selected.delete(id); }
        else {
          if (selected.size >= MAX_DEFEND) { toast(`You can defend with at most ${MAX_DEFEND} card(s)`); return; }
          selected.add(id);
        }
        el.classList.toggle("selected", selected.has(id));
        refresh();
      };
    } else {
      el.onclick = () => toast("Support cards cannot be played in defense");
    }
    grid.appendChild(el);
  }
  refresh();
  confirm.onclick = async () => {
    if (selected.size === 0) return;
    const cost = [...selected].reduce((s, id) => s + costOf(id), 0);
    if (cost > avail) { toast(`Not enough mana: cost ${cost}, only ${avail} left`); return; }
    const st2 = game.state;
    if (freeCols(st2, game.me).length === 0) { toast(orderHint()); return; }
    confirm.disabled = true;
    const col = nextSlotCol(st2, game.me);
    const resp = await sendAction([...selected], `stopover_${col}`, "defend");
    if (resp && resp.success === false) {
      toast(resp.message || "Action refused");
      confirm.disabled = false;
      return;
    }
    game.selected = new Set();
    modal.remove();
  };
  document.body.appendChild(modal);
  modal.querySelector(".modal-backdrop").onclick = () => modal.remove();
}

/* popup: choose the earth-rotation direction for the black_hole DWELLING tap
   (Mages, engine_version 26). The choice is sent with the tap message (the `rotation`
   field: "cw"|"ccw") and rotates the earth 3 cells (the 4 biomes shift position; every
   token stays on its cell). Free + once per turn, like the other dwelling taps. */
export function showBlackHoleRotationPopup() {
  const modal = document.createElement("div");
  modal.className = "modal pending-popup";
  const options = [
    `<button class="pending-opt" data-rotation="cw">↻ Clockwise</button>`,
    `<button class="pending-opt" data-rotation="ccw">↺ Counter-clockwise</button>`,
  ];
  modal.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-box">
      <h3>🕳 Black hole</h3>
      <p class="hint">Rotate the earth 3 cells (the biomes shift position — every token stays where it is).</p>
      <div class="pending-options">${options.join("")}</div>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelectorAll(".pending-opt").forEach(btn => {
    btn.onclick = () => {
      // sendAction(cards, to, mode, pendings, cell, dayNight, tempChange, cataclysmOrder, rotation)
      sendAction([], "dwelling", "dwelling_activation", [], null, null, null, null, btn.dataset.rotation);
      game.selected = new Set();
      modal.remove();
    };
  });
  modal.querySelector(".modal-backdrop").onclick = () => modal.remove();
}
