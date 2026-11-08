/* ============================================================ GlobeRunners UI — faction droplets */
/* Tiny pixel "droplets" shed by a card of MY hand when the player GRABS it
   (pointerdown): a few small squares detach from the pointer, fall under
   gravity to the floor (the bottom of the viewport), bounce once, then flatten
   into a puddle and fade. While the card is being dragged it keeps dripping one
   pixel at a time, so carrying a card leaves a thin trail.
   The COLOR is MY main faction's accent — theme.mjs writes --accent on
   #view-game from faction_themes.json; #fx-layer lives on <body> (outside that
   themed scope), so the accent is resolved at spawn time and copied onto the
   layer (same trick as anim.mjs flyCard's glow ring).
   Purely visual — no state, no messages, no engine coupling. Deliberately
   SPARSE (BURST pixels per grab, hard-capped live count) and completely silent
   under prefers-reduced-motion. */
"use strict";

import { REDUCED_MOTION } from "./anim.mjs";
import { THEME_DEFAULTS } from "./theme.mjs";

export const BURST = 6;        // pixels per grab — deliberately small ("not a lot")
export const DRIP = 1;         // pixels per trail tick while dragging
export const DRIP_MS = 130;    // trail throttle (ms)
const MAX_LIVE = 80;           // hard cap on simultaneous particles
const GRAVITY = 2300;          // px/s²
const FLOOR_MARGIN = 8;        // px above the viewport bottom = the floor line
const FADE_S = 0.26;           // puddle flatten + fade duration (s)
const MAX_AGE = 3.2;           // safety: a particle that never landed dies anyway

let layer = null;              // #fx-layer (created lazily on the first spawn)
let raf = 0;                   // one rAF drives every live particle
let last = 0;                  // previous frame timestamp (ms)
let lastDrip = 0;              // trail throttle timestamp (ms)
let dragging = false;          // a hand card is being dragged (native HTML5 DnD)
let trailWired = false;        // the document-level dragover listener is installed
const live = [];               // {el,size,x,y,vx,vy,rot,vr,age,rest,fade,bounces}

function fxLayer() {
  if (layer && layer.isConnected) return layer;
  // reuse an existing layer (a second module instance must never stack a second
  // #fx-layer — getElementById would then only ever see the first one)
  const existing = document.getElementById("fx-layer");
  if (existing) { layer = existing; return layer; }
  layer = document.createElement("div");
  layer.id = "fx-layer";
  document.body.appendChild(layer);
  return layer;
}

// MY faction accent, as a resolved color string (the layer is outside #view-game)
function myAccent() {
  const v = document.getElementById("view-game");
  const c = v ? getComputedStyle(v).getPropertyValue("--accent").trim() : "";
  return c || THEME_DEFAULTS.accent;
}

/**
 * Drop `n` faction-colored pixels from viewport point (x, y).
 * No-op under prefers-reduced-motion; silently stops at MAX_LIVE particles.
 */
export function spawnDroplets(x, y, n = BURST) {
  if (REDUCED_MOTION || !(n > 0)) return;
  const host = fxLayer();
  const acc = myAccent();
  host.style.setProperty("--accent", acc);      // the CSS glow resolves this
  for (let i = 0; i < n; i++) {
    if (live.length >= MAX_LIVE) break;
    const size = 3 + Math.round(Math.random() * 3);        // 3..6 px squares
    const el = document.createElement("div");
    el.className = "droplet";
    el.style.width = size + "px";
    el.style.height = size + "px";
    el.style.background = acc;
    if (Math.random() < 0.34) el.style.filter = "brightness(1.45)";   // a few bright specks
    const px = x + (Math.random() - 0.5) * 22;
    const py = y + (Math.random() - 0.5) * 10;
    // the FIRST position is written at spawn time, not only in the rAF loop —
    // otherwise the pixel flashes at the layer's top-left corner for a frame
    el.style.transform = `translate3d(${px}px,${py}px,0)`;
    host.appendChild(el);
    live.push({
      el, size,
      x: px,
      y: py,
      vx: (Math.random() - 0.5) * 80,
      vy: 60 + Math.random() * 120,             // starts by falling, not floating
      rot: Math.random() * 90,
      vr: (Math.random() - 0.5) * 520,
      age: 0, rest: false, fade: 0, bounces: 0,
    });
  }
  if (!raf) { last = performance.now(); raf = requestAnimationFrame(step); }
}

// one frame for every live particle: gravity → floor → one bounce → puddle → remove
function step(now) {
  raf = 0;
  const dt = Math.min(0.05, Math.max(0.001, (now - last) / 1000));
  last = now;
  const floor = window.innerHeight - FLOOR_MARGIN;
  for (let i = live.length - 1; i >= 0; i--) {
    const p = live[i];
    p.age += dt;
    if (p.rest) {                                // puddle: flatten + fade out
      p.fade += dt;
      const k = Math.min(1, p.fade / FADE_S);
      p.el.style.transform =
        `translate3d(${p.x}px,${p.y}px,0) scaleX(${(1 + 1.2 * k).toFixed(3)}) scaleY(${Math.max(0.16, 1 - 0.8 * k).toFixed(3)})`;
      p.el.style.opacity = (0.9 * (1 - k)).toFixed(3);
      if (k >= 1) { p.el.remove(); live.splice(i, 1); }
      continue;
    }
    p.vy += GRAVITY * dt;
    p.x += p.vx * dt;
    p.y += p.vy * dt;
    p.rot += p.vr * dt;
    if (p.y + p.size >= floor) {
      p.y = floor - p.size;
      if (p.bounces === 0 && p.vy > 320) {       // one small bounce, then it sticks
        p.bounces = 1; p.vy = -p.vy * 0.3; p.vx *= 0.45; p.vr *= 0.3;
      } else { p.rest = true; p.vr = 0; }
    }
    if (p.age > MAX_AGE) { p.el.remove(); live.splice(i, 1); continue; }
    p.el.style.transform = `translate3d(${p.x.toFixed(1)}px,${p.y.toFixed(1)}px,0) rotate(${p.rot.toFixed(1)}deg)`;
  }
  if (live.length) raf = requestAnimationFrame(step);
}

/**
 * Wire one of MY hand cards (called from interaction.mjs makeHandCard, the only
 * place my hand cards are built): a left-grab bursts BURST droplets, and while
 * the card is dragged it drips DRIP every DRIP_MS.
 */
export function attachHandDroplets(el) {
  el.addEventListener("pointerdown", (e) => {
    if (e.button) return;                 // left button / touch only (right click = zoom)
    spawnDroplets(e.clientX, e.clientY, BURST);
  });
  el.addEventListener("dragstart", () => { dragging = true; lastDrip = 0; });
  el.addEventListener("dragend", () => { dragging = false; });
  if (!trailWired) {
    // native HTML5 drag suppresses pointer/mouse events, so the trail position
    // comes from the document `dragover` stream (it fires under the cursor,
    // wherever it is). Installed once, on the first wired hand card.
    trailWired = true;
    document.addEventListener("dragover", (e) => {
      if (!dragging) return;
      const now = performance.now();
      if (now - lastDrip < DRIP_MS) return;
      lastDrip = now;
      spawnDroplets(e.clientX, e.clientY, DRIP);
    }, { passive: true });
  }
}
