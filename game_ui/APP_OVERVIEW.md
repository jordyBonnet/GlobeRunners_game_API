# `game_ui/app.py` — living overview

> **Contract: this file is the living overview of `game_ui/app.py`.** Before any
> non-trivial change to `app.py`, read this file (route order, middleware, AI wiring,
> deck validation). After the change, if the modification is **significant**
> (new/removed route, changed middleware/mounts, changed AI wiring, changed deck
> validation, changed the layering/flow between components), update this file in the
> same session — fix the affected sections and append a line to the **change log**
> below. Cosmetic changes (typos, comments, variable names) do not require a doc
> update. If you notice the doc has drifted from the code for any other reason,
> re-sync it.

## What it is

The single FastAPI server for the **full web app** (port **8001**). It does three
things:

1. **Serves the frontend** — `static/index.html` + the ES-module app (entry
   `static/app.mjs`), the CSS, and the card/game art from external folders.
2. **Adds UI routes** on top of the engine API — `GET /`, `GET /placeholder.svg`,
   `POST /create_game_ai`, `GET /api/state/{game}/{player}`.
3. **Embeds the engine API** — everything else (`/create_game`, `/join_game/{id}`,
   `/cardpool`, `/ws/{gid}/{player}`, …) is delegated to the `API.py` app, mounted
   at `/` **last** (so the UI routes above win over any same-path route in `api_app`).

One process = the whole game. The frontend talks to this one server only.

## Run

```bash
uv run python game_ui/app.py                  # -> http://127.0.0.1:8001
# or on another port:
GLOBE_UI_PORT=8002 uv run python game_ui/app.py
```

Kill all running instances (PowerShell):
```powershell
Get-NetTCPConnection -LocalPort 8001 -State Listen | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force }
```

## Imports & layering

```
game_ui/app.py
├── API.py            -> app as api_app, CreateGameRequest, check_deck   (embedded, mounted at "/")
├── engine.game_engine (as ge)        -> create_new_game, p2_connect_to_game,
│                                        get_current_game, current_game_json
├── models            -> PlayerState
├── ai_driver         -> random_ai_deck, run_ai_loop                      (the Robot)
└── fastapi / starlette -> FastAPI, FileResponse, JSONResponse, StaticFiles
```

`sys.path` is seeded with the project root (so `import API`, `import engine…` work
when launched as `python game_ui/app.py`). The layering is **UI on top, engine below**:
`app.py` never re-implements a rule — it calls `ge.*` and `API.py`, and the frontend
only ever speaks to the HTTP/WS surface.

## Route map

| Route | Handler | Notes |
|---|---|---|
| `GET /` | `index()` | `static/index.html` (setup page) |
| `GET /placeholder.svg` | `placeholder()` | `static/placeholder.svg`, `image/svg+xml` |
| `POST /create_game_ai` | `create_game_ai()` | new game vs the Robot (see *AI wiring*) |
| `GET /api/state/{game_id}/{player_name}` | `personalized_state()` | masked per-player state for polling |
| `GET /static/*` | (mount) | `static/` — css, js modules, placeholder |
| `GET /art/*` | (mount, `LOCAL_TEST=True`) **or** `serve_card_image()` (`LOCAL_TEST=False`) | main-card art (`<card_id>.png`) + support art (`<card_path>`) — local folder, or **server-cached** GitHub Release of `GlobeRunners_images` (`hosted_assets/art_cache/`, immutable `Cache-Control`) (see *Card art (`/art`)*) |
| `GET /assets/*` | (mount) | game assets (biomes, markers, logos, engineer drops) |
| `GET /cards_ex/*` | (mount) | faction placeholder art (dwelling card) |
| `*` (everything else) | `api_app` (mounted at `/`) | `/create_game`, `/join_game/{id}`, `/cardpool`, `/game/{id}`, `/ws/{gid}/{player}`, … |

Route resolution order: the four **route** handlers first, then the **static
mounts** (in the `STATIC_MOUNTS` order), then the **catch-all** `api_app` mount at
`/`. A more specific mount (`/static`, `/art`, …) always wins over the `/` mount.

## Function-by-function

