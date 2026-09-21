# GlobeRunners — the game

2-player card game (cell race) served on the web: both players put cards in
**mana**, play movement/effect cards on their turn, and effects are resolved at
the end of the turn in a simultaneous **trip chain** (action chain) for both
players. The first to reach the finish cell (`win_position = 24`) wins.

Platform: **FastAPI + WebSocket**, game state persisted in **SQLite**, card pool
in **Parquet** (polars), vanilla JS/CSS/HTML frontend.

## Project layout

```
├── engine/            # Game engine (rules, state, resolution) — the heart of the project
├── models.py          # Shared Pydantic models (PlayerState, GameState, Card)
├── API.py             # REST + WebSocket API layer of the engine (standalone service)
├── game_ui/           # Full web UI: frontend + UI routes + "Robot" player (AI)
│   └── static/        #   frontend (index.html, style.css, ES modules — entry: app.mjs)
├── player_ai/         # Heuristic AI (PlayerAI) — decision core of the Robot
├── games/             # SQLite database of played games (games.db) + game analysis
│   └── analysis/      #   dedicated web app for replay/analysis (see its README)
├── cards/             # cardpool.parquet — the pool of ~7600 cards (source of truth)
├── tests/             # E2E / smoke tests and diagnostic scripts
└── pyproject.toml     # Dependencies (uv): fastapi, uvicorn, polars, numpy, pillow, websockets
```

## `engine/` — the game engine

**What it does**: contains every rule of the game, in a single module
(`engine/game_engine.py`, ~950 lines). It is the only layer that knows the rules;
every client (API, UI, AI, analysis) calls into it.

**How to use it**: it is not an entry point — import it:

```python
import engine.game_engine as ge   # from the project root

game_id = ge.create_new_game(player_dict)              # p1 creates
ge.p2_connect_to_game(p2_dict, game_id)                # p2 joins, full initialization
state_json = ge.handle_websocket_message(game_id, player)  # 1 action -> JSON state
```

**General behavior**:

- **State**: a `GameState` (see `models.py`) serialized to JSON and stored in
  `games/games.db` (table `games(game_id, state_json)` — only the *final* state is kept).
  Every action re-reads the state from the database (source of truth), applies the
  rule, writes the state back. There is no in-memory state between calls.
- **Game loop (state machine driven by `GameState.state`)**:
  1. *"waiting for both players to put 3 cards in hand"* — both players put
     3 cards in the mana zone (initialization phase).
  2. *"waiting for first/second player (…) to play"* — each plays a card
     (mode `move`) or passes; played cards stack up in `action_chain`.
  3. *"waiting for both players to mana or pass"* — each puts 1 card in mana
     or passes.
  4. When both have acted → `process_trip_chain`: **step by step** resolution of
     the two chains (p1 action 1, p2 action 1, p1 action 2, …). A `discard` / `discard_oppo`
     effect can **pause** the chain: the state becomes
     *"turn N - waiting for NAME to discard K card(s)"*, the discarding player sends
     `{"cards": […K…], "to": "discard_pile", "mode": ""}` to choose which cards, and the
     chain resumes.
  5. End of turn: play order reversed, day ↔ night, draw of 3 cards (re-shuffle
     of the discard pile if the deck is empty), back to phase 2.
- **Main functions**:

  | Function | Role |
  |---|---|
  | `create_new_game` / `p2_connect_to_game` | creation / initialization (hands of 6, board of 24 cells over 4 biomes OC/MO/DE/JU, temperature = 2d20 nearest to 10, day/night) |
  | `handle_websocket_message` | **single entry point**: validates the message, routes by phase, updates the DB, returns the personalized JSON state |
  | `message_check` | validation of the player message format |
  | `player_play` | plays a card (checks mana, adds to `action_chain`) |
  | `process_trip_chain` / `process_card` | action resolution: condition → effect → advancing |
  | `is_condition_met` | evaluates card conditions (biome, distance, mana, hand, temperature, day/night) |
  | `apply_effect` | applies effects (`advancing`, `backward`, `draw`, `ramp`, `taxation`, `jump`, opponent effects, …) |
  | `process_advancing` / `_jump` | movement on the earth, trap/tomb cells, **win** (position ≥ 24) |
  | `_check_deadlock` | end of game if no playable card left (winner = the most advanced, else tie) |
  | `current_game_json` | **hidden information masking**: opponent's hand/mana/deck returned empty (only the counters stay public) |

