# `game_ui/static/` — frontend architecture overview (ES modules)

> **Contract: this file is the living map of the frontend.** Before any
> non-trivial change to the `game_ui/static/*.mjs` modules, read this file; after
> the change, update it in the same commit (new function / changed control flow /
> new message shape / new UI mode / renamed or removed function / new module /
> changed import graph). Trivial fixes (typo, debug line, CSS class tweak) do not
> require a doc update.

The frontend is a set of **vanilla ES modules** (no framework; `import`/`export`,
`"use strict"`). The entry point is **`app.mjs`**, loaded from `index.html` via
`<script type="module" src="/static/app.mjs">`. Importing `app.mjs` pulls the whole
dependency graph; `boot()` (in `setup.mjs`) wires the setup page and loads the card
pools. The old monolithic `app.js` was split into these modules — each file is now
self-contained and owns exactly one concern.

It drives the whole web UI of the game (served by `game_ui/app.py` on port 8001):
the setup/launch page, the game page (board, zones, trip chain, log) and the two-way
communication with the engine.

## Module map

| Module | Responsibility | Key exports |
|---|---|---|
| `utils.mjs` | Tiny DOM/fetch helpers shared by everything | `$`, `$$`, `toast`, `api` |
| `cards.mjs` | Card data (main + support pools), lookup helpers, UI constants, deck-size constants, `buildSupportDeck` | `CARDPOOL`, `SUPPORT`, `loadCardpool`, `loadSupportCards`, `cardInfo`, `cardTitle`, `cardImg`, `cardCost`, `cardName`, `earthBgSrc`, `ENGINEER_DROPS`, `ENGINEER_DWELLING`, `isEngineerDrop`, `isEngineerDwelling`, `DOCTOR_PENDING`, `DOCTOR_DWELLING`, `isDoctorPending`, `isDoctorDwelling`, `FACTIONS`, `SUPPORT_FACS`, `getFactionKey`, `dwellingPlaceholderSrc`, `factionLogoSrc`, `buildSupportDeck`, `MAIN_DECK_SIZE`, `SUPPORT_DECK_SIZE` |
| `phase.mjs` | Phase detection (parses the engine `state` string) + whose turn | `detectPhase`, `myTurn`, `START_MANA_N` |
| `game.mjs` | Game-page state object, engineer cell-selection mode, `enterGame` | `game`, `cellSelect`, `enterCellSelect`, `exitCellSelect`, `confirmCell`, `enterGame` |
| `comm.mjs` | WebSocket + polling + the `applyState` render funnel | `connectWs`, `sendAction`, `setConn`, `startPolling`, `stopPolling`, `applyState`, `wsUrl` |
| `state.mjs` | `renderAll` + the client-side mana guard | `checkMana`, `renderEnv`, `highlightValidCells`, `renderAll` |
| `zones.mjs` | Zone panels & side rows (hand, mana backs, dwelling/pendings/deck/discard) | `publicCount`, `renderOppZone`, `renderMyZone`, `renderSideRows`, `makeStaticCard` (also toggles the `#dz-mana.mana-waiting` purple glow) |
| `board.mjs` | The Earth ring + stopover slots + tokens/drops/markers | `BIOME_NAMES`, `N_CELLS`, `N_STOPOVERS`, `slotEls`, `POS24`, `buildBoard`, `renderBoard` |
| `actions.mjs` | Stopover ordering (**per-player** positions, engine_version 15) + the unified play dispatch | `playedCount`, `rootedCount`, `nextPosition`, `occupiedCols`, `freeCols`, `nextSlotCol`, `actionStopoverNum`, `canDropOnStopover`, `playCardToStopover`, `orderHint`, `dispatchPlay` |
| `interaction.mjs` | The hand card element (click-select, drag & drop, playable outline) | `makeHandCard` |
| `anim.mjs` | Card-movement ("where do my cards go") flight animation | `FLY_MS`, `FLY_STAGGER_MS`, `REDUCED_MOTION`, `snapshotZones`, `planPlayerMoves`, `flyCard`, `flipMyHand`, `popNewBoardTokens`, `animateZoneTransitions` |
| `log.mjs` | The game-log panel renderer | `effectLabel`, `stopoverLabel`, `makeLogCardEl`, `makeTag`, `buildLogEntryEl`, `buildLogTurnEl`, `fitLogWidth`, `renderLog` |
| `chainanim.mjs` | Trip-chain resolution animation (slot pulses + thread) | `clearChainAnim`, `placeThread`, `playChainAnim` |
| `modals.mjs` | Hover preview, card zoom modal, server message, endgame, leave | `showCardHover`, `hideCardHover`, `showCardModal`, `showServerMsg`, `showEndgame`, `leaveGame` |
| `setup.mjs` | Setup page (deck building, factions, launch, waiting) + `boot()` | `setup`, `initSetup`, `boot` |
| `app.mjs` | **Entry point** — imports `setup.mjs` (pulls the graph) and calls `boot()` | — (side effect) |

