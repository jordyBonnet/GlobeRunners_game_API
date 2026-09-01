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
    drop_tokens: Dict[int, int] = {}        # pet_trap (engine_version 8): cell index -> number of drop tokens on that cell (triggered when any player token arrives on the cell)
    winner: Optional[str] = None
    first_player_passed: bool = False
    second_player_passed: bool = False
    temperature: Optional[int] = None        # planet temperature, rolled with a die at game start
    day_night: Optional[str] = None          # "day" (card face up) or "night" (card face down), drawn at game start
    cataclysm_pile: Optional[List[str]] = None  # cataclysm pile: one card per biome (top = first element), rotated on every cataclysm trigger
    engine_version: Optional[int] = None     # rules version the game is played with (9 = + drop_on_board condition (any drop/trap on the earth); 8 = + pet_trap effect / drop tokens / INSTANT play-time effects; 7 = + copy_effect effect; 6 = + effect_canceled effect; 5 = + grappling_hook effect; 4 = + avalanche effect; 3 = + cataclysm condition; 2 = faction biome bonus + conditional block effect)
    log: List[Dict] = []                    # turn log (PUBLIC info only): one entry per resolved turn -> per stopover -> per player: card, condition met, effect, negative effects (blocked/canceled...), notes (cataclysm, grappling copy...), positions

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str) -> "GameState":
        return cls.model_validate_json(data)
