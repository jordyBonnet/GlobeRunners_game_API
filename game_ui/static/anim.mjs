/* ============================================================ GlobeRunners UI — card movement animation */
/* Every new state is re-rendered from scratch, so cards would simply "pop" into their new
   zone. To make it obvious WHERE cards come from and WHERE they go, we diff the previous
   and the new state per player (hand / mana / deck / discard / dwelling / stopovers) and fly
   a lightweight clone of each changed card from its old zone to its new zone.
   - MY cards are matched by id (each flies from its exact hand position);
   - the OPPONENT is masked (counts only) so for them the diff works by counts (card backs).
   Covered transitions: play/defend (hand→stopover), cleaning & mid-chain-win flush
   (stopover→discard), draw (deck→hand), mana placement (hand→mana), ramp (deck→mana),
   taxation (mana→discard), discard selection & discard effects (hand→discard),
   dwelling place (hand→dwelling) / wrecking ball (dwelling→discard), reshuffle (discard→deck).
   Purely visual — no state, no messages, no engine coupling (same contract as playChainAnim). */
"use strict";

import { $$ } from "./utils.mjs";
import { cardImg } from "./cards.mjs";
import { game } from "./game.mjs";
import { publicCount } from "./zones.mjs";
import { actionStopoverNum } from "./actions.mjs";
import { N_STOPOVERS, slotEls } from "./board.mjs";

export const FLY_MS = 520;                          // one card flight duration
export const FLY_STAGGER_MS = 110;                  // delay between consecutive flights
export const REDUCED_MOTION = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

// rect of the element's LAYOUT box. getBoundingClientRect() includes the element's
// current transform — inline AND a still-running (or even FROZEN) transition. In a
// hidden/throttled tab the 450 ms flip transition freezes at its start value, so
// the card's VISUAL position stays offset forever; reading that offset as the old
// position made every ~2.5 s poll re-detect the delta and re-trigger the slide —
// the "one hand card re-animates every ~2 s" glitch (2026-09-24). Subtracting the
// current COMPUTED transform (getComputedStyle sees inline, class, animation and
// in-flight transition values alike) gives the true layout position in every case.
function layoutRect(el) {
  const r = el.getBoundingClientRect();
  const m = /matrix\(([^)]+)\)/.exec(getComputedStyle(el).transform || "");
  if (!m) return r;                                       // transform: none
  const p = m[1].split(",").map(parseFloat);
  return { ...r, left: r.left - p[4], top: r.top - p[5] };
}

// capture the rects of the current (OLD) DOM zones — must run BEFORE renderAll() rewrites it
// NOTE: the stopover rows are #row-me / #row-oppo (and slotEls.me / slotEls.oppo),
//       while the zone panels are #my-* / #oppo-* — the two row ids differ!
export function snapshotZones() {
  const snap = { hand: {}, stopover: {} };
  for (const name of Object.keys((game.state && game.state.players) || {})) {
    const row = name === game.me ? "my" : "oppo";
    const boardRow = name === game.me ? "me" : "oppo";
    const hand = [];
    const handEl = document.getElementById(row + "-hand");
    if (handEl) for (const el of handEl.querySelectorAll(".card")) {
      hand.push({
        id: (el.dataset.hoverId && el.dataset.hoverId !== "__back__") ? el.dataset.hoverId : null,
        rect: layoutRect(el),
      });
    }
    snap.hand[name] = hand;
    const rowEl = document.getElementById("row-" + boardRow);
    if (rowEl) for (const slot of rowEl.querySelectorAll(".slot")) {
      const played = slot.querySelector(".played");
      if (played) snap.stopover[name + "|" + (parseInt(slot.dataset.col, 10) % N_STOPOVERS)] = played.getBoundingClientRect();
    }
  }
  return snap;
}

