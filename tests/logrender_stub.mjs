/* Stubs for testing the REAL game_ui/static/log.mjs renderLog in isolation.
   Exports every symbol log.mjs imports from cards.mjs / game.mjs / zones.mjs /
   modals.mjs / chainanim.mjs. playChainAnim is a spy. */
"use strict";
export const CARDPOOL = {};
// one support row, mirroring the /support_factions shape (no effect/condition column)
export const SUPPORT = {
  refinery: { card_name: 'refinery', support_faction_name: 'engineers', mana_cost: 3,
              description: 'When tapped, draw 1 card', card_path: 'Eng_refinery.png' },
};
// mirror of cards.mjs cardTitle (main row -> "name — faction", support row -> "name (⚙ faction) — description")
export const cardTitle = (id) => {
  const s = SUPPORT[id];
  if (s) return `${s.card_name} (\u2699 ${s.support_faction_name}) — ${s.description}`;
  const c = CARDPOOL[id];
  return c ? `${c.name} — ${c.faction}` : id;
};
export const cardImg = (id) => `data:image/svg+xml;utf8,` + encodeURIComponent(
  `<svg xmlns='http://www.w3.org/2000/svg' width='40' height='56'><rect width='40' height='56' fill='%23345'/><text x='4' y='30' fill='white' font-size='8'>${id}</text></svg>`);
export const game = { me: 'Alice', logTurnsRendered: 0, logTurnSigs: [] };
export const makeStaticCard = () => { const d = document.createElement('div'); d.className = 'static-card'; return d; };
export const showCardHover = () => {};
export const hideCardHover = () => {};
export const showCardModal = () => {};
export const chainAnimCalls = [];
export const playChainAnim = (t) => { chainAnimCalls.push({ turn: t && t.turn, sv: (t && t.stopovers || []).length }); };