### Import graph (safe circularity)

The modules are **heavily cross-importing** and the graph is **circular** (e.g.
`game ↔ comm ↔ state`, `actions ↔ board ↔ zones`, `setup ↔ game`). This is safe in
ES modules **because no module reads another module's `const` at top level** — every
cross-module reference is inside a **function body** (evaluated lazily, after the
whole graph has finished evaluating). Top-level statements are limited to:
self-contained `const` initializers, DOM wiring in `modals.mjs` (the `<script>` is
deferred, so the DOM is ready), a `resize` listener in `log.mjs`, and the `boot()`
call in `app.mjs`. **Keep it that way:** if you add a top-level statement that reads
an imported `const`, you can hit a TDZ error — move it inside a function.

## Ground rules it mirrors from the engine

These invariants are **re-implemented client-side** — keep them in sync with the
engine (`engine/game_engine.py`) and with `game_ui/ai_driver.py`:

| Invariant | Where |
|---|---|
| Player message shape `{cards, to, mode, pendings[, cell]}` | `comm.mjs` `sendAction()` |
| Card cost = main card `mana`, support card `mana_cost` (engine_version ≥ 12) | `cards.mjs` `cardCost()` + `state.mjs` `checkMana()` |
| Stopover ordering: **PER-PLAYER** (engine_version 15) — each player has their OWN 5 positions (1..5, columns 4,3,2,1,0). A player's rooted cards occupy the leading positions of that player's chain (position 1, 2, …) and the player's plays (move OR defend) go AFTER the rooted cards (position R+1, …). A player's rooted cards NEVER shift the opponent's positions. **The dwelling placeholder ALSO consumes one position** (the engine increments `play_count` when the refinery is placed, and `playedCount()` counts it via `p.dwelling && p.dwelling_slot != null`) so the next play lands on the position AFTER the placeholder | `actions.mjs` `playedCount()`, `rootedCount()`, `nextPosition()`, `freeCols()`, `nextSlotCol()`, `canDropOnStopover()` — mirrors `ge._player_stopover` / `ge._player_chain` (engine = single source of truth; the robot calls it directly in `ai_driver.py`) |
| Only 1 or 3 cards per mana message during init → init sends one at a time | `actions.mjs` `dispatchPlay()`, `init-mana` branch |
| Hidden info: opponent only seen via `*_count` and masked ids | `zones.mjs` `publicCount()`, `renderOppZone()` (card backs) |
| Deck = 20 main + 10 support, shuffled together before sending | `setup.mjs` `launch()` (`shuffleDeck`) |
| Duplicate main card ids rejected at import | `setup.mjs` `parseCsv` + CSV `onchange` toast |
| Discard selection (engine_version ≥ 13): `discard`/`discard_oppo` pause the chain — the discarding player picks exactly N hand cards, sent as `{cards:[…N…], to:"discard_pile", mode:""}`; only the discarding player may answer | `phase.mjs` `detectPhase` (`kind:"discard"`), `actions.mjs` `#btn-discard` handler, `interaction.mjs` `makeHandCard` multi-select |

## Global state (now module-scoped)

State lives in the owning module and is imported by its consumers (no `window.*`):

| Object | Module | Contents |
|---|---|---|
| `CARDPOOL` / `cardpoolLoaded` | `cards.mjs` | main card pool: `card_id -> row` (fetched once from `GET /cardpool`) |
| `SUPPORT` / `supportLoaded` | `cards.mjs` | support cards: `card_name -> row` (fetched once from `GET /support_factions`) |
| `FACTIONS` / `SUPPORT_FACS` | `cards.mjs` | the 6 main factions (key, name, logo) + `getFactionKey(name)` + `dwellingPlaceholderSrc(name)` (→ `/cards_ex/placeholder_<key>.png`); engineers/mages/doctors (banner) |
| `ENGINEER_DROPS`, `ENGINEER_DWELLING` | `cards.mjs` | UI constants for the 4 engineer drop cards (image + label) + the `refinery` dwelling card |
| `DOCTOR_PENDING`, `DOCTOR_DWELLING` | `cards.mjs` | UI constants for the 4 doctor pending cards (label only) + the `laboratory` dwelling card (engine_version 16) |
| `MAIN_DECK_SIZE` / `SUPPORT_DECK_SIZE` | `cards.mjs` | 20 / 10 (deck-size constants) |
| `setup` | `setup.mjs` | setup-page state: `deck` (20 main ids), `supportDeck` (10 support names), `name`, `mode` (`create\|ai\|join`), `faction`, `support`, `gameId` |
| `game` | `game.mjs` | game-page state: `id`, `me` (player name), `ws`, `state` (last **personalized** state), `selected` (Set of hand card ids), `dragCardId`, `pollTimer`, `lastWinnerShown`, `logTurnsRendered` (incremental log) |
| `cellSelect` | `game.mjs` | engineer drop placement mode: `{active, cardId}` |
| `slotEls` / `POS24` | `board.mjs` | DOM refs to the 2×5 stopover slots + the 24 earth-cell positions (`{x%, y%}`), built once by `buildBoard()` |
| `pendingResolvers` | `comm.mjs` | queue of Promise resolvers — serializes "send action → await reply" over the WS |
| `chainGen` | `chainanim.mjs` | generation counter that invalidates pending trip-chain animation timers |

