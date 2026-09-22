# GlobeRunners — Conditions & Effects (card `condition` / `effect` columns)

> Part of the project instructions, split out of `AGENTS.md` (2026-09-21).
> Back to [AGENTS.md](AGENTS.md). Cross-references like `see *Defense & blocking*` / *Turn anatomy* point to [AGENTS.md](AGENTS.md); `see *Engineers/Doctors/Mages*` point to [sup_fact_agent.md](sup_fact_agent.md).

### Conditions (card `condition` column)

Evaluated by `is_condition_met`. A met condition → the card's effect fires. A `no_condition` card always fires.

The robot's mirror (`PlayerAI._condition_met` in [player_ai/playerai.py](player_ai/playerai.py)) evaluates these conditions from PUBLIC info + its own state only - fair play: it never sees the opponent's hand/deck/mana. Since section B (#19, 2026-09-21) of [player_ai/AI_improvement_list.md](player_ai/AI_improvement_list.md), **every pool condition is evaluable** - `biome_*` included (my cell's board layout + `FACTION_BIOMES`, an exact engine mirror; an unknown condition name falls through to the permissive default below). No pool condition depends on hidden *contents*: the `*_oppo` ones are counts only. The single thing a fair-play robot cannot know at scoring time: an opponent's unseen INSTANT (`thermic_flux` / `Celestial_reversal`) may change temperature or phase between my declaration and resolution (see section G for an optional cheat-mode).

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
| `pending` | Player has **at least one pending card** in their **own pending zone** (the doctors' pending zone). See *Pending condition*. | 377 |

**Biome ↔ faction map** (a `biome_<Faction>` condition is met if the player's current cell belongs to one of these two biomes):
- Dwarves (`Dwa`) → `MO`, `OC`
- Demons (`Dem`) → `OC`, `DE`
- Twigs (`Twi`) → `JU`, `OC`
- Miaous (`Mia`) → `DE`, `JU`
- Orcs (`Orc`) → `MO`, `JU`
- Mummies (`Mum`) → `DE`, `MO`

> **Not implemented yet** (present in the card pool but not handled):
> - `face_point_left` (379), `face_point_right` (377).
> - **Canonical default: an unimplemented condition is treated as MET** (the engine's `is_condition_met` catch-all) → the card resolves with its effect + full advancing. This is the intended default, not a bug — these entries disappear as each condition is implemented (`cataclysm` was the first). The AI (`PlayerAI._condition_met`) uses the **same** default; it only deviates for conditions it cannot evaluate from its state (biome/board → treated as not met).
> - `pending` **is** implemented — see *Pending condition* (it was the last of the three unimplemented conditions; `face_point_left` / `face_point_right` remain).
>
> **`face_point_left` / `face_point_right` are excluded from NEW games.** `ge.get_cardpool()` (the *playable* pool) filters out these two conditions — every deck entry point goes through it: the `/cardpool` endpoint, the frontend starter deck, the deck validation in `API.py`/`game_ui/app.py` (a deck containing one is rejected with 400), and the robot deck (`ai_driver.random_ai_deck`). The raw pool `ge.CARDS_DB` stays **complete**, so OLD games that contain those cards still resolve and replay fine (the replay reads `CARDS_DB`, not `get_cardpool`). To lift the filter later, set `ge.EXCLUDED_CONDITIONS = ()`.

### Cataclysm (pile trigger, condition `cataclysm`)

The board carries a **cataclysm pile of 4 cards, one per biome** (`OC`, `MO`, `DE`, `JU`), **shuffled at board initialization** (state: `GameState.cataclysm_pile`, top = first element).

When a card with the `cataclysm` condition is **resolved** (move mode, not blocked) — in `_resolve_card`, exactly once per card — the trigger fires:

1. **Look at the top card of the pile** → it names a biome B.
2. **ALL player tokens** (both players, not only the opponent) standing on a cell of biome B are **knocked back to the FIRST cell of that biome** (the start of the 6-cell segment). Tokens not on B are untouched. — **EXCEPTION**: while a `nobodymoves` turn is active (Mages), the knockback **is the suppressed movement** — the trigger **still fires** (step 3: the pile rotates and the biome is announced) but **NO TOKEN IS KNOCKED BACK** (log note `nobodymoves — cataclysm knockback suppressed`). 
3. The drawn cataclysm card is put at the **BOTTOM of the pile** — the pile only rotates, it is never exhausted.

Then the condition is treated as **met** → the card's `effect` fires and its advancing is applied (from the new position, if the player was just knocked back).

- The strike lives in `trigger_cataclysm()` (side effect) + `_resolve_card()` (call site); `is_condition_met('cataclysm')` itself is side-effect free (pure `True`) so evaluation calls (AI, replay, unstoppable check) never double-fire.
- **Defend-mode** cataclysm cards and **blocked** cataclysm cards do **not** trigger (defend never evaluates the condition; a block cancels the whole card).
- **nobodymoves**: the cataclysm **knockback is a MOVEMENT**, so while `nobodymoves` is active (see *Mages* → `nobodymoves`) the trigger still fires (pile rotates, biome announced) but the knockback is **suppressed** — no token moves. The check lives in `trigger_cataclysm` (`nobodymoves_active`). Because the trigger still fires, the `⚡ cataclysm — <biome> strikes` log note is still written, so the replay's pile-rewind count (one note per real trigger) is unaffected.
- **Replay**: the replay reconstructs the initial pile by rewinding the stored final pile by the number of **actual triggers** of the game (the pile is a pure rotation, so only the starting card needs rewinding). A trigger fires only when a cataclysm card actually resolves — a **blocked** cataclysm card (or one flushed un-resolved by a mid-chain win) does **not** trigger. The replay therefore counts the engine's `⚡ cataclysm — <biome> strikes` log notes (exactly one per real trigger) rather than the number of cataclysm cards played.

### Drop on board (condition `drop_on_board`)

The **drop_on_board** condition checks whether **any drop or trap is on the Earth board**:

- **Met (True)** if:
  - Any **drop token** (placed by a `pet_trap` card) exists on the board (`GameState.drop_tokens` has at least one cell with count > 0), **OR**
  - Any cell in `GameState.earth` contains a `trap` or `drop` entry.
- **Not met (False)** if the board is clean (no drop tokens, no trap/drop cells).
- **Pure evaluation** — no side effects. The condition is checked at resolution time (like all other conditions), so the state of the board at that moment matters.
- **AI**: `PlayerAI._condition_met` mirrors this via `self.drops_on_board` (computed in `update_player_state` from the public board state — `drop_tokens` + `earth`). Unknown (no board info) → treated as not met (same as other board-dependent conditions like `biome_*`).
- **Replay**: no special handling needed — the replay calls `ge.is_condition_met` directly.
- **Note**: unimplemented `condition`s fall back to the canonical default — treated as **met** (the catch-all in `is_condition_met`).
- **Tests**: `tests/_drop_on_board.py` (7 tests: clean board, drop token, trap cell, drop cell, not-met resolution, met resolution).

### Pending condition (condition `pending`)

The **pending** condition checks whether **the player has at least one pending card in their OWN pending zone** (the doctors' pending zone, `PlayerState.pendings`):

- **Met (True)** iff `len(player.pendings) >= 1` — there is at least one pending card (epo/virus/bloodtest/mercurochrome, or a laboratory-tap `epo`) sitting in the player's pending zone **right now**.
- **Not met (False)** if the pending zone is empty (`pendings` is `[]` / `None`).
- **Pure evaluation** — no side effects, checked at resolution time (like all other conditions) against the pending zone as it is at that moment. Note: a pending card **attached** to a move card is *consumed from the zone* at play time, so it no longer counts toward this condition (it is no longer "in the pending zone").
- **AI**: `PlayerAI._condition_met` mirrors this via `len(self.player_state.pendings or []) >= 1`. Since A.1 ([AI_improvement_list](player_ai/AI_improvement_list.md)) the robot fills its own pending zone with Doctors cards (§A.2), so for a Doctor deck the condition CAN be met — no code change was needed (the mirror already read the real zone). Since **A.2** the robot also ATTACHES pendings to its move plays (`_choose_attachment`: epo on win-completing / tempo cards, mercurochrome vs block/landmine/lock; never when the effect cannot fire), so attached pending effects do fire from its plays.
- **Replay**: no special handling needed — the replay calls `ge.is_condition_met` directly.
- **Note**: unimplemented `condition`s fall back to the canonical default — treated as **met** (the `is_condition_met` catch-all).
- **Tests**: `tests/_pending_condition.py` (empty zone → not met, ≥1 pending → met; full not-met resolution with reduced advancing; AI mirror agrees).

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
| `discard` | Player discards `effect_number` cards. **The player CHOOSES which cards** — the trip chain pauses (`"turn N - waiting for NAME to discard K card(s)"`), the player sends `{cards:[…K…], to:"discard_pile", mode:""}` from their hand, and the chain resumes. (`effect_number` 0 / empty hand): the **last** cards of the hand are auto-discarded (no pause). See *Discard selection*. |
| `discard_oppo` | Opponent discards 1 card. **The OPPONENT CHOOSES which card** (same pause/choice flow, target = the opponent). (empty hand): the opponent's last hand card is auto-discarded. See *Discard selection*. |
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
| `rooted` | **Self-effect**: the card roots itself — grants the player a **one-shot rooted token** on the card. The card survives the cleaning phase and sits on a stopover on the board until the end of the next turn, when it is discarded. The card is **ACTIVE** on the trip chain — it applies only its **basic advancing** (no condition, no effect, but **BLOCKABLE** by the opponent's defend at the same per-player position). Cooldown: the card cannot get another token the next turn. See *Rooted*. | 172 |
| `wrecking_ball` | **INSTANT board effect**: at **play time** (move mode), **removes the opponent's dwelling card** (`PlayerState.dwelling`, if set) — the card is sent to the opponent's discard pile and the dwelling slot is cleared. A no-op today (no implemented effect places a dwelling card yet) but the removal system is live. See *Wrecking ball*. | 460 |
| `swap_cards` | **INSTANT chain effect**: at **play time** (move mode), the card **swaps its trip-chain position** with the entry chosen by the player (`message["swap_with"]`: a position 1..5 of the player's **own** chain — a play, a board placeholder (doctor pending / dwelling) or a rooted card). The two entries exchange their stopover columns at play time, so the trip chain resolves the swapped order (resolution order, facing, blocking). **Optional**: without `swap_with` the card simply advances as usual. A **defend** play is a plain no-op (no swap). `effect_number` is 0 in the pool and unused. See *Swap cards*. | 172 |

> All 22 `effect` values of the pool are now implemented (including `swap_cards` — see *Swap cards*). `effect_number` is the quantitative parameter where an effect has one (`advancing`, `draw`, …) and 0/unused where the meaning is intrinsic to the effect name (`swap_cards`, `rooted`, `avalanche`, …).

### Avalanche (effect `avalanche`)

The **avalanche** effect is the fixed-MO variant of the cataclysm knockback, fired from `apply_effect` (so it only triggers when the card's condition is met — a blocked or not-met avalanche card does nothing):

1. **ALL player tokens** (both players, the playing player included) standing on a cell of the **Mountain (MO)** biome are **knocked back to the FIRST cell of that biome** (the start of the 6-cell segment). Tokens not on MO are untouched.
2. The playing player then applies its **normal basic advancing** from its (possibly new) position — `avalanche` is **not** a movement-handling effect, so it is not in the `('advancing', 'backward', 'jump')` set.
3. `effect_number` is 0 in the pool and unused. No pile, no rotation — the MO is always the struck biome.

- Implementation: `apply_effect('avalanche', …)` → `_knockback_biome('MO', …)` — the same shared knockback helper as the cataclysm trigger.
- **Replay**: the replay pins the rules a game was played under automatically (it preserves the stored `engine_version` in `_build_initial_state`).
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

- Implementation: `apply_effect('grappling_hook', …)` is a **no-op marker**; the copy is applied in `process_trip_chain` via `grappling_copy_amount()` + `apply_grappling_copy()`. `_resolve_card` returns whether the grappling card **activated** (effect + condition met); `process_card` propagates that flag to the trip chain.

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

- Implementation: `apply_copy_effect(game, copier, copier_action, facing_player, facing_action, log_entry)` (guards + call to `apply_effect`); `_resolve_card` now returns a third flag `effect_activated` (condition met and not canceled) and `process_card` propagates it as a 3-tuple; `process_trip_chain` captures both players' flags per index and calls `apply_copy_effect` for each valid copier. The log entry of the copier gets the note `copy_effect — copied "<effect>" (<name>)`.
- **Replay**: the replay mirrors the copy in `_replay_trip_chain` (pinning the cards a copied zone-effect will move, then calling `ge.apply_copy_effect`).
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
- Log: placement note on the card's line (`🪤 pet_trap — drop token placed on cell N`), trigger note on the arriving player's line (`🪤 drop on cell N — knocked back −K`).

**Replay**: the replay mirrors the instant placement in `_replay_trip_chain` — every pet_trap move-card's token is placed on its owner's **pre-chain** position (the instant effect fired at play time, before any card moved) — and the final **drop tokens are compared** to the stored ones (a warning on divergence, like the zone checks).

**AI**: no mirror needed — the AI does not simulate effects (`PlayerAI` only evaluates conditions for scoring); a pet_trap card is simply worth its base advancing in its current valuation.

**Frontend**: the drop tokens render on the Earth (`.drop-token` at the cell's `POS24` point, `/assets/effect_pettrap.png`); `effectLabel` shows `trap — drop token here`. **Multi-token display**: each trap on a cell is drawn as its own marker in a slightly offset up-right fan (`MAX_FAN = 6` visible per cell, `FAN_STEP_PX = 5` deliberate overlap, z-index increases along the fan) so a stack of N traps reads as N tokens; the top token carries the exact-count badge when N > 1 (with the 6-cap, the badge is the source of truth for N > 6). Preview page: `tests/_drop_tokens_preview.html` (copy it into `game_ui/static/` and screenshot via headless Chrome to eyeball the fan).

### Rooted (effect `rooted`)

The **rooted** effect (self-effect: the card roots itself) gives the playing player a **one-shot rooted token** on that card. The card survives the cleaning phase and sits on a stopover on the board until the end of the **next** turn, when it is discarded.

**Token grant** (at resolution time, in `_resolve_card`, after `apply_effect`):
- When a `rooted` card is resolved (move mode, condition met, not blocked, not effect_canceled), the player gets a rooted token on that card.
- The card is added to `GameState.rooted_this_turn` (a per-turn list).
- **Cooldown**: the card's id is recorded in `GameState.rooted_history` with the current turn number. On the **next** turn, if the same card is played again with its condition met, it **cannot** get another token (the cooldown check in `_grant_rooted_token`). The cooldown expires after the following turn (the card can get a token again on turn N+2).
- **Blocked / not-met / canceled** rooted cards do **not** grant a token (like all effects, the rooted grant is gated on the effect being valid).

**End-of-turn settlement** (`_process_rooted_cards`, called in `_end_turn` after the trip chain, in ALL end-of-turn cases):
1. **Discard last turn's rooted cards**: any cards still in `rooted_on_board` (from the previous turn) are moved to their owner's discard pile (their token was one-shot — it is consumed by surviving one turn).
2. **Place this turn's rooted cards**: the cards in `rooted_this_turn` are pulled out of the discard pile and placed onto the owner's **per-player stopover slots** — the k-th rooted card of a player → that player's **position k** → column `5-k` (`ge._player_stopover` / per-player rule). One card per position (no stacking).
3. **Clear `rooted_this_turn`** (it is a per-turn buffer).

**On the trip chain (per-player model)**:
- A rooted card on the board is a **normal card** that occupies the **leading position** of its owner's trip chain (position 1, 2, …). The owner's plays go **after** the rooted cards (position R+1, …) — so the rooted card **consumes a slot** and the opponent's cards slide past it (per-player positions are independent).
- When resolved (`process_rooted_card`), the rooted card applies **only its basic advancing** (the card's `advancing` field) — **no condition check, no effect application, no grappling/copy** (it is a pure forward move, with the faction biome bonus if on a home biome).
- The rooted card is **BLOCKABLE**: the opponent's defend card at the **same per-player position** can block it (shield race: `Σ shields ≥ mana cost`), canceling its basic advancing and firing the defender's block effect.
- Multiple rooted cards (from different turns) keep their order: the older one (from the previous turn) is **discarded first** (at the end of the current turn's settlement), the newer one (from this turn) takes its place.
- `rooted_on_board` is public board info (visible to both players, safe in both endpoints).

**Model**: the card is **active** on the trip chain (basic advancing, blockable) and sits on the **per-player** position of its owner. The placement is engine-computed (`process_trip_chain`, `_process_rooted_cards`, `ge._player_stopover`).

**AI**: no mirror needed — `rooted` is an effect, and the AI does not simulate effects (`PlayerAI` only evaluates conditions for scoring). A rooted card is simply worth its base advancing in its current valuation (the token is a one-shot bonus the AI does not model). The robot places its cards using `ge._player_stopover` (per-player positions).

**Frontend**: the rooted token image (`/assets/effect_rooted.png`) renders on the card in the trip-chain display when the card is in `rooted_on_board`. The `effectLabel` shows `rooted` for `rooted` effect cards. The stopover board renders **per-player** slots — each player has their own 5 positions (1-5), and the rooted card sits on the owner's position 1 (the plays go after it).

### Wrecking ball (effect `wrecking_ball`)

The **wrecking_ball** effect is an **INSTANT board effect**: it fires **at play time** (inside `_apply_instant_effects`, right after a successful move-mode play) — **before** the trip chain resolves. It therefore **cannot be blocked, effect-canceled or conditioned** (a blocked wrecking_ball card still wrecks; a wrecking_ball played in **defend** mode does nothing). The card itself then resolves in the trip chain as a **no-op** (`apply_effect('wrecking_ball', …)` does nothing; the card only advances by its basic value — `effect_number` is 0 in the pool and unused).

**The wreck** (the instant part):
- **Removes the opponent's dwelling card** — the card sitting in the opponent's **dwelling spot** (`PlayerState.dwelling`, a single card id). If the opponent has no dwelling card (`dwelling is None`), the effect is a no-op.
- The removed dwelling card is **sent to the opponent's discard pile** and the **dwelling slot is cleared** (`dwelling = None`). The **placeholder** (`dwelling_slot`) **stays in place** (see below).
- **The placeholder STAYS IN PLACE**: when a wrecking_ball removes a dwelling card, **ONLY THE CARD GOES TO THE DISCARD** (`dwelling = None`); the placeholder (`dwelling_slot`) **remains on its stopover slot** — it still occupies the **consumed trip-chain position** (`play_count` is NOT released — the owner's **next play lands on the position AFTER the placeholder**, e.g. the owner placed the black_hole on position 1 / stopover_4, the wreck removes the card, and the owner's next play lands on position 2 / stopover_3) and **visually fills the slot until the cleaning phase clears it** (like every placeholder — "stays in place" = for the rest of the turn, not permanently). The action is annotated with `dwelling_removed` / `dwelling_removed_from` (as before) and logged (`💥 wrecking_ball — removed <name>'s dwelling card <id>` — the log lines are kept short and simple). **No AI mirror needed** (the robot never plays wrecking_ball, and placement is engine-computed). **Replay**: the wreck mirror keeps the placeholder in the replay's local state — the replay reads the RECORDED `to` values of plays, never re-derives them from `play_count`, so no pin is needed (verified by `tests/_wrecking_placeholder.py`).
  - **Edge case (accepted)**: if the owner **re-places a dwelling** after the wreck (same turn), the new placeholder takes the NEXT position (the single `dwelling_slot` field points at the new slot) and the old ghost placeholder is no longer *displayed* — but its position stays **consumed** (`play_count` counts both), so the plays after it land correctly (a visual-only gap in a rare edge case).
- The engine annotates the action with `dwelling_removed` (the card id) and `dwelling_removed_from` (the opponent's name) so the turn log can show it (`💥 wrecking_ball — removed <name>'s dwelling card <id>`).

**The dwelling spot** (`PlayerState.dwelling`):
- A per-player slot that holds **one** card id (the dwelling card). It is a separate field from the zones (hand/deck/discard/mana) — a dwelling card is **not** in any zone while it dwells.
- **Placed by the **Engineers' `refinery`** dwelling card (see *Engineers*) — played with `to: "dwelling"` (1 card, costs its `mana_cost`), a play-phase action. So `dwelling` is no longer always `None`: it holds the refinery (or any future dwelling card) until wrecking_ball removes it.
- **The removal system is live**: wrecking_ball removes the dwelling card (the engine checks `opponent.dwelling` and, if set, sends it to the discard + clears the slot).

**Replay**: the replay mirrors the instant removal in `_replay_trip_chain` and keeps the placeholder in its local state (see the *The wreck* bullet above).

**AI**: no mirror needed — `wrecking_ball` is an effect, and the AI does not simulate effects (`PlayerAI` only evaluates conditions for scoring). A wrecking_ball card is simply worth its base advancing in its current valuation (the wreck is a one-shot bonus the AI does not model; it is a real play today, since the Engineers' refinery can occupy the dwelling spot).

### Swap cards (effect `swap_cards`)

The **swap_cards** effect is an **INSTANT chain effect**: it fires **at play time** (inside `_apply_instant_effects`, right after a successful **move-mode** play) — **before** the trip chain resolves. It therefore **cannot be blocked, effect-canceled or conditioned** (a blocked swap_cards card still swaps; a swap_cards card played in **defend** mode does nothing). The card itself then resolves in the trip chain as a **no-op effect** (`apply_effect('swap_cards', …)` does nothing — the swap already happened at play time) and **advances by its basic value** (with the faction biome bonus if on a home biome) from its **new (swapped)** position. `effect_number` is 0 in the pool and unused.

**The swap** (the instant part):
- The player chooses the target with **`message["swap_with"]`** (an int 1..5 = a **position of the player's OWN trip chain** — validated by `message_check`). The target can be **any entry of the owner's own chain**: a **play** (move or defend), a **board placeholder** (a doctor pending placeholder or a dwelling placeholder), or a **rooted card** on the board.
- **The two entries exchange their stopover columns at play time**: the played card's recorded `to` becomes the target's stopover, and the target's slot becomes the played card's original stopover. The trip chain then resolves **by the recorded columns** (positions, facing, blocking, grappling/copy all read the `to` values — `ge._player_chain`), so the **swapped order is applied automatically**: resolution order (the card now resolves earlier/later), facing (it now faces the opponent's card at its new position), and blocking (defend cards on the new stopover now block it).
- **`swap_with` is OPTIONAL** — a swap_cards card played without it **simply advances as usual** (no swap; the condition/effect still apply at resolution). This is what the robot does (it never sends `swap_with`).
- **The card still applies its condition/effect at resolution** (from the swapped position) — the swap is an instant play-time effect, like every other instant. A swap_cards card with a condition that **fails** still advances by the **reduced amount** (mana − 1) from its swapped position; the swap itself is not rolled back.
- **The engine annotates the action** with `swapped_with` (the target's card name / id), `swapped_with_kind` (`'play'` / `'pending'` / `'dwelling'` / `'rooted'`) and `swapped_from` (the played card's original stopover) — recorded on the **shared action dict** (so it lands in `action_chain`, `messages_history`, and the turn log). The log note: `🔁 swap_cards — <card> swaps places with <target>`.
- **Pre-validation**: `player_play` pre-checks the swap target **before** any state change (an invalid target → the play is **rejected with no state change**; no partial swap). The swap is applied after the play succeeds (inside `_apply_instant_effects`).
- **The swap is a pure position exchange** — it does NOT change the card's hand/deck/mana zones, its cost (already paid), or its condition/effect. It only changes **where on the trip chain the card sits**.

**Implementation**:
- `ge._find_swap_target(player, position, current_game)` — resolves a position (1..5) to the owner's chain entry: `('play', action_dict)` / `('pending', [card, slot])` / `('dwelling', dwelling_card_id)` / `('rooted', rooted_entry)`. Reads the owner's `action_chain`, `pending_slots` (`[card, slot]` pairs), `dwelling_slot`, and `game.rooted_on_board` (owner-filtered).
- The swap branch in `_apply_instant_effects` (the `swap_cards` case) — exchanges the columns in place (rewrites the action's `to`, the pending pair's slot, the `dwelling_slot`, or the rooted entry's `stopover`) and annotates the action.
- `apply_effect('swap_cards', …)` is a **no-op marker** (the swap already fired at play time — the trip chain only advances the card).
- **Sync-back**: `p.dwelling_slot` is in the `player_play` sync-back loop (the WS copy path) — without it the dwelling-target swap would be lost on the copy (caught by `tests/_swap_cards_e2e.py` game 2).

**Note**: `swap_with` is optional — without it the card simply advances by its basic value from its normal position (no swap).

**AI**: no change — `PlayerAI` does not simulate effects (it only evaluates conditions for scoring) and **never sends `swap_with`**, so the robot simply plays swap_cards as a normal advancing card. (A future enhancement: the robot could choose a swap target.)

**Replay**: the swap mirror in `analyze_game`'s play-loop: the played card's recorded `to` is **already post-swap** in `messages_history` (the engine rewrote the action in place, and `messages_history` shares the dict) — so the play and a **play** target need nothing. Only a **placeholder/rooted target** (which the replay reconstructs itself) is mirrored: the replay rewrites the pending `[card, slot]` pair's slot (or `dwelling_slot`, or the rooted entry's `stopover`) to the played card's original column (`swapped_from`). The trip chain then resolves the swapped order via `ge._player_chain` (the same function the engine uses). Records a `swap_cards` event.

**Frontend** (`game_ui/static/`): `isSwapCards(id)` in `cards.mjs`. `dispatchPlay` in `actions.mjs` has a **swap_cards branch** (after the doctor/mage branches, before the normal move) that opens `showSwapPopup` — a **stopover mirror** (5 positions, the player's own chain: plays + placeholders + rooted) where the user **drag-and-drops** the played card onto a target (or **clicks** a target) to exchange places, or clicks **"Play without swap"** to skip. The chosen position is sent as `swap_with` (a 10th param of `sendAction` in `comm.mjs`, sent as `msg.swap_with`). Chains with `showPendingPopup` (a swap_cards card can also attach a pending card — the pending popup opens after the swap choice). `effectLabel` in `log.mjs` shows `swap — exchanges its chain position at play time`. Styles: `.swap-grid` / `.swap-cell` / `.dragover` in `style.css`. See `game_ui/static/app-js-overview.md`.

**Tests**:
- `tests/_swap_cards.py` (56 engine-level checks: all 4 target types (play/pending/dwelling/rooted) + edge cases: optional `swap_with` (no swap), defend no-op, self-swap no-op, invalid target rejection (no state change), log note, card conservation; deletes its own games)
- `tests/_swap_cards_e2e.py` (27 checks through the real `handle_websocket_message` entry point with `model_copy()`: game 1 = play↔pending swap (annotations, position rewrites, chain resolution, conservation); game 2 = play↔dwelling swap (the `dwelling_slot` sync-back through the copy path); deletes its own games)
- `tests/_swap_cards_replay.py` (12 checks: the replay mirrors the swap — the SWAP card resolves at its swapped position (position 1), the pending placeholder at position 2, `verified=True`, the `swap_cards` event recorded with the pending target, no unknown-card warnings; deletes its own game)

### Discard selection (effects `discard` / `discard_oppo` + the pending `bloodtest`)

The **discarding player chooses which cards** are discarded, instead of the engine auto-discarding the last N of the hand. This is the first **mid-trip-chain pause**: the chain stops, the player sends one more message, and the chain resumes.

Three sources trigger the same pause/choice flow:
- the `discard` effect (target = the actor),
- the `discard_oppo` effect (target = the opponent),
- the Doctors' pending **`bloodtest`** card attached to a play: it is **treated as `discard_oppo` with n = 1** — the OPPONENT discards 1 card and CHOOSES which (the attaching player's own hand is never touched). See *Pending cards* in [sup_fact_agent.md](sup_fact_agent.md).

**Flow** (both `discard` and `discard_oppo`; the *target* is the player who discards — the actor for `discard`, the **opponent** for `discard_oppo`):
1. The card resolves as usual: condition check → **its basic advancing is applied first** (the `discard`/`discard_oppo` effect does not handle movement, so `process_advancing` runs) → then `apply_effect` is reached.
2. `apply_effect` sets `GameState.pending_discard = {player, n}` (where `n = min(abs(effect_number), len(target.hand))`) and the source card's log entry is marked `_pending_discard`. The pending **`bloodtest`** sets the same marker itself: when the attached play resolves with its effect fired, `_apply_pending_effect` sets `pending_discard = {opponent, 1}` and marks the play's log entry `_pending_discard` (opponent hand empty → no pause, a no-op note is logged instead). The chain **pauses at the next boundary**: `process_trip_chain` (now a stage machine — see below) saves its full context in `GameState.chain_resume` (index, stage, both players' entries + flags) and sets the state to `"turn N - waiting for NAME to discard K card(s)"`.
3. The discarding player answers with `{"cards": […K…], "to": "discard_pile", "mode": "", "pendings": []}` — **exactly K** distinct cards from **their** hand. The engine (2.0 branch of `handle_websocket_message`) validates (sender must be the target, `to` must be `discard_pile`, count must equal K, all cards must be in hand), applies the choice (hand → discard pile, turn-log note on the source card's line), clears `pending_discard`, and **resumes** the chain with `process_trip_chain(resume=ctx)`.
4. If the resumed chain hits **another** discard effect, it pauses again (the same state string can recur — e.g. two discard cards in one turn). When the chain finally completes (no `chain_resume`), the engine runs the turn-end block (`_end_turn`: rooted settlement, deadlock check, draw 3, day/night flip, turn-order reversal, turn+1).

**Rules / invariants**:
- **Only the discarding player may answer**; a message from the opponent (or with the wrong `to`/count, or a card not in hand) is **rejected** with no state change — the chain stays paused.
- **Card conservation holds in every end state**, including a **mid-chain win**: if a win occurs, `flush_unresolved` clears `pending_discard` (the pause is abandoned, the would-be-discarded cards **stay in the hand** — they were never removed) and flushes the unprocessed played cards to their owners' discard, exactly as before.
- **`n = 0` never pauses**: if the target's hand is empty (or `effect_number` is 0), `apply_effect` falls through to the legacy path and nothing is discarded.
- **The stage machine** (`process_trip_chain`): the trip chain was refactored from a linear loop into an explicit stage machine with a `ctx` dict persisting `{index, stage, entries, flags}` across the pause. Stages per index: `first` → `second` → `log` → `copy`. The `discard` boundary check runs at each stage boundary; on a pending discard it calls `pause_discard(ctx)`. **Per-index flags are explicitly reset to defaults at every index transition** (in the `log` stage) — a `setdefault` would leave stale values from the previous index and cause an `IndexError`.
- **`GameState.pending_discard`** (`{player, n}`) and **`GameState.chain_resume`** (the saved `ctx`) are new model fields (`models.py`), both `Optional` (default `None`) so old saved games are unaffected.

**AI**: `PlayerAI.choose_discard(num_cards)` picks the **N least-valuable** hand cards (it does not simulate effects — it just minimizes the value of what it gives up) and returns the `{cards, to:"discard_pile", mode:""}` message. `game_ui/ai_driver.py` matches the discard-wait state with `DISCARD_RE` and routes to `choose_discard`. The Robot answers **every** discard pause (deliberately **not** gated on `st['acted']` — the same state string can recur in a turn with two discard effects).

**Frontend** (`game_ui/static/`): `detectPhase` returns `{kind:"discard", turn, actor, n}` for the discard-wait state; `renderAll` shows a red **DISCARD** button (`#btn-discard`, the only button visible) enabled iff `selected.size === n`; `makeHandCard` allows **multi-select** in discard mode (toggle, capped at N, toast on overflow); the button handler validates phase/count and sends `{cards:[…], to:"discard_pile", mode:""}`, clearing the selection only on success. See `game_ui/static/app-js-overview.md`.

**Tests**: `tests/_choose_discard.py` (5 scenarios: basic n=1, discard_oppo, n=2, chain continuation with two consecutive discard pauses). Deletes its own games.

**Replay**: the replay mirrors the pause/resume: it recognizes the `discard_pile` choice messages, applies the chosen cards via `_apply_discard_choice`, and **does not** apply a discard choice when `game.state == "game over"` (mid-chain win: the real engine abandoned the pause and the cards stayed in the hand). For a `bloodtest`-set pause the replay consumes the `pending_discard` left by the real `process_card` and applies the choice itself (`_pick_discard` picks the identity-consistent card from the opponent's reconstructed hand).
