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

## Invariants — never break

1. **No in-memory state**: every action re-reads the state from `games.db` (source of truth), applies the rule, writes the state back.
2. **Hidden information**: never return the opponent's hand/mana/deck to a player — always via `current_game_json` (masking). `GET /game/{id}` is the only full-state endpoint (debug/analysis).
3. **Player message**: `{"cards": [...], "to": "stopover_k|mana|...", "mode": ""|"move"|"defend"|"pass", "pendings": []}` — validated by `message_check`.
4. `handle_websocket_message` is the **single entry point** of the engine; go through it.
5. When changing a rule in `engine/`, also check the AI mirror (`player_ai/playerai.py` re-implements `_condition_met`) and the replay (`games/analysis`).

## Game rules

> **This section is the canonical game spec** (designer-confirmed). `engine/game_engine.py` is the current implementation; the `condition`/`effect` columns of `cards/cardpool.parquet` define what exists in the pool.
> (Historical note: earlier revisions of this spec carried **⚠ engine** flags where the engine lagged behind. Both of those gaps — the **+1 faction biome bonus** and the **conditional block-card effect** — are now implemented and no longer flagged.)

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
- **Instant effects** (move mode only): some effects fire **at play time**, before the trip chain resolves, and therefore **cannot be blocked, canceled or conditioned** — the card's `player_play` succeeds → the effect has already happened. Currently: `pet_trap` (see *Pet trap*). The effect is annotated on the player's message (`drop_placed_on`) so the turn log can show it.
- `mode: "move"` — play **exactly 1 card** onto a **stopover** slot (`to: "stopover_4"` ... `"stopover_0"`). Normal play: it will advance, trigger its effect, etc.
- `mode: "defend"` — play **1 to 5 cards** sideways (90°) onto a stopover slot. They do **not** advance and do **not** trigger their effect (see *Defense & blocking* below); their `shield` value contributes to blocking.
- On success: cards leave the hand, the action is appended to the player's **`action_chain`** (resolution order = play order), `mana_spend` increases.

**Branch — turn end:** the play phase ends **only when both players have passed**. As soon as that happens, the engine immediately runs **Phase 3 (resolution)**.

#### Phase 3 — Resolution (the "trip chain", `process_trip_chain`)

1. Played cards of both players are resolved **alternately, index by index**: action 1 of P1, action 1 of P2, action 2 of P1, action 2 of P2, … until both chains are exhausted (shorter chain simply ends).
2. **If the game ends mid-chain** (a win is declared), the remaining actions of the chain are **not resolved** — the game stops immediately (the engine checks the game state **before every action**, including the grappling/copy steps after each index).
3. **Card conservation on a mid-chain win**: played cards leave the hand at play time and only reach the discard when their action resolves (`process_card` does `discard.extend`) — so on a mid-chain win the **unprocessed** actions' cards would be lost from every zone. `process_trip_chain` therefore flushes them to their owners' **discard piles** at the win point (`flush_unresolved`, called in every game-over branch). The final state always conserves every card of the deck.

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
   - **Step B — Condition**: evaluate the card's `condition` (see *Conditions* below).
     - **`cataclysm` condition** → the **cataclysm trigger fires first** (see *Cataclysm* below), then the condition is treated as met.
     - **Condition met** → apply the card's `effect` (see *Effects*), then apply **basic advancing** — **except** when the effect itself handles movement (`advancing`, `backward`, `jump`), which already moved the player.
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
          "notes": ["faction biome bonus +1 (token on home biome)"]  // extra events that happened
        }
      ]
    }
  ]
}
```

- **Created**: both players' entries are created *before* their action resolves (so cross-events — a block fired on the opponent's card, a grappling copy — can attach to either line) and finalized after (positions reflect everything, including grappling copies). A turn in which **nothing** was played (both passed) is **dropped** from the log. If the **game ends mid-chain**, the partial turn is still kept.
- **`negatives`** (what prevented this card from doing its thing): `blocked — shields X ≥ cost Y`, `effect canceled by <name>`, …
- **`notes`** (extra events, any player's line): `faction biome bonus +1 …`, `🌋 cataclysm — <biome> strikes`, `🏔 avalanche — MO strikes`, `grappling hook — copied +N from the facing card`, `unstoppable — ignored the opponent block`, `block broken — shields X < cost Y`, `block card <name> — effect fired: <effect>`, `blocked <name>'s card on <stopover>`, `🪤 pet_trap — drop token placed on cell N (fires when any token arrives)`, `🪤 drop on cell N — knocked back −K`, `🏆 <name> reached cell 24 — win!`
- **`order`**: 1/2 = first/second player of **that turn** (turn order flips every turn).
- **Old games**: games created before this feature have `log: []` (the field defaults to empty) — the UI simply shows no log. No `engine_version` bump needed: the log is an additive read-only recap, it does not change any rule.
- **Replay/AI**: untouched — the log is written only by the engine; `replay.py` and `PlayerAI` ignore it. All engine functions that gained a `log_entry`/`oppo_entry` parameter default to `None`, so existing callers are unchanged.
- **Frontend** (`game_ui/static/`): collapsible panel right of the stopover board (`#log-panel` in `index.html`, styles in `style.css`, renderer in `app.js`). New turns are appended incrementally (existing collapse state is preserved across polling re-renders); on first render / rejoin, all turns appear collapsed except the latest. Card thumbs hover-zoom and open the card modal on click.

