/* ============================================================ GlobeRunners UI — communication */
/* WebSocket (the only action channel) + state polling (to see the opponent),
   and applyState() — the single funnel every state update goes through. */
"use strict";

import { $, api, toast } from "./utils.mjs";
import { game, exitCellSelect } from "./game.mjs";
import { detectPhase } from "./phase.mjs";
import { snapshotZones, animateZoneTransitions, REDUCED_MOTION } from "./anim.mjs";
import { renderAll } from "./state.mjs";
import { showServerMsg, showEndgame } from "./modals.mjs";

/* ---------------- websocket ---------------- */
export function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/${encodeURIComponent(game.id)}/${encodeURIComponent(game.me)}`;
}

let pendingResolvers = [];   // one resolver per WS message waiting for a reply

export function connectWs() {
  if (game.ws && game.ws.readyState <= WebSocket.OPEN) return;
  setConn(false);
  const ws = new WebSocket(wsUrl());
  game.ws = ws;

  ws.onopen = () => setConn(true);
  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    // a rejection has the shape {success: false, message}; a game state has no 'success' key
    if (data && data.success === false) showServerMsg(data.message || "Action refused");
    else applyState(data);
    const resolver = pendingResolvers.shift();
    if (resolver) resolver(data);
  };
  ws.onclose = () => setConn(false);
}

export function sendAction(cards, to, mode, pendings = [], cell = null) {
  /* sends an action and resolves with the server's reply (state or rejection).
     Sends are serialized: one WS message at a time.
     `cell` (0-23): target earth cell for an engineer drop placement (engine_version 12). */
  return new Promise((resolve) => {
    const doSend = () => {
      if (!game.ws || game.ws.readyState !== WebSocket.OPEN) {
        toast("Connection lost — retrying…");
        connectWs();
        setTimeout(doSend, 1500);
        return;
      }
      pendingResolvers.push(resolve);
      const msg = { cards: cards || [], to: to || "", mode: mode || "", pendings };
      if (cell != null) msg.cell = cell;
      game.ws.send(JSON.stringify(msg));
    };
    doSend();
  });
}

function setConn(ok) {
  const el = $("#conn-state");
  el.textContent = ok ? (game.id ? `● connected to game ${game.id}` : "● connected") : "○ disconnected";
  el.className = ok ? "conn-ok" : "conn-bad";
  if (!ok && game.id) setTimeout(connectWs, 2000);   // auto-reconnect
}

/* ---------------- state polling (see the opponent's actions) ---------------- */
export function startPolling() {
  stopPolling();
  const poll = async () => {
    if (!game.id) return;
    try {
      const st = await api(`/api/state/${encodeURIComponent(game.id)}/${encodeURIComponent(game.me)}`);
      if (st && st.players) applyState(st, true);
    } catch { /* server temporarily unavailable */ }
    game.pollTimer = setTimeout(poll, 2500);
  };
  poll();
}

export function stopPolling() { if (game.pollTimer) clearTimeout(game.pollTimer); game.pollTimer = null; }

/* ---------------- state application ---------------- */
export function applyState(st, fromPolling) {
  const wasOver = game.state && detectPhase(game.state) === "over";
  // card movement animation: snapshot the CURRENT DOM zones before the re-render, so the
  // old-state → new-state diff can fly the cards between zones (deck→hand, hand→stopover,
  // stopover→discard, …) — see the anim module
  const oldSt = game.state;
  const snap = (oldSt && st && oldSt.players && st.players && oldSt.id === st.id && !REDUCED_MOTION) ? snapshotZones() : null;
  exitCellSelect();   // any new state ends cell-selection mode (a placement / opponent move happened)
  game.state = st;
  renderAll();
  if (snap) animateZoneTransitions(oldSt, st, snap);
  if (st && st.message && typeof st.message === "object") {
    const txt = st.message.message || "";
    const isFailure = !st.message.success || /tried|must select|not enough/i.test(txt);
    showServerMsg(isFailure ? txt : "");   // empty -> cleared after 3 s
  }

  // end of game
  const over = detectPhase(st) === "over";
  if (over && !game.lastWinnerShown) {
    game.lastWinnerShown = true;
    stopPolling();
    showEndgame(st);
  } else if (!over && wasOver) {
    startPolling();   // new game on the same page? restart polling
  }
}