- **`index()`** — returns `static/index.html`.
- **`placeholder()`** — returns `static/placeholder.svg` (the card-art fallback the
  frontend's `onerror` handlers use). Served as a file (not an inline string).
- **`create_game_ai(req)`** — see *AI wiring* below.
- **`personalized_state(game_id, player_name)`** — the polling endpoint. Reads the
  **full** game via `ge.get_current_game`, then returns
  `ge.current_game_json(player_name, current_game)` — i.e. the state **masked for
  that player** (opponent hand/mana/deck hidden). `404` if the game or the player
  doesn't exist. This is the frontend's way of seeing the **opponent's** actions
  (the WS only pushes a state when *we* send). It is distinct from `GET /game/{id}`
  in `api_app`, which returns the **full** state (debug/analysis).
- **`serve_card_image(filename)`** — registered **only when `LOCAL_TEST` is False**:
  `GET /art/{filename}` first serves the file from the repo bundle
  `game_ui/hosted_assets/art/` **iff it exists there** (the 15 support cards,
  path-traversal-guarded with `is_relative_to`) → `FileResponse` with
  `Cache-Control: public, max-age=31536000, immutable`. Otherwise main cards
  (Dwa/Dem/Twi/Mia/Orc/Mum) are served from the **server-side cache**
  `game_ui/hosted_assets/art_cache/<card_id>.png` (gitignored runtime cache):
  on a miss the server downloads the GitHub Release asset of the
  `GlobeRunners_images` repo via `game_ui/github_assets.py` (a self-contained
  copy of the deckbuilding app's `lib/github_assets.py` — keep the two in sync if
  the releases change; incl. the Twigs/Orcs part2 split points and the Miaous 1–4
  split) **once**, stores it, and serves it with the same immutable headers
  (per-name `threading.Lock` so concurrent misses download only once; a download
  failure falls back to the placeholder redirect). Anything else redirects to
  `/placeholder.svg`. The cache exists because the GitHub release chain
  (github.com 302 `Cache-Control: no-cache` with **no validator** → rotating
  signed blob URL) is not reliably client-cacheable, and the frontend recreates
  the hand's `<img>` elements on every ~2.5 s poll — before the cache, main-faction
  cards re-downloaded ~1.2 MB per poll and flickered (the "one card re-animates
  every ~2 s" glitch, 2026-09-24).
- **`NoCacheStatic`** — pure-ASGI middleware: for any `http` request whose path
  starts with `/static/`, it appends `Cache-Control: no-cache, must-revalidate` to
  the response. Non-`/static/` requests pass straight through untouched. Implemented
  as a plain ASGI callable (not `BaseHTTPMiddleware`) — no per-request task
  overhead, and it only intercepts `/static/`.
- **`main()`** — reads `GLOBE_UI_PORT` (default `8001`) and runs uvicorn on
  `127.0.0.1`.

## Card art (`/art`)

The `/art` route depends on the **`LOCAL_TEST` toggle** (top of `app.py`):

- **`LOCAL_TEST = True`** (local development): `_ART_DIR`
  (`…/artdesign/cards_framed_0.6`) is mounted as `StaticFiles` — exactly like the
  other asset mounts (mounted **iff the directory exists**, else a warning is
  printed and the frontend falls back to `placeholder.svg`).
- **`LOCAL_TEST = False`** (Hugging Face / remote hosting): **no local art folders
  are used** — everything comes from the repo + GitHub:
  - the route `serve_card_image(filename)` handles `/art`: support art
    (`Eng_`/`Doc_`/`Mag_`) is **served from `game_ui/hosted_assets/art/`**
    (committed to this repo, 15 cards matching `support_factions.parquet`
    `card_path`); main cards are **served from the server-side cache**
    `game_ui/hosted_assets/art_cache/` (runtime cache, gitignored) — on a miss the
    server downloads the matching GitHub Release asset of
    `jordyBonnet/GlobeRunners_images` **once** (same source the deckbuilding app
    uses; `game_ui/github_assets.py` mirrors
    `GlobeRunners_deckbuild_app/lib/github_assets.py` — keep in sync if the
    releases change) — and every art response carries
    `Cache-Control: public, max-age=31536000, immutable` (the GitHub release chain
    is `no-cache` with no validator, so a direct 302 redirect made browsers
    re-download the ~1.2 MB PNG on every hand re-render — see the 2026-09-24
    change log); unknown files → redirect to `/placeholder.svg`.
  - the `/assets` and `/cards_ex` mounts point at the **repo bundle**
    `game_ui/hosted_assets/{assets,cards_ex}` — **pruned to exactly the files the
    frontend references** (32 board assets: playmat + `earth_cgf{1-4}_nomarker`,
    logos, markers, banners, effect/env icons; 6 `placeholder_<Faction>.png`
    dwelling cards) — copied from `…/artdesign/cards_assets` +
    `…/artdesign/cards_ex`, ~63 MB. (If a new `*.mjs` references a new asset, add
    it to `hosted_assets/` or the `onerror` placeholder fallback kicks in.)
  - the 8 GB main-card art (`cards_framed_0.6`) is **never bundled** — it lives in
    the GitHub Releases.

## Static mounts

`STATIC_MOUNTS` is a list of `(path, directory, note-if-missing)`. Each is mounted
with `StaticFiles` **iff the directory exists**; otherwise a warning is printed and
the mount is skipped (the frontend degrades gracefully — placeholder art / plain
board). The three mounts (`/art` is separate — see *Card art* above):

| path | directory (`LOCAL_TEST=True` → `False`) | missing → |
|---|---|---|
| `/static` | `game_ui/static` (both) | the app can't run (frontend) |
| `/assets` | `…/artdesign/cards_assets` → `game_ui/hosted_assets/assets` | board backgrounds are plain |
| `/cards_ex` | `…/artdesign/cards_ex` → `game_ui/hosted_assets/cards_ex` | dwelling placeholder images unavailable |

The asset folders are **hard-coded external paths** (sibling project
`GlobeRunners_card_system`) — do not "fix" them without checking the fallback still
works.

## AI wiring (`/create_game_ai`)

Creates a game the human plays immediately, with the Robot already in:

1. Validate `deck` non-empty + `check_deck(req.deck)` (main `card_id`s + support
   `card_name`s; rejects a main card appearing twice and a support card more than
   twice).
2. `name` ≥ 2 chars. The Robot is named **`Robot`**, or **`Robo`** if the human
   picked `robot` (case-insensitive) — avoids a name collision.
3. `ge.create_new_game(<human PlayerState>)` → `game_id`.
4. `ge.p2_connect_to_game(<Robot PlayerState, deck=random_ai_deck()>)` — the Robot
   joins, so the game initializes (hands dealt, planet rolled).
5. `asyncio.create_task(run_ai_loop(game_id, ai_name))` — the Robot plays in a
   background task (see `game_ui/ai_driver.py`). The loop is fire-and-forget; the
   response returns as soon as the game exists.
6. Response: `{success, game_id, player_id: <human>, opponent: <robot>}`.

`random_ai_deck()` builds a 20-main + 10-support deck for the Robot (never plays
support cards, but puts them in mana — see `ai_driver.py`).

## Invariants

- **Masking**: `personalized_state` returns `current_game_json(player, …)` — the
  opponent's hidden info is masked. Never return the opponent's hand/mana/deck
  contents to a player here.
- **Deck validation**: `create_game_ai` runs `check_deck` (from `API.py`) before
  creating the game — same validation as the 2-player path.
- **Route precedence**: UI routes + static mounts are declared **before** the `/`
  `api_app` mount, so they win. Keep new UI routes/mounts above the `api_app` mount.
- **Card art is switchable**: `/art` is either the local `_ART_DIR` mount
  (`LOCAL_TEST=True`) or the GitHub-sourcing route `serve_card_image`
  (`LOCAL_TEST=False`) — never both at once. In `False` mode the art is always
  served **from the local server** (repo bundle or the `art_cache/` download
  cache) with immutable `Cache-Control` — never a bare 302 to GitHub (that chain
  is uncacheable client-side: `no-cache` without validator + rotating signed
  URLs).
- **One server**: the frontend only talks to this server; `API.py` is embedded, not
  a separate process, in this deployment.

## Change log

- **2026-09-24** — main-card art is now **server-side cached**: `serve_card_image`
  fetches each GitHub Release asset **once** into
  `game_ui/hosted_assets/art_cache/<card_id>.png` (new gitignored runtime cache,
  per-name lock, placeholder fallback on download failure) and serves every art
  file with `Cache-Control: public, max-age=31536000, immutable`. Fixes the
  "one card re-animates every ~2 s" glitch: the previous bare 302 → GitHub chain
  (`no-cache`, no validator, rotating signed blob URL) was re-downloaded by
  strict browsers on every hand re-render (the poll is ~2.5 s), so the
  main-faction cards flickered every poll.
- **2026-09-23** — `LOCAL_TEST` toggle for card art: `True` → local
  `cards_framed_0.6` mount (previous behavior); `False` → new route
  `serve_card_image()`: support art served from the new **repo bundle
  `game_ui/hosted_assets/`** (15 support cards in `art/`, plus `assets/` +
  `cards_ex/` **pruned to the files the frontend references** — ~80 MB total, so
  `/assets` and `/cards_ex` also come from the repo bundle in `False` mode),
  main cards redirect to the
  `GlobeRunners_images` GitHub Releases (new `game_ui/github_assets.py`, mirror
  of the deckbuilding app's `lib/github_assets.py`); unknown → `placeholder.svg`.
  `/art` moved out of `STATIC_MOUNTS`.
- **2026-09-15** — Frontend split into ES modules (entry `app.mjs`); `app.py`
  cleaned up: `NoCacheStatic` rewritten as a pure-ASGI middleware (was
  `BaseHTTPMiddleware`), the three art mounts collapsed into the `STATIC_MOUNTS`
  loop, `placeholder.svg` served from a file (was an inline SVG string), `HERE`
  removed from `sys.path` (unused), docstring/routes refreshed. Added this doc.
