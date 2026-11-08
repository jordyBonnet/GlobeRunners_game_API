# GlobeRunners — Game log (`GameState.log`)

> Part of the project instructions, split out of `AGENTS.md` (2026-09-21).
> Back to [AGENTS.md](AGENTS.md). The public per-turn recap structure, log-note legend, and frontend rendering.

### Game log (`GameState.log`)

A **public recap of every resolved turn**, built by the engine during Phase 3 (`process_trip_chain`) and persisted with the game state. It contains **only public information** (played cards, positions, resolution outcomes) — never hand/mana/deck contents — so it is safe in both the personalized (`current_game_json`) and full (`GET /game/{id}`) endpoints.

Structure (a list, one object per resolved turn, in turn order):

```json
{
  "turn": 3,
  "instant": [
    { "player": "Alice", "what": "🏭 refinery placed on the board — tap: draw 1 card" },
    { "player": "Bob",   "what": "🔮 Celestial_reversal — day/night fixed to night" }
  ],
  "stopovers": [
    {
      "stopover": "stopover_4",
      "entries": [
        {
          "player": "Alice", "order": 1, "mode": "move",
          "to": "stopover_4",
          "cards": ["Dwa45_059aad"],
          "pos_before": 1, "pos_after": 7,
          "condition_met": true,          // true / false / null (defend, or blocked before evaluation)
          "effect": "advancing",          // the card's effect (informational)
          "shield": null,                 // shield value for defend cards, else null
          "negatives": ["blocked — shields 4 ≥ cost 2"],   // red flags: what went wrong for this card
          "notes": ["faction biome bonus +1"]  // extra events that happened (kept short and simple)
        }
      ]
    }
  ]
}
```

- **Created**: both players' entries are created *before* their action resolves (so cross-events — a block fired on the opponent's card, a grappling copy — can attach to either line) and finalized after (positions reflect everything, including grappling copies). A turn in which **nothing** was played (both passed) is **dropped** from the log — **unless** its `instant` section is non-empty (a **tap-only turn** keeps its entry, the instant lines are its only record). If the **game ends mid-chain**, the partial turn is still kept.
- **`instant`** (turn-level, `{'player', 'what'}` one line per event, written by `_append_instant`) — everything that happens **outside** the trip chain and therefore belongs on no card line:
  - **play-time instant effects**: `🪤 pet_trap — drop token placed on cell N`, `🧲 <drop> — drop placed on cell N`, `💥 wrecking_ball — removed <name>'s dwelling <card>`, `🔀 swap_cards — <card> swaps places with <target>`, `🔮 Celestial_reversal — day/night fixed to <phase>`, `🌡️ thermic_flux — temperature A → B °C`, `🚫 nobodymoves — all players' movement locked for the rest of the turn`, `☄️ Apocalypticritual — cataclysm order set: …`;
  - **dwelling PLACES** (`🏭 refinery / ⚗️ laboratory / 🕳️ black_hole placed on the board — tap: …`) and **dwelling TAPS** (`🏭 refinery tap — draws N card(s)` / `— nothing left to draw`, `⚗️ laboratory tap — adds an 'epo' pending card`, `🕳️ black_hole tap — the earth rotates 3 cells clockwise|counter-clockwise`);
  - **pending-zone PLACES**: `💊 <epo|virus|bloodtest|mercurochrome> — pending card placed, attach it to one of your move cards`.
  The entry is created at PLAY TIME (before `process_trip_chain`), which then **reuses** it (see the reuse comment in `process_trip_chain`) so the section is never orphaned.
- **`negatives`** (what prevented this card from doing its thing): `blocked — shields X ≥ cost Y`, `effect canceled by <name>`, `landmine — blocked`, `rooted on cooldown — no token`, `<source> — rooted card blocked`, …
- **`notes`** (extra events, any player's line — **kept short and simple: one line per event, no rule re-explanations in parentheses**):
  - *movement / board*: `faction biome bonus +1`, `🧲 boost on cell N — [+2 advancing | <name> +2 advancing]`, `🦘 trampoline on cell N — …jump +2`, `🩹 gluetrap on cell N — …knocked back -1`, `💥 landmine on cell N — <name> blocked`, `🪤 drop on cell N — …knocked back -K`, `⚡ cataclysm — <biome> strikes`, `🏔 avalanche — <biome> strikes`, `<knockback label>: <name> knocked back A → B`, `🏆 <name> reached cell 24 — win!`.
    **Whose token?** a drop / pet_trap note is written on the line of the card being **resolved**; when the token it caught is the **opponent's** (pushed there by `advancing_oppo` / `backward_oppo`) the note **names that player**, when it is the line owner's own token the name is omitted.
  - *blocks / facing*: `block broken — shields X < cost Y`, `block card <name> — effect fired: <effect>`, `blocked <name>’s card on <stopover>` (`… rooted card …`), `🌱 rooted card — basic advancing only`, `🌱 rooted — card survives onto the trip chain`, `grappling hook — copied +N`, `copy_effect — copied "<effect>" (<name>)`.
  - *locks / exceptions*: `🚫 nobodymoves — movement locked`, `nobodymoves — <effect|basic advancing|reduced advancing|cataclysm knockback> suppressed`, `nobodymoves — pending epo|virus suppressed`, `unstoppable — ignored the opponent block|the landmine block|the nobodymoves lock`.
  - *doctor pendings* (always on the line of the card they were **attached** to): firing → `pending epo — +1 advancing`, `pending virus — -1 knockback`, `💊 bloodtest — <name> must choose 1 card to discard` / `pending bloodtest — opponent has no cards in hand`; passive → `💊 mercurochrome attached — unstoppable this turn`; **wasted** → `💊 <pending> spent — the card was blocked by <name> | the card was canceled by a landmine | the card effect was canceled | the card's condition was not met | the game ended before it could fire` (a pending NEVER leaves the zone without a line).
  - *discard selection* (the chain paused and an answer arrived): `🗑 discard selection — <name> discarded: <card>`.
- **`order`**: 1/2 = first/second player of **that turn** (turn order flips every turn).
- **Empty log**: a game with no turns yet (or one that predates the log feature) has `log: []` (the field defaults to empty) — the UI simply shows no log. The log is an additive read-only recap; it does not change any rule.
- **Replay/AI**: the replay only **reads** two prefixes of the log (`⚡ cataclysm —` / `☄️ Apocalypticritual` in `notes` + the `instant` section, to rebuild the cataclysm pile) and ignores every other string, so **adding or rewording a note never breaks a replay** — but it must never *rename* those two prefixes.
- **Frontend** (`game_ui/static/`): collapsible panel right of the stopover board (`#log-panel` in `index.html`, styles in `style.css`, renderer in `log.mjs`). New turns are appended incrementally (existing collapse state is preserved across polling re-renders); on first render / rejoin, all turns appear collapsed except the latest. Card thumbs hover-zoom and open the card modal on click (`cardTitle` → support-aware: a support card shows its faction + description). The `instant` section renders as a **gold box at the top of the turn** (`.log-instant-sec`, before the stopovers); a card line whose card is a **support card** shows a neutral `⚙ <faction>` tag (`.log-tag.sup`) because the support table has no `effect` column — its effect text is in the instant box or on the card it was attached to.