Helpers: `$` / `$$` (querySelector shorthands), `toast()`, `api()` (fetch + JSON + error unwrap) — all in `utils.mjs`.

## Card data helpers (single lookup path — `cards.mjs`)

All card display goes through these — **never** read `CARDPOOL` directly for a
card that might be a support card:

- `cardInfo(id)` → row from `CARDPOOL` **or** `SUPPORT`, or null.
- `cardTitle(id)` → human title (support cards show `card_name (⚙ faction) — description`).
- `cardImg(id)` → `/art/<card_path>` for support cards, `/art/<card_id>.png` for main cards (the file name differs from the card name for support cards — that's why this helper exists).
- `earthBgSrc(earth)` → the Earth background image for a game, **derived from the board's biome order** (`earth` = `state.earth`, the engine's `biomes_order`): cell 0 is at the top of the ring (clockwise — `POS24`), so segment 0 (cells 0–5) is the image's **top-right quadrant**; the four configs cover one starting biome each — `DE→cfg1, MO→cfg2, OC→cfg3, JU→cfg4` → `/assets/earth_cgf<N>_nomarker.png`. The starting quadrant is always correct; the whole board matches 4/4 when the cyclic order matches the asset (12 of the 24 possible orders — the assets only contain 2 cyclic classes). Falls back to `/assets/earth_background_playmat.png` (the default in `index.html`) when the board is unknown.
- `cardCost(id)` → `mana` or `mana_cost`.
- `cardName(id)` → short name.
- `effectLabel(id)` (in `log.mjs`) → pretty effect label for the log (`advancing +2`, `recoil 1`, …).
- `getFactionKey(factionName)` → short key ("Dwarves" → "Dwa") from the `FACTIONS` table.
- `dwellingPlaceholderSrc(factionName)` → `/cards_ex/placeholder_<key>.png` (faction-specific placeholder art for the dwelling card in the stopover row).
- `factionLogoSrc(factionName)` → `/assets/logo_<key>.png` (the faction logo used as the player token on the Earth ring; `null` if the faction is unknown).

## The three launch branches (`setup.mjs` `launch()`)

| Mode | Request | Then |
|---|---|---|
| `ai` | `POST /create_game_ai` `{name, deck}` | straight into `enterGame()` |
| `create` | `POST /create_game` `{name, deck}` | shows the game id (copyable) + `startWaitingForOpponent()` → `enterGame()` when the 2nd player joins |
| `join` | `POST /join_game/{id}` `{name, deck}` | `enterGame()`; a **409** (already in the game, page reload) is swallowed if the player is present in `GET /game/{id}` |

`deck` is always the **shuffled 30-card mix** (20 main + 10 support) — the server validates it via `check_deck`.

Deck building (`setup.mjs`): `buildStarterDeck` (deterministic per name via FNV-1a
seed + LCG; rare cards weighted 1 vs 3; 20 unique main cards), `parseCsv` (extracts
card_ids from comma/semi/tab files, validates the `XXXn_hex` shape), `shuffleDeck`
(Fisher–Yates). Support deck: `buildSupportDeck` (in `cards.mjs`, 2× each of the 5
unique cards).

## Communication model (important!) — `comm.mjs`

- **WebSocket** (`/ws/{game}/{me}`) is the **only** action channel: `sendAction(cards, to, mode, pendings, cell)` → Promise. Sends are **serialized** through `pendingResolvers` (one outstanding request). Replies: a game state (applied via `applyState`) or a rejection `{success:false, message}` (toasted via `showServerMsg`).
- **Polling** (`GET /api/state/{game}/{me}`, every 2.5 s) exists because the WS only *receives* a state when **we** send — polling is how we see the **opponent's** actions. Both feed the same `applyState()` funnel.
- `applyState()` is the single entry to rendering; there is no other path that mutates `game.state` or the DOM game view.

