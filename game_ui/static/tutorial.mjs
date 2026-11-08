/* ============================================================ GlobeRunners UI — quick in-game tutorial */
/* A coach-mark overlay driven by the game state: the page darkens, a spotlight
   ("halo") frames the element being explained, and a tip card shows the text +
   images with an OK button, so the player reads at their own pace.

   Wiring: setup.mjs sets `tutorial.enabled` from the setup-page checkbox →
   game.mjs enterGame() calls startTutorial() → state.mjs renderAll() calls
   tutorialTick() on every state (idempotent) → modals.mjs leaveGame() calls
   stopTutorial().

   ONE control: the tip has a single button, **OK**, and it is the only button of
   the layer (no skip, no hide, no end) — on an "info" step the shade swallows every
   other click on the page, so OK really is the only clickable button on screen.
   (`Enter` / `Space` do the same as OK; `Esc` deliberately does nothing, so the run
   cannot be lost by a stray key.)

   Two step modes:
     "info" — the 4 shade rects CAPTURE clicks: the page is paused while the
              player reads; OK is the only way out.
     "act"  — the shade is purely visual (pointer-events:none): the player must
              do the real action (drag 3 cards to mana, drop a card on stopover 1,
              pass…), so the board controls stay live for those steps only. The step
              closes itself when `auto(c)` becomes true (the action was done) or when
              the moment has passed (`expired`).

   The tutorial is a pure READ-ONLY layer: it never sends a message and never
   mutates game.state — it reads game.state + the DOM and positions fixed divs.
   If a game popup opens (defense / discard / mage choices, whose z-index is below
   the overlay) the layer mutes itself (invisible + click-through) until the popup
   closes, so a mandatory popup is never covered. */
"use strict";

import { CARDPOOL, cardCost, cardImg, cardInfo, cardName, isSupportPlay } from "./cards.mjs";
import { game } from "./game.mjs";
import { detectPhase, myTurn, START_MANA_N } from "./phase.mjs";
import { cardLegend } from "./modals.mjs";
import { slotEls, N_STOPOVERS, N_CELLS, BIOME_NAMES, setMarkerOverride, clearMarkerOverrides } from "./board.mjs";
import { pulseStep, clearPulse, setChainAnimHold, playChainAnim } from "./chainanim.mjs";
import { nextSlotCol, freeCols } from "./actions.mjs";

/* ---------------------------------------------------------------- public state */
export const tutorial = {
  enabled: false,     // setup-page checkbox — set by setup.mjs before launch
  active: false,      // a run is in progress
  visible: false,     // a tip is on screen
  muted: false,       // a game popup is open — layer stepped aside
  index: -1,          // index of the current step in SEQ
};

let SEQ = [];                 // live sequence (the static STEPS + the generated resolution steps)
let doneIds = new Set();      // finished (OK / skipped / auto) step ids
let reposition = null;        // 500 ms setInterval id
let layer = null, tip = null, halo = null, arrow = null, shades = {};

/* engine mirror (engine.FACTION_BIOMES): the 2 biomes that give the +1 bonus */
const FACTION_BIOMES = {
  Dwarves: ["MO", "OC"], Demons: ["OC", "DE"], Twigs: ["JU", "OC"],
  Miaous: ["DE", "JU"], Orcs: ["MO", "JU"], Mummies: ["DE", "MO"],
};

const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const biomeList = (codes) => (codes || []).map((b) => BIOME_NAMES[b] || b).join(" / ");
const val = (v, c) => (typeof v === "function" ? v(c) : v);

/* ------------------------------------------------------------------- context */
function ctx() {
  const st = game.state;
  const players = (st && st.players) || null;
  const me = players ? players[game.me] : null;
  const oppoName = players ? Object.keys(players).find((n) => n !== game.me) : null;
  return {
    st, me, oppoName,
    ph: st ? detectPhase(st) : null,
    turn: (st && st.turn) || 1,
    iAmFirst: !!(st && st.turn_order && st.turn_order[0] === game.me),
  };
}
const myPlayTurn = (c) => !!c.ph && c.ph.kind === "play" && !!c.st && myTurn(c.st);
const manaAvail = (me) => (me ? (me.mana || []).length - (me.mana_spend || 0) : 0);
const handOf = (c) => (c.me && c.me.hand) || [];
/* did the opponent already play this turn? (public information — their cards are on the board) */
const oppoPlayed = (c) => {
  const o = (c.st && c.st.players && c.oppoName) ? c.st.players[c.oppoName] : null;
  return !!o && (o.action_chain || []).length > 0;
};
const cheapestHand = (c) => Math.min(...handOf(c).map((id) => CARDPOOL[id] ? (CARDPOOL[id].mana || 1) : 1), 99);
/* did I already play (move or defend) this turn? `action_chain` is the engine's own
   per-turn play list — it is cleared in the cleaning phase, so a non-empty chain means
   "this player has played at least one card this turn" */
const playedThisTurn = (c) => !!c.me && (c.me.action_chain || []).length > 0;

/* the DOM element of a card currently displayed in my hand (by card id) */
function handCardEl(id) {
  if (!id) return null;
  return document.querySelector(`#my-hand .card[data-hover-id="${CSS.escape(id)}"]`);
}
/* my next free stopover slot element (the halo follows the engine's own rule) */
function nextSlotEl(c) {
  const free = freeCols(c.st, game.me);
  const col = free.length ? nextSlotCol(c.st, game.me) : N_STOPOVERS - 1;
  return slotEls.me[col] || null;
}
const slotNum = (col) => N_STOPOVERS - col;   // board display number of a column

/* a main-faction hand card that shows as many frame elements as possible */
function anatomyCard(c) {
  const mains = handOf(c).filter((id) => !isSupportPlay(id) && CARDPOOL[id]);
  const score = (id) => {
    const r = CARDPOOL[id];
    return ((r.condition && r.condition !== "no_condition") ? 4 : 0)
      + ((r.shield || 0) > 0 ? 2 : 0) + ((r.advancing || 0) !== 0 ? 1 : 0);
  };
  return mains.slice().sort((a, b) => score(b) - score(a))[0] || handOf(c)[0] || null;
}

