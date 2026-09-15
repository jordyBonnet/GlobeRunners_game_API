# `game_ui/static/app.js` — architecture overview

> **Contract: this file is the living map of `app.js`.** Before any non-trivial
> change to `app.js`, read this file; after the change, update it in the same
> commit (new function / changed control flow / new message shape / new UI mode /
> renamed or removed function). Trivial fixes (typo, debug line, CSS class tweak)
> do not require a doc update.

Vanilla JS (no framework, no modules — one global scope, `"use strict"`), ~1500
lines. It drives the whole web UI of the game (served by `game_ui/app.py` on
port 8001): the setup/launch page, the game page (board, zones, trip chain, log)
and the two-way communication with the engine.

## Ground rules it mirrors from the engine

These invariants are **re-implemented client-side** — keep them in sync with the
engine (`engine/game_engine.py`) and with `game_ui/ai_driver.py`:

| Invariant | Where in app.js |
|---|---|
| Player message shape `{cards, to, mode, pendings[, cell]}` | `sendAction()` |
| Card cost = main card `mana`, support card `mana_cost` (engine_version ≥ 12) | `cardCost()` + `checkMana()` |
| Stopover ordering: k-th play of the turn (move **or** defend) → `stopover_{5-k}` | `playedCount()`, `nextSlotCol()`, `canDropOnStopover()` |
| Only 1 or 3 cards per mana message during init → init sends one at a time | `btn-play` handler, `init-mana` branch |
| Hidden info: opponent only seen via `*_count` and masked ids | `publicCount()`, `renderOppZone()` (card backs) |
| Deck = 20 main + 10 support, shuffled together before sending | `launch()` (`shuffleDeck`) |
| Duplicate main card ids rejected at import | `parseCsv` + CSV `onchange` toast |
| Discard selection (engine_version ≥ 13): `discard`/`discard_oppo` pause the chain — the discarding player picks exactly N hand cards, sent as `{cards:[…N…], to:"discard_pile", mode:""}`; only the discarding player may answer | `detectPhase` (`kind:"discard"`), `#btn-discard` handler, `makeHandCard` multi-select |

## Global state (module scope)

| Object | Contents |
|---|---|
| `CARDPOOL` / `cardpoolLoaded` | main card pool: `card_id -> row` (fetched once from `GET /cardpool`) |
| `SUPPORT` / `supportLoaded` | support cards: `card_name -> row` (fetched once from `GET /support_factions`) |
| `FACTIONS` | the 6 main factions (key, name, logo) + `getFactionKey(name)` (full name → short key) + `dwellingPlaceholderSrc(name)` (→ `/cards_ex/placeholder_<key>.png`) |
| `SUPPORT_FACS` | engineers / mages / doctors (banner) |
| `ENGINEER_DROPS`, `ENGINEER_DWELLING` | UI constants for the 4 engineer drop cards (image + label) + the `refinery` dwelling card |
| `setup` | setup-page state: `deck` (20 main ids), `supportDeck` (10 support names), `name`, `mode` (`create\|ai\|join`), `faction`, `support`, `gameId` |
| `game` | game-page state: `id`, `me` (player name), `ws`, `state` (last **personalized** state), `selected` (Set of hand card ids), `dragCardId`, `pollTimer`, `lastWinnerShown`, `logTurnsRendered` (incremental log) |
| `cellSelect` | engineer drop placement mode: `{active, cardId}` |
| `slotEls` / `POS24` | DOM refs to the 2×5 stopover slots + the 24 earth-cell positions (`{x%, y%}`), built once by `buildBoard()` |
| `pendingResolvers` | queue of Promise resolvers — serializes "send action → await reply" over the WS |

Helpers: `$` / `$$` (querySelector shorthands), `toast()`, `api()` (fetch + JSON + error unwrap).

## Card data helpers (single lookup path)

All card display goes through these — **never** read `CARDPOOL` directly for a
card that might be a support card:

- `cardInfo(id)` → row from `CARDPOOL` **or** `SUPPORT`, or null.
- `cardTitle(id)` → human title (support cards show `card_name (⚙ faction) — description`).
- `cardImg(id)` → `/art/<card_path>` for support cards, `/art/<card_id>.png` for main cards (the file name differs from the card name for support cards — that's why this helper exists).
- `cardCost(id)` → `mana` or `mana_cost`.
- `cardName(id)` → short name.
- `effectLabel(id)` → pretty effect label for the log (`advancing +2`, `recoil 1`, …).
- `getFactionKey(factionName)` → short key ("Dwarves" → "Dwa") from the `FACTIONS` table.
- `dwellingPlaceholderSrc(factionName)` → `/cards_ex/placeholder_<key>.png` (faction-specific placeholder art for the dwelling card in the stopover row).

## Section map (top → bottom of the file)

1. **Helpers** — `$`, `toast`, `checkMana` (client-side cost guard before sending), `api`.
2. **Card pool** — `loadCardpool`, the card helpers above, `FACTIONS`.
3. **Support factions** — `SUPPORT`, `loadSupportCards`, `buildSupportDeck` (2× each of the 5 unique cards), `SUPPORT_FACS`.
4. **Deck building** — `buildStarterDeck` (deterministic per name via FNV-1a seed + LCG; rare cards weighted 1 vs 3; 20 unique main cards), `parseCsv` (extracts card_ids from comma/semi/tab files, validates the `XXXn_hex` shape).
5. **Setup page** — `renderFactions`, `renderDeckPreview`, `renderSupportFactions`, `renderSupportPreview`, `updateLaunchBtn` (launch enabled iff name ≥ 2 chars + 20 main + 10 support + game id if joining), `initSetup` (wires all setup DOM: tabs, CSV upload, name, join id, copy, launch), `shuffleDeck` (Fisher–Yates), `launch` (the 3 branches below), `startWaitingForOpponent`/`stopWaiting` (1.5 s poll of `GET /game/{id}` until 2 players).
6. **Game page core** — `enterGame` (switch view, connect WS, start polling), `wsUrl`, `connectWs`, `sendAction`, `setConn` (auto-reconnect after 2 s), `startPolling`/`stopPolling`.
7. **Phase detection** — `detectPhase(st)` (parses the engine's `state` string into `init-mana` / `mana-pass` / `{kind:"play", turn, actor}` / `{kind:"discard", turn, actor, n}` (discard selection, engine_version ≥ 13) / `play-waiting` / `over` / `unknown`) and `myTurn(st)`. **All** button/drag enablement derives from these two.
8. **State application** — `applyState(st)` (single funnel for WS replies **and** poll responses: **snapshots the old DOM zones for the card-movement animation** (see 13c), resets cell-select, stores state, `renderAll()`, **flies the zone-transition cards** (hand→stopover, stopover→discard, deck→hand, …), shows server message, endgame detection) → `renderAll()` (topbar, phase label + action buttons, then the zone/board/log renderers below).
9. **Zones & rows** — `renderOppZone` (counters + card backs + landmine badge), `renderMyZone` (hand, mana backs, drag-and-drop targets, **and** the wiring of `#btn-play` / `#btn-defend` / `#btn-pass` / `#btn-tap` — see below), `renderSideRows` (dwelling / pendings / deck / discard for both players), `makeStaticCard`.
10. **Board** — `renderEnv`, `highlightValidCells`, `buildBoard` (one-shot DOM build: slots + `POS24` ring positions + the 24 invisible `#cell-targets`), `renderBoard` (tokens, pet-trap token fan, engineer board_drops fan, played cards per stopover row, **dwelling placeholder** in col `p.dwelling_slot` (only while it is set: the engine sets it at placement time and clears it in the cleaning phase, so the placeholder only shows during the placement turn and must not reappear at every new turn — stale `.dwelling-placeholder` elements are removed **unconditionally on every render**, before the conditional re-add, since the slots are built once and not rebuilt), slot filled/next classes). Constants: `N_CELLS=24`, `N_STOPOVERS=5`, `POS_RADIUS=49`, `MAX_FAN=6`, `FAN_STEP_PX=5`.
11. **Stopover ordering** — `playedCount`, `nextSlotCol`, `actionStopoverNum`, `canDropOnStopover`, `playCardToStopover`, `orderHint`.
12. **Engineer cell selection** — `enterCellSelect` / `exitCellSelect` / `_resetCellSelect` (toggles `body.cell-selecting`), `confirmCell` (sends the move with `cell`).
13. **Game log panel** — `effectLabel`, `stopoverLabel`, `makeLogCardEl`, `makeTag`, `buildLogEntryEl` (one player's line: order, name, pos, cards, condition/shield tags, effect, negatives, final pos, notes), `buildLogTurnEl`, `fitLogWidth` (caps the right-pinned panel to the free space), `renderLog` (**incremental**: only appends new turns — `game.logTurnsRendered` — so collapse state survives the 2.5 s polling re-renders; first render collapses all but the latest turn).
13c. **Card movement animation** (2026‑09) — the "where do my cards come from / go to" flight. Because `renderAll` rewrites the DOM from state, cards would otherwise pop between zones; this layer diffs the **previous** and **new** state per player and flies a lightweight clone (`.card-fly`) from the old zone rect to the new one. Functions (in order): `snapshotZones()` (captures the CURRENT hand-card rects + occupied stopover rects **before** `renderAll` — called from `applyState`), `planPlayerMoves(oldP, newP, isMe, player)` (set-diff of the public `action_chain` → hand→stopover (new plays) & stopover→discard (cleaning / mid-chain-win flush); dwelling place/wreck; my hand diffed by **id** (drawn / played / discarded / to-mana), the opponent's by **counts** (card backs) since the hand is masked; a mana increase with no matching hand loss = ramp (deck→mana), a mana decrease = taxation (mana→discard); a discard decrease + deck increase = reshuffle), `sourceRect` / `destRect` (resolve a move to concrete rects — live DOM for destinations, the pre-render snapshot for sources; **ID GOTCHA: the stopover rows are `#row-me` / `#row-oppo` and `slotEls.me` / `slotEls.oppo`, while the zone panels are `#my-*` / `#oppo-*`** — the two row ids differ), `flyCard(fromRect, toRect, cardId, delay)` (fixed-position clone, 2×rAF then a CSS transform/opacity transition; `FLY_MS=520ms`, stagger `FLY_STAGGER_MS=110ms`; removed after; face-down variant `.card-fly.back` for the opponent), `flipMyHand(snap)` (FLIP: the remaining hand cards slide to their re-centered position instead of jumping), `popNewBoardTokens` (new pet-trap / engineer drop tokens get the `.token-pop` animation), `animateZoneTransitions(oldSt, newSt, snap)` (orchestrates: my flights first, priority order plays → zone shuffling → draws). Constants: `FLY_MS`, `FLY_STAGGER_MS`, `REDUCED_MOTION` (skip all flights under `prefers-reduced-motion`). Purely visual — no state, no messages, no engine coupling (same contract as the trip-chain animation). **Known approximation**: when several engine actions land between two observed states (fast polling gaps), the diff attributes the net change (e.g. a mana increase with no matching hand loss flies deck→mana instead of hand→mana); the zone counts always end up correct because they come from the state, only the flight's origin may be a generic back.
13c-bonus. **Marker persistence** — `renderBoard` now **repositions** the two player markers (`.marker[data-player=…]`) instead of recreating them each render, so the CSS `left/top .45s` transition actually animates the token sliding along the ring on advance/recoil (re-creating them on every render killed the transition). Markers for players that left the game are removed.
13b. **Trip-chain resolution animation** (2026‑09) — `playChainAnim(t)`, `placeThread(col, firstRow)`, `clearChainAnim()` + `chainGen` (generation counter). Triggered from `renderLog` when a **genuinely new** turn log entry appears (`added && start > 0` — never on the first render/rejoin): the stopover slots pulse in the **exact resolution order** read from the public turn log (the engine appends the log's stopovers in resolution order and each entry carries `order` 1/2 = first/second player of the turn): stopover by stopover, first player's card (gold, `chain-first`) then second player's (silver, `chain-second`), ~460 ms per card. A glowing thread (`.chain-thread`, gold end on the first player's side via `first-top`/`first-bottom`) bridges the two facing slots while a stopover has both players' cards. Because the cleaning phase already cleared `action_chain` in the new state, the played cards are **cloned** onto the slots for the duration of the run (`.chain-anim-card`, keeps `.played` for layout + `data-hover-id` for the delegated hover preview) and removed when the run ends. `clearChainAnim()` bumps `chainGen` so pending `setTimeout(step)` calls are dead on arrival, and removes all pulse classes / threads / clones. `renderBoard`'s slot wipe uses `.played:not(.chain-anim-card)` so clones survive mid-run polling re-renders. Purely visual — no state, no messages, no engine coupling.
13b. **Trip-chain resolution animation** (2026‑09) — `playChainAnim(t)`, `placeThread(col, firstRow)`, `clearChainAnim()` + `chainGen` (generation counter). Triggered from `renderLog` when a **genuinely new** turn log entry appears (`added && start > 0` — never on the first render/rejoin): the stopover slots pulse in the **exact resolution order** read from the public turn log (stopovers are appended by the engine in resolution order; each entry's `order` 1/2 = first/second player of the turn): stopover by stopover, first player's card (gold, `chain-first`) then second player's (silver, `chain-second`), ~460 ms per card. A glowing vertical thread (`.chain-thread`, gold end on the first player's side — `first-top`/`first-bottom`) bridges the two facing slots while a stopover has both players' cards. Because the cleaning phase already cleared `action_chain` in the new state, the played cards are **cloned** onto the slots for the duration of the run (`.chain-anim-card`, keeping `.played` for layout + `data-hover-id` for the delegated hover preview) and removed when the run ends. `clearChainAnim()` bumps `chainGen` so any pending `setTimeout(step)` is dead on arrival, and removes all pulse classes / threads / clones. `renderBoard`'s slot wipe uses `.played:not(.chain-anim-card)` so clones survive mid-run polling re-renders. Purely visual — no state, no messages, no engine coupling.
14. **Hand & interaction** — `makeHandCard` (click-select with per-phase max, native drag & drop, right-click zoom, "playable" outline when affordable), hover preview (`showCardHover`/`hideCardHover` via **event delegation** on `document` so it survives re-renders; `__back__` shows the card back), `showCardModal` (zoom), `showServerMsg`, `showEndgame`, `leaveGame`.
15. **Boot** — async IIFE: `initSetup()` + load cardpool & support (tolerant of failure, toasts on error) + re-render the support section.

## The three launch branches (`launch()`)

| Mode | Request | Then |
|---|---|---|
| `ai` | `POST /create_game_ai` `{name, deck}` | straight into `enterGame()` |
| `create` | `POST /create_game` `{name, deck}` | shows the game id (copyable) + `startWaitingForOpponent()` → `enterGame()` when the 2nd player joins |
| `join` | `POST /join_game/{id}` `{name, deck}` | `enterGame()`; a **409** (already in the game, page reload) is swallowed if the player is present in `GET /game/{id}` |

`deck` is always the **shuffled 30-card mix** (20 main + 10 support) — the server validates it via `check_deck`.

## Communication model (important!)

- **WebSocket** (`/ws/{game}/{me}`) is the **only** action channel: `sendAction(cards, to, mode, pendings, cell)` → Promise. Sends are **serialized** through `pendingResolvers` (one outstanding request). Replies: a game state (applied via `applyState`) or a rejection `{success:false, message}` (toasted via `showServerMsg`).
- **Polling** (`GET /api/state/{game}/{me}`, every 2.5 s) exists because the WS only *receives* a state when **we** send — polling is how we see the **opponent's** actions. Both feed the same `applyState()` funnel.
- `applyState()` is the single entry to rendering; there is no other path that mutates `game.state` or the DOM game view.

## Action dispatch (what `#btn-play` sends, per phase)

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

- **Full re-render per state**: `applyState → renderAll` rewrites the zones, board rows and slots from scratch (the DOM is treated as a function of `game.state`). The exceptions are deliberately transient/incremental: the **log** (`game.logTurnsRendered`), the **selection/cell-select** UI, the **trip-chain animation** (its `.chain-anim-card` clones are spared by `renderBoard`'s slot wipe — `.played:not(.chain-anim-card)` — and torn down by `clearChainAnim` / the run's final step; `chainGen` invalidates pending timers), the **player markers** (`.marker[data-player=…]` are repositioned across renders so their CSS left/top transition animates the advance — see 13c-bonus), and the **card-flight clones** (`.card-fly`, self-removing, never part of the rendered state — see 13c).
- **Delegated hover**: card hover previews are wired on `document` (mouseover/mouseout + `.card[data-hover-id]`), not per element — required because re-renders destroy per-element listeners. New card elements **must** keep `data-hover-id`.
- **Art fallback**: every `<img>` gets `onerror → /placeholder.svg`; support art via `cardImg()` (`card_path`), main art `<card_id>.png`.
- **Board tokens**: positions come from `POS24` (polar ring, radius 49 %, cell 0 at top, clockwise); stacks of trap/drop tokens fan up-right (`MAX_FAN`, `FAN_STEP_PX`) with a count badge on the top one.
- **Opponent masking**: never render opponent hand/mana/deck *contents* — counts (`*_count` or list length) and card backs only.

## Checklist when modifying this file

1. New engine rule / message field (e.g. a new `to:` target, a new board field) → mirror it in: `sendAction` call site, `renderAll`/zone renderers, **and** the "mirrors the engine" table above.
2. New card type / support faction → extend `cardInfo`/`cardTitle`/`cardImg`/`cardCost`, the UI constants (`ENGINEER_DROPS` pattern) and the button dispatch table.
3. Any change to `renderLog` / `applyState` / `makeHandCard` → keep the incremental + delegation invariants (section "Rendering invariants").
4. Update this file in the same commit.