`applyState(st)` (single funnel for WS replies **and** poll responses): **snapshots
the old DOM zones for the card-movement animation** (see `anim.mjs`), resets
cell-select, stores state, `renderAll()` (in `state.mjs`), **flies the
zone-transition cards** (hand→stopover, stopover→discard, deck→hand, …), shows
server message, endgame detection.

`enterGame()` (in `game.mjs`): switch view, connect WS, start polling.

## Action dispatch (what a play sends, per phase) — `actions.mjs` `dispatchPlay`

`dispatchPlay(cardId, col=null)` is the **single** play path used by both the
`#btn-play` button and a click/drop on a stopover slot. It handles the engineer
cases (drop → cell select, refinery → dwelling zone), the doctor cases
(pending → pending zone, laboratory → dwelling zone), and the normal stopover
case (with the pending card selection popup when applicable):

| Phase | Selection | Message |
|---|---|---|
| `init-mana` | up to 3 (sent **one by one**) | `{cards:[id], to:"mana", mode:""}` |
| `mana-pass` | exactly 1 | `{cards:[id], to:"mana", mode:""}` |
| `play` + engineer **drop** | 1 → `enterCellSelect` → cell click | `{cards:[id], to:"stopover_N", mode:"move", cell:<0-23>}` |
| `play` + **refinery** (dwelling) | 1 | `{cards:[id], to:"dwelling", mode:""}` (slot must be empty) |
| `play` + doctor **pending** (epo/virus/bloodtest/mercurochrome) | 1 | `{cards:[id], to:"pending_zone", mode:""}` (engine_version ≥ 16) |
| `play` + **laboratory** (dwelling) | 1 | `{cards:[id], to:"dwelling", mode:""}` (slot must be empty; engine_version ≥ 16) |
| `play` + normal card | 1 | `{cards:[id], to:"stopover_N", mode:"move", pendings:[pending? or []]}` — if the player has pending cards, a **popup** appears to select one (or skip); `pendings` carries the selected pending card name (or `[]` if none) |
| defend button | 1 (support cards excluded) | `{cards:[id], to:"stopover_N", mode:"defend"}` |
| pass button | — | `{cards:[], to:"", mode:"pass"}` — **mana phase: pass is always allowed** (the engine decides whose pass it is); **play phase: gated on `myTurn()`** (2026-09-04 fix: `myTurn()` is false in the "mana-pass" phase because `detectPhase` returns the string `"mana-pass"` — the old handler used `myTurn()` as a global guard, making the mana-phase pass a silent no-op and stalling the game) |
| **Tap dwelling** button | — | `{cards:[], to:"dwelling", mode:"dwelling_activation"}` (free, once/turn; button visible iff `me.dwelling && !me.dwelling_tapped` and my play turn; **laboratory** tap adds an `epo` pending card instead of drawing) |
| **DISCARD** button (discard selection, engine_version ≥ 13) | exactly N (multi-select, N from the phase) | `{cards:[…N…], to:"discard_pile", mode:""}` — the trip chain is PAUSED mid-resolution waiting for this; only the discarding player's answer is accepted; the button is the only one shown and is enabled iff `selected.size === N` |

Every send is preceded by the same guards: my turn → slot order (a **free** position must remain in the player's OWN chain — `freeCols(game.state, game.me).length`; the player's rooted cards occupy the leading positions of that player's chain, so plays go AFTER them — **per-player**, never shared; column from `nextSlotCol`) → `checkMana(id)`. **engine_version 15**: the stopovers are **per-player** — each player has their own 5 positions, and a player's rooted cards (on the board from the previous turn) occupy the leading positions of that player's chain. The engine is the source of truth (it overwrites the client's `to`); the frontend mirrors the rule for display + validation.

## Rendering invariants

