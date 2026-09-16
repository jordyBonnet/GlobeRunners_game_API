/* ============================================================ GlobeRunners UI — phase detection */
/* Parses the engine's `state` string. ALL button/drag enablement derives from these two. */
"use strict";

import { game } from "./game.mjs";

export const START_MANA_N = 3;   // cards to put in mana at initialization

export function detectPhase(st) {
  const s = st.state || "";
  if (s === "game over") return "over";
  if (s.includes("waiting for both players to put")) return "init-mana";
  if (s.includes("waiting for both players to mana or pass")) return "mana-pass";

  // discard selection (engine_version 13): the trip chain paused so the discarding
  // player can choose which cards to discard — "turn N - waiting for NAME to discard K card(s)"
  const md = s.match(/turn (\d+) - waiting for (.+?) to discard (\d+) card\(s\)/);
  if (md) return { kind: "discard", turn: +md[1], actor: md[2], n: +md[3] };

  // play phase: "turn N - waiting for first/second player (NAME) to play"
  const m = s.match(/turn (\d+) - waiting for (first|second) player \((.+?)\) to play/);
  if (m) {
    return { kind: "play", turn: +m[1], actor: m[3] };
  }
  // between the two actions of a turn, the state may be transient
  const t = s.match(/turn (\d+)/);
  if (t) return { kind: "play-waiting", turn: +t[1] };
  return "unknown";
}

export function myTurn(st) {
  const ph = detectPhase(st);
  if (!ph || typeof ph !== "object") return false;
  return ph.kind === "play" && ph.actor === game.me;
}