### Conditions (card `condition` column)

Evaluated by `is_condition_met`. A met condition → the card's effect fires. A `no_condition` card always fires.

| Condition (exact pool values) | Meaning | #cards |
|---|---|---|
| `no_condition` | Always met. | 370 |
| `block` | Marker for dedicated defend/block cards (see *Defense & blocking*). Treated as met. | 48 |
| `cataclysm` | **Trigger condition** (see *Cataclysm* below): striking a biome first, then treated as met (the card's effect fires). | 379 |
| `biome_Dwa` / `biome_Dem` / `biome_Twi` / `biome_Mia` / `biome_Orc` / `biome_Mum` | Player must be standing on a cell of the **two biomes** associated with that faction. E.g. `biome_Dwa` (Dwarves) → cell is `MO` or `OC`. Full map below. | 48–109 each |
| `dist_ahead_sup_1` / `dist_ahead_sup_3` | Player is **strictly more than N cells ahead** of the opponent (I lead). | ~378 each |
| `dist_behind_sup_1` / `dist_behind_sup_3` | Opponent is **strictly more than N cells ahead** (I'm behind). | ~379 each |
| `mana_inf_6` | Player has **fewer than 6** cards in their own mana zone. | 329 |
| `mana_sup_5` | Player has **more than 5** cards in their own mana zone. | 329 |
| `mana_inf_6_oppo` | Opponent has **fewer than 6** cards in their mana zone. | 157 |
| `mana_sup_5_oppo` | Opponent has **more than 5** cards in their mana zone. | 157 |
| `cards_in_hand_inf_4` | Player has **fewer than 4** cards in hand. | 329 |
| `cards_in_hand_sup_3` | Player has **more than 3** cards in hand. | 329 |
| `cards_in_hand_inf_4_oppo` | Opponent has **fewer than 4** cards in hand. | 157 |
| `cards_in_hand_sup_3_oppo` | Opponent has **more than 3** cards in hand. | 157 |
| `temp_inf_6` / `temp_inf_11` / `temp_sup_9` / `temp_sup_15` | Planet temperature is **< N** / **> N** (temperature = 2d20, closest to 10, rolled at start). | 217–272 each |
| `day` | It is currently **day**. | 281 |
| `night` | It is currently **night**. | 205 |

**Biome ↔ faction map** (a `biome_<Faction>` condition is met if the player's current cell belongs to one of these two biomes):
- Dwarves (`Dwa`) → `MO`, `OC`
- Demons (`Dem`) → `OC`, `DE`
- Twigs (`Twi`) → `JU`, `OC`
- Miaous (`Mia`) → `DE`, `JU`
- Orcs (`Orc`) → `MO`, `JU`
- Mummies (`Mum`) → `DE`, `MO`

> **Not implemented yet** (present in the card pool but not handled):
> - `drop_on_board` (377), `face_point_left` (379), `face_point_right` (377), `pending` (377).
> - **Canonical default: an unimplemented condition is treated as MET** (the engine's `is_condition_met` catch-all) → the card resolves with its effect + full advancing. This is the intended default, not a bug — these entries disappear as each condition is implemented (`cataclysm` was the first). The AI (`PlayerAI._condition_met`) uses the **same** default; it only deviates for conditions it cannot evaluate from its state (biome/board → treated as not met).
> - `pending` is likely tied to the unused `pendings` message field / `pending_zone` destination (see the ToDo in `player_play`) — design the mechanic around that before coding.
>
> **`face_point_left` / `face_point_right` are excluded from NEW games.** `ge.get_cardpool()` (the *playable* pool) filters out these two conditions — every deck entry point goes through it: the `/cardpool` endpoint, the frontend starter deck, the deck validation in `API.py`/`game_ui/app.py` (a deck containing one is rejected with 400), and the robot deck (`ai_driver.random_ai_deck`). The raw pool `ge.CARDS_DB` stays **complete**, so OLD games that contain those cards still resolve and replay fine (the replay reads `CARDS_DB`, not `get_cardpool`). To lift the filter later, set `ge.EXCLUDED_CONDITIONS = ()`.

### Cataclysm (pile trigger, condition `cataclysm`)

The board carries a **cataclysm pile of 4 cards, one per biome** (`OC`, `MO`, `DE`, `JU`), **shuffled at board initialization** (state: `GameState.cataclysm_pile`, top = first element).

When a card with the `cataclysm` condition is **resolved** (move mode, not blocked) — in `_resolve_card`, exactly once per card — the trigger fires:

1. **Look at the top card of the pile** → it names a biome B.
2. **ALL player tokens** (both players, not only the opponent) standing on a cell of biome B are **knocked back to the FIRST cell of that biome** (the start of the 6-cell segment). Tokens not on B are untouched.
3. The drawn cataclysm card is put at the **BOTTOM of the pile** — the pile only rotates, it is never exhausted.

Then the condition is treated as **met** → the card's `effect` fires and its advancing is applied (from the new position, if the player was just knocked back).

- The strike lives in `trigger_cataclysm()` (side effect) + `_resolve_card()` (call site); `is_condition_met('cataclysm')` itself is side-effect free (pure `True`) so evaluation calls (AI, replay, unstoppable check) never double-fire.
- **Defend-mode** cataclysm cards and **blocked** cataclysm cards do **not** trigger (defend never evaluates the condition; a block cancels the whole card).
- **Rule version**: introduced with `engine_version = 3`. Games with `engine_version < 3` have no pile → the trigger is a no-op and the condition is simply treated as met (the pre-cataclysm behavior). The replay reconstructs the initial pile by rewinding the stored final pile by the number of **actual triggers** of the game (the pile is a pure rotation, so only the starting card needs rewinding). A trigger fires only when a cataclysm card actually resolves — a **blocked** cataclysm card (or one flushed un-resolved by a mid-chain win) does **not** trigger. The replay therefore counts the engine's `⚡ cataclysm — <biome> strikes` log notes (exactly one per real trigger) rather than the number of cataclysm cards played; it falls back to counting cataclysm move-cards only for games stored before the log feature existed.

### Effects (card `effect` column)

`effect_number` is the quantitative parameter (e.g. how many cards, how many cells). Movement effects handle their own advancing; the others leave advancing to the card's basic value (or reduced value if the condition failed).

| Effect | Meaning |
|---|---|
| `advancing` | Move **forward** by `basic advancing + effect_number` (extra cells on top of base). |
| `backward` | Move forward by basic advancing, then **recoil** by `effect_number` (negative, e.g. −1/−2). No recoil if the forward move already won the game. |
| `advancing_oppo` | Opponent moves forward by `effect_number`. |
| `backward_oppo` | Opponent recoils by `effect_number` (negative), clamped at cell 0. |
| `draw` | Player draws `effect_number` cards from their deck (reshuffles discard if needed). |
| `draw_oppo` | Opponent draws `effect_number` cards (a fatigue effect — they must manage more cards). |
| `discard` | Player discards `effect_number` cards (auto: last cards of their hand). |
| `discard_oppo` | Opponent discards 1 card (auto: last card of their hand). |
| `ramp` | Move `effect_number` cards from player's deck into their mana zone. |
| `ramp_oppo` | Move 1 card from opponent's deck into opponent's mana zone. |
| `taxation` | Move `effect_number` cards from player's mana zone into their discard (last mana cards first). |
| `taxation_oppo` | Move 1 card from opponent's mana zone into their discard. |
| `jump` | **Teleport** forward by the card's basic advancing, skipping intermediate cells (only the landing cell is checked for trap/drop). | 438 |
| `unstoppable` | Passive: the card **ignores blocking** (see *Defense & blocking*) **when its condition is met**. No effect of its own. | 201 |
| `avalanche` | **Board effect**: ALL player tokens (both players, the playing player included) standing on the **Mountain (MO)** biome are knocked back to the **FIRST cell** of that biome. The playing player then applies its normal basic advancing from its (possibly new) position. See *Avalanche*. | 172 |
| `grappling_hook` | The card advances by its basic value, then **copies the net advancing of the facing card** (the opponent's card at the same trip-chain index). The copy is applied **without** the faction biome bonus. See *Grappling hook*. | 316 |
| `effect_canceled` | **Reactive marker**: it does nothing to itself (the card only advances by its basic value). Instead, **before the opponent's facing card (on the SAME stopover) applies its effect**, the engine checks for a **valid** `effect_canceled` card here — if present, the facing card's **effect is canceled** (it does not fire; the card still advances by its basic value). See *Effect canceled*. | 172 |
| `copy_effect` | **Post-resolution copy**: the card advances by its basic value, then — once the facing card (same trip-chain index) has resolved too — it **applies the facing card's effect with itself as the actor**. Only when the facing card's effect actually fired. No recursion. See *Copy effect*. | 172 |
| `pet_trap` | **Board effect (INSTANT)**: at **play time** (move mode), leaves a **drop token** on the player's current cell. Any token that later **arrives** on that cell is knocked back by −1 per token; tokens are consumed on trigger. See *Pet trap*. | 100 |

> **Not implemented yet** (present in the card pool but the engine returns the state unchanged — the card only still advances):
>
> | Effect | #cards | Note |
> |---|---|---|
> | `wrecking_ball` | 460 | largest effect in the pool; needs a design (e.g. extra damage through a block?) |
> | `rooted` | 172 | |
> | `swap_cards` | 172 | |
>
> **All 3 have `effect_number = 0` in the pool** — the meaning is intrinsic to the effect name, not to a number. `wrecking_ball` and `swap_cards` are not referenced anywhere else in the codebase, so each requires a **design decision first**, then an engine implementation + AI mirror + replay note. (`copy_effect` **is** implemented — see *Copy effect*; `pet_trap` — see *Pet trap*.)

### Avalanche (effect `avalanche`)

The **avalanche** effect is the fixed-MO variant of the cataclysm knockback, fired from `apply_effect` (so it only triggers when the card's condition is met — a blocked or not-met avalanche card does nothing):

1. **ALL player tokens** (both players, the playing player included) standing on a cell of the **Mountain (MO)** biome are **knocked back to the FIRST cell of that biome** (the start of the 6-cell segment). Tokens not on MO are untouched.
2. The playing player then applies its **normal basic advancing** from its (possibly new) position — `avalanche` is **not** a movement-handling effect, so it is not in the `('advancing', 'backward', 'jump')` set.
3. `effect_number` is 0 in the pool and unused. No pile, no rotation — the MO is always the struck biome.

- Implementation: `apply_effect('avalanche', …)` → `_knockback_biome('MO', …)` — the same shared knockback helper as the cataclysm trigger.
- **Rule version**: introduced with `engine_version = 4`. Games with `engine_version < 4` keep the old behavior — `avalanche` is a **no-op** (the card only advances). The gate lives in the engine (`engine_version` check in `apply_effect`), so the replay pins old games automatically (it preserves the stored `engine_version` in `_build_initial_state`).
- **AI**: no mirror needed — `avalanche` is an effect, and the AI does not simulate effects (`PlayerAI` only evaluates conditions for scoring).

### Grappling hook (effect `grappling_hook`)

The **grappling_hook** effect makes a card **copy the advancement of the card it faces** — the opponent's card at the **same index of the trip chain** (the card resolved at the same turn slot). Fired from `apply_effect` (so it only triggers when the card's condition is met — a blocked or not-met grappling card does **not** copy):

1. The grappling card first applies its **own basic advancing** (as usual, including the faction biome bonus if on a home biome) — `grappling_hook` is **not** a movement-handling effect, so it is not in the `('advancing', 'backward', 'jump')` set.
2. After **both** cards at that index have resolved, the grappling player moves **forward again** by the **facing card's net advancing** (its actual position delta for that action, `max(0, …)` — a recoil/negative result copies as 0).
3. The copy is applied **without the faction biome bonus** (it is a pure copy of the facing card's movement, not an independent forward move). `effect_number` is 0 in the pool and unused.

**Timing** (handled in `process_trip_chain`, after both cards at an index resolve):
- **Grappling is the 1st player's card** → the 2nd player's card resolves first (its advancing is saved), then the 1st player copies it.
- **Grappling is the 2nd player's card** → the 1st player's card resolves first (saved), then the 2nd player copies it.
- **No facing card** (opponent has no card at that index) → nothing to copy, base advancing only.
- **No recursion**: if both players grapple at the same index, each copies the other's **base** advancing (before its own copy), so it does not chain.
- **A mid-chain win stops the chain before any copy**: the engine checks the game state after each action and after each copy application — if the facing card's action (or a copy) won the game, the remaining copies and all further actions are skipped, and the unprocessed played cards are flushed to their owners' discard (see *Phase 3*, item 3). A win **during** a grappling copy still counts (the copy is what won).

- Implementation: `apply_effect('grappling_hook', …)` is a **no-op marker**; the copy is applied in `process_trip_chain` via `grappling_copy_amount()` + `apply_grappling_copy()`. `_resolve_card` returns whether the grappling card **activated** (effect + condition met + `engine_version ≥ 5`); `process_card` propagates that flag to the trip chain.
- **Rule version**: introduced with `engine_version = 5`. Games with `engine_version < 5` keep the old behavior — `grappling_hook` is a **no-op** (the card only advances). The gate lives in the engine (`engine_version` check), so the replay pins old games automatically.
- **AI**: no mirror needed — `grappling_hook` is an effect, and the AI does not simulate effects (`PlayerAI` only evaluates conditions for scoring).

### Effect canceled (effect `effect_canceled`)

The **effect_canceled** card is a **reactive marker**: it does nothing to itself (when it resolves it simply advances by its basic value — `apply_effect('effect_canceled', …)` is a no-op). Its real action is to **cancel the effect of the card it faces** — the opponent's card played on the **same stopover**:

- **When** a card is resolved, **before its effect is applied** (`_resolve_card`), the engine checks whether the **opponent has a valid `effect_canceled` card on the same stopover** (move mode, and its **condition met**). If so, the facing card's **effect is canceled**: it does **not** fire (including any movement it would have caused), but the card **still advances by its basic value**.
- **Valid** = the `effect_canceled` card's own **condition is met** (evaluated against the player who played it). A not-met `effect_canceled` card is **not** a valid canceler, so the facing card's effect fires normally.
- **Same stopover only**: the canceler and the facing card must be on the same stopover column (e.g. both `stopover_4`). A `effect_canceled` on a different stopover does not cancel.
- **Move mode only**: a `effect_canceled` played in **defend** mode never fires its effect (like all defend cards), so it does not cancel.
- **Mutual case**: if both players put an `effect_canceled` on the same stopover, each card's (no-op) effect is canceled by the other — both simply advance by their basic value. It is a symmetric neutralization.
- `effect_number` is 0 in the pool and unused.

- Implementation: `_oppo_has_valid_effect_canceled(oppo, stopover, game)` (the check) + the cancel branch in `_resolve_card` (which receives the stopover from `process_card`). `apply_effect('effect_canceled', …)` stays a no-op (the cancel is applied to the **facing** card, not to the canceler).
- **Rule version**: introduced with `engine_version = 6`. Games with `engine_version < 6` keep the old behavior — `effect_canceled` is a **no-op** (the card only advances, nothing is ever canceled). The gate lives in the engine (`engine_version` check in `_resolve_card`), so the replay pins old games automatically.
- **AI**: no mirror needed — `effect_canceled` is an effect, and the AI does not simulate effects (`PlayerAI` only evaluates conditions for scoring).

### Copy effect (effect `copy_effect`)

The **copy_effect** card copies the **effect of the card it faces** — the opponent's card at the **same index of the trip chain** (the card resolved at the same turn slot / stopover). The copier applies the copied effect **with itself as the actor** (so `draw_oppo`, `taxation_oppo`, etc. target the copier's opponent):

1. The copy_effect card first resolves normally: condition check → (its own effect is a no-op) → **basic advancing** as usual (including the faction biome bonus if on a home biome).
2. **After both cards at that index have resolved**, if the facing card's effect **actually fired** (condition met, not blocked, not `effect_canceled`), the copier applies the facing card's `effect` with the facing card's `effect_number` and basic advancing (`apply_effect(facing_effect, …, copier, game)`). `effect_number` of the copy_effect card itself is 0 in the pool and unused.
3. **No recursion**: if the facing card is itself a `copy_effect`, there is nothing to copy — the copy does not fire (a facing copy_effect only ever advances by its basic value).
4. **Only when the facing effect fired**: facing card blocked → nothing to copy; facing condition not met → nothing to copy; facing effect canceled by a valid `effect_canceled` → nothing to copy. And symmetrically, a copy_effect whose **own** condition was not met (or that was blocked) never copies.
5. Copied effects that handle movement (`advancing`, `jump`, `backward`, …) move the **copier** (the actor); zone effects (`draw`, `ramp`, `discard`, `taxation`, …) act on the copier's zones (or the copier's opponent for `*_oppo`).

**Timing** (handled in `process_trip_chain`, after both cards at an index resolve — same slot as the grappling_hook copies):
- The copy fires once per index, per copier (both copiers can fire at the same index — each copies the other's *original* effect; since a facing copy_effect has nothing to copy, two mutual copy_effects copy nothing).
- A mid-chain win stops the chain: unprocessed indexes never copy.

- Implementation: `apply_copy_effect(game, copier, copier_action, facing_player, facing_action, log_entry)` (guards + call to `apply_effect`); `_resolve_card` now returns a third flag `effect_activated` (condition met and not canceled) and `process_card` propagates it as a 3-tuple; `process_trip_chain` captures both players' flags per index and calls `apply_copy_effect` for each valid copier. The log entry of the copier gets the note `copy_effect — copied "<effect>" from the facing card (<name>)`.
- **Rule version**: introduced with `engine_version = 7`. Games with `engine_version < 7` keep the old behavior — `copy_effect` is a **no-op** (the card only advances). The gate lives in the engine (`engine_version` check in `apply_copy_effect`), so the replay pins old games automatically; the replay mirrors the copy in `_replay_trip_chain` (pinning the cards a copied zone-effect will move, then calling `ge.apply_copy_effect`).
- **AI**: no mirror needed — `copy_effect` is an effect, and the AI does not simulate effects (`PlayerAI` only evaluates conditions for scoring).

### Pet trap (effect `pet_trap`)

The **pet_trap** effect is an **INSTANT board effect**: it fires **at play time** (inside `player_play`, right after a successful move-mode play) — **before** the trip chain resolves. It therefore **cannot be blocked, effect-canceled or conditioned** (a blocked pet_trap card still leaves its trap; a pet_trap played in **defend** mode does nothing). The card itself then resolves in the trip chain as a **no-op** (`apply_effect('pet_trap', …)` does nothing; the card only advances by its basic value — `effect_number` is 0 in the pool and unused).

**Placement** (the instant part):
- Leaves **one drop token** on the **cell where the playing player currently stands** (`GameState.drop_tokens: Dict[cell, count]`, stacks if several traps land on the same cell). Public board info — visible to both players, safe in both endpoints.
- **No immediate trigger**: if a player token is already on the cell (the placing player, or the opponent), nothing happens — the trap only fires on **arrival**.

**Trigger** (when any player token **arrives** on the cell, either player):
- **Stepping**: `process_advancing` checks the cell after **every step** (the `drop` check — same slot as `trap`).
- **Jump landing**: `_jump` checks the **landing cell** only (jumping OVER a trap cell does not trigger it).
- **Not on**: cataclysm/avalanche knockback (direct teleport, no per-step checks), initial placement, or any position change that does not "step in".
- **Effect**: ALL tokens on the cell are **consumed**, and the arriving token is **knocked back by −1 per token** (direct position change, clamped at cell 0 — it does **not** step cell by cell, so it cannot re-trigger other traps, and a knockback landing on another trap cell does not chain). Knockback cannot win the game (it is backward).
- Log: placement note on the card's line (`🪤 pet_trap — drop token placed on cell N (fires when any token arrives)`), trigger note on the arriving player's line (`🪤 drop on cell N — knocked back −K`).

**Rule version**: introduced with `engine_version = 8`. Games with `engine_version < 8` keep the old behavior — `pet_trap` is a **no-op** and no drop token is ever placed (the gate is in `player_play`, the trigger branch checks `drop_tokens`, which old games never fill). The replay pins old games automatically (it preserves the stored `engine_version`); for v8 games it mirrors the instant placement in `_replay_trip_chain` — every pet_trap move-card's token is placed on its owner's **pre-chain** position (the instant effect fired at play time, before any card moved) — and the final **drop tokens are compared** to the stored ones (a warning on divergence, like the zone checks).

**AI**: no mirror needed — the AI does not simulate effects (`PlayerAI` only evaluates conditions for scoring); a pet_trap card is simply worth its base advancing in its current valuation.

**Frontend**: the drop tokens render on the Earth (`.drop-token` at the cell's `POS24` point, `/assets/effect_pettrap.png`); `effectLabel` shows `trap — drop token here`. **Multi-token display**: each trap on a cell is drawn as its own marker in a slightly offset up-right fan (`MAX_FAN = 6` visible per cell, `FAN_STEP_PX = 5` deliberate overlap, z-index increases along the fan) so a stack of N traps reads as N tokens; the top token carries the exact-count badge when N > 1 (with the 6-cap, the badge is the source of truth for N > 6). Preview page: `tests/_drop_tokens_preview.html` (copy it into `game_ui/static/` and screenshot via headless Chrome to eyeball the fan).

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
| `condition` | 31 values (see *Conditions*) |
| `effect` | 22 values (see *Effects*) |
| `effect_number` | −2 … 3 — quantitative parameter of the effect |
| `rare` | True / False |
| `card_id`, `name`, `condeff_value`, `prompt`, `negative_prompt` | identity / text fields |

### Stopover & ordering convention (frontend + AI)

- The **k-th play of a turn — move OR defend** — is placed on `stopover_{5-k}`: 1st→`stopover_4`, 2nd→`stopover_3`, 3rd→`stopover_2`, 4th→`stopover_1`, 5th→`stopover_0` (capped at 5). **Both modes consume a slot** (the frontend's `playedCount`/`nextSlotCol` and `ai_driver.py` count `move`+`defend` alike), so two cards never share a stopover and the robot's k-th defend sits on the same column as the opponent's k-th card (which it blocks).
- This ordering matters for **blocking**: a defend card blocks the opponent card on the **same** stopover column, so the position is meaningful.
- The engine does **not** enforce which stopover a card targets beyond the `stopover_` prefix — the column choice is a **client convention** (frontend and `ai_driver.py`). *(Old games created before the ai_driver fix may have a defend + a move sharing one stopover; the frontend nudges the extra card up so both stay visible.)*

### Hidden information

- A player never sees the **contents** of the opponent's hand, mana zone, or deck — only the **counts** (`hand_count`, `mana_count`, `deck_count`).
- The engine itself reads the **full** state (it can evaluate `*_oppo` conditions against real values); masking is applied at the API/UI layer via `current_game_json`.

## Conventions

- Python ≥ 3.12, managed with **uv** (`pyproject.toml` / `uv.lock`). No pytest: `tests/*` are **manual smoke tests**.
- **Always run from the project root** (imports like `import engine.game_engine as ge`).
- Import the engine as `import engine.game_engine as ge`.
- New code, comments, and LLM-facing instructions are written in **English** (the existing UI and README are still in French — legacy, to be migrated).
- Ports: `API.py` → 8000, `game_ui/app.py` → 8001, `games/analysis` → 8017.

## Run

```bash
uv run python API.py                          # pure game server, :8000
uv run python game_ui/app.py                  # full web app, :8001
uv run uvicorn games.analysis.app:app --port 8017   # replay/analysis
```

## Test

```bash
uv run python tests/_diag.py       # engine unit tests (NOTE: writes to games.db)
uv run python tests/_copy_effect.py # copy_effect rule, engine_version 7 (also writes to games.db)
uv run python tests/_pet_trap.py    # pet_trap rule, engine_version 8 (also writes to games.db; 2 games are persisted for the replay self-test)
# the server on :8001 MUST be running for these two:
uv run python tests/_ui_e2e.py     # 2 fake players via REST+WS
uv run python tests/_ai_e2e.py     # human vs Robot
uv run python -m games.analysis.replay   # replay auto-test
```

## Known pitfalls

- `tests/_diag.py` **writes to `games.db`** — do not run it against a precious database.
- Art/assets folders: **hard-coded external paths** (`GenAI_TCG/lib/artdesign/…`) with a placeholder fallback — do not "fix" the paths without verifying the fallback still works.
- A player's deck is just a list of ids from the Parquet pool — do not invent fields outside the Parquet columns.
- Several `condition`/`effect` values exist in the pool but are **not implemented** in the engine (see the "Not implemented yet" notes) — the 4 remaining conditions are currently treated as **met** and the 3 remaining effects as **no-op**. Do not assume they work. (`cataclysm` **is** implemented — see *Cataclysm*; `avalanche` — see *Avalanche*; `grappling_hook` — see *Grappling hook*; `effect_canceled` — see *Effect canceled*; `copy_effect` — see *Copy effect*; `pet_trap` — see *Pet trap*.)
- **Rule versioning**: `GameState.engine_version` marks the rules a game was played under (`8` = current rules: + pet_trap instant effect/drop tokens; `7` = + copy_effect effect; `6` = + effect_canceled effect; `5` = + grappling_hook effect; `4` = + avalanche effect; `3` = + cataclysm condition/pile; `2` = faction biome bonus + conditional block-card effect). The engine sets it at game creation; the replay/analysis tooling reads it to pin **old** games (`< 2`: no biome bonus; `< 3`: no cataclysm pile → trigger is a no-op; `< 4`: avalanche is a no-op; `< 5`: grappling_hook is a no-op; `< 6`: effect_canceled is a no-op; `< 7`: copy_effect is a no-op; `< 8`: pet_trap is a no-op, no drop tokens) to the rules they were actually played under — otherwise the +1 biome bonus would drift every forward move on a home biome and old games could never verify. When you add a **new** engine rule, bump `engine_version` and teach the replay how to pin older games.
- **Replay self-test skips in-progress games** whose state is `turn N - waiting for … to play` (an unresolved trip chain can never verify against a full-history replay). The replay also keeps `game.day_night` in sync every turn — `day`/`night` conditions must evaluate against the turn parity, not a frozen "day".
- **Replay mid-chain-win semantics must mirror the engine exactly**: the engine checks the game state **before every action and after every grappling/copy step** and returns immediately on a win. The replay (`_replay_trip_chain`) enforces the same: no action, grappling copy, or copy_effect fires after a win, and the unprocessed played cards are flushed to their owners' discard (mirroring the engine's `flush_unresolved`). A win *during* a grappling copy still counts (the copy is what won). If you add a new post-action step to `process_trip_chain`, mirror its game-over ordering in the replay.
- **Replay card conservation for old games**: a few OLD games (created before the `flush_unresolved` fix) are missing their unprocessed played cards from all final zones — `analyze_game` reconstructs them in the discard before replaying (one-off historical artifact; the current engine conserves every card).
- The engine does **not** verify that the message sender is the player whose action the state expects (branching is on `state` only) — the WS layer trusts the `player_name` from the URL. Treat out-of-turn senders as a known gap, not a rule.

## Open issue — hand-accounting discrepancy (UNRESOLVED, revisit later)

> **Status: OPEN.** A replay-fidelity failure was found that we could not root-cause. It is **parked on purpose**: the plan is to let it accumulate over several more games, then re-investigate with a bigger sample to find the pattern. Do **not** "fix" it by guesswork.

**The symptom.** Game **`26_08_31_21_38_24_w4LNX`** (created **2026‑08‑31 21:38:24**, i.e. a *fresh* v9 game from the `_ai_e2e` run — **not** an old artifact) fails the replay self-test. The first divergence is at **turn 3**:

| | Graceful Leap Whispering Willow (`Mia11_b8b17e`, `cards_in_hand_sup_3_oppo`, `adv −1`, `mana 1`), played by the **Robot** |
|---|---|
| **Engine** (logged `condition_met`) | **NOT met** → the opponent (Alice) had **≤ 3** cards in hand → reduced advancing `mana − 1 = 0` → Robot stays at cell 4 |
| **Replay** (reconstructed hand) | **met** → Alice had **~8** in hand → full advancing `−1` → Robot 4→3 |

That 1-cell difference cascades into a final-position mismatch (Robot 22 vs 23) and a pet-trap token-cell mismatch, so the game is marked **not verified**.

**Why it's suspicious.** By every check, the **replay looks right (~8)** and the **engine's logged `≤3` is the anomaly** — yet the engine is what actually ran the game. A correct simulation of the rules (and controlled runs of the real engine) put Alice at **7–8** by turn 3.

**Already ruled out** (each verified directly against the code / DB):
- Not a replay **double-draw** — `_pick_draw` only reorders the deck; `_draw_for_next_turn` draws, and it mirrors the engine's draw exactly.
- Not a **wrong-player** lookup — `_get_oppo` is correct; `get_current_game` is a fresh DB read (no caching).
- Not a **card loss** — every card Alice and the Robot played is present in a final zone.
- Not a **hand-reducing effect** — the only `discard_oppo` cards in turns 1–3 (Aethel = defend, Graceful Leap = its own condition not met) never fired; nothing else reduces Alice's hand.
- Not a **missing draw** — the engine *does* draw 3/turn (confirmed in controlled games); the game ran 7 turns so turns 1–6 draws all happened.

**What we do NOT yet know.** The exact mechanism that made the engine's live Alice-hand drop to `≤3`. The engine's actual in-game state disagrees with a correct rule simulation, and we could not reproduce a path that produces it. It is a **pre-existing** issue in `cards_in_hand_*_oppo` hand-accounting that the `drop_on_board` change only *exposed* (the v9 AI picks a different card sequence, which surfaced it) — it is **not** caused by `drop_on_board`.

**Planned next step (later, after more games).** Re-run the replay self-test once several more games exist and collect **every** `cards_in_hand_*_oppo` divergence (not just `w4LNX`). A recurring pattern (same turn shape, same player, same preceding effect) will pin the mechanism. A useful diagnostic to build then: re-derive each player's hand **turn-by-turn** from `messages_history` + deck and flag where the engine's logged `condition_met` disagrees with the reconstructed hand — that turns this from a black box into a reproducible case.

**Do not** mark `w4LNX` (or its siblings) as a known-bad game to force a 100% pass — that hides the bug. Keep the self-test failing until the root cause is understood.