- **Player message format** (sent over WebSocket or called directly):

  ```json
  {"cards": ["id1"], "to": "stopover_3", "mode": "move", "pendings": []}
  ```

  `cards` = ids of the cards played, `to` = destination (`stopover_x`, `mana`, …),
  `mode` = `''` | `move` | `defend` | `pass`, `pendings` = pending effects.

- **Card pool**: read once at startup from `cards/cardpool.parquet`
  (`card_id` column → `mana`, `advancing`, `condition`, `effect`, `effect_number`,
  `faction`, `rare`, `name`, …).

## `models.py` — shared models

**Pydantic** models used by every layer:

- `Card` — description of a card (id, mana cost, type, effect);
- `PlayerState` — hand, mana, deck, discard piles, position, `action_chain`,
  `messages_history` (every accepted message is kept — this is what enables the
  replay in `games/analysis`), etc.;
- `GameState` — the 2 players, turn order, turn number, `state` (state machine),
  `earth` (24 cells × [biome, players, traps…]), `winner`, temperature, day/night,
  `engine_version` (rules version the game was created under).
  Serialization via `to_json()` / `GameState.from_json()`.

## `API.py` — REST + WebSocket layer (standalone service)

**What it does**: FastAPI wrapper around the engine, no UI at all. This is the
"pure game" service: create/join a game, read the state, play over WebSocket.

**How to use it**:

```
python API.py          # -> http://127.0.0.1:8000
```

**Endpoints**:

| Method | Route | Role |
|---|---|---|
| POST | `/create_game` | creates the game with p1 (`{"name", "deck"}`) → `game_id` |
| POST | `/join_game/{game_id}` | p2 joins, initialization, personalized state |
| GET | `/game/{game_id}` | **full** state (hidden info included — debug/analysis) |
| GET | `/cardpool` | card pool as JSON |
| WS | `/ws/{game_id}/{player_name}` | sends 1 action message → receives the personalized state |

