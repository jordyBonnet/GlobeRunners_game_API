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
    dwelling: Optional[str] = None  # dwelling (engine_version 12: card_id of the dwelling card on the board, e.g. the engineers' refinery)
    dwelling_slot: Optional[int] = None  # dwelling (engine_version 12): the stopover column (0-4) where the placeholder renders (set at placement time: engine_version 15 = the next position in the player's OWN chain (ge._player_stopover), 14 = the k-th FREE stopover (shared skip rule, ge._next_free_stopover_v14), < 14 = the k-th column; cleared in the cleaning phase so the placeholder only shows during the placement turn)
    dwelling_tapped: bool = False   # dwelling (engine_version 12): the dwelling card was already tapped this turn (1 tap/turn, untapped in the cleaning phase)
    play_count: int = 0            # stopover positions (engine_version 15): number of plays (move/defend/dwelling) this player has made THIS turn — the (play_count+1)-th play takes position (rooted count) + play_count + 1 in that player's OWN chain; reset to 0 in the cleaning phase
    landmine_blocked: bool = False  # landmine (engine_version 12): blocked by a landmine drop until the end of the current turn (move cards canceled — no effect, no advancing — except unstoppable)
    faction: Optional[str] = None  # main faction of the player (e.g. "Dwarves", "Demons", …) — derived from the deck at game creation; public info (needed for biome conditions + placeholder art)
    pendings: List[str] = []        # pending effects
    pending_slots: List = []   # doctors (engine_version 16): the PENDING PLACEHOLDERS currently showing — a per-turn display list (cleared in the cleaning phase, so it is NOT parallel to the persistent `pendings` zone). engine_version 20: when the attached pending card was placed THIS turn, its [card_name, slot] pair STAYS (the placeholder marks the consumed trip-chain position — v19 and below removed it on attachment, which freed the position and desynced the frontend's next-slot mirror). v19: each entry is a [card_name, slot] pair — attachment removed the pair BY CARD NAME (the old index-splice deleted the wrong placeholder once pendings and pending_slots diverged across turns). Legacy entries (bare int slot, v16-18; None = no placeholder, v18 lab tap) are still accepted by every reader. engine_version 18: the laboratory TAP's 'epo' got a None entry (no placeholder); v19+: it gets no entry at all

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
    engine_version: Optional[int] = None     # rules version the game is played with (20 = + the placeholder of a pending card ATTACHED to a main card this same turn STAYS IN PLACE (≤19 removed it on attachment — the freed position desynced the frontend's next-slot suggestion, which offered the main card's own slot for the next play); 19 = + pending_slots is a list of [card, slot] pairs — pending placeholders are a per-turn display list (cleared each turn) NOT parallel to the persistent `pendings` zone, so attachment removed the placeholder BY CARD NAME (≤18 spliced by index in `pendings`, which deleted the WRONG placeholder once the lists diverged across turns — game 26_09_17_21_33_39_d5oEd turn 4); the laboratory TAP's 'epo' gets no slot entry; all readers normalize the legacy bare-int/None shapes. 18 = + the laboratory TAP is a QUICK action with NO trip-chain placeholder — the tap's 'epo' pending card gets a None entry in pending_slots (no placeholder, no position consumed); games 16-17 keep the old behavior (the tap's 'epo' occupied a position). 17 = + board-furniture PLACEHOLDERS (doctor pending cards / dwelling cards) OCCUPY a trip-chain position: ge._player_chain includes them as empty 'placeholder' entries at their recorded stopover position (position = 5 - column), so a grappling_hook / copy_effect facing a placeholder faces an EMPTY position and copies NOTHING - the trip chain matches the board display. Games < 17 keep the legacy chain (placeholders excluded, plays renumbered densely). 16 = + Doctors support faction: 4 pending cards (epo/virus/bloodtest/mercurochrome) + laboratory dwelling; pending zone (to: 'pending_zone'), attachment to a main card (pendings: [name], effect fires when the main card's condition is met), laboratory tap adds an 'epo' pending card. 15 = + PER-PLAYER stopovers: each player has their OWN 5 stopover slots (position 1..5, columns 4,3,2,1,0). A player's rooted cards occupy the LEADING positions of that player's OWN trip chain (position 1, 2, …), and the player's plays go AFTER them (position R+1, …). A player's rooted cards never shift the opponent's positions. Rooted cards on the trip chain are NORMAL cards that apply only their basic advancing (no condition, no effect, but BLOCKABLE by the opponent's defend at the same position). Facing/blocking/grappling/copy are all by per-player position number. ge._player_stopover / ge._player_chain are the single source of truth; the robot (ai_driver) calls them directly and the frontend mirrors them. 14 = + stopover-skip rule (SHARED stopover, superseded by 15's per-player model): a stopover RESERVED by board furniture (a rooted-on-board card or a dwelling placeholder) is SKIPPED by the next play of either player (ge._next_free_stopover_v14); games < 14 keep the legacy plain convention (k-th column, no skip); 13 = + discard selection: discard / discard_oppo effects PAUSE the trip chain and the discarding player CHOOSES the card(s) (to: 'discard_pile'); 12 = + Engineers support faction: 4 drop cards (boost/trampoline/gluetrap/landmine) + refinery dwelling (tap 1x/turn to draw 1) + support cards cost their mana_cost; 11 = + wrecking_ball effect (INSTANT: removes the opponent's dwelling card); 10 = + rooted effect (card survives the cleaning phase on a stopover, one-shot, 1-turn cooldown); 9 = + drop_on_board condition (any drop/trap on the earth); 8 = + pet_trap effect / drop tokens / INSTANT play-time effects; 7 = + copy_effect effect; 6 = + effect_canceled effect; 5 = + grappling_hook effect; 4 = + avalanche effect; 3 = + cataclysm condition; 2 = faction biome bonus + conditional block effect)
    rooted_on_board: List[Dict] = []        # rooted (engine_version 10): cards sitting on a stopover from a previous turn's rooted effect: [{card_id, owner, stopover}]; discarded at the end of the FOLLOWING turn
    rooted_this_turn: List[Dict] = []       # rooted (engine_version 10): cards that earned a rooted token THIS turn (awaiting the end-of-turn placement onto a free stopover): [{card_id, owner}]
    rooted_history: Dict[str, int] = {}     # rooted (engine_version 10): card_id -> last turn it earned a rooted token (1-turn cooldown: cannot earn another the very next turn)
    board_drops: List[Dict] = []            # engineers (engine_version 12): drop tokens on the board (boost/trampoline/gluetrap/landmine): [{cell, kind, owner}]; each fires once when ANY token arrives on its cell, then is consumed
    log: List[Dict] = []                    # turn log (PUBLIC info only): one entry per resolved turn -> per stopover -> per player: card, condition met, effect, negative effects (blocked/canceled...), notes (cataclysm, grappling copy...), positions
    pending_discard: Optional[Dict] = None  # discard selection (engine_version 13): the trip chain is PAUSED waiting for the discarding player's choice: {player, n}; consumed by the to:'discard_pile' message (None when the chain is not paused)
    chain_resume: Optional[Dict] = None     # discard selection (engine_version 13): the trip chain's resume context (index, stage, log entries, per-player flags) saved by process_trip_chain when it pauses; cleared when the chain resumes

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str) -> "GameState":
        return cls.model_validate_json(data)