- **Full re-render per state**: `applyState → renderAll` rewrites the zones, board rows and slots from scratch (the DOM is treated as a function of `game.state`). The exceptions are deliberately transient/incremental: the **log** (`game.logTurnsRendered`, `log.mjs`), the **selection/cell-select** UI, the **trip-chain animation** (its `.chain-anim-card` clones are spared by `renderBoard`'s slot wipe — `.played:not(.chain-anim-card)` — and torn down by `clearChainAnim` / the run's final step; `chainGen` invalidates pending timers, `chainanim.mjs`), the **player markers** (`.marker[data-player=…]` are repositioned across renders so their CSS left/top transition animates the advance — `board.mjs`), and the **card-flight clones** (`.card-fly`, self-removing, never part of the rendered state — `anim.mjs`).
- **Delegated hover**: card hover previews are wired on `document` (mouseover/mouseout + `.card[data-hover-id]`), not per element — required because re-renders destroy per-element listeners. New card elements **must** keep `data-hover-id`. (in `modals.mjs`)
- **Art fallback**: every `<img>` gets `onerror → /placeholder.svg`; support art via `cardImg()` (`card_path`), main art `<card_id>.png`. (helper `cardEl()` in `cards.mjs`)
- **Earth background = the board's biome configuration**: `renderBoard` sets `#board-bg`'s src via `earthBgSrc(st.earth)` (see above) — the game's random biome order picks one of the four `earth_cgf*_nomarker.png` configs so the artwork matches the board. The src is only touched **when it actually changes** (the image is ~10 MB and the board re-renders on every state poll — a naive set would re-trigger the download each time). `index.html` keeps the plain playmat as the pre-game/default src. (`board.mjs` + `cards.mjs`)
- **Board tokens**: positions come from `POS24` (polar ring, radius 49 %, cell 0 at top, clockwise); stacks of trap/drop tokens fan up-right (`MAX_FAN`, `FAN_STEP_PX`) with a count badge on the top one. Drop tokens are **cleared and redrawn from state on every render** (`.drop-token` elements are NOT persisted — unlike the player `.marker`s): `renderBoard` removes the old ones before redrawing, so a token the engine **consumed** (a player stepped on it / jump-landed on it) disappears on the next poll, and polls don't stack duplicate images. (`board.mjs`)
- **Persistent board cards (survivors of the cleaning phase)**: three game-level / per-player fields render **outside** the `action_chain` into the stopover slots — the **dwelling placeholder** (`p.dwelling_slot`, a faction placeholder image in the slot the refinery/laboratory would have occupied, cleared by the engine in the cleaning phase), **pending placeholders** (`p.pending_slots`, the **per-turn placeholder list** — cleared by the engine in the cleaning phase, so it is **NOT parallel** to the persistent `p.pendings` zone; rendered with the **same faction-specific placeholder image as the dwelling cards** — `dwellingPlaceholderSrc(p.faction)` — with a gold border, engine_version ≥ 16. Entry shapes: **engine_version 19+ = `[card, slot]` pair**; v18 = bare slot index or `null` (the laboratory TAP's `epo` = no placeholder); v16–17 = bare slot. The render loop and `playedCount()` normalize both shapes and skip `null` slots. **engine_version 20: the placeholder of a pending card attached the SAME turn STAYS IN PLACE** (the engine keeps the `[card, slot]` pair in `pending_slots` until the cleaning phase — it marks the consumed trip-chain position, so the next play lands one position further and `playedCount()` — which counts the non-null entries — stays consistent with the engine's `play_count`; the tooltip reads "attached to a card this turn" when the pair's card is an action's `pending_card`). v19 and below removed the placeholder on attachment (the freed position desynced the next-slot mirror — the "next card on the occupied slot" bug in game `8cSTX`)), and **rooted-on-board cards** (game-level `rooted_on_board: [{card_id, owner, stopover}]` — a card that earned a rooted token last turn survives on a stopover until the end of the NEXT turn, engine_version ≥ 10). All three are rendered per owner row in `renderBoard` (stale elements are always removed first — the slots are built once, not rebuilt), all count as a **filled** slot, and none are part of the `anim.mjs` diff (they appear in place, no flight — same as the placeholder). The rooted card gets the green "stuck to the trip chain" frame + the rooted-token chip (`.rooted-on-board`, `.rooted-token` in `style.css`: a **circular dark-disc chip with the tree-with-roots icon + green ring**, top-right corner — a bare transparent PNG at the corner used to blend into the card art) and a `🌱 rooted — …` tooltip. **engine_version 15 (per-player stopovers)**: the rooted card is a **normal card** on the trip chain — it occupies the **leading position** of its owner's chain (position 1, 2, …) and applies its **basic advancing** when the chain resolves (no condition, no effect, but **BLOCKABLE** by the opponent's defend at the same per-player position). The owner's plays go **AFTER** the rooted cards (position R+1, …), so the rooted card **consumes a slot** and the opponent's cards are independent (per-player positions never shift each other). **Per-player rule (the main path)**: a player's next position = `rootedCount(owner) + playedCount(owner) + 1`; column = `5 − position`. The rule is **per-player** (each player has their OWN 5 positions), so the two players' cards may legitimately share a column (e.g. both at `stopover_4`). **Single source of truth: `ge._player_stopover(game, name, played_before)`** in `engine/game_engine.py` (with `ge._player_chain`) — the engine uses it for its own placements (dwelling placeholder slot + rooted end-of-turn placement) and the robot calls it directly (`ai_driver.py`); the **frontend mirrors it in JS** in `actions.mjs` (`rootedCount`/`playedCount`/`nextPosition`/`freeCols`/`nextSlotCol` — it cannot import Python; the mirror was verified case-by-case against the engine). `canDropOnStopover` guards on `freeCols(st, me)`, the defend/Pass-area guards and the engineer-drop `confirmCell` guard do the same, and `orderHint` names the player's next position. **Layered slot (defensive fallback)**: a play and a rooted card can still share a column in old games (engine_version < 15, the shared-stopover model) — in that case `renderBoard` tags the rooted card `.behind` (smaller, nudged up, tilted, z-index 1 under the full-size played card) so the slot reads "your card on top, the last-turn survivor underneath" — and **re-parents the token chip to the slot** (class `.chip-float`, z-index 6) so it escapes the rooted card's low z-index and stays clearly on top of the played card (top-right corner); the render-loop cleanup removes both `.rooted-on-board` elements **and** any orphaned `.rooted-token` in the slot before re-adding everything. (`board.mjs` + `style.css`)
- **Player tokens show the faction logo**: each player's Earth token (`.marker`) renders the player's **faction logo** (`.marker-logo`, `factionLogoSrc(p.faction)` → `/assets/logo_<key>.png`) filling the circle, with the player's **first letter** on top (`.marker-letter`); the me/oppo gradient doubles as the fallback background when the logo is missing (unknown faction → letter-only circle). The marker element is persisted across renders (repositioned, not recreated — see above), so the inner HTML (logo + letter) is rebuilt on every render without affecting the left/top slide transition. (`board.mjs` + `style.css`)
- **Opponent masking**: never render opponent hand/mana/deck *contents* — counts (`*_count` or list length) and card backs only. (`zones.mjs`)
- **Mana-waiting glow**: `#dz-mana` gets the `.mana-waiting` class (a dark↔light-purple `drop-shadow` pulse, `@keyframes manaPulse` in `style.css`) **only** while the phase is `init-mana` or `mana-pass` — toggled in `renderMyZone` from the same `canMana` gate that enables the drag target, so it lights up exactly when a card may be dropped into the mana zone and is removed on every other phase (re-renders are idempotent: `classList.toggle(..., canMana)`). Purely visual. (`zones.mjs` + `style.css`)

