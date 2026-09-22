# GlobeRunners — the game — Project instructions

2-player card game served on the web (FastAPI + WebSocket). Architecture details in `README.md` — read it before modifying the engine.

## Architecture (respect the layering)

- `engine/game_engine.py` — **the only layer that knows the rules**. API, UI, AI and analysis call it, never the other way around.
- `models.py` — shared Pydantic models (`Card`, `PlayerState`, `GameState`; `engine_version` records the rules version a game was created under).
- `API.py` — standalone REST + WS layer (port 8000).
- `game_ui/` — full web app (port 8001): vanilla frontend, `ai_driver.py` (Robot).
- `player_ai/` — `PlayerAI` heuristics, orchestrated by `game_ui/ai_driver.py`.
- `games/` — `games.db` (SQLite, final state of games) + `games/analysis/` (replay, port 8017).
- `cards/cardpool.parquet` — **single source of truth for cards** (~7600), read directly by engine/AI/analysis.
- `cards/support_factions.parquet` — the 15 **support faction** cards (Engineers/Mages/Doctors), a second deck mixed into each player's deck (20 main + 10 support = 30). See [sup_fact_agent.md](sup_fact_agent.md) for the full spec. **Engineers are implemented** (4 drop cards + the refinery dwelling, and all support cards cost their `mana_cost`); **Doctors are implemented** (4 pending cards + the laboratory dwelling; the lab tap is a quick action without a trip-chain placeholder; the pending placeholders are `[card, slot]` pairs and attachment removes the placeholder by card name; the placeholder of a pending card attached the SAME turn stays in place); the **`pending` CONDITION** is implemented (met iff ≥1 pending card in the player's own pending zone — see *Pending condition* in [cond_effects_agent.md](cond_effects_agent.md)); the **Mages' `Celestial_reversal`** card is implemented (INSTANT: fixes day/night to the player's choice for the rest of the game — see *Mages* in [sup_fact_agent.md](sup_fact_agent.md)); the **Mages' `nobodymoves`** card is implemented (INSTANT: locks ALL players' MOVEMENT for the rest of the turn — non-movement effects still fire, only unstoppable may move — see *Mages* in [sup_fact_agent.md](sup_fact_agent.md)); the **Mages' `thermic_flux`** card is implemented (INSTANT: changes the planet temperature by ±4 °C to the player's choice, clamped 1..20, permanently — see *Mages* in [sup_fact_agent.md](sup_fact_agent.md)); the **Mages' `Apocalypticritual`** card is implemented (INSTANT: at play time the player CHOOSES the order of all 4 cataclysm biomes and the cataclysm pile is set to that order — index 0 strikes next — see *Mages* in [sup_fact_agent.md](sup_fact_agent.md)); the **Mages' `black_hole`** card is implemented (a **DWELLING** card like the refinery/laboratory: placed in the dwelling zone, and its TAP rotates the earth 3 cells CW or CCW to the player's choice — the biomes shift position on the board, all tokens stay on their cell, cumulative + permanent — see *Mages* in [sup_fact_agent.md](sup_fact_agent.md)). All 5 Mages cards are now implemented.

## Frontend doc — `game_ui/static/*.mjs` ↔ `game_ui/static/app-js-overview.md`

The frontend is a set of **ES modules** in `game_ui/static/` (entry: `app.mjs`; the old monolithic `app.js` was split). `game_ui/static/app-js-overview.md` is the living architecture map of the module set (per-module responsibilities, global state, communication model, action dispatch, rendering invariants).

1. **READ `game_ui/static/app-js-overview.md` BEFORE making any change to the `game_ui/static/*.mjs` modules** (or before modifying the frontend in a way that touches its architecture).
2. **Any non-trivial change to a frontend module** (new function, changed control flow, new WS/message shape, new UI mode, renamed/removed function, new card type, new module, changed import graph) **must be accompanied by an update of `app-js-overview.md` in the same change**. Trivial fixes (typo, debug line, CSS tweak) do not require a doc update.

## Invariants — never break

1. **No in-memory state**: every action re-reads the state from `games.db` (source of truth), applies the rule, writes the state back.
2. **Hidden information**: never return the opponent's hand/mana/deck to a player — always via `current_game_json` (masking). `GET /game/{id}` is the only full-state endpoint (debug/analysis).
3. **Player message**: `{"cards": [...], "to": "stopover_k|mana|dwelling|discard_pile|...", "mode": ""|"move"|"defend"|"pass"|"dwelling_activation", "pendings": [], "cell": <0-23, optional>, "swap_with": <1-5, optional>}` — validated by `message_check`. The optional `cell` targets an engineer drop placement; `to: "dwelling"` places the dwelling card and `mode: "dwelling_activation"` taps it (no cards). The optional `swap_with` (a position 1..5 of the player's own trip chain) is the target of the **`swap_cards`** effect (see *Swap cards* in [cond_effects_agent.md](cond_effects_agent.md)); it is ignored unless the card being played has that effect.
4. `handle_websocket_message` is the **single entry point** of the engine; go through it.
5. When changing a rule in `engine/`, also check the AI mirror (`player_ai/playerai.py` re-implements `_condition_met`) and the replay (`games/analysis`).
6. **Never change `engine_version` without explicit user approval**: if a rule change warrants a new rules version, STOP and ask the user first ("Do you want to create a new version, e.g. \"1.1\"?"). Only after the user confirms the exact version string may it be set in `game_engine.py` (single assignment at game creation). Never invent, bump or rename the version on your own.

## Game rules

> **This section is the canonical game spec** (designer-confirmed). `engine/game_engine.py` is the current implementation; the `condition`/`effect` columns of `cards/cardpool.parquet` define what exists in the pool.
> (Historical note: earlier revisions of this spec carried **⚠ engine** flags where the engine lagged behind. Both of those gaps — the **+1 faction biome bonus** and the **conditional block-card effect** — are now implemented and no longer flagged.)
> **Split spec (2026-09-21):** the *Conditions*/*Effects* tables + per-card details live in [cond_effects_agent.md](cond_effects_agent.md); the *Support factions* (Engineers/Doctors/Mages) spec lives in [sup_fact_agent.md](sup_fact_agent.md); the *Game log* spec lives in [game_log_agent.md](game_log_agent.md). A `see *X*` reference in this file points to the file that holds X.

### Board & setup

- The board ("earth") is **24 cells**, indexed 0→23, split into **4 biomes × 6 cells**: `OC` (ocean), `MO`, `DE` (desert), `JU` (jungle). Biome order is randomized at game start.
- Both players **start on cell 0**.
- **Win condition**: the first player to advance to cell **24** (i.e. past the last cell) wins — checked on every forward step.
- Each player starts with **6 cards in hand** (drawn from their deck), **3 cards in the mana zone** (placed during initialization), the rest in the deck.
- **Temperature**: roll 2d20, keep the roll closest to 10 (ties broken randomly). Rolled once, at game start.
- **Day/night**: always starts on `day`, flips every new turn.
- **Turn order**: first player chosen randomly.

### Turn anatomy (chronological)

A game is a sequence of turns. Each turn has **4 phases**:

```
INIT (once, turn 1) → [ MANA → PLAY → RESOLUTION → CLEANING ] → turn+1 → repeat
```

**Turn 1's MANA phase is skipped** — the 3 mana cards placed during INIT already cover it.

#### Phase 0 — Initialization (state: `"waiting for both players to put 3 cards in hand"`)

1. Each player places **3 cards** from their hand into their **mana zone** (message `to: "mana"`; 1 or 3 cards per message allowed; cards not in hand → rejected).
2. When **both** players have exactly 3 cards in mana → **turn 1 begins directly in the PLAY phase** (its MANA phase is skipped: the initial mana is already set).

#### Phase 1 — Mana phase (state: `"waiting for both players to mana or pass"`) — *skipped on turn 1*

1. Each player must **either pass** or place **exactly 1 card** from hand into their mana zone (`to: "mana"`, exactly 1 card). Placing a mana card is free (no cost) and grows their mana pool.
2. When **both** have acted (mana or passed) → the **PLAY phase** begins.

#### Phase 2 — Play phase (state: `"turn N - waiting for first/second player (NAME) to play"`)

Players **alternate** acting; the state indicates whose action is expected. A player's options:

- **Pass** (`mode: "pass"`): declares "I play nothing more this turn". Costs nothing. Control passes to the opponent.
- **Play a card** (branch below). A player may play **several cards per turn** as long as they have mana and have not passed; after each play, control alternates to the opponent (unless they already passed).

**Playing a card:**
- Cost: sum of the `mana` values of the played card(s). Payable only from **available mana** = (cards in mana zone) − (mana already spent this turn, `mana_spend`). If cost > available → **rejected, no state change**.
- **Instant effects** (move mode only): some effects fire **at play time**, before the trip chain resolves, and therefore **cannot be blocked, canceled or conditioned** — the card's `player_play` succeeds → the effect has already happened. Currently: `pet_trap` (see *Pet trap* in [cond_effects_agent.md](cond_effects_agent.md)). The effect is annotated on the player's message (`drop_placed_on`) so the turn log can show it.
- `mode: "move"` — play **exactly 1 card** onto a **stopover** slot (`to: "stopover_4"` ... `"stopover_0"`). Normal play: it will advance, trigger its effect, etc.
- `mode: "defend"` — play **1 to 5 cards** sideways (90°) onto a stopover slot. They do **not** advance and do **not** trigger their effect (see *Defense & blocking* below); their `shield` value contributes to blocking.
- On success: cards leave the hand, the action is appended to the player's **`action_chain`** (resolution order = play order), `mana_spend` increases.

**Branch — turn end:** the play phase ends **only when both players have passed**. As soon as that happens, the engine immediately runs **Phase 3 (resolution)**.

#### Phase 3 — Resolution (the "trip chain", `process_trip_chain`)

1. Played cards of both players are resolved **alternately, index by index**: action 1 of P1, action 1 of P2, action 2 of P1, action 2 of P2, … until both chains are exhausted (shorter chain simply ends).
2. **If the game ends mid-chain** (a win is declared), the remaining actions of the chain are **not resolved** — the game stops immediately (the engine checks the game state **before every action**, including the grappling/copy steps after each index).
3. **Card conservation on a mid-chain win**: played cards leave the hand at play time and only reach the discard when their action resolves (`process_card` does `discard.extend`) — so on a mid-chain win the **unprocessed** actions' cards would be lost from every zone. `process_trip_chain` therefore flushes them to their owners' **discard piles** at the win point (`flush_unresolved`, called in every game-over branch). The final state always conserves every card of the deck. Since 2026-09-02 `flush_unresolved` also **clears both players' `action_chain`** after the flush (the cards are all in the discard by then) — before that the stored final state kept a **phantom `action_chain`** whose cards were already in the discard (double-counted in the JSON, stale cards on the board render). Old saved games may still have that phantom chain — the replay ignores the stored chain (it compares zones only), so this is cosmetic for old games.

**Resolving ONE action (`process_card`):**

1. **Defense action** (`mode: "defend"`): each defended card is checked —
   - if its `condition == "block"` → it is a *dedicated block card*: its **effect is armed but fires only when the opponent's card on that stopover was actually blocked** (see *Defense & blocking*). It does **not** fire when the defend action itself resolves.
   - otherwise → it does nothing here; its `shield` value is what counts when blocking the opponent's card (see *Defense & blocking*).
2. **Move action** (`mode: "move"`, exactly 1 card):
   - **Step A — Block check**: does the **opponent** have defend card(s) on the **same stopover**?
     - **Exception 1**: the card has `effect: "unstoppable"` **and its condition is met** → it **ignores the block** entirely.
     - **Exception 2**: the block only holds if the **sum of the opponent's defend shields** on that stopover is **≥ the card's `mana` cost**.
       - shields ≥ mana → **BLOCKED**: the moving card's effect/advancing are cancelled, **and the defender's dedicated block card(s) on that stopover now fire their effect** (a "Card blocked by …" message is set).
       - shields < mana → block fails, card resolves normally.
   - **Step B — Condition**: evaluate the card's `condition` (see *Conditions* in [cond_effects_agent.md](cond_effects_agent.md)).
     - **`cataclysm` condition** → the **cataclysm trigger fires first** (see *Cataclysm* in [cond_effects_agent.md](cond_effects_agent.md)), then the condition is treated as met.
     - **Condition met** → apply the card's `effect` (see *Effects* in [cond_effects_agent.md](cond_effects_agent.md)), then apply **basic advancing** — **except** when the effect itself handles movement (`advancing`, `backward`, `jump`), which already moved the player.
     - **Condition NOT met** → **no effect**; the card still advances by a **reduced amount = `mana` cost − 1** (can be 0 or negative — a negative value recoils the player, clamped at cell 0).
3. **Every card played this turn (move or defend) is moved to the discard pile at resolution time**, so the deck can be reshuffled later.

**Defense & blocking — details**
- A **stopover** is a shared slot: the k-th card of a turn is conventionally placed on `stopover_{4-k+1}` → columns 4, 3, 2, 1, 0 (see below).
- A defend card **blocks only the opponent card placed on the SAME stopover**.
- The block is a **shield race**: `Σ shields of defender's cards on that stopover` vs `mana cost of the attacking card`. Defender wins the block iff `Σ shields ≥ mana cost`.
- `shield` values: 0–6. A card with `shield 0` contributes nothing.
- **Block effect**: if the opponent's card is **blocked** and a defending card has `condition == "block"`, that card's **effect takes place at the moment of the block** (dedicated block card). A block card that did **not** block anything has **no** effect.

**Advancing — movement (`process_advancing`)**
- **Faction biome bonus**: if the player's token is on one of the **two biomes** of their faction (see the biome ↔ faction map below), the **forward** advancing value gets **+1** (applies to `process_advancing` and to `jump` distance; recoil / backward movement is **not** boosted).
- Move one cell at a time, forward (+1) or backward (−1).
- **Clamp**: a player can never go below cell 0.
- On **every forward step into cell ≥ 24** → **WIN**, game over (player is visually put back on cell 0 to show crossing the finish line).
- After landing on each cell, check the cell's content:
  - **`trap`** → apply the trap effect.
  - **`drop`** → apply the drop effect.
  - **Note**: `trap`/`drop` are **not in the effect pool** and nothing currently places them on the board, so both are **no-ops** today — the checks exist in `process_advancing`/`_jump` for future use.
- `jump` effect: teleports directly to `current_position + advancing`, **skipping all intermediate cells** (no per-step trap/drop checks), then checks **only the landing cell** for trap/drop. Jump always moves forward.

#### Phase 4 — Cleaning (end of turn)

1. **Stopover cleanup**: all cards played this turn (move and defend, both players) leave the stopovers and go to their owner's **discard pile** — so the deck can be reshuffled later. *(Engine: applied as each action resolves, in Phase 3.)*
2. **Untap**: all tapped (spent) mana cards are untapped — `mana_spend` resets to 0.
3. **Draw**: each player draws **up to 3 cards** from their deck; if the deck has fewer than 3, the **discard pile is reshuffled into a new deck** and drawing continues.
4. **Day/night flips** (day ↔ night).
5. **Turn order reverses** — the other player becomes "first" — and the turn counter increments.

*(Engine note: steps 2–5 are performed in the "start of next turn" block right after resolution, before the next MANA phase — same cycle, rotated.)*

### Game log (`GameState.log`)

→ [game_log_agent.md](game_log_agent.md) — the public per-turn recap structure, the `negatives`/`notes` legend, and the frontend rendering.

### Conditions & effects (card `condition` / `effect` columns)

→ Full spec in [cond_effects_agent.md](cond_effects_agent.md): the `condition` table, the `effect` table, and every per-card detail section (Cataclysm, Drop on board, Pending condition, Avalanche, Grappling hook, Effect canceled, Copy effect, Pet trap, Rooted, Wrecking ball, Swap cards, Discard selection).

### Win & end conditions

- **Win**: any player's forward movement reaches cell **24 or beyond** → that player is `winner`, state → `"game over"`. Resolved immediately, even mid-trip-chain (the winner's stored position is the **last applied step** — the winning step itself is not applied, so a 19→23 move that crosses 24 stores 23).
- **Card conservation**: a mid-chain win flushes the unprocessed played cards to their owners' discard piles (`process_trip_chain` → `flush_unresolved`), so the final state always contains **every** card of the deck exactly once across hand/deck/discard/mana. (Old games created before this fix may be missing a few cards — the replay reconstructs them in the discard.)
- **Deadlock**: after a resolution, if **both players** have **no cards** in hand, deck, **and** discard (everything stuck in their mana zones), the game can never progress:
  - winner = the player **furthest ahead** on the board;
  - if tied → **draw** (`winner` stays `None`).

### Card structure (columns of `cards/cardpool.parquet`)

| Column | Range / meaning |
|---|---|
| `faction` | Dwarves, Demons, Twigs, Miaous, Orcs, Mummies |
| `mana` | 1–5 — cost to play the card |
| `advancing` | −3 … 11 — base cells moved (can be negative) |
| `shield` | 0–6 — blocking strength when played in defend mode |
| `condition` | 31 values (see *Conditions* in [cond_effects_agent.md](cond_effects_agent.md)) |
| `effect` | 22 values (see *Effects* in [cond_effects_agent.md](cond_effects_agent.md)) |
| `effect_number` | −2 … 3 — quantitative parameter of the effect |
| `rare` | True / False |
| `card_id`, `name`, `condeff_value`, `prompt`, `negative_prompt` | identity / text fields |

### Support factions (second deck — Engineers / Doctors / Mages)

→ Full spec in [sup_fact_agent.md](sup_fact_agent.md): deck composition (20 main + 10 support) and all 3 support factions — Engineers (4 drop cards + refinery dwelling), Doctors (4 pending cards + laboratory dwelling), Mages (Celestial_reversal, nobodymoves, thermic_flux, Apocalypticritual, black_hole).

### Placeholders occupy trip-chain positions

Board furniture — a **doctor pending** placeholder (`PlayerState.pending_slots`) or a **dwelling** placeholder (`PlayerState.dwelling_slot`) — already consumed a **display** position (`player_play` advances `play_count` on placement, so the next play renders one stopover column later). But the **trip-chain resolution** (`ge._player_chain`) excluded the placeholders and renumbered the plays densely — so the internal facing (position number) did **not** match the board display, and a `grappling_hook` that visually faced a placeholder still **copied the advancement of the card in the next row** (and vice versa — the bug observed in game `26_09_17_18_39_29_78u8l`, turn 3: both grappling hooks activated and copied each other even though one faced the opponent's pending placeholder).

The trip chain matches the display:
- `ge._player_chain(game, name)` now includes **placeholder entries** (`{'kind': 'placeholder', 'position': 5 - column}`) for the player's `pending_slots` / `dwelling_slot`, and derives every play's position **from its recorded stopover column** (`position = 5 - column`) instead of dense enumeration. Rooted cards keep the leading positions as before.
- A **placeholder is an EMPTY position**: `process_trip_chain` skips it (no card, no advancing, no block, no log entry). A `grappling_hook` or `copy_effect` facing a placeholder therefore **copies nothing** (`grappling_copy_amount` = 0 → `apply_grappling_copy` is a clean no-op, no log note); `copy_effect` is already guarded by `e_f['kind'] == 'play'`; blocking is by stopover column, so defend cards never "block" a placeholder (there is no card there).
- **Chain gaps**: when a pending card is **attached** to a play it is removed from `pending_slots`, leaving a **gap** — the plays beyond it keep their recorded positions. `process_trip_chain` / the replay iterate `max_pos = max(entry positions)` (not `max(len(chain_f), len(chain_s))`), so a play past a gap is still resolved.
- **AI**: since A.1 (`player_ai/AI_improvement_list.md`, ✅ 2026-09-21) `PlayerAI` **does** play support cards, so the robot CAN create pending/dwelling placeholders: `ai_decide` therefore computes every move/defend/support-play stopover with `played_before = me.play_count` (the engine's own counter — it counts moves+defends AND placeholder placements; an action_chain-derived sum under-counts). The engine remains the single source truth (`player_play` overwrites the client's `to`). `put_mana` no longer burns support cards first (keep-value model). Since A.2 (same date) the robot also **attaches** pendings to move plays (`pendings: [name]` via `_choose_attachment` — epo/mercurochrome only, never when the effect cannot fire; see *A.2* in [AI_improvement_list](player_ai/AI_improvement_list.md)). Since §C–§G (2026-09-21/22) it also scores plays with a resolution-exact movement model + win detection + pass discipline (§C), reacts to declared threats with value-based multi-card defends instead of a coin flip (`choose_defend`, §D), and places reactive effects by what they face — `swap_with` decoys, `effect_canceled`/grappling/copy targeting (§E). The robot is **fair-play by default**: it never reads the opponent's hidden hand/deck/mana contents (only public counts); `run_ai_loop(..., hard_mode=True)` is an explicit opt-in for perfect information that only removes false-positive threat guesses, never adds threats. Status of every item: [player_ai/AI_improvement_list.md](player_ai/AI_improvement_list.md) (§A–§G done, §H open). See *A.1*.
- **Frontend**: no change — the placeholder rendering was already correct (the display was the *right* side of the bug); the facing display is column-based and the grappling/copy logic is engine-only.
- **Tests**: `tests/_grappling_placeholder.py` (a grappling hook facing a placeholder copies nothing, while a grappling hook facing a plain card copies its advancing; deletes its own games).

### Stopover & ordering convention (frontend + AI)

**Per-player stopovers (CURRENT MODEL).** Each player has their **OWN 5 stopover slots** (positions 1-5, columns 4, 3, 2, 1, 0). A player's **rooted cards** occupy the **leading positions** of that player's chain (position 1, 2, …), and the player's **plays** (move OR defend) go **after** the rooted cards (position R+1, R+2, …). A player's rooted cards **never shift the opponent's positions** — the two players' position numbers are independent.
- **Position → column**: position `p` → column `5-p` → `stopover_{5-p}` (position 1 → `stopover_4`, position 2 → `stopover_3`, …, position 5 → `stopover_0`). Capped at 5 (a 6th card would overflow).
- **`ge._player_stopover(game, name, played_before)`** is the **single source of truth**: it computes a player's stopover for their `(played_before+1)`-th play, after that player's rooted cards. The engine uses it for its own placements (the dwelling placeholder slot and the rooted end-of-turn placement), the robot calls it directly (`ai_driver.py`), and the **frontend mirrors the rule in JS** (`actions.mjs` — it cannot import Python).
- **`ge._player_chain(game, name)`** builds a player's full trip chain: `[{kind:'rooted', …, position}, …] + [{kind:'placeholder', …, position}, …] + [{kind:'play', …, position}, …]` — the rooted cards first, then the **board-furniture placeholders** (pending / dwelling — see *Placeholders occupy trip-chain positions*), then the plays. `process_trip_chain` iterates **positions** (1..max_pos, where max_pos = max entry position so gaps past an attached pending card are still covered) and resolves each player's entry at that position (placeholders are empty positions — skipped).
- **Blocking/facing is by per-player position number**: a defend card blocks the opponent's card at the **same position** (not the same column). `grappling_hook` and `copy_effect` also face the **same position number**.
- **Rooted cards on the trip chain** are **normal cards** (basic advancing only, no condition/effect, but **blockable**) — see *Rooted* in [cond_effects_agent.md](cond_effects_agent.md).
- **Legacy shared-stopover rule**: the superseded **shared** stopover-skip rule (`ge._next_free_stopover_v14` / `ge._occupied_stopover_cols`) — both players shared the same 5 columns, skipping furniture. Kept for legacy replay.
- **Pre-14 (legacy)**: the plain convention (1st→`stopover_4`, 2nd→`stopover_3`, … — no skip, shared columns).
- The engine **overwrites** the client's `to` with the computed per-player stopover (`player_play`), so the client's stopover choice is a **convention for display** only — the engine is the source of truth. *(Old games created before the per-player rule may have furniture on top of a play, or shared-column occupancy; the frontend keeps the layered fallback for those.)*

### Hidden information

- A player never sees the **contents** of the opponent's hand, mana zone, or deck — only the **counts** (`hand_count`, `mana_count`, `deck_count`).
- The engine itself reads the **full** state (it can evaluate `*_oppo` conditions against real values); masking is applied at the API/UI layer via `current_game_json`.

## Conventions

- Python ≥ 3.12, managed with **uv** (`pyproject.toml` / `uv.lock`). No pytest: `tests/*` are **manual smoke tests**.
- **Always run from the project root** (imports like `import engine.game_engine as ge`).
- Import the engine as `import engine.game_engine as ge`.
- New code, comments, and LLM-facing instructions are written in **English** (the existing UI and README are still in French — legacy, to be migrated).
- Ports: `API.py` → 8000, `game_ui/app.py` → 8001, `games/analysis` → 8017.

## Reference files
The following sections are into their own docs in the project root — read them IF NEEDED before the matching work:

- [cond_effects_agent.md](cond_effects_agent.md) — Conditions & effects: the `condition` table, the `effect` table, and the per-card detail sections (Cataclysm, Drop on board, Pending condition, Avalanche, Grappling hook, Effect canceled, Copy effect, Pet trap, Rooted, Wrecking ball, Swap cards, Discard selection)
- [sup_fact_agent.md](sup_fact_agent.md) — Support factions: deck composition + Engineers (drops + refinery), Doctors (pending zone + laboratory), Mages (Celestial_reversal, nobodymoves, thermic_flux, Apocalypticritual, black_hole)
- [game_log_agent.md](game_log_agent.md) — Game log (`GameState.log`) structure + log-note legend + frontend rendering
- [app_py_agent.md](app_py_agent.md) — `game_ui/app.py` living-doc rules (read `game_ui/APP_OVERVIEW.md` first)
- [run_agent.md](run_agent.md) — Run commands
- [test_agent.md](test_agent.md) — Test suite (manual smoke tests in `tests/*`)
- [pitfalls_agent.md](pitfalls_agent.md) — Known pitfalls (art paths, the `engine_version` field, replay invariants)
- [duplicate_cards_agent.md](duplicate_cards_agent.md) — Duplicate cards in hand (root causes found 2026-09-02 / 2026-09-04, both fixed)
- [open_issue_agent.md](open_issue_agent.md) — **Open issue**: hand-accounting discrepancy (UNRESOLVED — do not "fix" by guesswork)