The WebSocket rejects out-of-turn actions (checked against the official state in
the DB) and always returns a *personalized* state (opponent's hand/mana/deck masked).

## `game_ui/` — full web application

**What it does**: the game as experienced in the browser. Embeds the `API.py`
app (everything it exposes is available at the bottom of the stack) and adds:
the frontend, the "Robot" AI (game vs the machine), state polling, and the
art/assets serving.

**How to use it**:

```
python game_ui/app.py                  # -> http://127.0.0.1:8001
# or: uvicorn game_ui.app:app --port 8001   (port via GLOBE_UI_PORT)
```

Then open `http://127.0.0.1:8001/`: setup page (deck choice) → create, join
(`game_id`) or play **against the Robot**.

**Contents**:

| File | Role |
|---|---|
| `app.py` | root FastAPI app: UI routes + mount of `API.py` on `/` + assets |
| `static/` | vanilla frontend: `index.html`, `style.css`, a set of ES modules (entry: `app.mjs`, see `app-js-overview.md`) — dark theme, English UI |
| `ai_driver.py` | background AI loop: reads the state, decides via `PlayerAI`, plays |

**Added routes**: `GET /` (setup page), `GET /static/*`, `GET /art/<id>.png`
(card art, external folder with `placeholder.svg` fallback), `GET /assets/*`
(biomes/markers), `POST /create_game_ai` (game vs Robot — the robot joins
immediately and plays in an asyncio task), `GET /api/state/{gid}/{player}`
(personalized state on demand — the frontend polls this route to see the
opponent's actions, without ever exposing hidden information).

**Note**: the art and assets folders are **hard-coded external paths**
(pointing to `GlobeRunners_card_system/lib/artdesign/…`); if they are missing, the app works
with a SVG placeholder and neutral backgrounds.

## `player_ai/` — decision core of the Robot

**What it does**: the `PlayerAI` class (`player_ai/playerai.py`): a
**heuristic** strategy (no ML) that produces the engine-format action messages.

**How to use it**: never called directly by the user — `game_ui/ai_driver.py`
orchestrates it. Programmatic usage:

```python
from player_ai.playerai import PlayerAI
ai = PlayerAI(player_state)
ai.update_player_state(me, oppo, game)   # resynchronizes (opponent, temperature, day/night)
msg = ai.play_card()                     # or ai.put_mana(n, in_turn=...)
```

**Behavior**:

- `put_mana(n)`: puts the hand cards with the **highest mana costs**;
- `play_card()`: among the affordable cards, prefers the ones whose **condition
  is met** (re-evaluated AI-side via `_condition_met`, a mirror of
  `game_engine.is_condition_met` with public info only), then the highest
  `advancing`; passes if nothing is playable;
- `ai_driver.ai_decide` applies the `stopover_k` ordering rule (k-th card of the
  turn → `stopover_{4-k}`) and handles the phases (init / mana-pass / play) with
  a "pass" fallback if the action is rejected.

## `games/` — played games & analysis

- **`games.db`**: SQLite, table `games(game_id, state_json)` — the **final** state
  of every game, populated automatically by the engine (no action required).
- **`games/analysis/`**: web application that **replays** a stored game —
  turn-by-turn segmentation, replay through the real engine, reliability badge
  ("replay verified" / "approximate"). See **`games/analysis/README.md`** which
  documents its launch (`uvicorn games.analysis.app:app --port 8017`) and its
  self-test (`python -m games.analysis.replay`) in detail.

## `cards/` — card pool

**`cardpool.parquet`**: ~7600 cards (6 factions: Dwarves, Demons, Twigs, Miaous,
Orcs, Mummies). Columns: `card_id`, `name`, `faction`, `mana`, `advancing`,
`shield`, `condition`, `effect`, `effect_number`, `rare`, `condeff_value`,
`prompt`, `negative_prompt`.

This is the **source of truth** for the cards: the engine, the AI and the analysis
read it directly; the UIs only display `name` + art. Player decks are just a
list of ids from this pool.

## `tests/` — E2E tests and diagnostics

These are **manually run smoke tests** (no pytest framework); the two E2E ones
require the `game_ui` server running locally:

| File | Role | Launch (from the root) |
|---|---|---|
| `_ui_e2e.py` | 2 fictional players play a game over REST + WebSocket on `game_ui` (port 8001) | `python tests/_ui_e2e.py` |
| `_ai_e2e.py` | a human (WS) plays a full game against the Robot (`/create_game_ai`) | `python tests/_ai_e2e.py` |
| `_diag.py` | engine diagnostics: unit tests of `process_advancing`, game creation, etc. (uses the DB — pollutes `games.db`) | `python tests/_diag.py` |
| `_block_mode.py` | defense mode tests (condition "block"): block holds/fails, unstoppable, message validation | `python tests/_block_mode.py` |
| `_sample_deck.csv` | sample deck for the tests | — |

## Environment & dependencies

- Python **≥ 3.12**, managed by **uv** (`pyproject.toml` + `uv.lock`):
  `fastapi`, `uvicorn`, `websockets`, `polars`, `numpy`, `pillow`, `requests`.
- All launches happen **from the project root**.
- Default ports: `API.py` → **8000**, `game_ui/app.py` → **8001**,
  `games/analysis` → **8017**.
