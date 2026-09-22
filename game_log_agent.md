# GlobeRunners — Game log (`GameState.log`)

> Part of the project instructions, split out of `AGENTS.md` (2026-09-21).
> Back to [AGENTS.md](AGENTS.md). The public per-turn recap structure, log-note legend, and frontend rendering.

### Game log (`GameState.log`)

A **public recap of every resolved turn**, built by the engine during Phase 3 (`process_trip_chain`) and persisted with the game state. It contains **only public information** (played cards, positions, resolution outcomes) — never hand/mana/deck contents — so it is safe in both the personalized (`current_game_json`) and full (`GET /game/{id}`) endpoints.

Structure (a list, one object per resolved turn, in turn order):

```json
{
  "turn": 3,
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

- **Created**: both players' entries are created *before* their action resolves (so cross-events — a block fired on the opponent's card, a grappling copy — can attach to either line) and finalized after (positions reflect everything, including grappling copies). A turn in which **nothing** was played (both passed) is **dropped** from the log. If the **game ends mid-chain**, the partial turn is still kept.
- **`negatives`** (what prevented this card from doing its thing): `blocked — shields X ≥ cost Y`, `effect canceled by <name>`, …
- **`notes`** (extra events, any player's line — **kept short and simple: one line per event, no rule re-explanations in parentheses**): `faction biome bonus +1`, `🌋 cataclysm — <biome> strikes`, `🏔 avalanche — MO strikes`, `grappling hook — copied +N`, `unstoppable — ignored the opponent block`, `block broken — shields X < cost Y`, `block card <name> — effect fired: <effect>`, `blocked <name>'s card on <stopover>`, `🪤 pet_trap — drop token placed on cell N`, `🪤 drop on cell N — knocked back −K`, `🏆 <name> reached cell 24 — win!`
- **`order`**: 1/2 = first/second player of **that turn** (turn order flips every turn).
- **Empty log**: a game with no turns yet (or one that predates the log feature) has `log: []` (the field defaults to empty) — the UI simply shows no log. The log is an additive read-only recap; it does not change any rule.
- **Replay/AI**: untouched — the log is written only by the engine; `replay.py` and `PlayerAI` ignore it. All engine functions that gained a `log_entry`/`oppo_entry` parameter default to `None`, so existing callers are unchanged.
- **Frontend** (`game_ui/static/`): collapsible panel right of the stopover board (`#log-panel` in `index.html`, styles in `style.css`, renderer in `log.mjs`). New turns are appended incrementally (existing collapse state is preserved across polling re-renders); on first render / rejoin, all turns appear collapsed except the latest. Card thumbs hover-zoom and open the card modal on click.
