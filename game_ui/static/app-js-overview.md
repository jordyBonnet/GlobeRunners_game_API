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
| `cards.mjs` | Card data (main + support pools), lookup helpers, UI constants, deck-size constants, `buildSupportDeck` | `CARDPOOL`, `SUPPORT`, `loadCardpool`, `loadSupportCards`, `cardInfo`, `cardTitle`, `cardImg`, `cardCost`, `cardName`, `ENGINEER_DROPS`, `ENGINEER_DWELLING`, `isEngineerDrop`, `isEngineerDwelling`, `FACTIONS`, `SUPPORT_FACS`, `getFactionKey`, `dwellingPlaceholderSrc`, `buildSupportDeck`, `MAIN_DECK_SIZE`, `SUPPORT_DECK_SIZE` |
| `phase.mjs` | Phase detection (parses the engine `state` string) + whose turn | `detectPhase`, `myTurn`, `START_MANA_N` |
| `game.mjs` | Game-page state object, engineer cell-selection mode, `enterGame` | `game`, `cellSelect`, `enterCellSelect`, `exitCellSelect`, `confirmCell`, `enterGame` |
| `comm.mjs` | WebSocket + polling + the `applyState` render funnel | `connectWs`, `sendAction`, `setConn`, `startPolling`, `stopPolling`, `applyState`, `wsUrl` |
| `state.mjs` | `renderAll` + the client-side mana guard | `checkMana`, `renderEnv`, `highlightValidCells`, `renderAll` |
| `zones.mjs` | Zone panels & side rows (hand, mana backs, dwelling/pendings/deck/discard) | `publicCount`, `renderOppZone`, `renderMyZone`, `renderSideRows`, `makeStaticCard` (also toggles the `#dz-mana.mana-waiting` purple glow) |
| `board.mjs` | The Earth ring + stopover slots + tokens/drops/markers | `BIOME_NAMES`, `N_CELLS`, `N_STOPOVERS`, `slotEls`, `POS24`, `buildBoard`, `renderBoard` |
| `actions.mjs` | Stopover ordering + the unified play dispatch | `playedCount`, `nextSlotCol`, `actionStopoverNum`, `canDropOnStopover`, `playCardToStopover`, `orderHint`, `dispatchPlay` |
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
| Stopover ordering: k-th play of the turn (move **or** defend) → `stopover_{5-k}` | `actions.mjs` `playedCount()`, `nextSlotCol()`, `canDropOnStopover()` |
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
- `cardCost(id)` → `mana` or `mana_cost`.
- `cardName(id)` → short name.
- `effectLabel(id)` (in `log.mjs`) → pretty effect label for the log (`advancing +2`, `recoil 1`, …).
- `getFactionKey(factionName)` → short key ("Dwarves" → "Dwa") from the `FACTIONS` table.
- `dwellingPlaceholderSrc(factionName)` → `/cards_ex/placeholder_<key>.png` (faction-specific placeholder art for the dwelling card in the stopover row).

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
`#btn-play` button and a click/drop on a stopover slot. It handles the three
engineer cases (drop → cell select, refinery → dwelling zone, normal → stopover):

| Phase | Selection | Message |
|---|---|---|
| `init-mana` | up to 3 (sent **one by one**) | `{cards:[id], to:"mana", mode:""}` |
| `mana-pass` | exactly 1 | `{cards:[id], to:"mana", mode:""}` |
| `play` + engineer **drop** | 1 → `enterCellSelect` → cell click | `{cards:[id], to:"stopover_N", mode:"move", cell:<0-23>}` |
| `play` + **refinery** (dwelling) | 1 | `{cards:[id], to:"dwelling", mode:""}` (slot must be empty) |
| `play` + normal card | 1 | `{cards:[id], to:"stopover_N", mode:"move"}` |
| defend button | 1 (support cards excluded) | `{cards:[id], to:"stopover_N", mode:"defend"}` |
| pass button | — | `{cards:[], to:"", mode:"pass"}` — **mana phase: pass is always allowed** (the engine decides whose pass it is); **play phase: gated on `myTurn()`** (2026-09-04 fix: `myTurn()` is false in the "mana-pass" phase because `detectPhase` returns the string `"mana-pass"` — the old handler used `myTurn()` as a global guard, making the mana-phase pass a silent no-op and stalling the game) |
| **Tap dwelling** button | — | `{cards:[], to:"dwelling", mode:"dwelling_activation"}` (free, once/turn; button visible iff `me.dwelling && !me.dwelling_tapped` and my play turn) |
| **DISCARD** button (discard selection, engine_version ≥ 13) | exactly N (multi-select, N from the phase) | `{cards:[…N…], to:"discard_pile", mode:""}` — the trip chain is PAUSED mid-resolution waiting for this; only the discarding player's answer is accepted; the button is the only one shown and is enabled iff `selected.size === N` |

Every send is preceded by the same guards: my turn → slot order (`playedCount < 5` +
`nextSlotCol`) → `checkMana(id)`.

## Rendering invariants

- **Full re-render per state**: `applyState → renderAll` rewrites the zones, board rows and slots from scratch (the DOM is treated as a function of `game.state`). The exceptions are deliberately transient/incremental: the **log** (`game.logTurnsRendered`, `log.mjs`), the **selection/cell-select** UI, the **trip-chain animation** (its `.chain-anim-card` clones are spared by `renderBoard`'s slot wipe — `.played:not(.chain-anim-card)` — and torn down by `clearChainAnim` / the run's final step; `chainGen` invalidates pending timers, `chainanim.mjs`), the **player markers** (`.marker[data-player=…]` are repositioned across renders so their CSS left/top transition animates the advance — `board.mjs`), and the **card-flight clones** (`.card-fly`, self-removing, never part of the rendered state — `anim.mjs`).
- **Delegated hover**: card hover previews are wired on `document` (mouseover/mouseout + `.card[data-hover-id]`), not per element — required because re-renders destroy per-element listeners. New card elements **must** keep `data-hover-id`. (in `modals.mjs`)
- **Art fallback**: every `<img>` gets `onerror → /placeholder.svg`; support art via `cardImg()` (`card_path`), main art `<card_id>.png`. (helper `cardEl()` in `cards.mjs`)
- **Board tokens**: positions come from `POS24` (polar ring, radius 49 %, cell 0 at top, clockwise); stacks of trap/drop tokens fan up-right (`MAX_FAN`, `FAN_STEP_PX`) with a count badge on the top one. (`board.mjs`)
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