// anchor rect of a zone container (static in the DOM — its position is the same before/after the re-render)
function anchorRect(player, zone) {
  const row = player === game.me ? "my" : "oppo";
  const sel = {
    hand: "#" + row + "-hand",
    mana: "#" + row + "-mana-cards",
    deck: "#" + row + "-side .side-panel.deck",
    discard: "#" + row + "-side .side-panel.discard",
    dwelling: "#" + row + "-side .side-panel.dwelling",
  }[zone];
  const el = sel ? document.querySelector(sel) : null;
  return el ? el.getBoundingClientRect() : null;
}

// diff one player's zones between two states -> list of {player, id|null, from, to, col?, idx?}
// card ids are unique per deck, so the action_chain diff is a plain set diff on ids.
function planPlayerMoves(oldP, newP, isMe, player) {
  const moves = [];
  const handN = (p) => isMe ? (p.hand || []).length : publicCount(p, "hand_count");
  const manaN = (p) => isMe ? (p.mana || []).length : publicCount(p, "mana_count");
  const deckN = (p) => isMe ? (p.deck || []).length : publicCount(p, "deck_count");
  const discN = (p) => (p.discard || []).length;   // the discard pile is public for both players

  // --- stopovers (public action_chain): NEW plays came from the hand; REMOVED plays
  //     (cleaning, or the mid-chain-win flush) go to the discard ---
  const acts = (p) => (p.action_chain || []).filter((a) => a && (a.mode === "move" || a.mode === "defend") && a.cards && a.cards.length);
  const oldActs = acts(oldP), newActs = acts(newP);
  const oldIds = new Set(oldActs.flatMap((a) => a.cards));
  const newIds = new Set(newActs.flatMap((a) => a.cards));
  const newPlays = [];   // {id, col}
  for (const a of newActs) {
    const col = actionStopoverNum(a) % N_STOPOVERS;
    for (const id of a.cards) if (!oldIds.has(id)) newPlays.push({ id, col });
  }
  for (const a of oldActs) {
    const col = actionStopoverNum(a) % N_STOPOVERS;
    for (const id of a.cards) if (!newIds.has(id)) moves.push({ player, id, from: "stopover", col, to: "discard" });
  }
  for (const np of newPlays) moves.push({ player, id: np.id, from: "hand", to: "stopover", col: np.col });

  // --- dwelling: placed from the hand, or wrecked (→ discard) ---
  const oldDw = oldP.dwelling, newDw = newP.dwelling;
  if (oldDw && newDw !== oldDw) moves.push({ player, id: oldDw, from: "dwelling", to: "discard" });
  if (newDw && newDw !== oldDw) moves.push({ player, id: newDw, from: "hand", to: "dwelling" });

  // --- hand / mana / deck / discard ---
  const manaInc = Math.max(0, manaN(newP) - manaN(oldP));
  let manaFromHand = 0;
  if (isMe) {
    const oldHand = oldP.hand || [], newHand = newP.hand || [];
    const oldSet = new Set(oldHand), newSet = new Set(newHand);
    for (const id of newHand) if (!oldSet.has(id)) moves.push({ player, id, from: "deck", to: "hand" });   // draw
    const newPlaySet = new Set(newPlays.map((x) => x.id));
    for (const id of oldHand) {
      if (newSet.has(id)) continue;
      if (newPlaySet.has(id)) continue;            // already emitted as hand→stopover
      if (id === newDw) continue;                  // already emitted as hand→dwelling
      if (manaInc > manaFromHand) { moves.push({ player, id, from: "hand", to: "mana" }); manaFromHand++; continue; }
      moves.push({ player, id, from: "hand", to: "discard" });   // discard selection / discard effect / fallback
    }
  } else {
    // opponent's hand is hidden — work by counts (card backs)
    const drawnN = Math.max(0, handN(newP) - handN(oldP));
    for (let i = 0; i < drawnN; i++) moves.push({ player, id: null, from: "deck", to: "hand", idx: i });
    let lostN = Math.max(0, handN(oldP) - handN(newP));
    lostN = Math.max(0, lostN - newPlays.length);          // the new stopover plays left the hand (already emitted)
    if (newDw && !oldDw) lostN = Math.max(0, lostN - 1);   // the dwelling placement
    manaFromHand = Math.min(lostN, manaInc);
    for (let i = 0; i < manaFromHand; i++) moves.push({ player, id: null, from: "hand", to: "mana", idx: i });
    lostN -= manaFromHand;
    for (let i = 0; i < lostN; i++) moves.push({ player, id: null, from: "hand", to: "discard", idx: i });
  }

  // --- mana: leftover increase = ramp (deck→mana); decrease = taxation (mana→discard) ---
  const rampN = Math.max(0, manaInc - manaFromHand);
  for (let i = 0; i < rampN; i++) moves.push({ player, id: null, from: "deck", to: "mana", idx: i });
  const taxN = Math.max(0, manaN(oldP) - manaN(newP));
  for (let i = 0; i < taxN; i++) moves.push({ player, id: null, from: "mana", to: "discard", idx: i });

  // --- reshuffle: the discard empties back into the deck ---
  const reshN = Math.min(Math.max(0, discN(oldP) - discN(newP)), Math.max(0, deckN(newP) - deckN(oldP)));
  for (let i = 0; i < reshN; i++) moves.push({ player, id: null, from: "discard", to: "deck", idx: i });

  return moves;
}

