# GlobeRunners — Duplicate cards in hand (root causes found, fixed)

> Part of the project instructions, split out of `AGENTS.md` (2026-09-21).
> Back to [AGENTS.md](AGENTS.md). `see *Phase 3*` points to [AGENTS.md](AGENTS.md).

## Duplicate cards in hand (2026-09-02, root cause found + fixed)

**Symptom.** The user saw the same card twice in hand after tapping the refinery (3× refinery in game `26_09_02_18_54_10_PyaZf`; duplicate main cards in `26_09_02_16_27_30_11VCs`).

**Root cause.** Not an engine bug. A full audit showed **every engine zone transition conserves cards** (draw = deck→hand, play = hand→chain, resolve = chain→discard, reshuffle = discard→deck, …), the DB write is a single atomic full-state `UPDATE` (the final state is always one thread's consistent snapshot — a lost update can roll a game back but **cannot create a card**), and the frontend enforces exactly 20 main + 10 support. The actual hole: **the server accepted a 31-card deck**. `check_deck` (API.py) only checked card *existence* — it let a support card appear **3 times** (PyaZf: refinery ×3; the user's browser at the time was running an intermediate dev version of the deck builder). The engine faithfully conserves whatever the deck contains — so a deck with 3 refineries legitimately produced 3 refineries across hand/dwelling. `11VCs` is the same class: a deck with duplicated MAIN cards.

**Proof of conservation** (in `tests/_midchain_flush.py`): a scripted 2-player game to a mid-chain win verifies (a) final zones == original deck multiset for both players, (b) the loser's unprocessed card is flushed to the discard, (c) no phantom `action_chain` in the final state, (d) a stray message after "game over" moves no card.

**Fixes (2026-09-02):**
- `check_deck` (API.py): rejects a **main card appearing more than once** and a **support card appearing more than twice** (400). Still accepts standard 30-card decks and the 15-card E2E test decks. Applies to all three entry points (`/create_game`, `/join_game`, `/create_game_ai`).
- `game_ui/static/setup.mjs`: CSV import rejects duplicate card ids (toast); launch already required exactly 20 main + 10 support.
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
