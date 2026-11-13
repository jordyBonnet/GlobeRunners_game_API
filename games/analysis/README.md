# Game analysis — GlobeRunners

Web interface that reads **two game sources** — `games/games.db` and the
Hugging Face **backup dataset** (see *Second data source* below) — and analyzes
the **trip chain** resolution phase of a selected game: turn-by-turn summary,
played cards (art + name only, **no card id is ever displayed**), condition met
or not, effect produced, the **play-phase events** (engineer drops, dwelling
place/tap, pending places, the Mages' instants, `swap_cards`), **each player's
hand at the start of every turn** (reconstructed, see below), and each player's
final position after every turn.

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
| `hf_source.py` | Second game source: reads the finished games of the Hugging Face backup dataset (download + local mirror + metadata index, **read-only**) |
| `app.py` | FastAPI backend: `/api/games`, `/api/game/{id}`, `/api/hf_status`, `/card/{id}.png` + static frontend; merges the two sources |
| `static/` | Frontend (HTML/CSS/JS, dark theme, English interface) |
| `hf_cache/` | Local mirror of the dataset games (`games/<id>.json`) + `index.json` (selector metadata cache) — **gitignored**, re-downloadable |

## Data sources

- **Games, source 1 — `games/games.db`**: table `games(game_id, state_json)`
  (final state only), every game this machine knows (finished or not).
- **Games, source 2 — the HF dataset**: `jordyBonnet/globerunners-games`, files
  `games/<game_id>.json` — **exactly** `GameState.to_json()`, the same shape as
  the `state_json` column, so `replay.analyze_game()` replays either source
  unchanged. See *Second data source* below.
- **Cards**: `cards/cardpool.parquet` (names, factions, costs, effects) and
  `cards/support_factions.parquet` (the 15 support cards — engineers / mages /
  doctors — identified by `card_name`, art in `card_path`).
- **Card art**: external folder
  `C:\Users\jordy\Documents\python\projects\GlobeRunners_card_system\lib\artdesign\cards_framed_0.6`
  (main cards as `<card_id>.png`, support cards as `card_path`, e.g. `Eng_boost.png`).
  `/card/<id>.png` resolves a support card's **pool name** to its art file
  (`/card/boost.png` → `Eng_boost.png`), so support cards show their real art,
  name and support faction instead of "Carte inconnue". If an art is missing,
  the card is shown dimmed.

## Second data source: the HF backup dataset (`hf_source.py`)

`game_ui/hf_backup.py` uploads every **finished** game to the dataset
`jordyBonnet/globerunners-games` (`games/<game_id>.json`, full unmasked state).
The disk of a deployed container (HF Space / Render) is ephemeral, so the games
played **on the hosted app exist nowhere else** — this module is what makes them
analyzable.

- **Merge**: `/api/games` returns both sources, deduplicated, chronological; each
  entry carries a `source`: `db` (local only), `hf` (**hosted game, not in the
  local db** — shown as `☁️` in the selector) or `both`. `/api/game/{id}` reads
  `games.db` first and falls back to the dataset; `?source=db|hf` forces one
  (a forced source that does not hold the game is a 404). The game header shows
  where the analyzed game came from.
- **Source filter** (the `All / 💾 Local / ☁️ HF` tabs left of the selector, with a
  live count each): `All` = both sources deduplicated, `💾 Local` = every game
  `games.db` holds, `☁️ HF` = every game of the dataset — so a game stored locally
  **and** backed up (`both`) shows in the Local *and* the HF tab. The active tab is
  kept in the URL (`?filter=hf`, shareable; an unknown value falls back to `All`),
  the selected game is kept when the new tab holds it, otherwise the first visible
  one is loaded. Same filter exists on the API: `/api/games?filter=all|local|hf|`
  **`hosted-only`** (on the dataset but not in `games.db`); the UI filters the one
  merged list client-side, and `tests/_analysis_hf_source.py` checks both sides
  agree (`local` == the games.db set, `hf` == the dataset set, `all` == their union).
- **Local mirror** `hf_cache/`: a game is downloaded **once** and then served from
  the mirror (finished games are immutable), and `hf_cache/index.json` caches the
  selector metadata, so listing costs one file-listing call per 5 min
  (`GLOBE_ANALYSIS_HF_TTL`) and downloads only the new games.
- **Never breaks the app**: `huggingface_hub` is imported lazily and every network
  error is swallowed — the dataset is simply absent from the list, the status line
  under the header shows why (`⚠️ HF dataset unreachable …`, with a `↻ dataset`
  retry button that calls `?refresh=1`), and a failed listing **falls back to the
  mirror**, so already-downloaded hosted games stay analyzable offline.
  `/api/hf_status` exposes that state (`n_remote`, `n_mirrored`, `n_local`,
  `n_in_db`, `error`).
- **Read-only**: this module never writes to the dataset; uploads stay in
  `hf_backup.py`. Reads of the public dataset need no token; a private dataset
  would pick up `HF_TOKEN` / a cached `hf auth login` automatically.
- Configuration: `GLOBE_BACKUP_REPO` (same override as `hf_backup.py`),
  `GLOBE_ANALYSIS_HF=0` disables the source, `GLOBE_ANALYSIS_HF_TTL` seconds.
- Check it (also replays every dataset game through the real engine):
  `uv run python tests/_analysis_hf_source.py` — or just
  `python -m games.analysis.hf_source` for the listing.

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

## Support factions (engineers / mages / doctors)

A player's deck mixes main-faction cards with 10 support cards. The analysis
handles them:

- **Cards**: support cards resolve their real art (`card_path`, e.g.
  `Eng_boost.png`), name and support faction (shown in place of the biome — it
  identifies them as engineers / mages / doctors). A main card's faction label is
  replaced by the **biome** it stood on at the start of the resolution, but a
  support card keeps its support faction.
- **Engineers**: the 4 drop cards (boost / trampoline /
  gluetrap / landmine) and the refinery dwelling are described in place of a
  generic effect, and their **play-time actions** are shown as event chips in
  the turn: *dropped a … token on cell N*, *placed the refinery dwelling*,
  *tapped the refinery (drew a card)*. A **landmine** that blocks the arriving
  player is surfaced too: the affected card is tagged *⚡ canceled* and reads
  *Blocked by a landmine — no effect, no advancing*.
- **Doctors**: the pending cards are shown with their real art (the `epo` added
  by a laboratory tap included) and their play-time actions are event chips:
  *tapped the laboratory (added a epo pending card)*, *placed the … pending card
  in the pending zone*.
- **Mages**: the five cards are shown with their real art and their **instant**
  (play-time) effects are event chips mirroring the engine: *fixed day/night to
  … for the rest of the game*, *changed the planet temperature a → b °C*,
  *locked every player's movement for the rest of the turn*, *set the cataclysm
  order: … → …*, *tapped the black_hole — earth rotated 3 cells CW/CCW*.
  `swap_cards` is surfaced too (*swapped with the … at position N*).
- **Replay + verification**: the replay runs the real engine (drop placement,
  dwelling place/tap, landmine block, support-card cost, the Mages instants) and
  the verification also compares the final **board drops**, the **dwelling** and
  the earth/cataclysm state to the stored state.

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
