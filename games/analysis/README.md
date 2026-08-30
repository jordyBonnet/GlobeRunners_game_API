# Game analysis — GlobeRunners

Web interface that reads `games/games.db` and analyzes the **trip chain**
resolution phase of a selected game: turn-by-turn summary, played cards
(art + name only, **no card id is ever displayed**), condition met or not,
effect produced, **each player's hand at the start of every turn**
(reconstructed, see below), and each player's final position after every turn.

Each turn is a collapsible/expandable block ("Expand all" / "Collapse all" buttons).

## Launch

From the project root, with the virtual environment activated:

```powershell
.venv\Scripts\python.exe -m uvicorn games.analysis.app:app --port 8017
```

Then open http://127.0.0.1:8017/ in the browser.

## Folder contents

| File | Role |
|---|---|
| `replay.py` | Reconstructs the game from the stored final state (turn segmentation, replay through the real engine `engine/game_engine.py`, final verification) |
| `app.py` | FastAPI backend: `/api/games`, `/api/game/{id}`, `/card/{id}.png` + static frontend |
| `static/` | Frontend (HTML/CSS/JS, dark theme, English interface) |

## Data sources

- **Games**: `games/games.db` — table `games(game_id, state_json)` (final state only).
- **Cards**: `cards/cardpool.parquet` (names, factions, costs, effects).
- **Card art**: external folder
  `C:\Users\jordy\Documents\python\projects\GenAI_TCG\lib\artdesign\cards_framed_0.6`
  (`<card_id>.png`). If an art is missing, the card is shown dimmed.

## Hand reconstruction

The database only stores the **final state** of the game (shuffles were
unseeded, so the exact deck order is unrecoverable). The analysis therefore
reconstructs the hands by deduction:

- **All of a player's cards are known** (union of the final zones);
- the cards **played** and **put in mana** are known precisely (message history);
- the **drawn** cards are the ones that remain: the reconstruction places the right
  cards in hand at the right time (played cards first, survivors to the end next,
  deck remainder as filler), so that the real engine advances, recoils, draws and
  discards exactly the expected cards;
- the analysis is **verified at the end of the game**: final positions, zone
  counters and final hand/mana card identities must match the stored state
  (possibly extended: see below).

Compatibility workarounds for older engine versions:

- the **initial mana** may arrive as 3 messages of 1 card (instead of one
  message of 3); both formats are handled;
- some old games have **lost cards** (played but absent from all final zones):
  they are reconstructed in the discard pile, as the current engine would do;
- a starting hand may contain more than 3 cards (played on turn 1); the
  overflow goes to the discard pile at the end of turn 1.

## Replay reliability badge

- **✓ replay verified**: the recalculated final positions, zone counters and card
  identities exactly match the state stored in `games.db`.
- **⚠ approximate replay**: at least one condition not implemented in the current
  engine was assumed met (or the game is corrupted, or the final
  positions/identities cannot be reconstructed exactly); positions may diverge.
  Details are in the warning list under the header.

## Replay self-test

```powershell
.venv\Scripts\python.exe -m games.analysis.replay
```

Prints OK/FAIL for every game in `games.db`.
