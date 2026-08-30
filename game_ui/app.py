"""GlobeRunners — Web UI (FastAPI).

Serves the frontend (static/) and embeds the game API defined in API.py: a single
server exposes everything (the API's REST + WebSocket, plus the UI routes below).

Launch (from the project root):
    python game_ui/app.py                 # -> http://127.0.0.1:8001
or:
    uvicorn game_ui.app:app --port 8001   (GLOBE_UI_PORT env var to change the port)

Routes added on top of API.py:
    GET /                          index.html (setup page: deck + create/join)
    GET /static/*                  frontend css / js
    GET /art/<card_id>.png         card art (external folder, placeholder if absent)
    GET /assets/*                  game assets (biomes, markers, logos...)
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
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

# the API.py app (engine REST + WebSocket) — the webapp "interacts" with it by embedding it
from API import CreateGameRequest, app as api_app  # noqa: E402
import engine.game_engine as ge  # noqa: E402
from models import PlayerState  # noqa: E402
from ai_driver import random_ai_deck, run_ai_loop  # noqa: E402

STATIC_DIR = HERE / "static"

# external asset folders (card art + game assets)
ART_DIR = Path(r"C:\Users\jordy\Documents\python\projects\GenAI_TCG\lib\artdesign\cards_framed_0.6")
ASSETS_DIR = Path(r"C:\Users\jordy\Documents\python\projects\GenAI_TCG\lib\artdesign\cards_assets")

PLACEHOLDER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="180" height="260" viewBox="0 0 180 260">
  <rect x="4" y="4" width="172" height="252" rx="14" fill="#1d2438" stroke="#4a5578" stroke-width="3"/>
  <circle cx="90" cy="105" r="46" fill="#2c3a5e"/>
  <path d="M55 120 q18 -22 35 -8 q16 13 35 -6" stroke="#7f9bd4" stroke-width="5" fill="none" stroke-linecap="round"/>
  <text x="90" y="195" font-family="Segoe UI, Arial" font-size="17" fill="#cdd8f2" text-anchor="middle">Unknown card</text>
  <text x="90" y="222" font-family="Segoe UI, Arial" font-size="13" fill="#6d7ba0" text-anchor="middle">(missing art)</text>
</svg>"""

app = FastAPI(title="GlobeRunners - Web UI")


# ------------------------------------------------------------------ routes UI
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


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
    pool_ids = set(ge.get_cardpool()['card_id'].to_list())
    bad = [c for c in req.deck if c not in pool_ids]
    if bad:
        raise HTTPException(status_code=400, detail=f"unknown card id(s) in deck: {bad[:5]}")

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


@app.get("/placeholder.svg")
def placeholder():
    return Response(PLACEHOLDER_SVG, media_type="image/svg+xml")


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
# no-cache on /static: the layout evolves fast, avoid the browser serving stale css/js
class NoCacheStatic(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

app.add_middleware(NoCacheStatic)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if ART_DIR.is_dir():
    app.mount("/art", StaticFiles(directory=str(ART_DIR)), name="art")
else:
    print(f"[game_ui] art dir not found, card images will use the placeholder: {ART_DIR}")

if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
else:
    print(f"[game_ui] assets dir not found, board backgrounds will be plain: {ASSETS_DIR}")


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
