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
let wsGen = 0;               // generation counter — a replaced/closed socket's late events are stale
let reconnectTimer = null;   // the pending "retry in 2 s" timer (one at a time)
let retryToasted = false;    // "Connection lost" is toasted once per disconnect period, not per retry

/* In-flight actions never get their reply after a socket dies: settle them with a
   rejection-shaped sentinel so awaiting UI (discard/defend popups, the init-mana loop)
   un-hangs, toasts and lets the player retry — the 2.5 s poll re-syncs the state anyway. */
function settlePending() {
  if (!pendingResolvers.length) return;
  const resolvers = pendingResolvers; pendingResolvers = [];
  const r = { success: false, message: "Connection lost — the action may not have gone through" };
  for (const fn of resolvers) fn(r);
}

export function connectWs() {
  if (game.ws && game.ws.readyState <= WebSocket.OPEN) return;
  const gen = ++wsGen;   // invalidate the previous socket's handlers (leaveGame / reconnect races)
  const ws = new WebSocket(wsUrl());
  game.ws = ws;
  const isLive = () => gen === wsGen && game.ws === ws;

  ws.onopen = () => { if (isLive()) { retryToasted = false; setConn(true); } };
  ws.onmessage = (ev) => {
    if (!isLive()) return;   // reply that outlived its socket — the new socket owns the conversation
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    // a rejection has the shape {success: false, message}; a game state has no 'success' key
    if (data && data.success === false) showServerMsg(data.message || "Action refused");
    else applyState(data);
    const resolver = pendingResolvers.shift();
    if (resolver) resolver(data);
  };
  ws.onclose = () => {
    if (!isLive()) return;   // stale socket (a newer one exists / we left the game) — ignore
    settlePending();
    setConn(false);          // in-game: schedules the 2 s retry
  };
  ws.onerror = () => { /* onclose always follows; nothing to do */ };
}

/* Called when leaving the game: invalidates the socket (no spurious reconnect with the
   old game id), closes it, and settles any in-flight action promises. */
export function teardownWs() {
  wsGen++;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  const had = !!game.ws;
  if (had) { try { game.ws.close(); } catch {} game.ws = null; }
  settlePending();
}

export function sendAction(cards, to, mode, pendings = [], cell = null, dayNight = null, tempChange = null, cataclysmOrder = null, rotation = null, swapWith = null) {
  /* sends an action and resolves with the server's reply (state or rejection).
     Sends are serialized: one WS message at a time.
     `cell` (0-23): target earth cell for an engineer drop placement.
     `dayNight` ('day'/'night'): the Mages Celestial_reversal day/night choice
     — sent as the message `day_night` field.
     `tempChange` ('up'/'down'): the Mages thermic_flux +4/−4 °C choice
     — sent as the message `temp_change` field.
     `cataclysmOrder` (4-biome permutation, e.g. ['OC','DE','JU','MO']): the Mages
     Apocalypticritual cataclysm-order choice — sent as the message
     `cataclysm_order` field (index 0 = strikes next).
     `rotation` ('cw'/'ccw'): the Mages black_hole DWELLING tap direction
     — sent as the message `rotation` field (only for a
     dwelling_activation tap of the black_hole; omitted for every other action).
     `swapWith` (1..5): the swap_cards position-swap choice
     — the position of the OWN chain entry the card swaps with (a play, a board
     placeholder, a rooted card); omitted when the card is played without a swap. */
  return new Promise((resolve) => {
    let inFlight = false;   // the resolver is queued (sent, reply pending) — a close settles it,
                            // and a retry must not queue it twice (double-send / mis-resolved reply)
    const doSend = () => {
      if (!game.ws || game.ws.readyState !== WebSocket.OPEN) {
        if (!retryToasted) { retryToasted = true; toast("Connection lost — retrying…"); }
        connectWs();
        setTimeout(doSend, 1500);
        return;
      }
      if (!inFlight) { inFlight = true; pendingResolvers.push(resolve); }
      const msg = { cards: cards || [], to: to || "", mode: mode || "", pendings };
      if (cell != null) msg.cell = cell;
      if (dayNight != null) msg.day_night = dayNight;
      if (tempChange != null) msg.temp_change = tempChange;
      if (cataclysmOrder != null) msg.cataclysm_order = cataclysmOrder;
      if (rotation != null) msg.rotation = rotation;
      if (swapWith != null) msg.swap_with = swapWith;
      game.ws.send(JSON.stringify(msg));
    };
    doSend();
  });
}

function setConn(ok) {
  const el = $("#conn-state");
  if (ok) {
    el.textContent = game.id ? `● connected to game ${game.id}` : "● connected";
    el.className = "conn-ok";
    return;
  }
  // in-game → keep retrying until the socket is back (e.g. after a host sleep/wake);
  // out of a game (setup page) → just report disconnected, no reconnect loop
  if (game.id) {
    el.textContent = "○ reconnecting…";
    el.className = "conn-bad";
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectWs, 2000);
  } else {
    el.textContent = "○ disconnected";
    el.className = "conn-bad";
  }
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
  // cell-select (engineer drop) ends ONLY when the state actually CHANGED (a placement /
  // opponent move / rejection happened). The 2.5 s poll re-applies the SAME state while the
  // player is choosing a cell — an unconditional exit made the circle targets vanish within
  // 2.5 s of entering the mode (the "circles disappear before I can click" glitch).
  // Both states come from the same server serializer (stable key order), so a content
  // comparison is reliable; the state has no per-request volatile fields (see GameState).
  const stateChanged = !(oldSt && st && oldSt.id === st.id
    && JSON.stringify(oldSt) === JSON.stringify(st));
  if (stateChanged) exitCellSelect();
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