/* the card image with numbered hotspots — the positions match the card frame:
   the left column stacks mana cost / basic advancing / shield, the bottom strip
   reads condition | effect, and the two biome icons flank the card name. */
const CARD_SPOTS = [
  // 1-3 the left column (cost / advancing / shield) · 4-5 the bottom strip
  // (condition | effect) · 6 the two biome icons flanking the card name
  { n: 1, x: 8, y: 7 }, { n: 2, x: 8, y: 15 }, { n: 3, x: 8, y: 22 },
  { n: 4, x: 27, y: 87 }, { n: 5, x: 68, y: 87 },
  { n: 6, x: 12, y: 96 }, { n: 6, x: 87, y: 96 },
];
function anatomyHtml(id) {
  const r = CARDPOOL[id];
  if (!r) return "";
  const leg = cardLegend(id) || {};
  const rows = [
    ["1", "Mana cost", `${r.mana} — paying a card taps that many mana from your ⚡ zone`],
    ["2", "Basic advancing", `${r.advancing} — the cells the card moves you when its condition is met`],
    ["3", "Block value (shield)", `${r.shield} — played sideways in defense, it blocks a card costing up to ${r.shield}`],
    ["4", "Condition", leg.condition || "no condition — always met"],
    ["5", "Effect", leg.effect || "no effect"],
    ["6", "Faction biomes", biomeList(FACTION_BIOMES[r.faction]) + " — standing there, your forward advancing gets +1"],
  ];
  return `<div class="tu-anatomy">
      <img src="${cardImg(id)}" alt="" onerror="this.onerror=null;this.src='/placeholder.svg'">
      ${CARD_SPOTS.map((s) => `<span class="tu-hot" style="left:${s.x}%;top:${s.y}%">${s.n}</span>`).join("")}
    </div>
    <ul class="tu-anat-legend">${rows.map((x) => `<li><b>${x[0]}</b> <i>${x[1]}</i> — ${esc(x[2])}</li>`).join("")}</ul>`;
}

/* the little race schematic of step 1: cell 0 (start) → cell 23 → cell 24 (win) */
function trackSvg() {
  const W = 320, H = 40, y = 22;
  const x0 = 26, x1 = W - 26;
  let dots = "";
  for (let i = 0; i <= 23; i++) {
    const x = x0 + (i / 23) * (x1 - x0);
    dots += `<circle cx="${x.toFixed(1)}" cy="${y}" r="${i === 0 ? 5 : 2.6}" fill="${i === 0 ? "var(--accent)" : "#5f6f9c"}"/>`;
  }
  return `<svg class="tu-track" viewBox="0 0 ${W} ${H}" role="img" aria-label="race track from cell 0 to cell 24">
      <line x1="${x0}" y1="${y}" x2="${x1}" y2="${y}" stroke="#3b4a76" stroke-width="2"/>
      ${dots}
      <text x="${x0}" y="${y - 10}" font-size="11" text-anchor="middle">🏁</text>
      <text x="${x0}" y="${y + 15}" font-size="8" fill="#8b9ac7" text-anchor="middle">cell 0</text>
      <text x="${x1}" y="${y - 10}" font-size="11" text-anchor="middle">🏆</text>
      <text x="${x1}" y="${y + 15}" font-size="8" fill="#8b9ac7" text-anchor="middle">cell 24 = WIN</text>
    </svg>`;
}

