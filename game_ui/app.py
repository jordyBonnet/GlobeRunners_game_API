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
    GET /report                    the balance-lab report (AI vs AI mass simulation,
                                   balance_lab/report.py) + GET /report/assets/*

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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
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
# Balance lab (AI vs AI mass simulation) — see balance_lab/README.md. What ships and
# what does not (see .gitignore): `report.html` + `assets/` ARE committed on purpose
# (a deployed Space only gets what Git tracks, and this route serves exactly those two),
# while the lab DATA/code (results.csv, balance_lab.db, shards/, *.py) stays local.
# So a server without a committed report answers the "not generated yet" page.
_LAB_DIR = ROOT / "balance_lab"
_LAB_REPORT = _LAB_DIR / "report.html"

STATIC_MOUNTS = [
    ("/static", STATIC_DIR, "the frontend static folder is missing!"),
    ("/assets", _ART_ROOT / "cards_assets" if LOCAL_TEST else _HOSTED_DIR / "assets",
     "board backgrounds will be plain"),
    ("/cards_ex", _ART_ROOT / "cards_ex" if LOCAL_TEST else _HOSTED_DIR / "cards_ex",
     "faction placeholder images will be unavailable"),
    # the report's own images (faction logos + support banners, ~220 KB) — the report
    # HTML references them RELATIVELY (src="assets/logo_Dwa.png"), and GET /report
    # rewrites that prefix to /report/assets/ (see balance_report).
    ("/report/assets", _LAB_DIR / "assets", "the balance report will be missing its images"),
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

    # the robot plays in a background task; `tutorial` is the setup page's checkbox
    # (it guarantees the turn-1 teaching play — see ai_decide / *AI wiring*).
    asyncio.create_task(run_ai_loop(game_id, ai_name, tutorial=req.tutorial))
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


# ------------------------------------------------------------------ balance lab report
# `balance_lab/report.py` renders a standalone report.html (self-contained apart from
# its relative `assets/` images). It is served here so the main menu can link to it —
# re-reading the file on every request means a regenerated report shows immediately,
# and `no-store` stops the browser from keeping the previous one on a plain reload.
_NO_STORE = {"Cache-Control": "no-store"}

_REPORT_MISSING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GlobeRunners — balance lab report</title>
<link rel="icon" href="/assets/GlobeRunners_icone.png">
<style>
  body{margin:0;min-height:100vh;background:radial-gradient(1200px 700px at 50% -10%,#1a2444 0%,#0b1020 60%) fixed;
       color:#e8eefc;font-family:"Segoe UI",system-ui,Arial,sans-serif;display:grid;place-items:center;padding:28px}
  .box{max-width:620px;background:#182036;border:1px solid #2d3a5f;border-radius:14px;padding:26px 28px;line-height:1.7}
  h1{font-size:1.25rem;margin:0 0 10px;color:#4fd1c5}
  code{background:#131a2e;border:1px solid #2d3a5f;border-radius:5px;padding:1px 6px;font-size:.85em;color:#9fd4ff}
  pre{background:#131a2e;border:1px solid #2d3a5f;border-radius:10px;padding:12px 14px;overflow-x:auto;font-size:.82rem;color:#c0e884}
  a{color:#f6ad55}
  p.dim{color:#9fb0d0;font-size:.9rem}
</style>
</head>
<body>
  <div class="box">
    <h1>📊 No balance report on this server</h1>
    <p>The report is rendered offline from the AI-vs-AI simulation results
       (<code>balance_lab/results.csv</code>). Only <code>balance_lab/report.html</code>
       and its <code>assets/</code> are committed — the lab data is not — so this page
       appears when no report has been generated/committed yet. Run the lab and
       regenerate it, then reload:</p>
    <pre>python balance_lab/run_lab.py --games 20   # the simulation (all 171 matchups)
python balance_lab/report.py               # render report.html (+ assets/)</pre>
    <p class="dim">No server needed either: <code>balance_lab/report.html</code> is a single
       self-contained page — open it straight from disk.</p>
    <p><a href="/">&larr; Back to the game</a></p>
  </div>
</body>
</html>
"""


@app.get("/report")
@app.get("/report/")
def balance_report():
    """The balance-lab report (AI vs AI win rates over the 18 deck archetypes).

    Serves `balance_lab/report.html` with its relative image paths rewritten to
    /report/assets/* (the STATIC_MOUNTS entry). 404 + a how-to page when there is no
    report to serve — i.e. the lab was never run here and no report.html is committed
    (`balance_lab/report.html` + `assets/` are committed, its data/code are not).
    """
    if not _LAB_REPORT.is_file():
        return HTMLResponse(_REPORT_MISSING_HTML, status_code=404, headers=_NO_STORE)
    html = _LAB_REPORT.read_bytes().replace(b'src="assets/', b'src="/report/assets/')
    return HTMLResponse(html, headers=_NO_STORE)


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
