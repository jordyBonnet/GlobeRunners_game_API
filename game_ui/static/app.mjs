/* ============================================================ GlobeRunners UI — entry point */
/* The app is a set of ES modules (same directory). Importing setup.mjs pulls the
   whole dependency graph (utils, cards, game, comm, phase, state, zones, board,
   actions, interaction, modals, anim, chainanim, log); boot() wires the setup page
   and loads the card pools. Every other module's top-level side effects (hover
   listeners, modal backdrop, leave/endgame buttons, log resize) run on import. */
"use strict";

import { boot } from "./setup.mjs";

boot();
