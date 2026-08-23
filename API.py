""" FastAPI wrapper around the game engine (engine/game_engine.py), ready to be served on the web.

Endpoints
---------
POST /create_game              body {"name": str, "deck": [card_id, ...]}
                               -> creates a new game with p1, returns {"success", "game_id", "player_id"}
POST /join_game/{game_id}      body {"name": str, "deck": [...]}
                               -> registers p2 and initializes the game (hands dealt, planet rolled),
                                  returns p2's personalized state
GET  /game/{game_id}           -> full game state as JSON (no hidden info, for debugging/analysis)
GET  /cardpool                 -> card pool as a list of dicts

WS   /ws/{game_id}/{player_name}
                               the client sends one JSON message at a time:
                               {"cards": [...], "to": "...", "mode": "...", "pendings": []}
                               the server replies with the personalized game state (opponent's
                               hand/mana/deck hidden). The first message received on connect is
                               the initial personalized state.

Run locally:  python API.py        (serves http://127.0.0.1:8000)
"""

import json
import re

from fastapi import FastAPI, WebSocket, HTTPException, WebSocketDisconnect
from pydantic import BaseModel
from typing import List

from engine import game_engine as ge
from models import PlayerState


class CreateGameRequest(BaseModel):
    name: str
    deck: List[str]


app = FastAPI(title="GlobeRunners API")

# "turn N - waiting for first/second player (NAME) to play" -> only NAME may act
_TURN_RE = re.compile(r"waiting for (?:first|second) player \((.+?)\) to play")


def _expected_player_name(state: str):
    """Return the name of the player who must act now, or None if any registered player may act."""
    m = _TURN_RE.search(state)
    return m.group(1) if m else None


def _check_deck(deck: List[str]):
    pool_ids = set(ge.get_cardpool()['card_id'].to_list())
    bad = [c for c in deck if c not in pool_ids]
    if bad:
        raise HTTPException(status_code=400, detail=f"unknown card id(s) in deck: {bad[:5]}")


@app.post("/create_game")
def create_game(req: CreateGameRequest):
    """Create a new game with the first player (p1). Returns the game_id to share with p2."""
    if not req.deck:
        raise HTTPException(status_code=400, detail="deck must not be empty")
    _check_deck(req.deck)
    player = PlayerState(name=req.name, deck=list(req.deck))
    game_id = ge.create_new_game(player.model_dump())
    return {"success": True, "game_id": game_id, "player_id": req.name}


@app.post("/join_game/{game_id}")
def join_game(game_id: str, req: CreateGameRequest):
    """Register the second player and initialize the game. Returns p2's personalized state."""
    if not req.deck:
        raise HTTPException(status_code=400, detail="deck must not be empty")
    _check_deck(req.deck)
    try:
        conn, _, current_game = ge.get_current_game(game_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"game {game_id} not found")
    conn.close()

    if req.name in current_game.players:
        raise HTTPException(status_code=409, detail=f"player '{req.name}' is already in the game")
    if len(current_game.players) >= 2:
        raise HTTPException(status_code=409, detail="game is full (max 2 players)")

    player = PlayerState(name=req.name, deck=list(req.deck))
    return json.loads(ge.p2_connect_to_game(player.model_dump(), game_id))


@app.get("/game/{game_id}")
def get_game(game_id: str):
    """Full game state (no hidden information) - for debugging/analysis."""
    try:
        return json.loads(ge.get_game(game_id))
    except Exception:
        raise HTTPException(status_code=404, detail=f"game {game_id} not found")


@app.get("/cardpool")
def cardpool():
    """Card pool as a list of dicts."""
    return ge.get_cardpool().to_dicts()


@app.websocket("/ws/{game_id}/{player_name}")
async def websocket_endpoint(websocket: WebSocket, game_id: str, player_name: str):
    await websocket.accept()

    try:
        conn, _, current_game = ge.get_current_game(game_id)
    except Exception:
        await websocket.send_text(json.dumps({"success": False, "message": f"game {game_id} not found"}))
        await websocket.close(code=1008)
        return
    conn.close()

    if player_name not in current_game.players:
        await websocket.send_text(json.dumps({
            "success": False,
            "message": f"player '{player_name}' is not registered in game {game_id} (use /create_game or /join_game first)"
        }))
        await websocket.close(code=1008)
        return

    # send the initial personalized state to the connecting player
    await websocket.send_text(ge.current_game_json(player_name, current_game))

    try:
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"success": False, "message": "invalid JSON"}))
                continue

            # always check against the authoritative game state in the DB (a connection only
            # sees updates when IT sends a message, so per-connection state can be stale)
            conn, _, current_game = ge.get_current_game(game_id)

            # reject out-of-turn messages (the engine would otherwise attribute them to the wrong player)
            expected = _expected_player_name(current_game.state)
            if expected is not None and expected != player_name:
                conn.close()
                await websocket.send_text(json.dumps({
                    "success": False,
                    "message": f"not your turn - waiting for {expected} to play"
                }))
                continue

            # rebuild the player state from the authoritative game (own record is complete)
            player = current_game.players[player_name].model_copy()
            player.message = message
            conn.close()

            response_json = ge.handle_websocket_message(game_id, player)
            await websocket.send_text(response_json)
    except WebSocketDisconnect:
        print(f"WebSocket disconnected: {game_id=} {player_name=}")
    except Exception as e:
        print(f"WebSocket error: {e}")
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed, ignore


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
