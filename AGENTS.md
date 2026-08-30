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
- `mode: "move"` — play **exactly 1 card** onto a **stopover** slot (`to: "stopover_4"` ... `"stopover_0"`). Normal play: it will advance, trigger its effect, etc.
- `mode: "defend"` — play **1 to 5 cards** sideways (90°) onto a stopover slot. They do **not** advance and do **not** trigger their effect (see *Defense & blocking* below); their `shield` value contributes to blocking.
- On success: cards leave the hand, the action is appended to the player's **`action_chain`** (resolution order = play order), `mana_spend` increases.

**Branch — turn end:** the play phase ends **only when both players have passed**. As soon as that happens, the engine immediately runs **Phase 3 (resolution)**.

#### Phase 3 — Resolution (the "trip chain", `process_trip_chain`)

1. Played cards of both players are resolved **alternately, index by index**: action 1 of P1, action 1 of P2, action 2 of P1, action 2 of P2, … until both chains are exhausted (shorter chain simply ends).
2. **If the game ends mid-chain** (a win is declared), the remaining actions of the chain are **not resolved** — the game stops immediately.

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

### Cataclysm (pile trigger, condition `cataclysm`)

The board carries a **cataclysm pile of 4 cards, one per biome** (`OC`, `MO`, `DE`, `JU`), **shuffled at board initialization** (state: `GameState.cataclysm_pile`, top = first element).

When a card with the `cataclysm` condition is **resolved** (move mode, not blocked) — in `_resolve_card`, exactly once per card — the trigger fires:

1. **Look at the top card of the pile** → it names a biome B.
2. **ALL player tokens** (both players, not only the opponent) standing on a cell of biome B are **knocked back to the FIRST cell of that biome** (the start of the 6-cell segment). Tokens not on B are untouched.
3. The drawn cataclysm card is put at the **BOTTOM of the pile** — the pile only rotates, it is never exhausted.

Then the condition is treated as **met** → the card's `effect` fires and its advancing is applied (from the new position, if the player was just knocked back).

- The strike lives in `trigger_cataclysm()` (side effect) + `_resolve_card()` (call site); `is_condition_met('cataclysm')` itself is side-effect free (pure `True`) so evaluation calls (AI, replay, unstoppable check) never double-fire.
- **Defend-mode** cataclysm cards and **blocked** cataclysm cards do **not** trigger (defend never evaluates the condition; a block cancels the whole card).
- **Rule version**: introduced with `engine_version = 3`. Games with `engine_version < 3` have no pile → the trigger is a no-op and the condition is simply treated as met (the pre-cataclysm behavior). The replay reconstructs the initial pile by rewinding the stored final pile by the number of resolved cataclysm moves of the game (the pile is a pure rotation, so only the starting card needs rewinding).

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

> **Not implemented yet** (present in the card pool but the engine returns the state unchanged — the card only still advances):
>
> | Effect | #cards | Note |
> |---|---|---|
> | `wrecking_ball` | 460 | largest effect in the pool; needs a design (e.g. extra damage through a block?) |
> | `copy_effect` | 172 | copy another card's effect |
> | `rooted` | 172 | |
> | `swap_cards` | 172 | |
> | `pet_trap` | 100 | |
>
> **All 5 have `effect_number = 0` in the pool** — the meaning is intrinsic to the effect name, not to a number. None of them is referenced anywhere else in the codebase (no trap/drop/zone backing them), so each requires a **design decision first**, then an engine implementation + AI mirror + replay note.

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

### Win & end conditions

- **Win**: any player's forward movement reaches cell **24 or beyond** → that player is `winner`, state → `"game over"`. Resolved immediately, even mid-trip-chain.
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

- The **k-th move played in a turn** is placed on `stopover_{5-k}`: 1st→`stopover_4`, 2nd→`stopover_3`, 3rd→`stopover_2`, 4th→`stopover_1`, 5th→`stopover_0` (capped at 5).
- This ordering matters for **blocking**: a defend card blocks the opponent card on the **same** stopover column, so the position is meaningful.
- The engine does **not** enforce which stopover a card targets beyond the `stopover_` prefix — the column choice is a **client convention** (frontend and `ai_driver.py`).

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
# the server on :8001 MUST be running for these two:
uv run python tests/_ui_e2e.py     # 2 fake players via REST+WS
uv run python tests/_ai_e2e.py     # human vs Robot
uv run python -m games.analysis.replay   # replay auto-test
```

## Known pitfalls

- `tests/_diag.py` **writes to `games.db`** — do not run it against a precious database.
- Art/assets folders: **hard-coded external paths** (`GenAI_TCG/lib/artdesign/…`) with a placeholder fallback — do not "fix" the paths without verifying the fallback still works.
- A player's deck is just a list of ids from the Parquet pool — do not invent fields outside the Parquet columns.
- Several `condition`/`effect` values exist in the pool but are **not implemented** in the engine (see the "Not implemented yet" notes) — the 4 remaining conditions are currently treated as **met** and the 5 remaining effects as **no-op**. Do not assume they work. (`cataclysm` **is** implemented — see *Cataclysm*; `avalanche` — see *Avalanche*; `grappling_hook` — see *Grappling hook*; `effect_canceled` — see *Effect canceled*.)
- **Rule versioning**: `GameState.engine_version` marks the rules a game was played under (`6` = current rules: + effect_canceled effect; `5` = + grappling_hook effect; `4` = + avalanche effect; `3` = + cataclysm condition/pile; `2` = faction biome bonus + conditional block-card effect). The engine sets it at game creation; the replay/analysis tooling reads it to pin **old** games (`< 2`: no biome bonus; `< 3`: no cataclysm pile → trigger is a no-op; `< 4`: avalanche is a no-op; `< 5`: grappling_hook is a no-op; `< 6`: effect_canceled is a no-op) to the rules they were actually played under — otherwise the +1 biome bonus would drift every forward move on a home biome and old games could never verify. When you add a **new** engine rule, bump `engine_version` and teach the replay how to pin older games.
- The engine does **not** verify that the message sender is the player whose action the state expects (branching is on `state` only) — the WS layer trusts the `player_name` from the URL. Treat out-of-turn senders as a known gap, not a rule.
