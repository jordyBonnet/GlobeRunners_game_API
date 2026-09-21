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

// Earth background image for a game, derived from the board's biome order
// (state.earth — the engine's biomes_order, public board info). Cell 0 is at the
// top of the ring, clockwise (board.mjs POS24), so segment 0 (cells 0–5) is the
// TOP-RIGHT quadrant of the image. The four configs cover one starting biome each:
//   DE → cfg1, MO → cfg2, OC → cfg3, JU → cfg4
// so the starting quadrant (where both players begin) is always drawn correctly,
// and the whole board matches 4/4 when the board's cyclic order matches the asset
// (12 of the 24 possible orders — the assets only contain 2 cyclic classes).
// Falls back to the plain playmat when the board is unknown (setup screen, old state).
export function earthBgSrc(earth) {
  const first = (earth && earth[0] && earth[0][0]) || null;
  const CFG = { DE: 1, MO: 2, OC: 3, JU: 4 };
  if (!CFG[first]) return "/assets/earth_background_playmat.png";
  return `/assets/earth_cgf${CFG[first]}_nomarker.png`;
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

/* ---------------- Doctors support faction (engine_version 16) ---------------- */
// 4 pending cards (placed in the pending zone, attachable to a main card) + 1 dwelling card.
export const DOCTOR_PENDING = {
  epo:           { label: "epo — +1 advancing" },
  virus:         { label: "virus — −1 knockback" },
  bloodtest:     { label: "bloodtest — discard 1 card" },
  mercurochrome: { label: "mercurochrome — unstoppable" },
};
export const DOCTOR_DWELLING = "laboratory";   // tap once/turn -> adds an 'epo' pending card
export const isDoctorPending   = (id) => !!DOCTOR_PENDING[id];
export const isDoctorDwelling  = (id) => id === DOCTOR_DWELLING;

/* ---------------- Mages support faction (engine_version 22) ---------------- */
// 5 cards, implemented card by card. Celestial_reversal (first, v22): an INSTANT
// play-time effect — the player CHOOSES day or night (sent in the action message as
// `day_night`) and it is FIXED for the rest of the game (the day/night no longer
// flips each turn). Played in MOVE mode onto a stopover, resolves as a no-op (like
// a placeholder).
export const MAGE_CELASTIAL_REVERSAL = "Celestial_reversal";
export const isMageCelestial = (id) => id === MAGE_CELASTIAL_REVERSAL;
// Mages `nobodymoves` (engine_version 23): an INSTANT play-time effect — it BLOCKS
// ALL PLAYERS for the rest of the turn (their MOVE cards are canceled, only
// "unstoppable" cards may advance; DEFEND unaffected). Played in MOVE mode onto a
// stopover, resolves as a no-op (like a placeholder). No choice to make — the block
// is automatic (no popup, no cell selection), so it plays through the normal move path.
export const MAGE_NOBODYMOVES = "nobodymoves";
export const isMageNobodyMoves = (id) => id === MAGE_NOBODYMOVES;
// Mages `thermic_flux` (engine_version 24): an INSTANT play-time effect — the player
// CHOOSES +4 °C or −4 °C (sent in the action message as `temp_change`: "up"|"down")
// and the planet temperature changes by that amount, CLAMPED to 1..20, PERMANENTLY.
// Played in MOVE mode onto a stopover, resolves as a no-op (like a placeholder).
// Needs a popup to pick the direction (like Celestial_reversal's day/night popup).
export const MAGE_THERMIC_FLUX = "thermic_flux";
export const isMageThermicFlux = (id) => id === MAGE_THERMIC_FLUX;
// Mages `Apocalypticritual` (engine_version 25): an INSTANT play-time effect — the
// player CHOOSES THE ORDER OF ALL 4 CATACLYSM CARDS (sent in the action message as
// `cataclysm_order`: a permutation of the 4 biomes, index 0 = strikes next) and the
// cataclysm pile is SET to that order, PERMANENTLY (until the next ritual).
// Played in MOVE mode onto a stopover, resolves as a no-op (like a placeholder).
// Needs a popup to arrange the 4 biomes (like Celestial_reversal / thermic_flux).
export const MAGE_APOCALYPTICRITUAL = "Apocalypticritual";
export const isMageApocalypticritual = (id) => id === MAGE_APOCALYPTICRITUAL;
// Mages `black_hole` (engine_version 26): a DWELLING card (like the refinery /
// laboratory) — NOT an instant-at-play card. It is PLACED in the dwelling zone
// (to: "dwelling") and its effect fires on a TAP: it ROTATES THE EARTH 3 CELLS in the
// player's chosen direction (sent in the tap message as `rotation`: "cw"|"ccw"). The
// 4 biomes shift position on the board while every token (players, drops, traps) stays
// on its cell. The tap is free + once per turn, and needs a popup to pick the direction
// (like Celestial_reversal's day/night popup).
export const MAGE_BLACK_HOLE = "black_hole";
export const isMageBlackHole = (id) => id === MAGE_BLACK_HOLE;

export const isSupportPlay     = (id) => isEngineerDrop(id) || isEngineerDwelling(id) || isDoctorPending(id) || isDoctorDwelling(id) || isMageCelestial(id) || isMageNobodyMoves(id) || isMageThermicFlux(id) || isMageApocalypticritual(id) || isMageBlackHole(id);

// swap_cards (engine_version 29): a MAIN-card effect (156 cards in the pool) — an
// INSTANT play-time effect: the card SWAPS its trip-chain position with one of the
// player's OWN chain entries (a play, a board placeholder, a rooted card). The
// player makes the choice in a popup (actions.mjs showSwapPopup) and sends the
// target position as the message `swap_with` field. Optional — without it the card
// simply advances as usual. (A MAIN effect, not a support card: isSupportPlay is
// untouched, and it goes through the normal move-card dispatch branch.)
export const isSwapCards = (id) => !!(CARDPOOL[id] && CARDPOOL[id].effect === 'swap_cards');

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

// faction logo (the player token on the Earth ring, /assets/logo_<key>.png); null if the faction is unknown
export function factionLogoSrc(factionName) {
  const f = FACTIONS.find((f) => f.name === factionName);
  return f ? f.logo : null;
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
