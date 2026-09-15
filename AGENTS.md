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
- `cards/support_factions.parquet` — the 15 **support faction** cards (Engineers/Mages/Doctors), a second deck mixed into each player's deck (20 main + 10 support = 30). See *Support factions*. **Engineers are implemented** (`engine_version` 12: 4 drop cards + the refinery dwelling, and all support cards cost their `mana_cost`); **Mages and Doctors are not implemented yet** (0-cost no-ops in the engine).

## Frontend doc — `game_ui/static/app.js` ↔ `game_ui/static/app-js-overview.md`

`game_ui/static/app-js-overview.md` is the living architecture map of `app.js` (sections, global state, communication model, action dispatch, rendering invariants).

1. **READ `game_ui/static/app-js-overview.md` BEFORE making any change to `game_ui/static/app.js`** (or before modifying the frontend in a way that touches its architecture).
2. **Any non-trivial change to `app.js`** (new function, changed control flow, new WS/message shape, new UI mode, renamed/removed function, new card type) **must be accompanied by an update of `app-js-overview.md` in the same change**. Trivial fixes (typo, debug line, CSS tweak) do not require a doc update.

## Invariants — never break

1. **No in-memory state**: every action re-reads the state from `games.db` (source of truth), applies the rule, writes the state back.
2. **Hidden information**: never return the opponent's hand/mana/deck to a player — always via `current_game_json` (masking). `GET /game/{id}` is the only full-state endpoint (debug/analysis).
3. **Player message**: `{"cards": [...], "to": "stopover_k|mana|dwelling|discard_pile|...", "mode": ""|"move"|"defend"|"pass"|"dwelling_activation", "pendings": [], "cell": <0-23, optional>}` — validated by `message_check`. The optional `cell` targets an engineer drop placement (engine_version 12); `to: "dwelling"` places the dwelling card and `mode: "dwelling_activation"` taps it (no cards).
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
| `drop_on_board` | **Any** drop token (pet_trap) or trap cell exists on the Earth board. See *Drop on board*. | 377 |