function sourceRect(m, snap) {
  if (m.from === "hand") {
    const arr = snap.hand[m.player] || [];
    if (m.id) {
      const hit = arr.find((h) => h.id === m.id);
      if (hit) return hit.rect;
    }
    if (arr.length) return arr[Math.max(0, arr.length - 1 - (m.idx || 0))].rect;   // opponent: the last backs
    return null;
  }
  if (m.from === "stopover") return snap.stopover[m.player + "|" + m.col] || null;
  return anchorRect(m.player, m.from);   // deck / mana / discard / dwelling
}

function destRect(m) {
  const row = m.player === game.me ? "my" : "oppo";              // zone panels: #my-hand / #oppo-hand
  const boardRow = m.player === game.me ? "me" : "oppo";          // stopover rows: slotEls.me / slotEls.oppo
  if (m.to === "hand") {
    if (m.id) {
      const el = Array.from(document.querySelectorAll("#" + row + "-hand .card")).find((c) => c.dataset.hoverId === m.id);
      if (el) return el.getBoundingClientRect();
    }
    const cards = Array.from(document.querySelectorAll("#" + row + "-hand .card"));
    if (cards.length) return cards[Math.max(0, cards.length - 1 - (m.idx || 0))].getBoundingClientRect();   // opponent: the new backs
    return anchorRect(m.player, "hand");
  }
  if (m.to === "stopover") {
    const slot = slotEls[boardRow] && slotEls[boardRow][m.col];
    return slot ? slot.getBoundingClientRect() : null;
  }
  return anchorRect(m.player, m.to);
}

// one flying clone: starts at `fromRect` (old zone), glides to `toRect` (new zone) while fading
function flyCard(fromRect, toRect, cardId, delay) {
  const go = () => {
    const el = document.createElement("div");
    el.className = "card-fly" + (cardId ? "" : " back");
    if (cardId) {
      const img = document.createElement("img");
      img.alt = "";
      img.src = cardImg(cardId);
      img.onerror = () => { img.onerror = null; img.src = "/placeholder.svg"; };
      el.appendChild(img);
    }
    const w = fromRect.width, h = fromRect.height;
    el.style.left = fromRect.left + "px";
    el.style.top = fromRect.top + "px";
    el.style.width = w + "px";
    el.style.height = h + "px";
    // the clone lives on <body> (outside the themed scopes) — copy the current
    // faction accent so its glow ring matches the game's palette (theme.mjs)
    const vgame = document.getElementById("view-game");
    if (vgame) {
      const acc = getComputedStyle(vgame).getPropertyValue("--accent").trim();
      if (acc) el.style.setProperty("--accent", acc);
    }
    document.body.appendChild(el);
    const dx = (toRect.left + toRect.width / 2) - (fromRect.left + w / 2);
    const dy = (toRect.top + toRect.height / 2) - (fromRect.top + h / 2);
    // two rAFs so the browser commits the start position before the transition runs
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.style.transform = "translate(" + dx + "px," + dy + "px) scale(.82)";
      el.style.opacity = "0";
    }));
    setTimeout(() => el.remove(), FLY_MS + 180);
  };
  if (delay > 0) setTimeout(go, delay); else go();
}

