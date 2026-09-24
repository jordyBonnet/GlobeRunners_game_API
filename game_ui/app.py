"""GlobeRunners — Web UI (FastAPI).

Serves the frontend (static/) and embeds the game API defined in API.py: a single
server exposes everything (the API's REST + WebSocket, plus the UI routes below).

Launch (from the project root):
    uv run python game_ui/app.py                 # -> http://127.0.0.1:8001
kill all running instances of the UI (PowerShell):
    Get-NetTCPConnection -LocalPort 8001 -State Listen | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force }

Routes added on top of API.py:
    GET /                          index.html (setup page: deck + create/join)
    GET /static/*                  frontend css / js (the app is a set of ES modules, entry: app.mjs)
    GET /art/<card_id>.png         card art — LOCAL_TEST=True: external local folder;
                                   LOCAL_TEST=False: support art from game_ui/hosted_assets/
                                                       + GitHub Releases for main cards
                                                       (see github_assets.py)
    GET /assets/*                  game assets (biomes, markers, logos...)
    GET /cards_ex/*                faction placeholder art (dwelling card)
    GET /placeholder.svg           fallback image for cards without art
    GET /api/state/{gid}/{player}  personalized state on demand (polling) — the
                                   opponent's hidden info stays masked (unlike
                                   GET /game/{id} which is reserved for debug).

Everything else (/create_game, /join_game/{id}, /cardpool, /ws/{gid}/{player})
is delegated to the API.py app.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

# the API.py app (engine REST + WebSocket) — the webapp "interacts" with it by embedding it
from API import CreateGameRequest, check_deck, app as api_app  # noqa: E402
import engine.game_engine as ge  # noqa: E402
from models import PlayerState  # noqa: E402
from ai_driver import random_ai_deck, run_ai_loop  # noqa: E402
import hf_backup  # noqa: E402

STATIC_DIR = HERE / "static"

# ------------------------------------------------------------------ card art source
# LOCAL_TEST = True  -> serve card art + board assets from the LOCAL art folders
#                       (_ART_ROOT, the sibling project GlobeRunners_card_system).
# LOCAL_TEST = False -> self-contained hosting (no local art folders — Hugging Face
#                       / any other host):
#                         - support art (Eng_/Doc_/Mag_)  -> game_ui/hosted_assets/art/
#                         - main-card art (Dwa/Dem/Twi/Mia/Orc/Mum)
#                                                           -> GitHub Releases of
#                                                             GlobeRunners_images
#                                                             (see github_assets.py)
#                         - board assets (/assets, /cards_ex) -> game_ui/hosted_assets/
LOCAL_TEST = False

# external art root (LOCAL_TEST=True): (mount path, directory, note if missing)
_ART_ROOT = Path(r"C:\Users\jordy\Documents\python\projects\GlobeRunners_card_system\lib\artdesign")
_ART_DIR = _ART_ROOT / "cards_framed_0.6"
# self-contained hosting bundle (LOCAL_TEST=False), committed to this repo:
#   hosted_assets/art/      the 15 support-faction cards (Eng_/Doc_/Mag_)
#   hosted_assets/assets/   board assets (biomes, markers, logos, effect icons…)
#   hosted_assets/cards_ex/  card backs + faction placeholder art
_HOSTED_DIR = STATIC_DIR.parent / "hosted_assets"
STATIC_MOUNTS = [
    ("/static", STATIC_DIR, "the frontend static folder is missing!"),
    ("/assets", _ART_ROOT / "cards_assets" if LOCAL_TEST else _HOSTED_DIR / "assets",
     "board backgrounds will be plain"),
    ("/cards_ex", _ART_ROOT / "cards_ex" if LOCAL_TEST else _HOSTED_DIR / "cards_ex",
     "faction placeholder images will be unavailable"),
]

app = FastAPI(title="GlobeRunners - Web UI")

# game-over backup: every finished game is uploaded (background thread) to the
# Hugging Face dataset — see game_ui/hf_backup.py. I/O stays out of the engine;
# failures are logged, never raised.
hf_backup.register()


# ------------------------------------------------------------------ routes UI
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/placeholder.svg")
def placeholder():
    return FileResponse(STATIC_DIR / "placeholder.svg", media_type="image/svg+xml")


if not LOCAL_TEST:
    # card art: support cards (Eng_/Doc_/Mag_) are served from the repo bundle
    # (hosted_assets/art/); main cards come from their GitHub Release
    # (github_assets.py) — fetched server-side ONCE and cached in
    # hosted_assets/art_cache/; anything else -> placeholder.svg.
    #
    # Why the server-side cache (2026-09-24, the "one card re-animates every
    # ~2 s" glitch): the GitHub release chain is github.com 302 (Cache-Control:
    # no-cache, NO validator) -> rotating signed blob URL. Browsers that honor
    # that no-cache cannot reuse the chain: every fresh <img> — and renderAll
    # recreates the hand's <img> elements on EVERY ~2.5 s state poll —
    # re-downloaded the ~1.2 MB PNG, so the main-faction cards flickered
    # (re-appeared) every poll. Serving the art from a stable local URL with an
    # immutable cache header makes the browser cache it for a year: one
    # download per card, ever.
    import threading  # noqa: E402
    import urllib.request  # noqa: E402
    from github_assets import get_image_url, has_github_art  # noqa: E402

    _HOSTED_ART = (_HOSTED_DIR / "art").resolve()
    _ART_CACHE = (_HOSTED_DIR / "art_cache").resolve()
    _ART_CACHE_LOCK = threading.Lock()
    _IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}

    @app.get("/art/{filename:path}")
    def serve_card_image(filename: str):
        """Support art from the repo bundle; main art server-cached from GitHub;
        anything else -> placeholder.svg."""
        local = (_HOSTED_DIR / "art" / filename).resolve()
        if local.is_file() and local.is_relative_to(_HOSTED_ART):
            return FileResponse(local, headers=_IMMUTABLE)
        name = filename[:-4] if filename.lower().endswith(".png") else filename
        if has_github_art(name):
            cached = (_ART_CACHE / (name + ".png")).resolve()
            if cached.is_file() and cached.is_relative_to(_ART_CACHE):
                return FileResponse(cached, headers=_IMMUTABLE)
            with _ART_CACHE_LOCK:            # one download per card, ever
                if not (cached.is_file() and cached.is_relative_to(_ART_CACHE)):
                    try:
                        with urllib.request.urlopen(get_image_url(name), timeout=25) as r:
                            data = r.read()
                        _ART_CACHE.mkdir(parents=True, exist_ok=True)
                        cached.write_bytes(data)
                    except Exception:
                        return RedirectResponse("/placeholder.svg", status_code=302)
            return FileResponse(cached, headers=_IMMUTABLE)
        return RedirectResponse("/placeholder.svg", status_code=302)


@app.post("/create_game_ai")
async def create_game_ai(req: CreateGameRequest):
    """Create a game against the AI: the robot joins the game immediately.

    body {"name": str, "deck": [card_id, ...]}
    -> {"success", "game_id", "player_id", "opponent"} — the player can enter the
       game right away (the opponent is already in) and the AI loop plays in the
       background (ai_driver.run_ai_loop).
    """
    if not req.deck:
        raise HTTPException(status_code=400, detail="deck must not be empty")
    check_deck(req.deck)   # main faction cards (cardpool) + support faction cards (card_name)

    name = req.name.strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="name must be at least 2 characters")
    ai_name = "Robo" if name.lower() == "robot" else "Robot"   # avoid name collision

    player = PlayerState(name=name, deck=list(req.deck))
    game_id = ge.create_new_game(player.model_dump())

    # the robot joins and the game is initialized (hands dealt, planet rolled)
    ai_player = PlayerState(name=ai_name, deck=random_ai_deck())
    ge.p2_connect_to_game(ai_player.model_dump(), game_id)

    # the robot plays in a background task
    asyncio.create_task(run_ai_loop(game_id, ai_name))
    return {"success": True, "game_id": game_id, "player_id": name, "opponent": ai_name}


@app.get("/api/state/{game_id}/{player_name}")
def personalized_state(game_id: str, player_name: str):
    """Personalized game state for one player (opponent's hand/mana/deck masked).

    Serves client-side polling: each WebSocket connection only receives a state
    when IT sends a message, so the frontend polls this route periodically to see
    the opponent's actions without ever exposing their hidden information.
    """
    try:
        conn, _, current_game = ge.get_current_game(game_id)
    except Exception:
        return JSONResponse({"success": False, "message": f"game {game_id} not found"}, status_code=404)
    conn.close()

    if player_name not in current_game.players:
        return JSONResponse(
            {"success": False, "message": f"player '{player_name}' is not registered in game {game_id}"},
            status_code=404,
        )
    return json.loads(ge.current_game_json(player_name, current_game))


# ------------------------------------------------------------------ statiques
# no-cache on /static: the layout evolves fast, avoid the browser serving stale css/js.
# Pure ASGI middleware (cheaper than BaseHTTPMiddleware: no task-per-request).
class NoCacheStatic:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/static/"):
            await self.app(scope, receive, send)
            return

        async def send_no_cache(message):
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (b"cache-control", b"no-cache, must-revalidate"),
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_no_cache)


app.add_middleware(NoCacheStatic)

for path, directory, note in STATIC_MOUNTS:
    if directory.is_dir():
        app.mount(path, StaticFiles(directory=str(directory)), name=path.strip("/"))
    else:
        print(f"[game_ui] {directory} not found: {note}")

# card art: LOCAL_TEST=True -> local folder (mounted like the other assets);
# LOCAL_TEST=False -> the github-redirect route registered above (no mount).
if LOCAL_TEST:
    if _ART_DIR.is_dir():
        app.mount("/art", StaticFiles(directory=str(_ART_DIR)), name="art")
    else:
        print(f"[game_ui] {_ART_DIR} not found: card images will use the placeholder")


# ------------------------------------------------------------------ game API
# everything that didn't match above (/create_game, /join_game/{id}, /cardpool,
# /ws/{gid}/{player}, ...) is handled by the API.py app
app.mount("/", api_app, name="game-api")


def main():
    import uvicorn

    port = int(os.environ.get("GLOBE_UI_PORT", "8001"))
    print(f"GlobeRunners UI -> http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
