from fastapi import FastAPI, WebSocket, HTTPException, WebSocketDisconnect
from typing import Dict
from models import GameState, PlayerState
from engine import game_engine as ge
from pydantic import BaseModel
import time

app = FastAPI()


@app.post("/create_game/{player_name}")
def create_game(player_name: str):
    game_id = ge.create_new_game(player_name)
    return {"success": "True", "game_id": game_id, "player_id": player_name}

@app.websocket("/ws/{game_id}/{player_name}")
async def websocket_endpoint(websocket: WebSocket, game_id: str, player_name: str):
    await websocket.accept()
    await websocket.send_text("Connected to game.")
    try:
        while True:
            data = await websocket.receive_text()

            response = ge.handle_websocket_message(game_id, player_name, data)

            await websocket.send_text(f"Echo: {response}")
    except WebSocketDisconnect:
        print(f"WebSocket disconnected: {game_id=} {player_name=}")
    except Exception as e:
        print(f"WebSocket error: {e}")
        # Optionally, try to close if not already closed
        try:
            await websocket.close()
        except RuntimeError:
            pass  # Already closed, ignore

# TODO: Add endpoints for play, pass, defend, concede, get state

def main():
    print("Hello from ydb-g!")


if __name__ == "__main__":
    main()