// the remaining hand cards re-center when the row grows/shrinks — slide them (FLIP) instead of jumping
// 2026-09-24 robustness fix (the "one hand card re-animates every ~2 s" glitch):
// the double-rAF below can be delayed or skipped entirely (hidden/throttled tab,
// busy main thread) — then the inline transform STUCK in place, and every ~2.5 s
// state poll re-detected the offset and re-animated the card, forever.
// Three layers now make this impossible:
//   1. the snapshot records the LAYOUT rect (layoutRect) — a stuck transform no
//      longer fakes a position delta;
//   2. a card is never re-flipped within 900 ms (lastFlip);
//   3. a fallback timer clears the transform + transition even if the rAF chain
//      never runs — a transform can no longer stick.
const lastFlip = new Map();   // hoverId -> Date.now() of the last flip
function flipMyHand(snap) {
  const now = Date.now();
  for (const h of (snap.hand[game.me] || [])) {
    if (!h.id) continue;
    if (now - (lastFlip.get(h.id) || 0) < 900) continue;
    const el = Array.from(document.querySelectorAll("#my-hand .card")).find((c) => c.dataset.hoverId === h.id);
    if (!el) continue;                                // left the hand — its flight is handled above
    const r = el.getBoundingClientRect();
    const dx = h.rect.left - r.left, dy = h.rect.top - r.top;
    if (Math.abs(dx) < 2 && Math.abs(dy) < 2) continue;
    lastFlip.set(h.id, now);
    el.style.transition = "none";
    el.style.transform = "translate(" + dx + "px," + dy + "px)";
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.style.transition = "transform 450ms cubic-bezier(.3,.75,.4,1)";
      el.style.transform = "";
    }));
    setTimeout(() => { el.style.transition = ""; el.style.transform = ""; }, 900);
  }
}

// new board tokens (pet trap / engineer drops) pop into place instead of appearing
function popNewBoardTokens(oldSt, newSt) {
  const tokenCount = (st) => {
    let n = 0;
    for (const v of Object.values(st.drop_tokens || {})) n += (v || 0);
    n += (st.board_drops || []).length;
    return n;
  };
  const added = tokenCount(newSt) - tokenCount(oldSt);
  if (added <= 0) return;
  const tokens = $$("#markers-layer .drop-token");
  for (let i = Math.max(0, tokens.length - added); i < tokens.length; i++) tokens[i].classList.add("token-pop");
}

export function animateZoneTransitions(oldSt, newSt, snap) {
  const moves = [];
  const names = Object.keys(newSt.players || {});
  const me = names.indexOf(game.me) !== -1 ? game.me : null;
  const order = me ? [me, ...names.filter((n) => n !== me)] : names;   // my flights first
  for (const name of order) {
    if (!oldSt.players || !oldSt.players[name]) continue;
    moves.push(...planPlayerMoves(oldSt.players[name], newSt.players[name], name === me, name));
  }
  if (moves.length) {
    // plays first, then the zone shuffling, then the draws — reads as one coherent beat
    const PRIORITY = { "hand:stopover": 0, "hand:dwelling": 1, "hand:mana": 2, "hand:discard": 3,
      "stopover:discard": 4, "dwelling:discard": 5, "mana:discard": 6, "deck:mana": 7, "deck:hand": 8, "discard:deck": 9 };
    moves.sort((a, b) => (PRIORITY[a.from + ":" + a.to] || 0) - (PRIORITY[b.from + ":" + b.to] || 0));
    let i = 0;
    for (const m of moves) {
      const from = sourceRect(m, snap);
      const to = destRect(m);
      if (!from || !to) continue;
      flyCard(from, to, m.id, i * FLY_STAGGER_MS);
      i++;
    }
  }
  flipMyHand(snap);
  popNewBoardTokens(oldSt, newSt);
}