### Card-movement animation (`anim.mjs`)

Because `renderAll` rewrites the DOM from state, cards would otherwise pop between
zones; this layer diffs the **previous** and **new** state per player and flies a
lightweight clone (`.card-fly`) from the old zone rect to the new one. Functions (in
order): `snapshotZones()` (captures the CURRENT hand-card rects + occupied stopover
rects **before** `renderAll` — called from `applyState`), `planPlayerMoves(oldP, newP,
isMe, player)` (set-diff of the public `action_chain` → hand→stopover (new plays) &
stopover→discard (cleaning / mid-chain-win flush); dwelling place/wreck; my hand
diffed by **id** (drawn / played / discarded / to-mana), the opponent's by **counts**
(card backs) since the hand is masked; a mana increase with no matching hand loss =
ramp (deck→mana), a mana decrease = taxation (mana→discard); a discard decrease + deck
increase = reshuffle), `sourceRect` / `destRect` (resolve a move to concrete rects —
live DOM for destinations, the pre-render snapshot for sources; **ID GOTCHA: the
stopover rows are `#row-me` / `#row-oppo` and `slotEls.me` / `slotEls.oppo`, while the
zone panels are `#my-*` / `#oppo-*`** — the two row ids differ), `flyCard(fromRect,
toRect, cardId, delay)` (fixed-position clone, 2×rAF then a CSS transform/opacity
transition; `FLY_MS=520ms`, stagger `FLY_STAGGER_MS=110ms`; removed after; face-down
variant `.card-fly.back` for the opponent), `flipMyHand(snap)` (FLIP: the remaining
hand cards slide to their re-centered position instead of jumping),
`popNewBoardTokens` (new pet-trap / engineer drop tokens get the `.token-pop`
animation), `animateZoneTransitions(oldSt, newSt, snap)` (orchestrates: my flights
first, priority order plays → zone shuffling → draws). Constants: `FLY_MS`,
`FLY_STAGGER_MS`, `REDUCED_MOTION` (skip all flights under `prefers-reduced-motion`).
Purely visual — no state, no messages, no engine coupling. **Known approximation**:
when several engine actions land between two observed states (fast polling gaps), the
diff attributes the net change (e.g. a mana increase with no matching hand loss flies
deck→mana instead of hand→mana); the zone counts always end up correct because they
come from the state, only the flight's origin may be a generic back.

### Trip-chain resolution animation (`chainanim.mjs`)