/* --------------------------------------------------------------- step content */
const STEPS = [
  /* 1 — main goal */
  {
    id: "goal", kicker: "🎯 The goal", title: "Race around the Earth",
    pad: 26,   // the two player tokens sit on the ring edge — frame them too
    el: () => document.getElementById("board-stage"),
    imgs: () => [],
    body: () => `${trackSvg()}
      <p>The Earth is a ring of <b>24 cells</b> split in <b>4 biomes</b> of 6 cells
      (Ocean, Mountain, Desert, Jungle) — the order is randomized every game.</p>
      <p>Both players start on <b>cell 0</b> (🏁). <b>The first token to reach cell 24</b> —
      past the last cell — <b>wins the game</b>.</p>
      <p>You do not move by yourself: you <b>play cards</b>, and each card advances your token.</p>`,
  },

  /* 2.1 — trip chain */
  {
    id: "chain", kicker: "🧭 The board", title: "The trip chain (stopovers 1 → 5)",
    el: () => document.getElementById("stopovers"),
    body: () => `<p>Each player has <b>their own 5 stopover slots</b>. A played card lands on the
      <b>next free position, 1 → 5</b>: your row is at the bottom, the opponent's on top.</p>
      <p>Cards <b>facing each other at the same position</b> interact — that is what makes a
      defense block, a copy effect or a grappling hook line up.</p>
      <p>At resolution the chain is played <b>position 1 first, then 2, then 3…</b>, alternately:
      first player's card, then second player's.</p>`,
  },

  /* 2.2 — hand */
  {
    id: "hand", kicker: "🧭 The board", title: "Your hand",
    el: () => document.getElementById("my-hand"),
    body: () => `<p>The cards you can play. <b>Drag one onto a stopover</b> to play it —
      or click the card, then click the slot (or use the <b>Play the card</b> button).</p>
      <p>Hovering a card shows it 3× bigger with its <b>condition and effect written out</b>.
      A glowing outline means you can afford it this turn.</p>`,
  },

  /* 2.3 — mana zone */
  {
    id: "mana", kicker: "🧭 The board", title: "Your mana zone ⚡",
    el: () => document.getElementById("dz-mana"),
    imgs: () => ["/assets/marker_mana.png"],
    body: () => `<p>Every card costs <b>mana</b>, and mana is paid <b>from this zone</b>: each
      point of cost taps one card here (a spent card turns sideways for the rest of the turn).</p>
      <p>The counter reads <b>available / total</b>. You can never play more than what is
      available — and every turn's <b>mana phase</b> lets you add one more card to grow the pool.</p>`,
  },

  /* 2.4 — discard */
  {
    id: "discard", kicker: "🧭 The board", title: "Your discard pile 🗑️",
    el: () => document.querySelector("#my-side .side-panel.discard"),
    imgs: () => ["/assets/marker_bin.png"],
    body: () => `<p>Every card you play ends up here once it resolved.</p>
      <p>When your deck runs empty and you still have to draw, <b>this pile is shuffled back
      into a new deck</b> — nothing is lost.</p>
      <p><b>Click the panel</b> to read every card you discarded. Discard piles are public
      information, for both players.</p>`,
  },

  /* 3 — card anatomy */
  {
    id: "anatomy", kicker: "🃏 Reading a card", title: "Everything a card says",
    when: (c) => handOf(c).some((id) => CARDPOOL[id]),
    el: (c) => handCardEl(anatomyCard(c)),
    anatomy: (c) => anatomyHtml(anatomyCard(c)),
    body: () => `<p>This is one of <b>your main-faction cards</b> — every card of the game reads
      the same way. The numbers below match the numbered circles on the card.</p>`,
  },

  /* 4 — the 3 central environment cards */
  {
    id: "env", kicker: "🌍 The planet", title: "Day/night • temperature • cataclysms",
    el: () => document.getElementById("env-cards"),
    imgs: () => ["/assets/envcard_day_night_back.png", "/assets/envcard_temperature_fc.png", "/assets/envcard_cata_back.png"],
    body: () => `<p><b>☀️/🌙 Day / night</b> — flips at every new turn. Some cards only work by day, others only by night.</p>
      <p><b>🌡️ Temperature (1…20)</b> — rolled once at the start with 2d20 (closest to 10) and
      it stays for the game. Cards ask for it under or over a threshold.</p>
      <p><b>☄️ Cataclysm pile</b> — one card per biome. When a cataclysm triggers, the <b>top</b>
      biome strikes: every token on that biome falls back to its <b>first cell</b>, then the pile
      rotates.</p>`,
  },

  /* 5 — initialization: 3 cards in mana (DO IT) */
  {
    id: "init-mana", kicker: "5️⃣ Your first move", title: "Put 3 cards in your mana zone",
    mode: "act",
    when: (c) => c.ph === "init-mana",
    expired: (c) => c.ph !== "init-mana",
    auto: (c) => !!c.me && (c.me.mana || []).length >= START_MANA_N,
    el: () => document.getElementById("dz-mana"),
    body: (c) => `<p><b>Drag 3 cards</b> from your hand into the glowing ⚡ zone (you can also click
      3 cards and press <b>Place in mana</b>).</p>
      <p><b>Tip:</b> a card placed here never leaves — so the usual opening is to put your
      <b>highest mana-cost cards</b> in the zone: one big card pays for one big play later,
      and you keep the cheap ones to spend from hand.</p>
      <p class="tu-live">Waiting for you… mana in zone: <b>${(c.me && c.me.mana || []).length}/${START_MANA_N}</b></p>`,
  },

  /* 6 — first play of turn 1 (DO IT) */
  {
    id: "play-1", kicker: "6️⃣ The play phase", title: "Play your first card on stopover 1",
    mode: "act",
    when: (c) => c.turn === 1 && myPlayTurn(c),
    expired: (c) => c.turn > 1 || (c.ph && c.ph.kind === "discard"),
    auto: (c) => playedThisTurn(c) || !myPlayTurn(c),
    el: (c) => nextSlotEl(c),
    body: (c) => {
      const oppo = c.oppoName || "The opponent";
      const opening = c.iAmFirst ? ""
        : oppoPlayed(c)
          ? `<p><b>${esc(oppo)}</b> already opened the turn — their card faces the slot
             highlighted below: answer on <b>your</b> stopover ${slotNumOf(c)}.</p>`
          : `<p><b>${esc(oppo)}</b> plays first this turn — wait for their card,
             then play yours.</p>`;
      return `<p><b>Drag a card from your hand onto stopover ${slotNumOf(c)}</b> — the first
        position of your trip chain.</p>
        <p>Playing a card <b>costs its mana</b> (the top-left drop): you have
        <b>${manaAvail(c.me)}</b> available, so the card must cost ${manaAvail(c.me)} or less,
        otherwise the play is refused.</p>
        <p>You can also play cards <b>sideways in defense</b> with the <b>⛨ Play in defense</b>
        button (1–5 cards at once): they don't advance, they block the card facing them.</p>
        ${opening}`;
    },
  },

  /* 7 — keep playing or pass (DO IT) — only AFTER the player's first card of the turn:
     OK-ing step 6 without playing must NOT stack a second "your move" tip on top of the
     board, so `when` waits for a real play (`playedThisTurn`); the layer simply steps off
     screen until then. */
  {
    id: "play-more", kicker: "7️⃣ Keep going", title: "Spend the rest, or Pass",
    mode: "act",
    when: (c) => c.turn === 1 && myPlayTurn(c) && playedThisTurn(c),
    // the turn is over (resolved / turn counter moved on) -> nothing left to spend,
    // and the next step (resolution) must not be blocked behind this one
    expired: (c) => c.turn > 1 || !!resolvedTurn(c),
    auto: (c) => !myPlayTurn(c),
    el: (c) => (manaAvail(c.me) < cheapestHand(c) ? document.getElementById("btn-pass") : nextSlotEl(c)),
    body: (c) => {
      const can = manaAvail(c.me) >= cheapestHand(c);
      return can
        ? `<p><b>Card down.</b> You still have <b>${manaAvail(c.me)} mana</b> available: play another
           card on stopover ${slotNumOf(c)}, <b>or</b> press <b>Pass</b> if you would rather save your
           cards for next turn.</p>
           <p>Passing costs nothing. The turn only ends when <b>both</b> players have passed.</p>`
        : `<p><b>Card down</b> — and no mana left for a playable card (<b>${manaAvail(c.me)}</b>
           available, cheapest card in hand costs <b>${cheapestHand(c)}</b>): press <b>Pass</b>.</p>
           <p>Passing costs nothing — and the turn ends when <b>both</b> players have passed.</p>`;
    },
  },

  /* 8 — resolution phase intro */
  {
    id: "resolve-intro", kicker: "8️⃣ Resolution", title: "Both passed → the trip chain resolves",
    when: (c) => !!resolvedTurn(c),
    el: () => document.getElementById("stopovers"),
    // pin both tokens where their chain began: the replay walks them card by card
    enter: (c) => startReplay(c),
    body: () => `<p>Now every card of the chain is processed <b>position by position, 1 → 5</b>,
      and <b>alternately</b>: first player's card, then second player's.</p>
      <p>For each card, in this exact order:</p>
      <ol class="tu-ol">
        <li><b>Block check</b> — a defense card facing it blocks the play if its total shield
          ≥ this card's mana cost;</li>
        <li><b>Condition</b> — is it met right now?</li>
        <li><b>Effect</b> — applied only if the condition was met;</li>
        <li><b>Basic advancing</b> — the card's advancing value (+1 if you stand on one of your
          faction's biomes). Condition not met → no effect and only <b>cost − 1</b> cells.</li>
      </ol>
      <p>The engine resolved all of this in one go; from now on the tutorial <b>replays it
        card by card</b>, so press OK and follow the glowing slot →</p>`,
  },

  /* 9 — game log */
  {
    id: "log", kicker: "9️⃣ The log", title: "📜 The game log = the true history",
    when: (c) => !!((c.st && c.st.log) || []).length,
    el: () => document.getElementById("log-panel"),
    // the replay is over: the tokens go back to the (identical) real positions
    enter: () => endReplay(),
    body: () => `<p>Every resolved turn, <b>stopover by stopover</b>, with both players' cards,
      the <b>✓/✗ condition</b>, the <b>effect that actually fired</b>, the penalties
      (<i>blocked</i>, <i>canceled</i>, …) and the <b>before → after</b> position.</p>
      <p>Click a turn (or a stopover) to open/close it. Hover a card in the log to enlarge it.
      When you wonder "what just happened to me?", the answer is here.</p>`,
  },

  /* 10 — cleaning phase */
  {
    id: "cleaning", kicker: "🔟 Cleaning", title: "End of turn: the cleaning phase",
    when: (c) => c.turn >= 2,
    el: () => document.getElementById("topbar"),
    body: () => `<p>Five things happen, in order:</p>
      <ol class="tu-ol">
        <li>every card played this turn goes to its owner's <b>discard pile</b> (survivors: a
          <i>rooted</i> card stays on the board one more turn);</li>
        <li>all tapped mana <b>untaps</b> — your pool is full again;</li>
        <li>each player <b>draws up to 3 cards</b> (the discard is reshuffled if the deck runs out);</li>
        <li><b>day ⇄ night</b> flips;</li>
        <li>the <b>turn order reverses</b> — the other player becomes the first player.</li>
      </ol>`,
  },

  /* 11 — the next turn's mana phase (DO IT) */
  {
    id: "mana-2", kicker: "1️⃣1️⃣ A new turn", title: "Mana phase: 1 card, or Pass",
    mode: "act",
    when: (c) => c.ph === "mana-pass",
    expired: (c) => c.ph !== "mana-pass",
    auto: (c) => c.ph !== "mana-pass",
    el: () => document.getElementById("dz-mana"),
    body: () => `<p>Every new turn opens with the <b>mana phase</b>: put <b>exactly one</b> card
      from your hand into ⚡ (your pool grows — more mana next turns) <b>or Pass</b>.</p>
      <p>Putting a card in mana is free, but that card is out of your hand for good: it becomes
      pure fuel.</p>
      <p>Then the <b>play phase</b> runs exactly like the previous turn — play while you have
      mana, then Pass.</p>`,
  },

  /* 12 — the support faction, closing note */
  {
    id: "support", kicker: "1️⃣2️⃣ One last thing", title: "Your support faction (10 of your 30 cards)",
    // never an info step ON TOP of a mana phase the player still has to answer
    when: (c) => c.turn >= 2 && c.ph !== "mana-pass",
    // …and the tour has to END sometime: 4 turns in, this last step is not worth waiting for
    expired: (c) => c.turn > 3,
    el: (c) => handCardEl(handOf(c).find((id) => isSupportPlay(id))) || document.getElementById("my-hand"),
    imgs: () => ["/assets/supfac_eng_banner.png", "/assets/supfac_mag_banner.png", "/assets/supfac_doc_banner.png"],
    label: "Finish",
    body: () => `<p>Half of your 10 support cards are pairs of the <b>5 cards of your support
      faction</b>. They cost mana like any other card — hover one in your hand to read it.</p>
      <p><b>⚙ Engineers</b> — place <b>drops on the Earth</b> (landmine, gluetrap, slowing trap,
      boost); they hit whoever arrives, <b>including you</b>. Dwelling: the <b>refinery</b>
      (tap 1×/turn: draw a card).</p>
      <p><b>✚ Doctors</b> — park cards in the <b>pending zone</b>, then <b>attach</b> one to a
      main card later to bolt its effect onto that play. Dwelling: the <b>laboratory</b>
      (tap: gain an <i>epo</i> pending).</p>
      <p><b>✦ Mages</b> — change the planet itself: fix <b>day/night</b>, <b>±4 °C</b>, choose the
      <b>cataclysm order</b>, <b>freeze all movement</b> for the turn, or <b>rotate the Earth</b>
      3 cells (black hole).</p>
      <p>That's everything — <b>good luck, and claim the Globe!</b></p>`,
  },
];

