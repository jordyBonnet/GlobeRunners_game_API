from pydantic import BaseModel
from typing import List, Dict, Optional

class Card(BaseModel):
    id: str
    name: str
    mana_cost: int
    type: str
    effect: Optional[Dict] = None

class PlayerState(BaseModel):
    name: str
    hand: Optional[List[str]] = None
    mana: Optional[List[str]] = None
    mana_spend: Optional[int] = 0
    deck: Optional[List[str]] = None
    discard: Optional[List[str]] = None
    current_position: Optional[int] = 0
    message: Optional[Dict] = None
    messages_history: List[Dict] = []
    action_chain: List[Dict] = []    # a list of dict of cards send by player
    dwelling: Optional[str] = None  # dwelling
    pendings: List[str] = []        # pending effects

class GameState(BaseModel):
    id: str
    players: Dict[str, PlayerState] = None      # refers to PlayerState model
    turn_order: List[str] = []                  # list of players names in turn order (this is automatically set in game engine)
    turn: int = 1
    state: str = "waiting for 2nd player"
    message: Optional[Dict] = None
    earth: Optional[List[List[str]]] = []   # a list of token's list for every position
    winner: Optional[str] = None
    first_player_passed: bool = False
    second_player_passed: bool = False

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str) -> "GameState":
        return cls.model_validate_json(data)