`playChainAnim(t)`, `placeThread(col, firstRow)`, `clearChainAnim()` + `chainGen`
(generation counter). Triggered from `renderLog` when a **genuinely new** turn log
entry appears (`added && start > 0` — never on the first render/rejoin): the stopover
slots pulse in the **exact resolution order** read from the public turn log (the
engine appends the log's stopovers in resolution order and each entry carries `order`
1/2 = first/second player of the turn): stopover by stopover, first player's card
(gold, `chain-first`) then second player's (silver, `chain-second`), ~460 ms per card.
A glowing thread (`.chain-thread`, gold end on the first player's side via
`first-top`/`first-bottom`) bridges the two facing slots while a stopover has both
players' cards. Because the cleaning phase already cleared `action_chain` in the new
state, the played cards are **cloned** onto the slots for the duration of the run
(`.chain-anim-card`, keeps `.played` for layout + `data-hover-id` for the delegated
hover preview) and removed when the run ends. `clearChainAnim()` bumps `chainGen` so
pending `setTimeout(step)` calls are dead on arrival, and removes all pulse classes /
threads / clones. `renderBoard`'s slot wipe uses `.played:not(.chain-anim-card)` so
clones survive mid-run polling re-renders. Purely visual — no state, no messages, no
engine coupling.

## Checklist when modifying the frontend

1. New engine rule / message field (e.g. a new `to:` target, a new board field) → mirror it in: `sendAction` call site (`comm.mjs`), `renderAll`/zone renderers (`state.mjs`/`zones.mjs`/`board.mjs`), **and** the "mirrors the engine" table above.
2. New card type / support faction → extend `cardInfo`/`cardTitle`/`cardImg`/`cardCost` (`cards.mjs`), the UI constants (`ENGINEER_DROPS` pattern) and the button dispatch table (`actions.mjs`).
3. Any change to `renderLog` / `applyState` / `makeHandCard` → keep the incremental + delegation invariants (section "Rendering invariants").
4. New module or new top-level cross-module `const` read → re-verify the import graph is still TDZ-safe (see "Import graph").
5. Update this file in the same commit.

## Change log