**Biome ↔ faction map** (a `biome_<Faction>` condition is met if the player's current cell belongs to one of these two biomes):
- Dwarves (`Dwa`) → `MO`, `OC`
- Demons (`Dem`) → `OC`, `DE`
- Twigs (`Twi`) → `JU`, `OC`
- Miaous (`Mia`) → `DE`, `JU`
- Orcs (`Orc`) → `MO`, `JU`
- Mummies (`Mum`) → `DE`, `MO`

> **Not implemented yet** (present in the card pool but not handled):
> - `face_point_left` (379), `face_point_right` (377), `pending` (377).
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

### Drop on board (condition `drop_on_board`)

The **drop_on_board** condition checks whether **any drop or trap is on the Earth board**:

- **Met (True)** if:
  - Any **drop token** (placed by a `pet_trap` card) exists on the board (`GameState.drop_tokens` has at least one cell with count > 0), **OR**
  - Any cell in `GameState.earth` contains a `trap` or `drop` entry.
- **Not met (False)** if the board is clean (no drop tokens, no trap/drop cells).
- **Pure evaluation** — no side effects. The condition is checked at resolution time (like all other conditions), so the state of the board at that moment matters.
- **AI**: `PlayerAI._condition_met` mirrors this via `self.drops_on_board` (computed in `update_player_state` from the public board state — `drop_tokens` + `earth`). Unknown (no board info) → treated as not met (same as other board-dependent conditions like `biome_*`).
- **Replay**: no special handling needed — the replay calls `ge.is_condition_met` directly and preserves the stored `engine_version`, so the v9 gate auto-pins old games.
- **Rule version**: introduced with `engine_version = 9`. Games with `engine_version < 9` keep the canonical default — the condition is treated as **met** (the catch-all). The gate lives in the engine (`engine_version` check in `is_condition_met`), so the replay pins old games automatically.
- **Tests**: `tests/_drop_on_board.py` (7 tests: clean board, drop token, trap cell, drop cell, v8 pinning, not-met resolution, met resolution).

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
| `discard` | Player discards `effect_number` cards. **Since engine_version 13 the player CHOOSES which cards** — the trip chain pauses (`"turn N - waiting for NAME to discard K card(s)"`), the player sends `{cards:[…K…], to:"discard_pile", mode:""}` from their hand, and the chain resumes. Pre-13 (or `effect_number` 0 / empty hand): the **last** cards of the hand are auto-discarded (no pause). See *Discard selection*. |
| `discard_oppo` | Opponent discards 1 card. **Since engine_version 13 the OPPONENT CHOOSES which card** (same pause/choice flow, target = the opponent). Pre-13: the opponent's last hand card is auto-discarded. See *Discard selection*. |
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
| `rooted` | **Self-effect**: the card roots itself — grants the player a **one-shot rooted token** on the card. The card survives the cleaning phase and sits on a free stopover column on the board until the end of the next turn, when it is discarded. Inert on the board (no block, no effect). Cooldown: the card cannot get another token the next turn. See *Rooted*. | 172 |
| `wrecking_ball` | **INSTANT board effect**: at **play time** (move mode), **removes the opponent's dwelling card** (`PlayerState.dwelling`, if set) — the card is sent to the opponent's discard pile and the dwelling slot is cleared. A no-op today (no implemented effect places a dwelling card yet) but the removal system is live. See *Wrecking ball*. | 460 |

> **Not implemented yet** (present in the card pool but the engine returns the state unchanged — the card only still advances):
>
> | Effect | #cards | Note |
> |---|---|---|
> | `swap_cards` | 172 | |
>
> **`effect_number = 0` in the pool** — the meaning is intrinsic to the effect name, not to a number. `swap_cards` is not referenced anywhere else in the codebase, so it requires a **design decision first**, then an engine implementation + AI mirror + replay note. (`copy_effect` **is** implemented — see *Copy effect*; `pet_trap` — see *Pet trap*; `rooted` — see *Rooted*; `wrecking_ball` — see *Wrecking ball*.)

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

### Rooted (effect `rooted`)

The **rooted** effect (self-effect: the card roots itself) gives the playing player a **one-shot rooted token** on that card. The card survives the cleaning phase and sits on a free stopover column on the board until the end of the **next** turn, when it is discarded.

**Token grant** (at resolution time, in `_resolve_card`, after `apply_effect`):
- When a `rooted` card is resolved (move mode, condition met, not blocked, not effect_canceled) and `engine_version ≥ 10`, the player gets a rooted token on that card.
- The card is added to `GameState.rooted_this_turn` (a per-turn list).
- **Cooldown**: the card's id is recorded in `GameState.rooted_history` with the current turn number. On the **next** turn, if the same card is played again with its condition met, it **cannot** get another token (the cooldown check in `_grant_rooted_token`). The cooldown expires after the following turn (the card can get a token again on turn N+2).
- **Blocked / not-met / canceled** rooted cards do **not** grant a token (like all effects, the rooted grant is gated on the effect being valid).

**End-of-turn settlement** (`_process_rooted_cards`, called in `handle_websocket_message` after the trip chain, in ALL end-of-turn cases):
1. **Discard last turn's rooted cards**: any cards still in `rooted_on_board` (from the previous turn) are moved to their owner's discard pile (their token was one-shot — it is consumed by surviving one turn).
2. **Place this turn's rooted cards**: the cards in `rooted_this_turn` are moved from the discard pile onto **free stopover columns** (stopover_4 first, then 3, 2, …) and added to `rooted_on_board`. One card per stopover (no stacking). If fewer than 5 cards, they occupy the first free stopovers in order.
3. **Clear `rooted_this_turn`** (it is a per-turn buffer).

**On the board** (`rooted_on_board`):
- A rooted card on the board is **inert**: it does not block, does not defend, does not trigger its effect again. It simply sits on a stopover column until it is discarded at the end of the next turn.
- Multiple rooted cards (from different turns) keep their order: the older one (from the previous turn) is discarded first, the newer one (from this turn) takes its place.
- `rooted_on_board` is public board info (visible to both players, safe in both endpoints).

**Rule version**: introduced with `engine_version = 10`. Games with `engine_version < 10` keep the old behavior — `rooted` is a **no-op** (the card only advances, no token, no board placement). The gate lives in the engine (`engine_version` check in `_grant_rooted_token` and `_process_rooted_cards`), so the replay pins old games automatically (it preserves the stored `engine_version`; `_process_rooted_cards` is a no-op for old games).

**AI**: no mirror needed — `rooted` is an effect, and the AI does not simulate effects (`PlayerAI` only evaluates conditions for scoring). A rooted card is simply worth its base advancing in its current valuation (the token is a one-shot bonus the AI does not model).

**Frontend**: the rooted token image (`/assets/effect_rooted.png`) renders on the card in the trip-chain display when the card is in `rooted_on_board`. The `effectLabel` shows `rooted` for `rooted` effect cards.

### Wrecking ball (effect `wrecking_ball`)

The **wrecking_ball** effect is an **INSTANT board effect**: it fires **at play time** (inside `_apply_instant_effects`, right after a successful move-mode play) — **before** the trip chain resolves. It therefore **cannot be blocked, effect-canceled or conditioned** (a blocked wrecking_ball card still wrecks; a wrecking_ball played in **defend** mode does nothing). The card itself then resolves in the trip chain as a **no-op** (`apply_effect('wrecking_ball', …)` does nothing; the card only advances by its basic value — `effect_number` is 0 in the pool and unused).

**The wreck** (the instant part):
- **Removes the opponent's dwelling card** — the card sitting in the opponent's **dwelling spot** (`PlayerState.dwelling`, a single card id). If the opponent has no dwelling card (`dwelling is None`), the effect is a no-op.
- The removed dwelling card is **sent to the opponent's discard pile** and the **dwelling slot is cleared** (`dwelling = None`).
- The engine annotates the action with `dwelling_removed` (the card id) and `dwelling_removed_from` (the opponent's name) so the turn log can show it (`💥 wrecking_ball — removed <name>'s dwelling card <id>`).

**The dwelling spot** (`PlayerState.dwelling`):
- A per-player slot that holds **one** card id (the dwelling card). It is a separate field from the zones (hand/deck/discard/mana) — a dwelling card is **not** in any zone while it dwells.
- **Placeable since `engine_version` 12** by the **Engineers' `refinery`** dwelling card (see *Engineers*) — played with `to: "dwelling"` (1 card, costs its `mana_cost`), a play-phase action. So `dwelling` is no longer always `None`: it holds the refinery (or any future dwelling card) until wrecking_ball removes it.
- **The removal system is live**: wrecking_ball removes the dwelling card (the engine checks `opponent.dwelling` and, if set, sends it to the discard + clears the slot).

**Rule version**: introduced with `engine_version = 11`. Games with `engine_version < 11` keep the old behavior — `wrecking_ball` is a **no-op** (the card only advances, nothing is ever removed; the gate is the `engine_version >= 11` check in `_apply_instant_effects`). The replay mirrors the instant removal in `_replay_trip_chain` (gated on `engine_version >= 11`), so it stays faithful when dwelling cards are added later.

**AI**: no mirror needed — `wrecking_ball` is an effect, and the AI does not simulate effects (`PlayerAI` only evaluates conditions for scoring). A wrecking_ball card is simply worth its base advancing in its current valuation (the wreck is a one-shot bonus the AI does not model; it is a real play today, since the Engineers' refinery can occupy the dwelling spot).

### Discard selection (effects `discard` / `discard_oppo`, engine_version 13)

Since `engine_version = 13`, the **discarding player chooses which cards** are discarded, instead of the engine auto-discarding the last N of the hand. This introduces the first **mid-trip-chain pause**: the chain stops, the player sends one more message, and the chain resumes.

**Flow** (both `discard` and `discard_oppo`; the *target* is the player who discards — the actor for `discard`, the **opponent** for `discard_oppo`):
1. The card resolves as usual: condition check → **its basic advancing is applied first** (the `discard`/`discard_oppo` effect does not handle movement, so `process_advancing` runs) → then `apply_effect` is reached.
2. `apply_effect` sets `GameState.pending_discard = {player, n}` (where `n = min(abs(effect_number), len(target.hand))`) and the source card's log entry is marked `_pending_discard`. The chain **pauses at the next boundary**: `process_trip_chain` (now a stage machine — see below) saves its full context in `GameState.chain_resume` (index, stage, both players' entries + flags) and sets the state to `"turn N - waiting for NAME to discard K card(s)"`.
3. The discarding player answers with `{"cards": […K…], "to": "discard_pile", "mode": "", "pendings": []}` — **exactly K** distinct cards from **their** hand. The engine (2.0 branch of `handle_websocket_message`) validates (sender must be the target, `to` must be `discard_pile`, count must equal K, all cards must be in hand), applies the choice (hand → discard pile, turn-log note on the source card's line), clears `pending_discard`, and **resumes** the chain with `process_trip_chain(resume=ctx)`.
4. If the resumed chain hits **another** discard effect, it pauses again (the same state string can recur — e.g. two discard cards in one turn). When the chain finally completes (no `chain_resume`), the engine runs the turn-end block (`_end_turn`: rooted settlement, deadlock check, draw 3, day/night flip, turn-order reversal, turn+1).

**Rules / invariants**:
- **Only the discarding player may answer**; a message from the opponent (or with the wrong `to`/count, or a card not in hand) is **rejected** with no state change — the chain stays paused.
- **Card conservation holds in every end state**, including a **mid-chain win**: if a win occurs, `flush_unresolved` clears `pending_discard` (the pause is abandoned, the would-be-discarded cards **stay in the hand** — they were never removed) and flushes the unprocessed played cards to their owners' discard, exactly as before.
- **`n = 0` never pauses**: if the target's hand is empty (or `effect_number` is 0), `apply_effect` falls through to the legacy path and nothing is discarded.
- **Pre-13 games keep the legacy behavior**: `engine_version < 13` auto-discards the **last** cards of the hand (no pause). The gate is `engine_version >= 13 and n > 0` in `apply_effect`, so the replay pins old games automatically (it preserves the stored `engine_version`).
- **The stage machine** (`process_trip_chain`): the trip chain was refactored from a linear loop into an explicit stage machine with a `ctx` dict persisting `{index, stage, entries, flags}` across the pause. Stages per index: `first` → `second` → `log` → `copy`. The `discard` boundary check runs at each stage boundary; on a pending discard it calls `pause_discard(ctx)`. **Per-index flags are explicitly reset to defaults at every index transition** (in the `log` stage) — a `setdefault` would leave stale values from the previous index and cause an `IndexError`.
- **`GameState.pending_discard`** (`{player, n}`) and **`GameState.chain_resume`** (the saved `ctx`) are new model fields (`models.py`), both `Optional` (default `None`) so old saved games are unaffected.

**AI**: `PlayerAI.choose_discard(num_cards)` picks the **N least-valuable** hand cards (it does not simulate effects — it just minimizes the value of what it gives up) and returns the `{cards, to:"discard_pile", mode:""}` message. `game_ui/ai_driver.py` matches the discard-wait state with `DISCARD_RE` and routes to `choose_discard`. The Robot answers **every** discard pause (deliberately **not** gated on `st['acted']` — the same state string can recur in a turn with two discard effects).

**Frontend** (`game_ui/static/`): `detectPhase` returns `{kind:"discard", turn, actor, n}` for the discard-wait state; `renderAll` shows a red **DISCARD** button (`#btn-discard`, the only button visible) enabled iff `selected.size === n`; `makeHandCard` allows **multi-select** in discard mode (toggle, capped at N, toast on overflow); the button handler validates phase/count and sends `{cards:[…], to:"discard_pile", mode:""}`, clearing the selection only on success. See `game_ui/static/app-js-overview.md`.

**Tests**: `tests/_choose_discard.py` (5 scenarios: basic n=1, discard_oppo, n=2, chain continuation with two consecutive discard pauses, v12 legacy auto-discard). Deletes its own games.

**Rule version**: introduced with `engine_version = 13`. Games with `engine_version < 13` keep the old behavior — `discard`/`discard_oppo` **auto-discard** the last cards of the hand (no pause). The gate lives in the engine (`apply_effect`), so the replay pins old games automatically. The replay mirrors the pause/resume: it recognizes the `discard_pile` choice messages, applies the chosen cards via `_apply_discard_choice`, and **does not** apply a discard choice when `game.state == "game over"` (mid-chain win: the real engine abandoned the pause and the cards stayed in the hand).

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

### Support factions (second deck, menu selection)

- Data: `cards/support_factions.parquet` — 15 cards across 3 support factions (Engineers, Mages, Doctors; 5 unique each). Columns: `support_faction_name`, `card_name`, `mana_cost`, `description`, `card_path` (art file name, e.g. `Mag_black_hole.png`).
- **Deck composition since 2026-09**: a player's deck = **20 main-faction cards** + **10 support cards** (2 copies of each of the 5 unique support cards) = **30 cards, shuffled together** (the UI shuffles before sending). Support card ids in the deck are the `card_name` values (e.g. `black_hole`) — they are **not** in `cardpool.parquet`.
- **Art**: support card art is served at `/art/<card_path>` (e.g. `/art/Mag_black_hole.png`) — **not** `/art/<card_name>.png` (the file name differs from the card name, unlike main cards where file name = `card_id`). Frontend helper: `cardImg(id)` in `game_ui/static/app.js`.
- **Engine**: **Engineers are implemented** (`engine_version` 12) — see *Engineers* below. **Mages and Doctors are not implemented yet**: their cards are **not** in `ge.CARDS_DB`, so every `CARDS_DB.filter(is_in([id]))` lookup for their condition/effect/advancing comes back empty and the guards (`is_empty()`) make them **effect no-ops** (playable, can go to mana, resolve with no condition/effect/advancing). Do not "fix" them to errors or invent rules. Note: since `engine_version` 12 **every** support card (Mages/Doctors included) **costs its `mana_cost`** when played (see `_play_cost`) — only their *effect* is still a no-op, not their cost.
- **AI**: `PlayerAI` never **plays** support cards (`play_card`/`defend_card` filter by `CARDS_DB`, so the robot never plays an engineer drop/dwelling or any support card). But it **does put support cards into the mana zone**: `put_mana` considers the whole hand (main **and** support — the engine accepts any card in hand into mana, each counting as 1 mana) and prefers support cards first (they are dead weight in the robot's hand, so they make free mana tokens). This is required for a 20-main + 10-support deck: the robot's 6-card init hand can have fewer than 3 main cards, and the init phase must put exactly 3 in mana in one valid message (the engine only accepts 1 or 3 per mana message). The Robot's starter deck is 20 random main + 10 random support (`ai_driver.random_ai_deck`).
- **API**: `GET /support_factions` lists the 15 cards (banner images at `/art/supfac_{eng,mag,doc}_banner.png`); `check_deck` (API.py, public) validates decks that mix main `card_id`s and support `card_name`s — it also **rejects a deck containing the same main card twice** (a card can only appear once; support card names legitimately appear 2×). The frontend CSV import (`parseCsv` in `app.js`) rejects duplicate ids with a clear toast before the deck is used.

### Engineers (support faction, engine_version 12)

The **Engineers** support faction is fully implemented (the first of the three). Its 5 cards are: **4 drop cards** (boost, trampoline, gluetrap, landmine) + **1 dwelling card** (refinery). All support cards (engineers included) now **cost their `mana_cost`** when played in `engine_version` 12+ games (see *Support card cost* below).

**The 4 drop cards** (all `mana_cost` 1 except landmine = 3). Each is played in **MOVE mode** with a **target earth cell** (`message["cell"]`, 0-23). At **play time** (instant, like pet_trap/wrecking_ball — before the trip chain, so it **cannot be blocked, effect-canceled or conditioned**), a **drop token** is placed on that cell (`GameState.board_drops: [{cell, kind, owner}]`, public board info). The card itself then resolves as a **no-op** (only advances by its basic value; `effect_number` is 0 and unused). The FIRST token (either player) that **arrives** on the cell triggers the drop, then the token is **consumed**:

| Card (card_name) | mana_cost | Token image | Trigger when any token arrives on the cell |
|---|---|---|---|
| `boost` | 1 | `/assets/supfac_eng_boost.png` | Arriving token advances **+2** (step-by-step via `process_advancing`: win-check + can chain-trigger further drops, no biome bonus) |
| `trampoline` | 1 | `/assets/supfac_eng_trampoline.png` | Arriving token **jumps +2** (teleport via `_jump`, landing-cell check only) |
| `gluetrap` | 1 | `/assets/supfac_eng_slowingtrap.png` | Arriving token **knocked back −1** (direct, clamped at cell 0, no re-trigger) |
| `landmine` | 3 | `/assets/supfac_eng_mine.png` | Arriving player is **blocked for the rest of the turn**: `landmine_blocked = True` — their MOVE cards are **canceled** (no effect, no advancing) **except** an `unstoppable` card whose condition is met; DEFEND cards are unaffected. Cleared in the cleaning phase. Also **stops the remaining steps of the current movement** that just landed on it. |

- **Placement rules**: playing a drop requires the `cell` field (the engine rejects a drop without it). Placing it on a cell **already occupied** by a token does **not** activate it (it only fires on **arrival**). Multiple drops on the same cell fire **in placement order**; if a drop moves the token **off** the cell (boost/trampoline/gluetrap), the remaining drops on that cell **stay** (the position is re-checked before each).
- **Trigger sites**: `process_advancing` (every step) and `_jump` (landing cell) both call `_trigger_board_drops` after the pet_trap check. **Critical**: the earth re-sync (remove the player from all cells, append once at the final position) must run **BEFORE** `_trigger_board_drops`, because a boost/trampoline inside the trigger recurses into `process_advancing`/`_jump` — otherwise the recursive earth sync hits a `ValueError` (list.remove on an absent token).
- **Landmine gating**: `_player_blocked(player, game)` (v12+ and `landmine_blocked`) gates the move branch of `process_card` (cancels the card) and both grappling-hook and copy_effect copies in `process_trip_chain`. The `was_blocked` flag is captured before the movement loop so a landmine fired **mid-movement** breaks the current move but doesn't re-fire on the stale flag.
- **Rule version**: introduced with `engine_version = 12`. Games with `engine_version < 12` keep the old behavior — drops are inert (no token placed, `_apply_instant_effects` gate), landmines never block, dwelling placement/tap is rejected, and support cards are free. The gates live in the engine, so the replay pins old games automatically (it preserves the stored `engine_version`).
- **Replay**: `_replay_trip_chain` mirrors the instant drop placement (reads `message["cell"]` + the card for `ge.ENGINEER_DROPS`), the dwelling placement/tap (hand→dwelling, or `_pick_draw` + `ge._draw_cards`), and the landmine gates. The final **board_drops** and **dwelling** are compared to the stored ones (a warning on divergence). The final **dwelling** card is included in the conservation pool (it is not in any standard zone).
- **AI**: no mirror needed — `PlayerAI` skips support cards entirely (it never plays an engineer card). `drops_on_board` (the `drop_on_board` condition) now also counts `board_drops`.
- **Frontend** (`game_ui/static/`): `ENGINEER_DROPS`/`isEngineerDrop`/`isEngineerDwelling` in `app.js`. Playing a drop card enters **cell-selection mode** (24 clickable targets on the Earth ring, `#cell-targets` + `body.cell-selecting`), then sends the action with `cell`. Playing the refinery sends `to: "dwelling"`. A **Tap dwelling** button (`#btn-tap`) sends `mode: "dwelling_activation"`. Board drops render on the Earth (`.drop-token.eng-drop`), the dwelling card in the dwelling panel (dimmed via `.dwelling-tapped` when tapped), and a `💣 blocked` badge (`.landmine-badge`) appears on the blocked player's banner. `cardCost(id)` handles support-card costs for the mana check.
- **Preview**: `tests/_engineers_preview.html` (copy into `game_ui/static/` and screenshot via headless Chrome, like `_drop_tokens_preview.html`).

**The dwelling card — `refinery`** (`mana_cost` 3). Placed in the **dwelling spot** (`PlayerState.dwelling`, a single card id, public board info) — **one at a time, permanent** until removed by a `wrecking_ball` (see *Wrecking ball*). The dwelling spot is now **placeable**: it is filled by playing the refinery (`to: "dwelling"`, 1 card, costs `mana_cost`), a **play-phase action** that alternates control like a normal play (not in the trip chain, not resolved by `process_card`). **Tap**: once per turn (`mode: "dwelling_activation"`, no cards, **free**) → the owner **draws 1** card; `dwelling_tapped` is set and reset in the cleaning phase (untapped every turn). Since 2026‑09 the **tap is a QUICK action** — it does **not** alternate control: the state stays "waiting for <this player> to play", so the tapper may still play a card or pass afterwards (the engine sets no state transition in the tap branch of `player_play`). Placing the dwelling still alternates like a normal play. (No `engine_version` bump: the change only affects floor alternation, which the replay does not simulate — it replays each player's message segments — so old v12+ games stay verifiable; the engine code is shared for all v12+ games.)

**Dwelling placeholder** (UI feature, `engine_version` 12+). When a player places a dwelling card (refinery), a **faction-specific placeholder image** appears in the **stopover slot that the refinery would have occupied** if it were a normal play (determined by the play order at placement time: 1st action → stopover 1, 2nd → stopover 2, etc.). The slot is stored in `PlayerState.dwelling_slot` (0-4 column index), **set at placement time and cleared by the engine in the cleaning phase** (the per-player reset in `_end_turn`) — so the placeholder only shows during the placement turn and must **not** reappear at every new turn (the frontend renders it only while `dwelling_slot` is set; the `dwelling` card itself stays on the board). The placeholder is purely visual — it does not block, defend, or trigger any effect — but it **fills the slot** while shown so both players can see the dwelling card is on the board. The image is chosen by the player's main faction (`PlayerState.faction`, derived from the deck at game creation): `placeholder_Dwa.png`, `placeholder_Dem.png`, `placeholder_Twi.png`, `placeholder_Mia.png`, `placeholder_Orc.png`, `placeholder_Mum.png` (served at `/cards_ex/placeholder_<key>.png` from `GlobeRunners_card_system/lib/artdesign/cards_ex/`). The slot is also marked as "filled" (not dimmed) when it contains a dwelling placeholder. When a `wrecking_ball` removes the dwelling card, both `dwelling` and `dwelling_slot` are cleared. Frontend: `getFactionKey()` + `dwellingPlaceholderSrc()` in `app.js`, `.dwelling-placeholder` class in `style.css`.

**Support card cost** (new rule, `engine_version` 12): `_play_cost(current_game, card_id)` returns a main card's `mana` (from the pool) or — for a support card in a v12+ game — its `mana_cost` (from `ge.SUPPORT_DB`). So **every** support card now costs its `mana_cost` when played (previously 0). The frontend mirrors this via `cardCost(id)`.

**Tests**: `tests/_engineers.py` (16 tests: cost, drop placement, no-cell rejection, boost/trampoline/gluetrap/landmine triggers, landmine block/exception/cleaning, dwelling place/tap, wrecking ball, drop_on_board, old-game v11 pinning, full kept game with conservation).

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

## `game_ui/app.py` — living doc (`game_ui/APP_OVERVIEW.md`)

`game_ui/APP_OVERVIEW.md` is the **living overview** of `game_ui/app.py` (what it does, route map, function-by-function, integration map, invariants, change log). Two mandatory habits:

1. **Before** making ANY change to `game_ui/app.py`, **read `game_ui/APP_OVERVIEW.md` first** — so you already know the file's architecture, route order and invariants before touching it.
2. **After** the change: if the modification is **significant** (new/removed route, changed middleware/mounts, changed AI wiring, changed deck validation, changed the layering/flow between components), update `game_ui/APP_OVERVIEW.md` **in the same session** — fix the affected sections and append a line to its *change log*. Cosmetic changes (typos, comments, variable names) do NOT require a doc update. If you notice the doc has drifted from the code for any other reason, re-sync it.

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
uv run python tests/_drop_on_board.py # drop_on_board condition, engine_version 9 (also writes to games.db)
uv run python tests/_rooted.py        # rooted effect, engine_version 10 (also writes to games.db; 1 game is persisted for the replay self-test)
uv run python tests/_wrecking_ball.py # wrecking_ball effect, engine_version 11 (also writes to games.db)
uv run python tests/_engineers.py    # Engineers support faction, engine_version 12 (drops + refinery dwelling + support cost; also writes to games.db; 1 game is persisted for the replay self-test)
uv run python tests/_tap_dup.py     # refinery TAP draw must not duplicate a card (2026-09-04: the tap branch used to drop the copy's deck/discard sync-back; drives the WS model_copy() path like API.py/ai_driver; deletes its own game)
uv run python tests/_midchain_flush.py # check_deck validation (support cap, main uniqueness) + mid-chain win: phantom action_chain cleared, card conservation, flush to discard, game-over guard (uses a TEMP DB — does NOT touch games.db)
uv run python tests/_choose_discard.py # discard selection, engine_version 13 (discard/discard_oppo pause + choice, chain continuation, v12 legacy auto-discard; deletes its own games)
# the server on :8001 MUST be running for these two:
uv run python tests/_ui_e2e.py     # 2 fake players via REST+WS
uv run python tests/_ai_e2e.py     # human vs Robot
uv run python -m games.analysis.replay   # replay auto-test
```

## Known pitfalls

- `tests/_diag.py` **writes to `games.db`** — do not run it against a precious database.
- Art/assets folders: **hard-coded external paths** (`GlobeRunners_card_system/lib/artdesign/…`) with a placeholder fallback — do not "fix" the paths without verifying the fallback still works.
- A player's deck is just a list of ids from the Parquet pool — do not invent fields outside the Parquet columns.
- Several `condition`/`effect` values exist in the pool but are **not implemented** in the engine (see the "Not implemented yet" notes) — the 3 remaining conditions are currently treated as **met** and the 1 remaining effect (`swap_cards`) as **no-op**. Do not assume they work. (`cataclysm` **is** implemented — see *Cataclysm*; `avalanche` — see *Avalanche*; `grappling_hook` — see *Grappling hook*; `effect_canceled` — see *Effect canceled*; `copy_effect` — see *Copy effect*; `pet_trap` — see *Pet trap*; `drop_on_board` — see *Drop on board*; `rooted` — see *Rooted*; `wrecking_ball` — see *Wrecking ball*.)
- **Rule versioning**: `GameState.engine_version` marks the rules a game was played under (`13` = current rules: + **discard selection** — `discard`/`discard_oppo` pause the trip chain so the discarding player picks which cards (see *Discard selection*); `12` = + Engineers support faction (4 drop cards boost/trampoline/gluetrap/landmine + refinery dwelling) + support cards cost their `mana_cost`; `11` = + wrecking_ball instant effect (removes the opponent's dwelling card); `10` = + rooted effect/token; `9` = + drop_on_board condition; `8` = + pet_trap instant effect/drop tokens; `7` = + copy_effect effect; `6` = + effect_canceled effect; `5` = + grappling_hook effect; `4` = + avalanche effect; `3` = + cataclysm condition/pile; `2` = faction biome bonus + conditional block-card effect). The engine sets it at game creation; the replay/analysis tooling reads it to pin **old** games (`< 2`: no biome bonus; `< 3`: no cataclysm pile → trigger is a no-op; `< 4`: avalanche is a no-op; `< 5`: grappling_hook is a no-op; `< 6`: effect_canceled is a no-op; `< 7`: copy_effect is a no-op; `< 8`: pet_trap is a no-op, no drop tokens; `< 9`: drop_on_board is treated as met — canonical default; `< 10`: rooted is a no-op, no token/board; `< 11`: wrecking_ball is a no-op, no dwelling removal; `< 12`: engineer drops are inert, dwelling placement/tap rejected, landmines never block, support cards are free; `< 13`: `discard`/`discard_oppo` **auto-discard** the last cards of the hand — no pause/choice) to the rules they were actually played under — otherwise the +1 biome bonus would drift every forward move on a home biome and old games could never verify. When you add a **new** engine rule, bump `engine_version` and teach the replay how to pin older games.
- **Replay self-test skips in-progress games** whose state is `turn N - waiting for … to play` (an unresolved trip chain can never verify against a full-history replay). The replay also keeps `game.day_night` in sync every turn — `day`/`night` conditions must evaluate against the turn parity, not a frozen "day".
- **Replay mid-chain-win semantics must mirror the engine exactly**: the engine checks the game state **before every action and after every grappling/copy step** and returns immediately on a win. The replay (`_replay_trip_chain`) enforces the same: no action, grappling copy, or copy_effect fires after a win, and the unprocessed played cards are flushed to their owners' discard (mirroring the engine's `flush_unresolved`). A win *during* a grappling copy still counts (the copy is what won). If you add a new post-action step to `process_trip_chain`, mirror its game-over ordering in the replay.
- **Replay card conservation for old games**: a few OLD games (created before the `flush_unresolved` fix) are missing their unprocessed played cards from all final zones — `analyze_game` reconstructs them in the discard before replaying (one-off historical artifact; the current engine conserves every card).
- The engine does **not** verify that the message sender is the player whose action the state expects (branching is on `state` only) — the WS layer trusts the `player_name` from the URL. Treat out-of-turn senders as a known gap, not a rule.

## Duplicate cards in hand (2026-09-02, root cause found + fixed)

**Symptom.** The user saw the same card twice in hand after tapping the refinery (3× refinery in game `26_09_02_18_54_10_PyaZf`; duplicate main cards in `26_09_02_16_27_30_11VCs`).

**Root cause.** Not an engine bug. A full audit showed **every engine zone transition conserves cards** (draw = deck→hand, play = hand→chain, resolve = chain→discard, reshuffle = discard→deck, …), the DB write is a single atomic full-state `UPDATE` (the final state is always one thread's consistent snapshot — a lost update can roll a game back but **cannot create a card**), and the frontend enforces exactly 20 main + 10 support. The actual hole: **the server accepted a 31-card deck**. `check_deck` (API.py) only checked card *existence* — it let a support card appear **3 times** (PyaZf: refinery ×3; the user's browser at the time was running an intermediate dev version of the deck builder). The engine faithfully conserves whatever the deck contains — so a deck with 3 refineries legitimately produced 3 refineries across hand/dwelling. `11VCs` is the same class: a deck with duplicated MAIN cards.

**Proof of conservation** (in `tests/_midchain_flush.py`): a scripted 2-player game to a mid-chain win verifies (a) final zones == original deck multiset for both players, (b) the loser's unprocessed card is flushed to the discard, (c) no phantom `action_chain` in the final state, (d) a stray message after "game over" moves no card.

**Fixes (2026-09-02):**
- `check_deck` (API.py): rejects a **main card appearing more than once** and a **support card appearing more than twice** (400). Still accepts standard 30-card decks and the 15-card E2E test decks. Applies to all three entry points (`/create_game`, `/join_game`, `/create_game_ai`).
- `game_ui/static/app.js`: CSV import rejects duplicate card ids (toast); launch already required exactly 20 main + 10 support.
- `engine/game_engine.py` `create_new_game`: prints a **loud warning** if a deck with duplicate ids is created anyway (defense in depth for clients that bypass `check_deck`).
- `engine/game_engine.py` `handle_websocket_message`: **game-over guard** — a message on a finished game is rejected ("Game over — winner: …") and moves no card (before, a stray refinery tap after the win would have been the obvious "I draw again" culprit; it now can't draw).
- `flush_unresolved` clears `action_chain` after the flush (see *Phase 3*, item 3).

**Old saved games** (`PyaZf`, `11VCs`, and any similar deck) keep their 31+ card state — they are a **historical artifact** (the deck was invalid at creation), not an engine bug. They fail the replay self-test by conservation/identity, which is now *understood* — they are NOT the same issue as `w4LNX`/`SfPzi` below. Do not "fix" their stored state. (Same class of artifact: game `26_09_04_22_43_59_c3tg4`, created 2026-09-04 while the **refinery-tap bug** below was live — 31 cards in the zones, `boost` ×2: the tap draw left the card in the deck and `ramp_oppo` moved the phantom copy into the mana zone. It fails the replay self-test by conservation; understood, do not "fix" its stored state.)

### Second root cause found 2026-09-04 — the refinery TAP draw duplicated a card (FIXED)

The symptom above ("the effect draw is OK, but the draw of the game phase duplicates the last drawn card") had a **second, distinct** root cause, reproduced live in the web app (game `26_09_04_22_43_59_c3tg4`):

- **Root cause**: the WS layer (`API.py`) and the AI driver pass `player` as a **`model_copy()`** of the stored `PlayerState`. The dwelling-tap branch in `handle_websocket_message` drew with `_draw_cards(player, 1)` — popping the **copy's** deck and appending to the **copy's** hand — and its sync-back loop copied `hand`, `mana_spend`, `dwelling`, `dwelling_tapped` to the authoritative state **but not `deck` (nor `discard`, which `_next_from_deck` reshuffles)**. The drawn card therefore stayed in the persisted deck: it existed in **two zones at once**, and the next draw (cleaning draw, `ramp`/`ramp_oppo`, …) re-drew it → duplicate card in hand. (In the live repro the phantom copy was grabbed by the robot's `ramp_oppo` and landed in my mana zone — 31 cards in 30-card zones.)
- **Why the tests missed it**: `tests/_engineers.py` & co. call `ge.player_play(...)` with `gs.players[name]` — the **authoritative** object — so the copy path was never exercised. `tests/_tap_dup.py` drives every action through `ge.handle_websocket_message(game_id, player.model_copy())`, exactly like `API.py`/`ai_driver.py`; it **fails on the buggy code** (drawn card still in the deck) and passes on the fix.
- **Fix** (engine, dwelling-tap sync loop): also sync `p.deck = player.deck` and `p.discard = player.discard` (the tap is the only branch that mutates the copy's deck/discard).
- **Second bug found in the same session (frontend, FIXED)**: the **Pass button was a silent no-op during the mana phase** — `#btn-pass`'s handler started with `if (!myTurn(game.state)) return;`, but `myTurn()` is false in the `mana-pass` phase (`detectPhase` returns the string `"mana-pass"`), so the `ph3 === "mana-pass"` branch was dead code and the game stalled (the button looked enabled and the hint even said "or click 'Pass'"). Fixed in `game_ui/static/app.js` (mana-phase pass no longer gated on `myTurn()`); documented in `game-ui/static/app-js-overview.md` (dispatch table).
- **Verification** (web app, game `26_09_04_23_11_54_KdS4r`): refinery placed + tapped via the UI — the tap draw (`boost`) left the deck (16→15), zones total exactly 22/22, and after the cleaning draw the hand has **no duplicate** (boost ×1). Before the fix the same flow produced 23 cards with `boost` ×2.

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

> **New data point (2026-09-02).** Game **`26_09_02_12_38_02_SfPzi`** (a v12 AI-e2e game) also fails the replay self-test with a **zone-identity/count** mismatch (Alice hand replayed=6/stored=0, mana 9/15; Robot hand 5/10, deck 7/4, discard 2/5). **Critically, it never exercised a v12 mechanic** (`board_drops: []`, both `dwelling: None`, no landmine) — the Robot's support cards (Mages) simply sat unplayed in its deck/hand. So this is the **same pre-existing hand-accounting issue surfacing in a different game**, **not** a v12/Engineers bug. The v12 games that *did* play engineer drops (`…LFYFE`, `…0OmJ6`) verify **clean** (only the known zone-identity warnings). This is the "accumulate over more games" the plan calls for.

> **Do not confuse with the duplicate-deck games (RESOLVED, see above).** `26_09_02_16_27_30_11VCs` and `26_09_02_18_54_10_PyaZf` also fail the replay self-test, but their root cause is **known and is NOT this hand-accounting issue**: their decks were invalid at creation (31 cards — a support card ×3 / main cards duplicated — accepted by the old `check_deck`). They now fail *by design* (conservation/identity vs a non-standard deck). The genuinely unexplained failures remain `w4LNX` and `SfPzi` (and `26_09_01_20_14_31_WzWsv` — unanalyzed, candidate for the next investigation pass).

**Planned next step (later, after more games).** Re-run the replay self-test once several more games exist and collect **every** `cards_in_hand_*_oppo` divergence (not just `w4LNX`). A recurring pattern (same turn shape, same player, same preceding effect) will pin the mechanism. A useful diagnostic to build then: re-derive each player's hand **turn-by-turn** from `messages_history` + deck and flag where the engine's logged `condition_met` disagrees with the reconstructed hand — that turns this from a black box into a reproducible case.

**Do not** mark `w4LNX` (or its siblings) as a known-bad game to force a 100% pass — that hides the bug. Keep the self-test failing until the root cause is understood.
