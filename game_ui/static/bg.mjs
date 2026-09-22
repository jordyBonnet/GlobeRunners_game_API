/* ============================================================ GlobeRunners UI — setup-page animated card background */
/* Random cards (2/3 main faction / 1/3 support) float up the screen behind the
   setup page at low opacity, rotating slowly. All visual knobs live in
   `background_cards_parameters.json` (same folder) — edit it and reload to
   explore configurations, no code change needed:

     enabled                     master switch (false = no background at all)
     cards_per_minute            spawn rate (new cards per minute)
     speed_px_per_s              base rise speed, bottom → top (px/s, per-card ± jitter)
     card_width_px               base card width (px; height follows the 180/260 art ratio)
     size_jitter                 random width variation, e.g. 0.3 = ±30 %
     rotation_speed_deg_per_s    base rotation speed (deg/s, random CW or CCW per card, ±50 %)
     rotation_range_deg          [min, max] starting rotation in degrees — 0 = straight,
                                 90 = tapped sideways (e.g. [0, 90] or [0, 0])
     opacity                     max card opacity while on screen (0..1, spec: 0.3);
                                 cards fade in at the bottom edge and out at the top
     spread_px                   horizontal reach from the SCREEN CENTER: the card center appears within
                                 ± spread_px of the middle of the page (Less = a narrow band around the
                                 center, More = a wider space). A value ≥ half the screen width = full width.
     max_cards                   hard cap of cards on screen (safety for high spawn rates)

   Card distribution (fixed, not configurable): 9 EQUAL buckets — 2/3 main faction
   cards split 1/6 each across the 6 factions, + 1/3 support cards split 1/3 each
   across the 3 support factions (i.e. every one of the 9 factions = exactly 1/9). */
"use strict";

import { $, toast } from "./utils.mjs";
import { CARDPOOL, SUPPORT, cardImg, FACTIONS, SUPPORT_FACS } from "./cards.mjs";

const CFG_PATH = "/static/background_cards_parameters.json";
const CARD_RATIO = 260 / 180;    // card art height/width ratio (same as the .card elements)

const bg = {
  active: false,
  raf: 0,
  cfg: null,
  cards: [],        // {el, x, y, w, h, speed, rot, rotSpeed}
  spawnAcc: 0,      // seconds accumulated toward the next spawn
  lastT: 0,         // rAF timestamp of the previous tick
};

/* ---------------------------------------------------------------- layer (created lazily) */
function ensureLayer() {
  let layer = $("#bg-cards");
  if (!layer) {
    layer = document.createElement("div");
    layer.id = "bg-cards";
    layer.setAttribute("aria-hidden", "true");
    document.body.insertBefore(layer, document.body.firstChild);
  }
  return layer;
}

/* ---------------------------------------------------------------- card picking */
/* 9 EQUAL buckets (built once): the 6 main factions (2/3 of the cards, 1/6 each)
   + the 3 support factions (1/3 of the cards, 1/3 each) — every faction = 1/9. */
let BUCKETS = null;
function buckets() {
  if (!BUCKETS) {
    BUCKETS = [];
    for (const f of FACTIONS)
      BUCKETS.push(Object.keys(CARDPOOL).filter((id) => CARDPOOL[id].faction === f.name));
    for (const f of SUPPORT_FACS)
      BUCKETS.push(Object.keys(SUPPORT).filter((name) => SUPPORT[name].support_faction_name === f.key));
  }
  return BUCKETS;
}
/* returns a card id (main card_id or support card_name) or null if no pool is loaded */
function pickCardId() {
  const list = buckets().filter((b) => b.length);
  if (!list.length) return null;
  const bucket = list[Math.floor(Math.random() * list.length)];
  return bucket[Math.floor(Math.random() * bucket.length)];
}

function makeCardEl(src) {
  const el = document.createElement("div");
  el.className = "bg-card";
  const img = new Image();
  img.alt = "";
  img.src = src;
  img.onload = () => { img.onerror = null; };
  img.onerror = () => { img.onerror = null; img.src = "/placeholder.svg"; };
  el.appendChild(img);
  return el;
}