function slotNumOf(c) {
  const free = c.st ? freeCols(c.st, game.me) : [];
  return free.length ? slotNum(nextSlotCol(c.st, game.me)) : 1;
}

/* the turn whose trip chain has just been written to the log (both players passed) */
function resolvedTurn(c) {
  const log = (c.st && c.st.log) || [];
  return log.find((t) => (t.stopovers || []).length && t.turn === 1) || null;
}

/* ================================================ the turn-1 resolution REPLAY
   The engine resolves the whole turn in ONE message: when the client first sees the
   turn log, both tokens already stand on their final cells. So the tutorial replays
   the chain instead of only describing it — the tokens are pinned to their chain-start
   cells (`setMarkerOverride`, board.mjs), then the chain is walked entry by entry in
   the engine's own order (the log lists the stopovers, and inside a stopover the first
   player's entry before the second player's):

     card tip (what the engine actually did: block check → condition → effect →
     advancing + the total) → Earth tip ("watch the token") → OK slides the token to
     that entry's `pos_after` and holds the next tip for a moment, so the movement is
     actually seen.

   Nothing here changes the game: the overrides are a pure view layer, cleared when the
   replay ends (the `log` step) or when the run stops — by then the replayed position
   equals the real one, so the token does not jump. A page reload mid-replay simply
   drops the overrides (tokens back to their real cells, self-healing). */