- **2026-09-17 — Earth background derives from the board's biome order.** Before, the board always showed the plain `earth_background_playmat.png`. Now `renderBoard` picks the config image matching this game's random biome order — `cards.mjs` `earthBgSrc(earth)` maps the biome of **cell 0** (segment 0 = the image's top-right quadrant, since `POS24` puts cell 0 at the top, clockwise) to `DE→cfg1, MO→cfg2, OC→cfg3, JU→cfg4` (`/assets/earth_cgf<N>_nomarker.png`); the four assets cover one starting biome each, so the starting quadrant is always drawn correctly (12 of the 24 possible board orders match 4/4 — the assets only contain 2 cyclic classes). The src is set only on actual change (~10 MB image, board re-renders per poll). No engine change: `state.earth` (public board info) already carries the biome order. `index.html` cache-bust bumped `?v=21` → `?v=22`.
- **2026-09-17 — v20: same-turn attached pending placeholder STAYS IN PLACE.** Engine rule (`engine_version` 20): when a pending card is placed this turn and then attached to a main card, its `[card, slot]` pair **stays** in `pending_slots` until the cleaning phase (v19 and below removed it — the freed position desynced the frontend's next-slot mirror, which offered the main card's own slot for the next play; game `8cSTX`). Frontend: `board.mjs` placeholder tooltip now distinguishes "attached" (the pair's card is an action's `pending_card`) from "waiting"; `playedCount()` needed **no change** (it counts non-null `pending_slots` entries, which now match the engine's `play_count`). `index.html` cache-bust bumped `?v=20` → `?v=21`.
- **2026-09-17 — fixed: consumed trap/drop tokens stayed on the board (visual bug).** The engine consumes drop tokens correctly (pet_trap `drop_tokens` in `apply_effect('drop')`, engineer `board_drops` in `_trigger_board_drops` — both verified against real game data and the `_pet_trap.py` / `_engineers.py` smoke tests), but the frontend **appended** `.drop-token` elements on every render and **never removed** the old ones — a token consumed by the engine kept showing on the Earth (and every poll stacked another copy of the same token). Fix: `renderBoard` now clears `#markers-layer .drop-token` before redrawing the current set (the player `.marker` persistence for the CSS slide transition is untouched). `popNewBoardTokens` still works: it runs **after** `renderAll` and targets the last-N elements of the fresh set. `index.html` cache-bust bumped `?v=19` → `?v=20`.
- **2026-09-17 — pending placeholders are `[card, slot]` pairs; attachment removes the placeholder BY CARD NAME (engine_version 19 — bug fix).** Bug (game `26_09_17_21_33_39_d5oEd`, turn 4): `p.pendings` is a **persistent** zone but `p.pending_slots` is **cleared every turn** by the cleaning phase — from turn 2 on the two lists are NOT parallel. Attaching an old pending card (low index in `pendings`) spliced `pending_slots` at that index and deleted the placeholder of a card placed the **current** turn (attaching `virus` wiped the mercurochrome placeholder → the "next stopover" hint pointed at slot 2 instead of slot 3). Fix: the engine stores `pending_slots` as `[card, slot]` pairs (v19+) and attachment removes the first pair **matching the card name**; the laboratory tap records no entry (v19; v18 recorded `null`). Frontend: `board.mjs` render loop iterates the entries and normalizes both shapes (pair → `[card, slot]`, legacy → `[null, slot]`, `null` slot skipped; tooltip uses the pair's card name); `actions.mjs` `playedCount()` counts the slot part of each entry (legacy `null`/v18 tap entries still skipped). `index.html` cache-bust bumped `?v=18` → `?v=19`.
- **2026-09-17 — laboratory tap is a QUICK action with NO trip-chain placeholder (engine_version 18).** The lab tap (doctors' dwelling) still adds an `epo` to `p.pendings` (it shows in the pending-zone panel and can be attached to a main card), but the engine now records `pending_slots` with a **`null` entry** (list stays parallel to `pendings`, so the attach-removal by index is unchanged) and consumes **no position** (`play_count` untouched). Frontend: `board.mjs` already skipped `null` slots in its render loop (pairing by index stays correct); `actions.mjs` `playedCount()` now counts only **non-null** `pending_slots` entries (so a lab-tap epo no longer displaces the next play). `index.html` cache-bust bumped `?v=17` → `?v=18`.
- **2026-09-17 — pending placeholders use the dwelling placeholder art.** The doctor **pending** placeholder in a stopover slot (`.pending-placeholder`) now renders the **same faction-specific placeholder image as the dwelling cards** — `dwellingPlaceholderSrc(p.faction)` → `/cards_ex/placeholder_<key>.png` — instead of the pending card's own art (`/art/<card_name>.png`). Visual change only (`board.mjs`); the gold-border `.pending-placeholder` frame and tooltip are unchanged. `index.html` cache-bust bumped `?v=16` → `?v=17`.
- **2026-09-17 — topbar labels: turn number + connection.** `state.mjs` `renderAll` renders the topbar turn label as `Turn N (first player: <name>)` (was `Tour N`) using `st.turn_order[0]`; `comm.mjs` `setConn` shows `● connected to game <id>` (was `● connected`) when `game.id` is set.
- **2026-09-17 — dwelling placeholder now consumes a stopover position (bug fix).** Symptom: on a turn where the player placed the refinery first, the NEXT play was routed to the SAME position as the placeholder (the engine had already incremented `play_count` for the dwelling, so the engine put the next play on position 2, but the frontend still computed position 1 → the user was asked to play on stopover 1 instead of stopover 2). Root cause: `playedCount()` in `actions.mjs` only counted the `action_chain` plays — the dwelling placeholder is a separate field (`p.dwelling` + `p.dwelling_slot`), not an `action_chain` entry, so it was invisible to the position math. Fix: `playedCount()` now adds 1 when `p.dwelling && p.dwelling_slot != null` (the slot is set at placement and cleared in the cleaning phase, so it is non-null exactly while the placeholder is showing this turn). This propagates to `nextPosition()`/`nextSlotCol()`/`freeCols()`/`canDropOnStopover()`/`orderHint()` and the board slot-highlight. `index.html` cache-bust bumped `?v=15` → `?v=16`.

- **2026-09-17 — engine_version 15, per-player stopovers.** The stopover board moved from the **shared** model (v14: both players share 5 columns, skipping board furniture) to a **per-player** model (v15: each player has their OWN 5 positions). A player's rooted cards occupy the leading positions of that player's chain (position 1, 2, …) and the player's plays go after them (position R+1, …); a player's rooted cards never shift the opponent's positions. A rooted card on the trip chain is now a **normal card** (basic advancing only, no condition/effect, but **blockable** by the opponent's defend at the same per-player position). Frontend: `actions.mjs` gained `rootedCount()`/`nextPosition()` and `freeCols()`/`nextSlotCol()`/`occupiedCols()` are now per-player (they take the player name); `canDropOnStopover`/`orderHint`/`dispatchPlay` use the per-player rule; `board.mjs` slot-highlight + `game.mjs`/`zones.mjs` guards pass the player name to `freeCols`. The engine is the source of truth (`ge._player_stopover`/`ge._player_chain`; it overwrites the client's `to`); the robot calls it directly (`ai_driver.py`). Mirrors `ge._player_stopover`.