/* ---------------------------------------------------------------- spawn */
function spawn() {
  const cfg = bg.cfg;
  const jitter = cfg.size_jitter ?? 0.3;
  const w = Math.max(40, cfg.card_width_px * (1 + (Math.random() * 2 - 1) * jitter));
  const h = w * CARD_RATIO;
  const id = pickCardId();
  const el = makeCardEl(id ? cardImg(id) : "/placeholder.svg");
  el.dataset.hoverId = id || "";   // → the delegated big-hover preview (modals.mjs)
  el.style.width = w + "px";
  el.style.height = h + "px";
  ensureLayer().appendChild(el);
  const [rMin, rMax] = cfg.rotation_range_deg || [0, 90];
  const rot = rMin + Math.random() * (rMax - rMin);
  const dir = Math.random() < 0.5 ? -1 : 1;
  // horizontal spawn: the card CENTER within ± spread_px of the screen center
  const W = window.innerWidth;
  const spread = Math.min(cfg.spread_px ?? W, W / 2);        // cap: ≥ half width = full screen
  const cx = W / 2 + (Math.random() * 2 - 1) * spread;
  const x = Math.min(Math.max(0, cx - w / 2), Math.max(0, W - w));
  const c = {
    el, w, h, id,
    x,   // fully on-screen at spawn
    y: window.innerHeight,           // starts just below the bottom edge
    speed: (cfg.speed_px_per_s) * (0.6 + Math.random() * 0.8),
    rot,
    rotSpeed: dir * (cfg.rotation_speed_deg_per_s) * (0.5 + Math.random()),
  };
  bg.cards.push(c);
  applyCard(c);   // position + opacity IMMEDIATELY (no flash before the first rAF tick)
}

/* write the card's transform + edge-fade opacity to the DOM (called at spawn and every tick) */
function applyCard(c) {
  const H = window.innerHeight;
  const fade = Math.max(30, c.h * 0.4);
  const aIn = Math.min(1, Math.max(0, (H - c.y) / fade));          // fading in at the bottom
  const aOut = Math.min(1, Math.max(0, (c.y + c.h) / fade));       // fading out at the top
  c.el.style.transform = `translate3d(${c.x}px, ${c.y}px, 0) rotate(${c.rot}deg)`;
  c.el.style.opacity = ((bg.cfg.opacity ?? 0.3) * aIn * aOut).toFixed(3);
}

/* ---------------------------------------------------------------- animation loop */
function tick(t) {
  if (!bg.active) return;
  if (!bg.lastT) bg.lastT = t;
  let dt = (t - bg.lastT) / 1000;
  bg.lastT = t;
  if (dt > 0.25) dt = 0.25;          // clamp the gap after a tab switch

  const cfg = bg.cfg;

  // spawn
  const interval = 60 / (cfg.cards_per_minute || 0.001);   // seconds between cards
  bg.spawnAcc += dt;
  while (bg.spawnAcc >= interval) {
    bg.spawnAcc -= interval;
    if (bg.cards.length < (cfg.max_cards ?? 40)) spawn();
  }

  // update
  for (let i = bg.cards.length - 1; i >= 0; i--) {
    const c = bg.cards[i];
    // hovering a card (see modals.mjs big preview): freeze it so it does not drift away from the cursor
    if (!c.el.matches(":hover")) {
      c.y -= c.speed * dt;
      c.rot += c.rotSpeed * dt;
    }
    if (c.y + c.h < 0) { c.el.remove(); bg.cards.splice(i, 1); continue; }
    applyCard(c);
  }
  bg.raf = requestAnimationFrame(tick);
}

/* ---------------------------------------------------------------- start / stop */
export async function startBgCards() {
  if (bg.active) return;
  let cfg;
  try {
    const res = await fetch(CFG_PATH);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    cfg = await res.json();
  } catch (err) {
    toast(`Background cards off: cannot read ${CFG_PATH} (${err.message})`);
    return;
  }
  if (!cfg.enabled) return;
  bg.cfg = cfg;
  bg.lastT = 0;
  bg.spawnAcc = 0;
  bg.active = true;
  ensureLayer();
  // initial cards: queued just BELOW the bottom edge (staggered) so they all
  // ENTER from the bottom and flow up one after another — never pre-placed
  // in the middle of the screen
  const n = Math.min(cfg.max_cards ?? 40, Math.max(3, Math.round((cfg.max_cards ?? 40) / 4)));
  let yNext = window.innerHeight;
  for (let i = 0; i < n; i++) {
    spawn();
    const c = bg.cards[bg.cards.length - 1];
    c.y = yNext;
    yNext += c.h * (0.6 + Math.random() * 0.6);
  }
  bg.raf = requestAnimationFrame(tick);
}

export function stopBgCards() {
  if (!bg.active) return;
  bg.active = false;
  cancelAnimationFrame(bg.raf);
  bg.cards.length = 0;
  bg.cfg = null;
  const layer = $("#bg-cards");
  if (layer) layer.remove();
}
