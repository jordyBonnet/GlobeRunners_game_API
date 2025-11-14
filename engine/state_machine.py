import json
import os
from typing import Dict, List, Optional
from models import Card, GameState, PlayerState
import sqlite3
from datetime import datetime

CARDS_DIR = os.path.join(os.path.dirname(__file__), '../cards')

DB_PATH = os.path.join(os.path.dirname(__file__), '../games/games.db')

class GameEngine:
    def __init__(self, player: PlayerState):
        player.id = player.name + '_' + ''.join(__import__('secrets').choice(__import__('string').ascii_letters + __import__('string').digits) for _ in range(4))
        game_id = datetime.now().strftime("%y_%m_%d_%H_%M_%S") + '_' + ''.join(__import__('secrets').choice(__import__('string').ascii_letters + __import__('string').digits) for _ in range(5))
        self.state = GameState(
            id=game_id,
            players={player.id: player.model_dump()},
            turn_order=[],
            current_turn=0,
            chain=[],
            phase="waiting players",
            winner=None,
            step=0
        )

    def save_to_db(self):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS games (game_id TEXT PRIMARY KEY, state_json TEXT)''')
        state_json = self.state.model_dump_json()
        c.execute('''INSERT OR REPLACE INTO games (game_id, state_json) VALUES (?, ?)''', (self.state.game_id, state_json))
        conn.commit()
        conn.close()

    def handle_websocket_message(self, player_id: str, message: str):   # main part of the game code
        # Process incoming WebSocket messages
        
        if len(self.state.players) != 2:
            self.state.phase = "waiting players"
            return "Waiting for the two players to join..."

        if len(self.state.players) == 2 and self.state.phase=="waiting players":
            self.state.phase = "Init game"
            return "Both players have joined. The game is starting...\n"

        if self.state.phase == "Init game":
            self.state.phase = "player turn"
            return "The game has started. It's your turn."

        if message.startswith("card "):
            card_id = message.split(" ")[1]
            # Example: Play the card
            self.play_card(player_id, card_id)
            return f"Played card: {card_id}"
        return "Unknown command"

    @staticmethod
    def load_from_db(game_id: str):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''SELECT state_json FROM games WHERE game_id = ?''', (game_id,))
        row = c.fetchone()
        conn.close()
        if row:
            state = GameState.model_validate_json(row[0])
            engine = GameEngine(game_id)
            engine.state = state
            return engine
        return None

    def add_player(self, player_id: str):
        # ...existing code...
        pass

    def submit_deck(self, player_id: str, deck: List[str]):
        # ...existing code...
        pass

    def start_game(self):
        # ...existing code...
        pass

    def play_card(self, player_id: str, card_id: str, slot: int):
        # ...existing code...
        pass

    def pass_turn(self, player_id: str):
        # ...existing code...
        pass

    def defend(self, player_id: str, card_ids: List[str], slot: int):
        # ...existing code...
        pass

    def concede(self, player_id: str):
        # ...existing code...
        pass

    def update_state(self):
        # ...existing code...
        pass

    def load_card(self, card_id: str) -> Optional[Card]:
        card_path = os.path.join(CARDS_DIR, f"{card_id}.json")
        if os.path.exists(card_path):
            with open(card_path, 'r') as f:
                return Card(**json.load(f))
        return None