let moveHoldUntil = 0;         // timestamp before which no new tip may appear
const MOVE_HOLD_MS = 900;      // the token's CSS slide is 450 ms — leave room to see it

const earthEl = () => document.getElementById("board-stage");

/* "Ocean"/"Mountain"/… of a cell, or null (public info: st.earth) */
function biomeAt(cell) {
  const list = ((game.state && game.state.earth) || [])[Math.max(0, Math.min(N_CELLS - 1, cell))] || [];
  const b = list.find((x) => BIOME_NAMES[x]);
  return b ? BIOME_NAMES[b] : null;
}

/* +1 forward bonus when `name` stands on one of their faction's two biomes
   (the player's faction is public: it is their token logo) */
function biomeBonusAt(name, cell) {
  const f = ((game.state && game.state.players && game.state.players[name]) || {}).faction;
  const own = FACTION_BIOMES[f];
  if (!own) return 0;
  const list = ((game.state && game.state.earth) || [])[Math.max(0, Math.min(N_CELLS - 1, cell))] || [];
  return list.some((x) => own.includes(x)) ? 1 : 0;
}

const markerEl = (name) => document.querySelector(`#markers-layer .marker[data-player="${CSS.escape(name)}"]`);
function markerHi(name, on) {
  const el = markerEl(name);
  if (el) el.classList.toggle("tu-marker-hi", !!on);
}

/* pin both tokens where their chain began (entry order = resolution order) */
function startReplay(c) {
  const t = resolvedTurn(c);
  if (!t) return;
  const first = {};
  for (const sv of (t.stopovers || [])) {
    for (const e of (sv.entries || [])) {
      if (e && e.player != null && first[e.player] == null && e.pos_before != null) first[e.player] = e.pos_before;
    }
  }
  for (const [name, cell] of Object.entries(first)) setMarkerOverride(name, cell);
  // the cleaning phase already emptied the slots — put this turn's cards back on them (in
  // hold mode `playChainAnim` only places the clones, it never self-advances), otherwise
  // every card tip of the replay would frame an empty slot. `log.mjs` cannot do it here:
  // its trip-chain guard deliberately skips the first rendered turn — which IS turn 1.
  playChainAnim(t);
}

/* hand the board back to the game (also safe when nothing was pinned) */
function endReplay() {
  moveHoldUntil = 0;
  clearMarkerOverrides();
  clearPulse();
  document.querySelectorAll(".marker.tu-marker-hi").forEach((m) => m.classList.remove("tu-marker-hi"));
  setChainAnimHold(false);              // clears the cloned cards on the slots too
}

/* the shield total of the defense play(s) facing this entry on the same stopover */
function facingShields(sv, e) {
  return (sv.entries || []).reduce((n, o) => {
    if (!o || o === e || o.mode !== "defend") return n;
    if (typeof o.shield === "number") return n + o.shield;
    return n + (o.cards || []).reduce((k, id) => k + ((cardInfo(id) || {}).shield || 0), 0);
  }, 0);
}

/* one step per card of that resolved turn, in the engine's resolution order, each one
   followed by the Earth step that moves the token that card moved */
function resolutionSteps(c) {
  const t = resolvedTurn(c);
  if (!t) return [];
  const out = [];
  for (const sv of (t.stopovers || [])) {
    const m = /^stopover_(\d+)/.exec(sv.stopover || "");
    if (!m) continue;
    const col = parseInt(m[1], 10) % N_STOPOVERS;
    const num = slotNum(col);
    const ents = (sv.entries || []).filter((e) => e && (e.cards || []).length);
    for (const e of ents) {
      const id = e.cards[0];
      const row = e.player === game.me ? "me" : "oppo";
      const other = ents.find((o) => o !== e && (o.player === game.me) !== (e.player === game.me));
      // keep that facing pair lit (gold = first player of the turn) while the card is
      // explained AND while its token moves: the pair is the subject of both tips
      const pulse = () => pulseStep(row, col, e.order === 1, other ? (other.player === game.me ? "me" : "oppo") : null);
      out.push({
        id: `res-${sv.stopover}-${e.player}`,
        kicker: `8️⃣ Resolution · stopover ${num} · ${e.order === 1 ? "1st" : "2nd"} card`,
        title: `${e.player}: ${cardName(id)}`,
        el: () => slotEls[row][col] || null,
        imgs: () => [cardImg(id)],
        enter: pulse,
        body: () => resBody(e, id, num, t, sv),
      });
      // a defense play never moves a token — no Earth step for it
      if (e.mode !== "defend") {
        out.push({
          id: `res-${sv.stopover}-${e.player}-move`,
          kicker: "8️⃣ Resolution · the Earth",
          title: `🌍 ${e.player}'s token`,
          el: earthEl,
          enter: () => { pulse(); markerHi(e.player, true); },
          // OK IS the movement, then a beat of quiet so the slide is actually seen
          onDone: () => {
            const to = (e.pos_after != null) ? e.pos_after : e.pos_before;
            setMarkerOverride(e.player, to != null ? to : 0);
            markerHi(e.player, false);
            return MOVE_HOLD_MS;
          },
          body: () => moveBody(e),
        });
      }
    }
  }
  return out;
}

