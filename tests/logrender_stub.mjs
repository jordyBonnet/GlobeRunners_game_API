/* Stubs for testing the REAL game_ui/static/log.mjs renderLog in isolation.
   Exports every symbol log.mjs imports from cards.mjs / game.mjs / zones.mjs /
   modals.mjs / chainanim.mjs. playChainAnim is a spy. */
"use strict";
export const CARDPOOL = {};
export const cardImg = (id) => `data:image/svg+xml;utf8,` + encodeURIComponent(
  `<svg xmlns='http://www.w3.org/2000/svg' width='40' height='56'><rect width='40' height='56' fill='%23345'/><text x='4' y='30' fill='white' font-size='8'>${id}</text></svg>`);
export const game = { me: 'Alice', logTurnsRendered: 0, logTurnSigs: [] };
export const makeStaticCard = () => { const d = document.createElement('div'); d.className = 'static-card'; return d; };
export const showCardHover = () => {};
export const hideCardHover = () => {};
export const showCardModal = () => {};
export const chainAnimCalls = [];
export const playChainAnim = (t) => { chainAnimCalls.push({ turn: t && t.turn, sv: (t && t.stopovers || []).length }); };
