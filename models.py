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
    dwelling: Optional[str] = None  # dwelling: card_id of the dwelling card on the board (refinery / laboratory / black_hole)
    dwelling_slot: Optional[int] = None  # dwelling: the stopover column (0-4) where the placeholder renders (set at placement time: the next position in the player's OWN chain — ge._player_stopover); cleared in the cleaning phase so the placeholder only shows during the placement turn
    dwelling_tapped: bool = False   # dwelling: the dwelling card was already tapped this turn (1 tap/turn, untapped in the cleaning phase)
    play_count: int = 0            # stopover positions: number of plays (move/defend/dwelling) this player has made THIS turn — the (play_count+1)-th play takes position (rooted count) + play_count + 1 in that player's OWN chain; reset to 0 in the cleaning phase. A wrecking_ball that removes the player's dwelling card does NOT touch play_count (the placeholder's consumed position stays consumed — the placeholder STAYS in place and the next play lands after it)
    landmine_blocked: bool = False  # landmine: blocked by a landmine drop until the end of the current turn (move cards canceled — no effect, no advancing — except unstoppable)
    faction: Optional[str] = None  # main faction of the player (e.g. "Dwarves", "Demons", …) — derived from the deck at game creation; public info (needed for biome conditions + placeholder art)
    pendings: List[str] = []        # pending zone (doctors): the pending cards currently in the player's zone
    pending_slots: List = []   # doctors: the PENDING PLACEHOLDERS currently showing — a per-turn display list (cleared in the cleaning phase, so it is NOT parallel to the persistent `pendings` zone). Each entry is a [card_name, slot] pair. When the attached pending card was placed THIS turn, its pair STAYS (the placeholder marks the consumed trip-chain position). The laboratory TAP's 'epo' gets no entry (no placeholder)

class GameState(BaseModel):
    id: str
    players: Dict[str, PlayerState] = None      # refers to PlayerState model
    turn_order: List[str] = []                  # list of players names in turn order (this is automatically set in game engine)
    turn: int = 1
    state: str = "waiting for 2nd player"
    message: Optional[Dict] = None
    earth: Optional[List[List[str]]] = []   # a list of token's list for every position
    drop_tokens: Dict[int, int] = {}        # pet_trap: cell index -> number of drop tokens on that cell (triggered when any player token arrives on the cell)
    winner: Optional[str] = None
    first_player_passed: bool = False
    second_player_passed: bool = False
    temperature: Optional[int] = None        # planet temperature, rolled with a die at game start (can be changed by a Mages thermic_flux card — clamped to 1..20)
    temperature_initial: Optional[int] = None  # the temperature rolled at game start (never changed). Lets the replay evaluate temp_* conditions correctly for games with a thermic_flux (the stored `temperature` is the FINAL value, which would be wrong for a temp_* condition resolved BEFORE the change). The replay falls back to the stored `temperature` when this is None.
    day_night: Optional[str] = None          # "day" (card face up) or "night" (card face down), drawn at game start
    day_night_fixed: bool = False            # Celestial_reversal (Mages): True once a Celestial_reversal card has been played — the day/night is then FIXED (set to the player's chosen value) for the rest of the game and no longer flips each turn (the cleaning phase skips the flip)
    nobodymoves_active: bool = False         # nobodymoves (Mages): True while the current turn's MOVEMENT is LOCKED — set at play time (an INSTANT effect) when a nobodymoves card is played, cleared in the cleaning phase. While True, ALL players' MOVE cards still RESOLVE (condition checked + non-movement effects fire + defend/block race happens) but their MOVEMENT is suppressed: no basic advancing, no movement effects (advancing/backward/jump/advancing_oppo/backward_oppo/avalanche), no pending epo/virus, no grappling/copy copies; the only exception is an "unstoppable" card whose condition is met, which still moves; DEFEND cards are unaffected
    cataclysm_pile: Optional[List[str]] = None  # cataclysm pile: one card per biome (top = first element), rotated on every cataclysm trigger
    earth_rotation: int = 0                      # black_hole (Mages): the CUMULATIVE rotation of the earth in CELLS (signed: clockwise positive, counter-clockwise negative — the direction is chosen per tap). Drives the frontend background CSS (transform: rotate(earth_rotation * 15deg), transform-origin: center; 1 cell = 15deg since the board is 24 cells / 360deg) and is the rotation log the replay reconstructs from
    earth_initial_b0: Optional[str] = None       # black_hole (Mages): the biome of cell 0 (the top) at game init — the base for the background art (the frontend pins the base image to this and ROTATES it by earth_rotation * 15deg, so the art tracks the engine's rotated biome positions instead of jumping to a new image). Set at game init (earth[0][0]). When None, the frontend falls back to the current cell-0 biome.
    engine_version: Optional[str] = None     # rules version the game is played with (string; all current rules are active for every new game)
    rooted_on_board: List[Dict] = []        # rooted: cards sitting on a stopover from a previous turn's rooted effect: [{card_id, owner, stopover}]; discarded at the end of the FOLLOWING turn
    rooted_this_turn: List[Dict] = []       # rooted: cards that earned a rooted token THIS turn (awaiting the end-of-turn placement onto their owner's leading stopover): [{card_id, owner}]
    rooted_history: Dict[str, int] = {}     # rooted: card_id -> last turn it earned a rooted token (1-turn cooldown: cannot earn another the very next turn)
    board_drops: List[Dict] = []            # engineers: drop tokens on the board (boost/trampoline/gluetrap/landmine): [{cell, kind, owner}]; each fires once when ANY token arrives on its cell, then is consumed
    log: List[Dict] = []                    # turn log (PUBLIC info only): one entry per turn -> 'instant' (list of {player, what}: the play-time instant effects + dwelling taps of the turn — pet_trap, engineer drops, wrecking_ball, the Mages' instant cards, refinery/laboratory/black_hole taps — rendered at the TOP of the turn, before the stopovers) + 'stopovers' -> per player: card, condition met, effect, negative effects (blocked/canceled...), notes (cataclysm, grappling copy...), positions
    pending_discard: Optional[Dict] = None  # discard selection: the trip chain is PAUSED waiting for the discarding player's choice: {player, n}; consumed by the to:'discard_pile' message (None when the chain is not paused)
    chain_resume: Optional[Dict] = None     # discard selection: the trip chain's resume context (index, stage, log entries, per-player flags) saved by process_trip_chain when it pauses; cleared when the chain resumes

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str) -> "GameState":
        return cls.model_validate_json(data)