/* what the Earth tip announces: the exact movement — and why there is none sometimes */
function moveBody(e) {
  const from = (e.pos_before != null) ? e.pos_before : null;
  const to = (e.pos_after != null) ? e.pos_after : from;
  const d = (from != null && to != null) ? to - from : 0;
  const who = esc(e.player);
  const at = (n) => (n == null ? "?" : `cell ${n} (${biomeAt(n) || "?"})`);
  if (d === 0) {
    const neg = (e.negatives || []).join(" · ").toLowerCase();
    const why = /blocked|canceled/.test(neg)
      ? "the card was <b>blocked</b> — a blocked card neither applies its effect nor advances"
      : e.effect === "effect_canceled"
        ? "its effect was <b>canceled</b>"
        : from === 0
          ? "a token can never go below <b>cell 0</b>, so a recoil keeps it on the start cell"
          : "this card does not move its owner here";
    return `<p><b>${who} does not move.</b> ${why.charAt(0).toUpperCase() + why.slice(1)} —
      the token stays on <b>${at(to)}</b>.</p>
      <p>Press <b>OK</b> for the next card of the chain.</p>`;
  }
  const card = cardInfo(e.cards[0]);
  const base = card ? card.advancing : null;
  const bonus = biomeBonusAt(e.player, from);
  const sign = d > 0 ? "+" : "";
  const how = e.condition_met === false
    ? `Condition ✗ not met → <b>no effect</b>, and the card only advances by its
       <b>mana cost − 1</b>.`
    : (e.effect === "jump"
      ? `The <b>jump</b> effect teleports the token straight to ${at(to)} — it does
         <b>not</b> walk the cells in between, so nothing on them can trigger.`
      : base != null
        ? `Basic advancing: the card's value <b>${base}</b>${bonus
          ? ` <b>+1</b> because it stands on one of its faction's biomes` : ""}.`
        : "Basic advancing moved the token by the amount shown above.");
  return `<p><b>${who}</b>: <b>${at(from)}</b> → <b>${at(to)}</b>
      — total <span class="tu-total">${sign}${d}</span>.</p>
    <p>${how}</p>
    <p>Press <b>OK</b> and watch the token slide.</p>`;
}

/* what actually happened to one card, read from the log entry (no guesswork) */
function resBody(e, id, num, t, sv) {
  const leg = cardLegend(id) || {};
  const card = cardInfo(id) || {};
  const known = !!cardInfo(id);          // false = the id is not in the served pool
  const cost = cardCost(id);
  const negatives = (e.negatives || []).join(" · ").toLowerCase();
  const blocked = /blocked|canceled/.test(negatives);
  const out = [];
  out.push(`<p><b>${esc(e.player)}</b> played it on <b>stopover ${num}</b>${e.order === 1
    ? " — as <b>first player</b>, their card is processed first at this position"
    : " — as <b>second player</b>, right after the first player's card at this position"}.</p>`);
  /* the play-time instants of THIS card already happened, before the chain ran */
  for (const ins of (t.instant || [])) {
    if (ins && ins.player === e.player && String(ins.what || "").includes(cardName(id))) {
      out.push(`<p class="tu-live">⚡ ${esc(ins.what)} — that happened <b>at play time</b>:
        an instant effect can never be blocked or canceled.</p>`);
    }
  }
  const shields = facingShields(sv, e);
  if (e.mode === "defend") {
    out.push(`<p><b>Defense play</b> (mana cost <b>${cost}</b>): engaged sideways, it does
      <b>not</b> advance. Its shield (<b>${e.shield ?? 0}</b>) is compared to the mana cost of
      the card facing it — shield ≥ cost blocks that card entirely.</p>`);
  } else {
    out.push(`<p><b>1. Block check</b>${shields > 0
      ? ` — <b>${shields}</b> shield(s) facing it on this stopover, this card costs <b>${known ? cost : "?"}</b>`
      : " — no defense card facing it on this stopover"}<br>
      → <b>${blocked ? "<span class='tu-bad'>blocked</span>" : "<span class='tu-good'>not blocked</span>"}</b></p>`);
    out.push(`<p><b>2. Condition</b> — ${esc(leg.condition || "this card has no condition")}<br>
      → <b>${e.condition_met === true ? "<span class='tu-good'>✓ met</span>"
        : e.condition_met === false ? "<span class='tu-bad'>✗ not met</span>" : "not evaluated"}</b></p>`);
    out.push(`<p><b>3. Effect</b> — ${esc(leg.effect || "none")}<br>
      → <b>${e.condition_met === false ? "<span class='tu-bad'>not applied</span>"
        : blocked ? "<span class='tu-bad'>canceled</span>"
        : leg.effect ? "<span class='tu-good'>applied</span>" : "nothing to apply"}</b></p>`);
    const before = (e.pos_before != null) ? e.pos_before : null;
    const after = (e.pos_after != null) ? e.pos_after : before;
    const d = (after != null && before != null) ? after - before : null;
    if (before != null) {
      out.push(`<p><b>4. Basic advancing</b>${known
        ? ` — card value <b>${card.advancing ?? 0}</b>${biomeBonusAt(e.player, before)
          ? " <b>+1</b> faction biome" : ""}${e.condition_met === false
            ? `; condition ✗ → only <b>cost − 1 = ${Math.max((cost || 1) - 1, 0)}</b>` : ""}`
        : " — the log records the movement below (this card is not in the served card pool, so its own numbers are unavailable)"}.</p>
        <p class="tu-total">Total advancement: cell <b>${before}</b> → <b>${after}</b>
          = <b>${d > 0 ? "+" : ""}${d}</b></p>`);
    }
  }
  for (const s of (e.negatives || [])) out.push(`<p class="tu-live">⚠ ${esc(s)}</p>`);
  for (const s of (e.notes || [])) out.push(`<p class="tu-live">📝 ${esc(s)}</p>`);
  return out.join("");
}

/* ------------------------------------------------------------------ the layer */
function buildLayer() {
  if (layer) return;
  layer = document.createElement("div");
  layer.id = "tu-layer";
  layer.className = "tu-layer hidden";
  layer.innerHTML = `
    <div class="tu-shade" data-side="top"></div>
    <div class="tu-shade" data-side="bottom"></div>
    <div class="tu-shade" data-side="left"></div>
    <div class="tu-shade" data-side="right"></div>
    <div class="tu-halo"></div>
    <div class="tu-arrow" data-dir="none"></div>
    <div class="tu-tip" data-side="bottom">
      <div class="tu-kicker"></div>
      <h3 class="tu-title"></h3>
      <div class="tu-imgs"></div>
      <div class="tu-body"></div>
      <div class="tu-foot">
        <span class="tu-progress"></span>
        <span class="tu-btns"><button class="tu-ok primary-btn small-btn">OK</button></span>
      </div>
    </div>`;
  document.body.appendChild(layer);
  tip = layer.querySelector(".tu-tip");
  halo = layer.querySelector(".tu-halo");
  arrow = layer.querySelector(".tu-arrow");
  for (const el of layer.querySelectorAll(".tu-shade")) shades[el.dataset.side] = el;

  // ONE button for the whole overlay — the OK of the tip being shown
  layer.querySelector(".tu-ok").onclick = advance;
  document.addEventListener("keydown", onKey);
}

