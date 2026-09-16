/* ============================================================ GlobeRunners UI — cards */
/* Card data (main + support pools), the card helpers every render site goes through,
   the engineer UI constants, the factions table and the deck-size constants. */
"use strict";

import { api } from "./utils.mjs";

/* ---------------- card pools ---------------- */
export const CARDPOOL = {};            // card_id -> row (from /cardpool)
let cardpoolLoaded = false;

export async function loadCardpool() {
  if (cardpoolLoaded) return;
  const rows = await api("/cardpool");
  for (const r of rows) CARDPOOL[r.card_id] = r;
  cardpoolLoaded = true;
}

export const SUPPORT = {};             // support card_name -> row (from /support_factions)
let supportLoaded = false;

export async function loadSupportCards() {
  if (supportLoaded) return;
  const rows = await api("/support_factions");
  for (const r of rows) SUPPORT[r.card_name] = r;
  supportLoaded = true;
}

/* ---------------- card helpers (single lookup path) ----------------
   All card display goes through these — NEVER read CARDPOOL directly for a
   card that might be a support card. */

// card data: main faction cards (from /cardpool) or support faction cards (from /support_factions)
export function cardInfo(id) {
  return (CARDPOOL && CARDPOOL[id]) || SUPPORT[id] || null;
}

// readable title for a card (main or support)
export function cardTitle(id) {
  const c = cardInfo(id);
  if (!c) return id;
  if (c.card_name) return `${c.card_name} (\u2699 ${c.support_faction_name}) — ${c.description}`;   // support card
  return `${c.name} — ${c.faction}`;                                                               // main card
}

export function cardImg(id) {
  // art is served by /art/<...>.png; if the file doesn't exist, the onerror handler falls back to the placeholder.
  // support cards use their `card_path` (e.g. Mag_black_hole.png); main cards use <card_id>.png
  const s = SUPPORT[id];
  if (s) return `/art/${encodeURIComponent(s.card_path)}`;
  return `/art/${encodeURIComponent(id)}.png`;
}

// card cost: main cards use `mana`; support cards (engine_version 12+) use `mana_cost`
export function cardCost(id) {
  const main = CARDPOOL[id];
  if (main && main.mana != null) return main.mana;
  const sup = SUPPORT[id];
  if (sup && sup.mana_cost != null) return sup.mana_cost;
  return 0;
}

export function cardName(id) {
  if (CARDPOOL[id]) return CARDPOOL[id].name;
  if (SUPPORT[id]) return SUPPORT[id].card_name;
  return id;
}

// a face-up card element (img + art fallback) — the shared render site for the
// deck previews, the side panels and the stopover cards
export function cardEl(id) {
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.hoverId = id;
  el.title = cardTitle(id);
  const img = document.createElement("img");
  img.alt = "";
  img.src = cardImg(id);
  img.onerror = () => { img.onerror = null; img.src = "/placeholder.svg"; };
  el.appendChild(img);
  return el;
}

/* ---------------- Engineers support faction (engine_version 12) ---------------- */
// 4 drop cards (played in MOVE mode onto a chosen earth cell -> instant token) + 1 dwelling card.
// kind -> board token image (served at /assets/). UI-level constants; the engine is authoritative.
export const ENGINEER_DROPS = {
  boost:      { img: "/assets/supfac_eng_boost.png",      label: "boost — +2 advancing" },
  trampoline: { img: "/assets/supfac_eng_trampoline.png", label: "trampoline — +2 jump" },
  gluetrap:   { img: "/assets/supfac_eng_slowingtrap.png",label: "gluetrap — −1 knockback" },
  landmine:   { img: "/assets/supfac_eng_mine.png",       label: "landmine — blocks the arriving player for the turn" },
};
export const ENGINEER_DWELLING = "refinery";   // tap once/turn -> draw 1
export const isEngineerDrop    = (id) => !!ENGINEER_DROPS[id];
export const isEngineerDwelling= (id) => id === ENGINEER_DWELLING;
export const isSupportPlay     = (id) => isEngineerDrop(id) || isEngineerDwelling(id);

/* ---------------- factions & support factions ---------------- */
export const FACTIONS = [
  { key: "Dwa", name: "Dwarves", logo: "/assets/logo_dwarves.png" },
  { key: "Dem", name: "Demons",  logo: "/assets/logo_demons.png" },
  { key: "Twi", name: "Twigs",   logo: "/assets/logo_twigs.png" },
  { key: "Mia", name: "Miaous",  logo: "/assets/logo_miaous.png" },
  { key: "Orc", name: "Orcs",    logo: "/assets/logo_orcs.png" },
  { key: "Mum", name: "Mummies", logo: "/assets/logo_mummies.png" },
];

// faction full name → short key ("Dwarves" → "Dwa")
export function getFactionKey(factionName) {
  const f = FACTIONS.find(f => f.name === factionName);
  return f ? f.key : null;
}

// placeholder image for the dwelling card (served from /cards_ex/)
export function dwellingPlaceholderSrc(factionName) {
  const key = getFactionKey(factionName);
  return key ? `/cards_ex/placeholder_${key}.png` : "/placeholder.svg";
}

export const SUPPORT_FACS = [
  { key: "engineers", name: "Engineers", banner: "/assets/supfac_eng_banner.png" },
  { key: "mages",     name: "Mages",     banner: "/assets/supfac_mag_banner.png" },
  { key: "doctors",   name: "Doctors",   banner: "/assets/supfac_doc_banner.png" },
];

export function buildSupportDeck(key) {
  // 2 copies of each of the 5 cards of the support faction (10 total)
  const cards = Object.values(SUPPORT).filter((r) => r.support_faction_name === key);
  const deck = [];
  for (const c of cards) deck.push(c.card_name, c.card_name);
  return deck;
}

/* ---------------- deck sizes ---------------- */
export const MAIN_DECK_SIZE = 20;      // main faction starter deck (20 cards)
export const SUPPORT_DECK_SIZE = 10;   // support faction deck (2 x 5 unique cards)
