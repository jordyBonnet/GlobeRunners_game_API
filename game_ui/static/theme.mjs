/* ============================================================ GlobeRunners UI — faction theme */
/* Per-faction UI colors, loaded from /static/faction_themes.json (EDITABLE —
   served no-cache, so a color tweak only needs a page reload).
   Only THREE variables are set per scope: --accent, --accent-2,
   --accent-contrast. Every tint/glow/gradient in style.css derives from them
   with color-mix(), so a scope override (setup page, opponent containers,
   board markers) cascades correctly. The page BACKGROUND is never themed. */
"use strict";

// built-in fallback palette (the old teal/orange) — used when a faction has no entry
export const THEME_DEFAULTS = { accent: "#4fd1c5", accent2: "#f6ad55", onAccent: "#04211f" };

let themes = null;      // null = not loaded yet; a map (possibly empty) = loaded
let fetchPromise = null;

/* fetch /static/faction_themes.json once (a failed fetch just means: defaults). */
export function loadFactionThemes() {
  if (themes) return Promise.resolve(themes);
  if (!fetchPromise) {
    fetchPromise = fetch("/static/faction_themes.json")
      .then((r) => (r.ok ? r.json() : {}))
      .then((j) => { themes = (j && typeof j === "object") ? j : {}; return themes; })
      .catch(() => { themes = {}; return themes; });
  }
  return fetchPromise;
}

// the theme entry for a MAIN faction (full name, e.g. "Dwarves"); null if unknown
export function factionTheme(factionName) {
  const t = (themes && factionName) ? themes[factionName] : null;
  return (t && t.accent) ? t : null;
}

// the requested theme per scope element + role — lets the async JSON load
// (or a late load) re-apply the last request once the data is available.
// One element (e.g. #view-game) can carry BOTH the "me" variables and the
// "oppo" ones, so requests are tracked per (element, role) pair.
const scopeRequests = new WeakMap();   // el -> Map(role -> factionName)

// Apply a faction's colors to an element scope:
//   role "me"   -> --accent / --accent-2 / --accent-contrast
//   role "oppo" -> --oppo-accent / --oppo-accent-2 / --oppo-accent-contrast
// Unknown/missing factions fall back to the built-in default palette.
// If the themes JSON is not loaded yet, the application is re-run when it is
// (the last request per element+role always wins).
export function applyFactionTheme(scopeEl, factionName, role = "me") {
  const el = scopeEl;
  if (!el) return;
  let byRole = scopeRequests.get(el);
  if (!byRole) { byRole = new Map(); scopeRequests.set(el, byRole); }
  byRole.set(role, factionName);
  const applyNow = () => {
    const cur = scopeRequests.get(el);
    if (!cur || !cur.has(role)) return;
    const t = factionTheme(cur.get(role)) || THEME_DEFAULTS;
    const p = (role === "oppo") ? "--oppo-" : "--";
    el.style.setProperty(p + "accent", t.accent);
    el.style.setProperty(p + "accent-2", t.accent2);
    el.style.setProperty(p + "accent-contrast", t.onAccent);
  };
  if (themes) { applyNow(); return; }
  loadFactionThemes().then(applyNow).catch(() => {});
}

/* Apply a faction's colors to the OPPONENT-specific containers of the game page
   (#oppo-zone, #oppo-side, #row-oppo) by overriding --accent there — the
   elements inside those scopes (borders, counters, slot watermarks…) then
   resolve the opponent's palette instead of mine. */
export function applyOpponentTheme(oppoFaction) {
  for (const id of ["oppo-zone", "oppo-side", "row-oppo"]) {
    const el = document.getElementById(id);
    if (el) applyFactionTheme(el, oppoFaction, "me");   // --accent override in that scope
  }
}

// Sync the game-page themes from the current state: MY faction on #view-game
// (the whole page's default), the OPPONENT's faction on #view-game as --oppo-*
// (the opponent's board marker reads those) AND on the opponent containers.
export function syncGameThemes(meState, oppoState) {
  const vgame = document.getElementById("view-game");
  applyFactionTheme(vgame, meState ? meState.faction : null, "me");
  applyFactionTheme(vgame, oppoState ? oppoState.faction : null, "oppo");
  applyOpponentTheme(oppoState ? oppoState.faction : null);
}