function onKey(e) {
  if (!tutorial.active || !tutorial.visible || tutorial.muted) return;
  // never hijack a field, and never double-fire with a focused game button
  // (Enter on a focused "Pass" would otherwise pass AND dismiss the step)
  const t = e.target;
  if (t && /^(INPUT|TEXTAREA|SELECT|BUTTON|A|TEXT)$/.test(t.tagName || "")) return;
  if (t && t.isContentEditable) return;
  // Enter/Space = OK. No Escape: there is no other control anymore, and a stray key
  // must not silently throw away the rest of the tour.
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); advance(); }
}

/* a game popup (defense / discard / mage choices / zoom / endgame) is open */
const popupOpen = () => !!document.querySelector(
  ".modal:not(.hidden), .pending-popup, .discard-popup, .defend-popup, .swap-grid");

function rectOf(step, c) {
  const el = step && step.el ? step.el(c) : null;
  if (!el || !el.getBoundingClientRect) return null;
  const r = el.getBoundingClientRect();
  return (r.width < 1 && r.height < 1) ? null : r;
}

function setRect(el, l, t, w, h) {
  el.style.display = (w <= 0 || h <= 0) ? "none" : "block";
  el.style.left = `${Math.max(0, l)}px`;
  el.style.top = `${Math.max(0, t)}px`;
  el.style.width = `${Math.max(0, w)}px`;
  el.style.height = `${Math.max(0, h)}px`;
}

/* reposition the spotlight + the tip around the live element rect */
function sync() {
  if (!tutorial.active || !layer) return;
  const c = ctx();
  const hideAll = tutorial.muted;
  layer.classList.toggle("tu-mute", hideAll);
  if (hideAll) { layer.classList.remove("hidden"); return; }
  if (!tutorial.visible) { layer.classList.add("hidden"); return; }

  const step = SEQ[tutorial.index];
  const r = rectOf(step, c);
  const vw = window.innerWidth, vh = window.innerHeight, GAP = 16;
  const P = (step && step.pad) || 10;    // roomier frame for the whole-board steps
  if (!r) {
    // no (or invisible) target: dim the whole page and centre the tip
    setRect(shades.top, 0, 0, vw, vh);
    for (const k of ["bottom", "left", "right"]) setRect(shades[k], 0, 0, 0, 0);
    halo.classList.add("hidden");
    arrow.dataset.dir = "none";
    tip.classList.add("tu-center");
    tip.style.left = `${Math.max(8, (vw - tip.offsetWidth) / 2)}px`;
    tip.style.top = `${Math.max(8, (vh - tip.offsetHeight) / 2)}px`;
    return;
  }
  halo.classList.remove("hidden");
  tip.classList.remove("tu-center");
  const hole = {
    l: Math.max(0, r.left - P), t: Math.max(0, r.top - P),
    r: Math.min(vw, r.right + P), b: Math.min(vh, r.bottom + P),
  };
  setRect(shades.top, 0, 0, vw, hole.t);
  setRect(shades.bottom, 0, hole.b, vw, vh - hole.b);
  setRect(shades.left, 0, hole.t, hole.l, hole.b - hole.t);
  setRect(shades.right, hole.r, hole.t, vw - hole.r, hole.b - hole.t);
  halo.style.left = `${hole.l}px`; halo.style.top = `${hole.t}px`;
  halo.style.width = `${hole.r - hole.l}px`; halo.style.height = `${hole.b - hole.t}px`;

  /* the tip goes on the side with the most room; a narrow screen gets a bottom sheet */
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  const room = { bottom: vh - hole.b, top: hole.t, right: vw - hole.r, left: hole.l };
  let side = "bottom";
  for (const k of ["bottom", "right", "left", "top"]) if (room[k] > room[side]) side = k;
  let x, y;
  const sheet = vw < 760;
  if (sheet) side = "sheet";
  if (side === "bottom") { x = (hole.l + hole.r) / 2 - tw / 2; y = hole.b + GAP; }
  else if (side === "top") { x = (hole.l + hole.r) / 2 - tw / 2; y = hole.t - th - GAP; }
  else if (side === "right") { x = hole.r + GAP; y = (hole.t + hole.b) / 2 - th / 2; }
  else if (side === "left") { x = hole.l - tw - GAP; y = (hole.t + hole.b) / 2 - th / 2; }
  else { x = (vw - tw) / 2; y = vh - th - 12; }   // sheet
  const fits = x >= 6 && y >= 6 && x + tw <= vw - 6 && y + th <= vh - 6;
  x = Math.min(Math.max(8, x), Math.max(8, vw - tw - 8));
  y = Math.min(Math.max(8, y), Math.max(8, vh - th - 8));
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
  /* the tail is a LAYER child (not a .tu-tip ::after) — inside the tip it would count
     as overflow and raise a horizontal scrollbar on this scrollable card */
  const cx = Math.min(Math.max((hole.l + hole.r) / 2, x + 18), x + tw - 18);
  const cy = Math.min(Math.max((hole.t + hole.b) / 2, y + 18), y + th - 18);
  if (!fits || side === "sheet") { arrow.dataset.dir = "none"; return; }
  arrow.dataset.dir = side === "bottom" ? "up" : side === "top" ? "down"
    : side === "right" ? "left" : "right";
  if (side === "bottom") { arrow.style.left = `${cx - 9}px`; arrow.style.top = `${y - 18}px`; }
  else if (side === "top") { arrow.style.left = `${cx - 9}px`; arrow.style.top = `${y + th}px`; }
  else if (side === "right") { arrow.style.left = `${x - 18}px`; arrow.style.top = `${cy - 9}px`; }
  else { arrow.style.left = `${x + tw}px`; arrow.style.top = `${cy - 9}px`; }
}

function render(step, c) {
  tip.querySelector(".tu-kicker").textContent = val(step.kicker, c) || "";
  tip.querySelector(".tu-title").textContent = val(step.title, c) || "";
  const imgs = val(step.imgs, c) || [];
  const bodyHtml = (val(step.body, c) || "") + (step.anatomy ? (step.anatomy(c) || "") : "");
  // an "act" step is re-rendered on every tick (its live counters change) — never
  // rewrite the image/body DOM when nothing changed (an <img> re-attach flickers)
  const sig = imgs.join("|") + "\u0000" + bodyHtml;
  if (tip.dataset.sig !== sig) {
    tip.dataset.sig = sig;
    tip.querySelector(".tu-imgs").innerHTML = imgs
      .map((u) => `<img class="tu-img" src="${u}" alt="" onerror="this.onerror=null;this.remove()">`).join("");
    tip.querySelector(".tu-body").innerHTML = bodyHtml;
  }
  tip.querySelector(".tu-ok").textContent = step.label || "OK";
  const act = step.mode === "act";
  layer.classList.toggle("tu-act", act);
  tip.querySelector(".tu-progress").textContent = `Step ${tutorial.index + 1} / ${SEQ.length}` + (act ? " • your move" : "");
  tip.classList.add("show");
}

/* ---------------------------------------------------------------- flow control */
function show(step, c) {
  tutorial.visible = true;
  layer.classList.remove("hidden");
  render(step, c);
  // a step can attach a visual to the board while it is up (light a facing pair, pin a
  // token highlight…) — a throwing enter() must never break the run
  if (step.enter) { try { step.enter(c); } catch (err) { console.error("[tutorial] enter:", err); } }
  sync();
}

function advance() {
  let holdMs = 0;
  if (tutorial.index >= 0 && SEQ[tutorial.index]) {
    const cur = SEQ[tutorial.index];
    doneIds.add(cur.id);
    // `onDone` is the step's own consequence (the replay moves a token when OK is pressed
    // on its Earth tip) and may return a delay in ms: the layer stays off screen that
    // long, so the movement is seen instead of being covered by the next tip
    if (cur.onDone) { try { holdMs = val(cur.onDone, ctx()) || 0; } catch (err) { console.error("[tutorial] onDone:", err); } }
  }
  tutorial.visible = false;
  if (holdMs) { moveHoldUntil = Date.now() + holdMs; sync(); return; }
  if (tutorial.index >= SEQ.length - 1) { stopTutorial("done"); return; }
  tick();
}

/* insert the per-card resolution steps right after the intro step (once) */
let resExpanded = false;
function expandResolution(c) {
  if (resExpanded) return;
  const i = SEQ.findIndex((s) => s.id === "resolve-intro");
  if (i < 0) return;
  const extra = resolutionSteps(c);
  if (!extra.length) return;
  SEQ.splice(i + 1, 0, ...extra);
  resExpanded = true;
}

function tick() {
  // the tutorial is an optional overlay: a bug here must never break renderAll
  // (which calls tutorialTick) nor the game itself
  try {
    tickInner();
  } catch (err) {
    console.error("[tutorial] stopped after an error:", err);
    stopTutorial("error");
  }
}

function tickInner() {
  if (!tutorial.active || !game.state || !game.state.players) return;
  if (!layer) buildLayer();
  const c = ctx();
  if (c.ph === "over") { stopTutorial("over"); return; }

  const muted = popupOpen();
  if (muted !== tutorial.muted) { tutorial.muted = muted; sync(); }
  if (muted) return;

  // a token slide is playing (the resolution replay moved a marker): step off the screen
  // until it is over, so the player actually sees the movement
  if (moveHoldUntil && Date.now() < moveHoldUntil) { sync(); return; }

  // a live "act" step closes itself when the player did the action
  const cur = tutorial.visible ? SEQ[tutorial.index] : null;
  if (cur) {
    if (cur.mode === "act" && cur.auto && cur.auto(c)) { advance(); return; }
    if (cur.when && !cur.when(c) && cur.expired && cur.expired(c)) { advance(); return; }
    // the live "waiting for you…" counters
    if (cur.mode === "act") { render(cur, c); sync(); }
    else sync();
    return;
  }
  expandResolution(c);

  // linear scan: the first un-finished step that is eligible becomes visible
  while (tutorial.index + 1 < SEQ.length) {
    const s = SEQ[tutorial.index + 1];
    if (doneIds.has(s.id)) { tutorial.index++; continue; }
    if (s.expired && s.expired(c)) { doneIds.add(s.id); tutorial.index++; continue; }
    if ((s.when ? s.when(c) : true)) { tutorial.index++; show(s, c); return; }
    // the moment hasn't come yet — wait for the next state, and step off the screen
    // meanwhile: a finished tip must never stay frozen on screen (OK would look dead)
    sync();
    return;
  }
  if (tutorial.index >= SEQ.length - 1 && !tutorial.visible) stopTutorial("done");
}

export function tutorialTick() { tick(); }

export function startTutorial() {
  try {
    startInner();
  } catch (err) {
    console.error("[tutorial] cannot start:", err);
    stopTutorial("error");
  }
}

function startInner() {
  buildLayer();
  SEQ = STEPS.slice();
  doneIds = new Set();
  resExpanded = false;
  tutorial.active = true;
  tutorial.visible = false;
  tutorial.muted = false;
  tutorial.index = -1;
  document.body.classList.add("tu-on");   // CSS: raise the trip chain over the log panel
  setChainAnimHold(true);                  // the chain replay is driven step by step (see below)
  moveHoldUntil = 0;
  layer.classList.remove("hidden");
  if (!reposition) reposition = setInterval(() => { if (tutorial.active) tick(); }, 500);
  tick();
}

export function stopTutorial() {
  tutorial.active = false;
  document.body.classList.remove("tu-on");
  tutorial.visible = false;
  endReplay();                            // unpins the tokens, clears the clones + pulse
  if (reposition) { clearInterval(reposition); reposition = null; }
  if (layer) layer.classList.add("hidden");
  if (tip) tip.classList.remove("show");
}
