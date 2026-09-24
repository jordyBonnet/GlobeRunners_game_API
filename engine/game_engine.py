""" game engine that receives and processes game events from FastAPI WebSocket connections """

import os
from datetime import datetime
import sqlite3
from models import Card, GameState, PlayerState
import json
import random
import re
import polars as pl

CARDS_DB_PATH = os.path.join(os.path.dirname(__file__), '../cards/cardpool.parquet')
CARDS_DB = pl.read_parquet(CARDS_DB_PATH)

# Support faction cards (cards/support_factions.parquet): card_name -> row.
# Not in the main pool (CARDS_DB) - they are mixed into a player's deck (10 of them)
# and resolve as 0-cost no-ops unless a rule covers them (all three support
# factions — Engineers, Doctors, Mages — are implemented).
SUPPORT_DB_PATH = os.path.join(os.path.dirname(__file__), '../cards/support_factions.parquet')
SUPPORT_DB = {}
if os.path.exists(SUPPORT_DB_PATH):
    for _r in pl.read_parquet(SUPPORT_DB_PATH).iter_rows(named=True):
        SUPPORT_DB[_r['card_name']] = _r

# ---------------------------------------------------------------------------
# Engineers (support faction)
#
# 4 DROP cards: played in MOVE mode; the play must carry the target cell
# (message 'cell', 0..23). At PLAY TIME the card leaves a VISIBLE token on that
# cell of the earth (GameState.board_drops). The FIRST token that ARRIVES on the
# cell (stepping or a jump landing) fires the drop, then it is consumed. Placing
# a drop on a cell where a token already stands does NOT fire it.
ENGINEER_DROPS = (
    'boost',        # +2 advancing (stepped through the cells)
    'trampoline',   # +2 jump (teleport, only the landing cell is checked)
    'gluetrap',     # -1 knockback (direct, clamped at cell 0, no re-trigger)
    'landmine',     # the arriving player is BLOCKED for the rest of the turn:
                    # its move cards are canceled (no effect, no advancing) - the
                    # only exception is an unstoppable card with its condition met
)
# 1 DWELLING card: placed in the dedicated dwelling zone (one card at a time, from
# the hand), PERMANENT (not discarded at end of turn), tapped at most ONCE per
# turn (free tap; untapped in the cleaning phase). Removed by wrecking_ball.
ENGINEER_DWELLING = 'refinery'   # tap effect: draw 1 card

# Doctors (support faction)
# Pending cards (placed in the pending zone, attachable to a main card — max 1):
#   epo           +1 advancing (added to the main card's effect)
#   virus          -1 knockback (added to the main card's effect)
#   bloodtest      OPPONENT discards 1 card (treated as a `discard_oppo` effect:
#                  the opponent CHOOSES which card — same pause/choice flow, see
#                  the pending_discard mechanism in process_trip_chain)
#   mercurochrome  unstoppable (the main card ignores blocks — checked at block time)
# Dwelling card (placed in the dwelling slot, tap 1x/turn):
#   laboratory     when tapped, adds an "epo" pending card to the player's pending zone
DOCTOR_PENDING = ('epo', 'virus', 'bloodtest', 'mercurochrome')
DOCTOR_DWELLING = 'laboratory'   # tap effect: add an 'epo' pending card

# Mages (support faction)
# Celestial_reversal (mana_cost 2): an INSTANT play-time effect — the player CHOOSES
# day or night (message 'day_night': 'day' or 'night') and it is FIXED for the rest
# of the game (game.day_night set to the choice, game.day_night_fixed = True, so the
# day/night no longer flips each turn). The card is played in MOVE mode onto a stopover
# (it occupies a position) and resolves as a NO-OP (not in the main pool: no advance,
# no effect) — like a placeholder.
MAGE_CELASTIAL_REVERSAL = 'Celestial_reversal'
# nobodymoves (mana_cost 3): an INSTANT play-time effect — it LOCKS ALL PLAYERS'
# MOVEMENT for the rest of the TURN. Their MOVE cards still RESOLVE (condition check,
# non-movement effects like draw/ramp/discard/taxation still fire, and the defend/block
# race still happens) but their MOVEMENT is suppressed: no basic advancing, no movement
# effects (advancing/backward/jump/advancing_oppo/backward_oppo/avalanche), no pending
# epo/virus, and no grappling/copy copies. The only exception is an "unstoppable" card
# whose condition is met (it still moves) — the same exception as the landmine, but the
# landmine CANCELS the card while nobodymoves LOCKS its movement. DEFEND cards are
# unaffected (they never advance). The lock is game-level (game.nobodymoves_active = True),
# set at play time (cannot be blocked / canceled / conditioned) and cleared in the cleaning
# phase. The card is played in MOVE mode onto a stopover and resolves as a NO-OP (not in
# the main pool: no advance, no effect) — like a placeholder.
MAGE_NOBODYMOVES = 'nobodymoves'
# thermic_flux (mana_cost 2): an INSTANT play-time effect — the
# player CHOOSES to INCREASE (+4 °C) or DECREASE (−4 °C) the planet temperature
# (message 'temp_change': 'up' or 'down') and the engine applies that delta to
# game.temperature, CLAMPED to 1..20, PERMANENTLY (mutated at play time, so it cannot
# be blocked / canceled / conditioned). The new value is the FINAL temperature for the
# rest of the game (it is read by the temp_inf_6 / temp_inf_11 / temp_sup_9 /
# temp_sup_15 conditions at resolution time). The card is played in MOVE mode onto a
# stopover (it occupies a position) and resolves as a NO-OP (not in the main pool: no
# advance, no effect) — like a placeholder.
MAGE_THERMIC_FLUX = 'thermic_flux'
# Apocalypticritual (mana_cost 3): an INSTANT play-time effect —
# the player CHOOSES THE ORDER OF ALL 4 CATACLYSM CARDS: a permutation of the 4
# biomes (message 'cataclysm_order': e.g. ['OC','DE','JU','MO']) and the engine SETS
# the cataclysm pile to exactly that order (index 0 = the biome that strikes NEXT),
# PERMANENTLY (until the next Apocalypticritual reorders it — mutated at play time,
# so it cannot be blocked / canceled / conditioned). The new order is read by
# trigger_cataclysm at resolution time (a 'cataclysm'-condition card strikes the top
# of the pile, then rotates it). The card is played in MOVE mode onto a stopover
# (it occupies a position) and resolves as a NO-OP (not in the main pool: no
# advance, no effect) — like a placeholder.
MAGE_APOCALYPTICRITUAL = 'Apocalypticritual'
# black_hole (mana_cost 3): a DWELLING card (like the Engineers'
# refinery and the Doctors' laboratory) — NOT an instant-at-play card. It is PLACED in
# the dwelling zone (to: 'dwelling', 1 card, costs its mana_cost, stopover placeholder,
# removed by wrecking_ball) and its effect fires on a TAP (mode 'dwelling_activation',
# free + once per turn, gated by dwelling_tapped). The tap ROTATES THE EARTH 3 CELLS in
# the player's chosen direction (message 'rotation': 'cw' or 'ccw'): the 4 biomes shift
# position in current_game.earth (the biome codes at earth[i][0] rotate by 3 cells) while
# EVERY TOKEN — both players' positions, the pet_trap drop tokens, and the engineer board
# drops — STAYS on its own cell index (the cells are addressed by index, so a rotation
# changes which BIOME a given cell belongs to, not where the tokens are). The rotation is
# cumulative (each tap rotates from the current order; 8 distinct states since 24/3 = 8).
# earth_rotation (signed cumulative cells, drives the frontend background CSS) and
# earth_initial_b0 (cell-0 biome at game init, the base for the background art) record it.
MAGE_BLACK_HOLE = 'black_hole'

DB_PATH = os.path.join(os.path.dirname(__file__), '../games/games.db')

start_cards_in_hand = 6
start_cards_in_mana = 3
turn_n_draw_cards = 3
n_cells_by_biome = 6
BIOMES = ['OC', 'MO', 'DE', 'JU']
win_position = 24

def _derive_faction(deck):
    """Derive the player's main faction from their deck (first main card's faction).
    Returns the full faction name (e.g. "Dwarves") or None."""
    if not deck:
        return None
    for card_id in deck:
        rows = CARDS_DB.filter(pl.col('card_id') == card_id)
        if not rows.is_empty():
            return str(rows['faction'][0])
    return None

def create_new_game(player: PlayerState):
    """ create a new game directly adding p1 (that just created the game)
     returns the game_id """
    game_id = datetime.now().strftime("%y_%m_%d_%H_%M_%S") + '_' + ''.join(__import__('secrets').choice(__import__('string').ascii_letters + __import__('string').digits) for _ in range(5))
    if not os.path.exists(DB_PATH):
        print(f'database does not exist, creating new database at {DB_PATH}')
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS games (game_id TEXT PRIMARY KEY, state_json TEXT)''')
        conn.commit()
        conn.close()
    
    # Derive the player's main faction from their deck (public info: needed for
    # biome conditions, placeholder art, and the frontend's faction display).
    player['faction'] = _derive_faction(player.get('deck') or [])
    game = GameState(
        id=game_id,
        players={player['name']: player},
    )

    # Defense in depth: a deck with the same card id more than once means that card
    # WILL appear multiple times in hand/zones (the engine conserves whatever the deck
    # contains). The REST layer (check_deck) rejects such decks for main cards and for
    # >2 support copies — but any client that bypasses it (old cached UI, manual POST)
    # would otherwise create the game silently. Flag it loudly in the server log.
    try:
        deck = player.get('deck') or []
        seen, deck_dups = set(), set()
        for c in deck:
            if c in seen:
                deck_dups.add(c)
            seen.add(c)
        if deck_dups:
            print(f'\n!!! WARNING: game {game_id} — deck of {player["name"]} has DUPLICATE card(s): '
                  f'{sorted(deck_dups)} (deck size {len(deck)}). Duplicates will show up in hand/zones.\n')
    except Exception:
        pass

    # Insert the new game into the database
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO games (game_id, state_json) VALUES (?, ?)", (game_id, game.to_json()))
    conn.commit()
    conn.close()

    return game_id

def get_current_game(game_id=None):
    """ try to find game in db if yes returns the game (GameState) object """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Find the game_id in the database
    c.execute("SELECT state_json FROM games WHERE game_id = ?", (game_id,))
    row = c.fetchone()
    if row is None:
        print(f"Game with id {game_id} not found.")

    current_game = GameState.from_json(row[0])
    return conn, c, current_game

# Faction home biomes: if a player's token is on one of these two biomes, its
# forward advancing value gets +1 (design rule). Shared by the condition check
# (biome_X conditions) and the biome bonus.
FACTION_BIOMES = {
    'Dwarves': ('MO', 'OC'),
    'Demons': ('OC', 'DE'),
    'Twigs': ('JU', 'OC'),
    'Miaous': ('DE', 'JU'),
    'Orcs': ('MO', 'JU'),
    'Mummies': ('DE', 'MO'),
}

# condition name -> faction (conditions biome_X mean "standing on one of faction X's biomes")
BIOME_CONDITION_FACTION = {
    'biome_Dwa': 'Dwarves',
    'biome_Dem': 'Demons',
    'biome_Twi': 'Twigs',
    'biome_Mia': 'Miaous',
    'biome_Orc': 'Orcs',
    'biome_Mum': 'Mummies',
}


def roll_temperature():
    """ roll a d20 twice, keep the value closest to 10 (ties broken randomly) """
    rolls = [random.randint(1, 20), random.randint(1, 20)]
    best_dist = min(abs(r - 10) for r in rolls)
    return random.choice([r for r in rolls if abs(r - 10) == best_dist])

def p2_connect_to_game(player: PlayerState, game_id):
    """ connect 2nd player and initialize the game
     leave the game state as :  waiting for both players to put {start_cards_in_mana} cards in hand """

    conn, c, current_game = get_current_game(game_id)

    # 1. Add the new player to the game (derive faction from deck if not set)
    if not player.get('faction'):
        player['faction'] = _derive_faction(player.get('deck') or [])
    current_game.players[player['name']] = PlayerState.model_validate(player)
    
    # Also ensure player 1's faction is set (in case it was missing at creation time)
    for p in current_game.players.values():
        if not p.faction and p.deck:
            p.faction = _derive_faction(p.deck)
    
    # 2. Randomly choose first player
    players_list = list(current_game.players.keys())
    random.shuffle(players_list)
    current_game.turn_order = players_list
    
    # 3. Edit players hands (and make sure all list fields are initialized)
    for p in current_game.players.values():
        if p.deck is None:
            p.deck = []
        if p.discard is None:
            p.discard = []
        p.hand = random.sample(p.deck, start_cards_in_hand)
        for card in p.hand:
            p.deck.remove(card)
        p.mana = []
    
    # 4. initialize earth cells
    current_game.earth = [[] for _ in range(n_cells_by_biome*len(BIOMES))]
    biomes_order = random.sample(BIOMES, len(BIOMES))
    for i, biome in enumerate(biomes_order):
        for j in range(n_cells_by_biome):
            current_game.earth[i * n_cells_by_biome + j] = [biome]
    current_game.earth[0].append(current_game.turn_order[0])   # first player starts at position 0
    current_game.earth[0].append(current_game.turn_order[1])   # second player starts at position 0
    # black_hole (Mages): record cell-0's biome at init — the base for
    # the background art (the frontend pins the base image to this biome and ROTATES it
    # by earth_rotation * 15deg so the art tracks the engine's rotated biomes).
    current_game.earth_initial_b0 = biomes_order[0]
    current_game.earth_rotation = 0

    # 4.5 Cataclysm pile: one card per biome (4 cards), shuffled at board init.
    #     Top = first element. Each cataclysm trigger takes the top card, strikes
    #     its biome and puts it at the BOTTOM of the pile (see trigger_cataclysm).
    current_game.cataclysm_pile = random.sample(BIOMES, len(BIOMES))
    print(f'Cataclysm pile initialized: {current_game.cataclysm_pile}')

    # 5. Roll planet temperature (2x d20, keep value closest to 10); day/night always starts on "day" and flips each turn
    current_game.temperature = roll_temperature()
    current_game.temperature_initial = current_game.temperature   # Mages thermic_flux: record the rolled value (the stored `temperature` may later be changed by a thermic_flux — the replay uses this to evaluate temp_* conditions correctly)
    current_game.day_night = 'day'
    print(f'Planet initialized: temperature = {current_game.temperature}, {current_game.day_night}')

    # 6. Change game state
    current_game.state = f"waiting for both players to put {start_cards_in_mana} cards in hand"

    # 7. Mark the rules version this game is played with (the replay/analysis
    #    tooling uses it to pin older games to the rules they were actually played under).
    #    All current rules are active for every new game.
    current_game.engine_version = "1.0"

    # Update the game state in the database
    c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (current_game.to_json(), game_id))
    conn.commit()
    conn.close()
    # current_game.to_json()
    return current_game_json(player['name'], current_game)

def current_game_json(player_name: str, current_game: GameState):
    """ return the current game to the player, but hides the hand, mana and deck of the opponent.
       The counts stay public (hand_count / mana_count / deck_count) so the UI can show
       "N cards in hand" and "N mana" without revealing the cards. """
    data = json.loads(current_game.to_json())
    for name, p in (data.get("players") or {}).items():
        if name == player_name:
            continue
        p["hand_count"] = len(p.get("hand") or [])
        p["mana_count"] = len(p.get("mana") or [])
        p["deck_count"] = len(p.get("deck") or [])
        p["hand"] = []
        p["mana"] = []
        p["deck"] = []
    return json.dumps(data)

def get_game(game_id=None):
    """ get all informations about a game returns it as a json """
    _, _, current_game = get_current_game(game_id)

    return current_game.to_json()

def _check_deadlock(current_game):
    """ check if the game reached a dead end: both players have no cards left in hand/deck/discard
     (all their cards are stuck in the mana zones). In that case nobody can ever move again, so
     the game ends: winner = player furthest ahead, draw (winner stays None) on tie.
     returns True if a deadlock was detected (and sets state to "game over") """
    for p in current_game.players.values():
        if len(p.hand or []) + len(p.deck or []) + len(p.discard or []):
            return False

    first = current_game.players[current_game.turn_order[0]]
    second = current_game.players[current_game.turn_order[1]]
    # winner is the player furthest ahead (tie -> draw, winner stays None)
    if (first.current_position or 0) != (second.current_position or 0):
        current_game.winner = max((first, second), key=lambda p: p.current_position or 0).name

    current_game.state = "game over"
    return True

def _card_names(card_ids):
    """ public names for the turn log (main cards from the pool, support cards from
     the support table; unknown ids fall back to their id) """
    out = []
    for cid in card_ids:
        if cid in SUPPORT_DB:
            out.append(cid)
        else:
            row = CARDS_DB.filter(CARDS_DB['card_id'] == cid)
            out.append(row['name'][0] if not row.is_empty() else cid)
    return out

def _end_turn(current_game, message):
    """ turn-end block (run after the trip chain COMPLETES - whether it ran fresh or
     resumed from a discard-selection pause): settles the rooted cards, then either
     ends the game (win / deadlock) or prepares the next turn (turn order reversed,
     zone resets, cleaning draws, day/night flip). Returns (current_game, success, message). """
    # rooted: settle the rooted cards NOW (after the
    # chain resolved, before any game-over / deadlock / next-turn branch) so
    # card conservation holds in ALL end-of-turn cases: last turn's rooted
    # cards are discarded (they served their one extra turn) and this turn's
    # rooted cards are pulled out of the discard and placed onto free
    # stopovers (they survive). Must run before action chains are cleared.
    current_game = _process_rooted_cards(current_game)

    # if a card was blocked in defend mode during resolution, propagate the
    # info to the client (otherwise the local "action successful" message overwrites it)
    if isinstance(current_game.message, dict) and "blocked" in current_game.message.get("message", "").lower():
        message = current_game.message["message"]

    if current_game.state == "game over":
        return current_game, True, f"Game over! Winner: {current_game.winner}"
    if _check_deadlock(current_game):
        # all cards stuck in mana zones -> nobody can move anymore, end the game
        if current_game.winner is not None:
            return current_game, True, f"Deadlock - no more playable cards for both players. Winner (furthest ahead): {current_game.winner}"
        return current_game, True, "Deadlock - no more playable cards for both players. Draw!"

    # ... then prepare for next turn
    current_game.turn_order = current_game.turn_order[::-1]     # change turn order
    current_game.first_player_passed = False                    # reset first player passed
    current_game.second_player_passed = False                   # reset second player passed
    current_game.turn += 1                                      # increment turn
    # nobodymoves: the movement lock lasts until the end of the
    # turn — clear the game-level flag so the next turn starts unlocked (like landmine).
    current_game.nobodymoves_active = False
    # flip day/night each new turn — SKIPPED once a Mages Celestial_reversal card
    # fixed it (game.day_night_fixed = True, day_night already set
    # to the chosen value at play time). Default False, so the flip happens normally.
    if not current_game.day_night_fixed:
        current_game.day_night = 'night' if current_game.day_night == 'day' else 'day'

    for p in current_game.players.values():                     # reset necessary players state
        p.mana_spend = 0
        p.action_chain = []
        # stopover positions: each player's plays this turn reset
        p.play_count = 0
        # landmine: the block lasts until the end of the turn
        p.landmine_blocked = False
        # dwelling: the dwelling card can be tapped again next turn
        p.dwelling_tapped = False
        # dwelling placeholder: the placeholder image only shows
        # during the placement turn — clear the stored slot here (cleaning phase) so it
        # does not reappear at every new turn (the frontend renders it iff the slot is set)
        p.dwelling_slot = None
        # pending placeholders (doctors): same logic — the stopover
        # placeholders only show during the placement turn
        p.pending_slots = []

        # draw the first 3 cards from deck to hand
        draw_n = min(turn_n_draw_cards, len(p.deck))
        new_cards = p.deck[:draw_n]
        p.hand.extend(new_cards)
        for card in new_cards:
            p.deck.remove(card)

        # manage situation where deck has less than 3 cards
        if draw_n < turn_n_draw_cards:
            p.deck.extend(p.discard)
            random.shuffle(p.deck)
            p.discard.clear()
            # Draw remaining cards to complete 3
            remaining_draw = turn_n_draw_cards - draw_n
            draw_n2 = min(remaining_draw, len(p.deck))
            new_cards2 = p.deck[:draw_n2]
            p.hand.extend(new_cards2)
            for card in new_cards2:
                p.deck.remove(card)

    # waiting for players to put 1 card in mana or pass
    current_game.state = f"waiting for both players to mana or pass"
    return current_game, True, message

# ---------------------------------------------------------------------------
# Game-over hook (optional): called ONCE per game, when the state transitions
# to "game over" (win, mid-chain win, or deadlock). The engine stays rule-pure:
# the callback does whatever I/O it wants (e.g. game_ui/hf_backup.py backing the
# game up to a Hugging Face dataset) and a failing callback never breaks the
# game (exceptions are caught + logged). Register with set_game_over_hook(fn).
# ---------------------------------------------------------------------------
_game_over_hook = None


def set_game_over_hook(fn):
    """Register `fn(game_id, current_game)` to be called once when a game ends.
    Pass None to clear. Exceptions inside `fn` are caught and logged."""
    global _game_over_hook
    _game_over_hook = fn


def _fire_game_over_hook(game_id, current_game):
    if _game_over_hook is None:
        return
    try:
        _game_over_hook(game_id, current_game)
    except Exception as e:
        print(f'\t\t[engine] game-over hook failed (game {game_id}): {e!r}')


def handle_websocket_message(game_id: str, player: PlayerState):   # main part of the game code
    """ handle websocket message this is the main code of the game
    messages are expected to be in the format:
    {
        'cards': random.sample(p1.hand, 3),     # cards selected by user - LIST (if move mode, max 1 card, if defend mode, no max)
        'to': 'mana',                           # destination selected by user - STRING [stopover_x, mana, pending_zone, dwelling, discard_pile]
        'mode': '',                             # mode selected by user - STRING ['', move, defend, dwelling_activation, pass]
        'pendings': []                          # cards in pendings zone that has to be added to a normal move card - LIST
    }
    """
    conn, c, current_game = get_current_game(game_id)
    was_over = current_game.state == "game over"
    
    success = True
    message = f"Action successful for {player.name}"

    # -1. Game over: nothing more can be played (a stray tap/play from the client
    #     would otherwise be acknowledged and the finished state re-written; it must
    #     not move any card — e.g. a refinery tap after the win would draw from the deck)
    if current_game.state == "game over":
        winner = current_game.winner
        message = f"Game over — winner: {winner}!" if winner else "Game over — draw."
        print(f'\t\tgame over: message from {player.name} ignored ({message})')
        current_game.message = {'success': False, 'message': message}
        conn.close()
        return current_game_json(player.name, current_game)

    ##  Process the message and update the game state if valid ##

    # 0. Validate the message
    success, message = message_check(player.message)
    if not success:
        print(f"Message validation failed: {message}")
        current_game.message = {'success': success, 'message': message}
        conn.close()
        return current_game_json(player.name, current_game)
    
    # 1. after game initialisation
    if current_game.state == f"waiting for both players to put {start_cards_in_mana} cards in hand":        
        # Add the cards from the message to player's mana zone
        for card_id in player.message['cards']:
            if card_id in player.hand:
                player.hand.remove(card_id)
                player.mana.append(card_id)
            else:
                success = False
                message = f"Player {player.name} tried to put card(s) in mana, that are not in hand: {card_id}"

        # Update the player's state in the game
        if success:
            for p in current_game.players.values():
                if p.name == player.name:
                    p.hand = player.hand
                    p.mana = player.mana
                    p.messages_history.append(player.message)

            # Check if both players has put {start_cards_in_mana} cards in mana zone
            if all(len(p.mana) == start_cards_in_mana for p in current_game.players.values()):
                current_game.state = f"turn {current_game.turn} - waiting for first player ({current_game.turn_order[0]}) to play"

    # 2. Turns phase
    elif 'turn' in current_game.state:

        # 2.0 Discard selection: the trip chain is
        # PAUSED waiting for the discarding player to CHOOSE the card(s) to discard
        # (to: 'discard_pile'). Only the discarding player can answer; the choice
        # moves the chosen cards from the hand to the discard pile, then the chain
        # resumes EXACTLY where it stopped (process_trip_chain with the saved ctx).
        m_discard = re.match(r"turn \d+ - waiting for (.+?) to discard (\d+) card\(s\)$", current_game.state)
        if m_discard:
            disc_name, need = m_discard.group(1), int(m_discard.group(2))
            disc_player = current_game.players.get(disc_name)
            if disc_player is None or player.name != disc_name:
                success = False
                message = f"Waiting for {disc_name} to choose the {need} discard card(s)"
            else:
                cards = player.message['cards']
                if player.message['to'] != 'discard_pile':
                    success = False
                    message = "Discard selection must be sent with to: 'discard_pile'"
                elif len(cards) != need or len(set(cards)) != len(cards):
                    success = False
                    message = f"{disc_name} must choose exactly {need} card(s) to discard"
                elif any(c not in (player.hand or []) for c in cards):
                    success = False
                    message = f"Discard selection: some chosen card is not in {disc_name}'s hand"
                else:
                    # apply the choice: hand -> discard pile (loop var must not shadow the DB cursor `c`)
                    for card_id in cards:
                        player.hand.remove(card_id)
                    if player.discard is None:
                        player.discard = []
                    player.discard.extend(cards)
                    # turn log note on the source card's line (public info)
                    resume_ctx = current_game.chain_resume or {}
                    for e in (resume_ctx.get('entry_f'), resume_ctx.get('entry_s')):
                        if isinstance(e, dict) and e.get('_pending_discard'):
                            e['notes'].append(f'🗑 discard selection — {disc_name} discarded: '
                                              + ', '.join(_card_names(cards)))
                            e.pop('_pending_discard', None)
                    print(f'\t\t\tdiscard selection: {player.name} discards {cards} (hand now {len(player.hand)})')
                    for p in current_game.players.values():
                        if p.name == player.name:
                            p.hand = player.hand
                            p.discard = player.discard
                            p.messages_history.append(player.message)
                    current_game.pending_discard = None
                    resume_ctx = current_game.chain_resume
                    current_game.chain_resume = None
                    current_game = process_trip_chain(current_game, resume=resume_ctx)
                    # the resumed chain either COMPLETED (chain_resume stays None ->
                    # run the turn-end block) or PAUSED on another discard (the state
                    # is already the new "waiting for X to discard K card(s)" state
                    # -> nothing more to do this message)
                    if current_game.chain_resume is None:
                        current_game, success, message = _end_turn(current_game, message)
                    else:
                        # paused on ANOTHER discard: propagate the new waiting state
                        message = current_game.state
        else:
            # 2.1 First player's turn
            if f"waiting for first player ({current_game.turn_order[0]}) to play" in current_game.state:
                player, current_game, success, message = player_play(first_second='first', player=player, current_game=current_game)

            # 2.2 Second player's turn
            elif f"waiting for second player ({current_game.turn_order[1]}) to play" in current_game.state:
                player, current_game, success, message = player_play(first_second='second', player=player, current_game=current_game)

            # 2.3 Check turn end (if both players passed)
            if current_game.first_player_passed and current_game.second_player_passed:
                # ... if YES go through all the actions chain ...
                current_game = process_trip_chain(current_game)
                # the chain either COMPLETED (chain_resume is None -> run the
                # turn-end block) or PAUSED on a discard selection (chain_resume is
                # set, state is already "waiting for X to discard K card(s)" -> the
                # turn is NOT over yet: wait for the discarding player's choice)
                if current_game.chain_resume is None:
                    current_game, success, message = _end_turn(current_game, message)
                else:
                    # paused on a discard selection: propagate the waiting state
                    message = current_game.state

    # 3. End turn - waiting for players to put mana or pass
    elif current_game.state == f"waiting for both players to mana or pass":
        
        # Add the cards from the message to player's mana zone
        if player.message['mode'] == 'pass':
            if player.name == current_game.turn_order[0]:
                current_game.first_player_passed = True
            elif player.name == current_game.turn_order[1]:
                current_game.second_player_passed = True
        else:
            if len(player.message['cards']) != 1:  # only 1 card to put in mana
                success = False
                message = f"{player.name} must select exactly 1 card to put in mana during this phase."
            else:
                for card_id in player.message['cards']: # Add card to mana
                    player.hand.remove(card_id)
                    player.mana.append(card_id)
                # we will use x_player_passed to know if player has put mana or passed
                if player.name == current_game.turn_order[0]:
                    current_game.first_player_passed = True
                elif player.name == current_game.turn_order[1]:
                    current_game.second_player_passed = True
                
        # Update the player's state in the game
        if success:
            for p in current_game.players.values():
                if p.name == player.name:
                    p.hand = player.hand
                    p.mana = player.mana
                    p.messages_history.append(player.message)

        # check if both players passed
        if current_game.first_player_passed and current_game.second_player_passed:
            current_game.state = f"turn {current_game.turn} - waiting for first player ({current_game.turn_order[0]}) to play"
            current_game.first_player_passed = False
            current_game.second_player_passed = False

    # Update message from game
    current_game.message = {'success': success, 'message': message}

    # Update the game state in the database
    c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (current_game.to_json(), game_id))
    conn.commit()
    conn.close()
    # game ended during this message -> fire the (optional) game-over hook once
    if not was_over and current_game.state == "game over":
        _fire_game_over_hook(game_id, current_game)
    return current_game_json(player.name, current_game)

# ---------------------------------------------------------------------------
# Playable pool (deck entry points): cards with these conditions are NOT allowed
# in NEW games (the deck must be built from, and validated against, this pool).
# `face_point_left` / `face_point_right` are not implemented yet — the moment they
# are (or the rule is dropped), remove them from the tuple (or set it to ()).
#
# NOTE: this filters get_cardpool() only — the raw pool (CARDS_DB) stays COMPLETE,
# so the engine can still resolve those cards in OLD games, and the replay / AI /
# tests keep seeing them.
# ---------------------------------------------------------------------------
EXCLUDED_CONDITIONS = ('face_point_left', 'face_point_right')
PLAYABLE_POOL = CARDS_DB.filter(~pl.col('condition').is_in(EXCLUDED_CONDITIONS))


def get_cardpool() -> pl.DataFrame:
    """ Card pool for NEW games (deck construction, starter deck, deck validation,
    robot deck). Excludes EXCLUDED_CONDITIONS — see the note above. """
    return PLAYABLE_POOL

def message_check(message):
    """ check that the message received is valid """
    if not isinstance(message, dict):
        return False, "Message must be a dictionary"
    if 'cards' not in message or 'to' not in message or 'mode' not in message or 'pendings' not in message:
        return False, "Message must contain 'cards', 'to', 'mode', and 'pendings' keys"
    if not isinstance(message['cards'], list):
        return False, "'cards' must be a list"
    if not isinstance(message['to'], str):
        return False, "'to' must be a string"
    if not isinstance(message['mode'], str):
        return False, "'mode' must be a string"
    if not isinstance(message['cards'], list):
        return False, "'cards' must be a list"
    if not isinstance(message['pendings'], list):
        return False, "'pendings' must be a list"

    # Check if 'to' is 'mana', then cards list length must be max 1
    if message['to'] == 'mana':
        if len(message['cards']) not in (1, 3):
            return False, "When 'to' is 'mana', 'cards' list must contain exactly 1 or 3 cards"

    # check if mode is move then cards list length is max 1 and to is stopover_x (with where x is an integer)
    if message['mode'] == 'move':
        if len(message['cards']) < 1:
            return False, "When mode is 'move', 'cards' list must contain at least 1 card"
        if len(message['cards']) > 1:
            return False, "When mode is 'move', 'cards' list must contain at most 1 card"
        if not message['to'].startswith('stopover_'):
            return False, "When mode is 'move', 'to' must be in the format 'stopover_x' (where x is an integer)"

    # defend mode: 1 to 5 cards played sideways (90°) on the stopover, they block the
    # opponent card on the same stopover
    if message['mode'] == 'defend':
        if len(message['cards']) < 1:
            return False, "When mode is 'defend', 'cards' list must contain at least 1 card"
        if len(message['cards']) > 5:
            return False, "When mode is 'defend', 'cards' list must contain at most 5 cards"
        if not message['to'].startswith('stopover_'):
            return False, "When mode is 'defend', 'to' must be in the format 'stopover_x' (where x is an integer)"

    # dwelling actions: place the dwelling card
    # (to 'dwelling', exactly 1 card) or tap it (mode 'dwelling_activation', no card)
    if message['to'] == 'dwelling':
        if message['mode'] == 'dwelling_activation':
            if len(message['cards']) != 0:
                return False, "When mode is 'dwelling_activation', 'cards' must be empty (the tap uses the card on the board)"
        elif len(message['cards']) != 1:
            return False, "When 'to' is 'dwelling', 'cards' must contain exactly 1 card (the dwelling card to place)"

    # optional target cell:
    # an engineer drop played in MOVE mode must carry the earth cell it is placed on
    if 'cell' in message and message['cell'] is not None:
        cell = message['cell']
        if isinstance(cell, bool) or not isinstance(cell, int) or not (0 <= cell < 24):
            return False, "'cell' must be an integer between 0 and 23"

    # optional day/night choice:
    # a Celestial_reversal played in MOVE mode must carry the chosen phase ('day'/'night')
    if 'day_night' in message and message['day_night'] is not None:
        if message['day_night'] not in ('day', 'night'):
            return False, "'day_night' must be 'day' or 'night'"

    # optional temperature direction:
    # a thermic_flux played in MOVE mode must carry the chosen direction ('up'/+4 or 'down'/−4)
    if 'temp_change' in message and message['temp_change'] is not None:
        if message['temp_change'] not in ('up', 'down'):
            return False, "'temp_change' must be 'up' or 'down'"

    # optional cataclysm order:
    # an Apocalypticritual played in MOVE mode must carry the chosen order —
    # a PERMUTATION of the 4 biomes (each exactly once; index 0 strikes next)
    if 'cataclysm_order' in message and message['cataclysm_order'] is not None:
        order = message['cataclysm_order']
        if (not isinstance(order, list) or len(order) != len(BIOMES)
                or any(not isinstance(b, str) or b not in BIOMES for b in order)
                or len(set(order)) != len(order)):
            return False, "'cataclysm_order' must be a permutation of the 4 biomes (OC, MO, DE, JU)"

    # optional rotation direction:
    # a black_hole tap (mode 'dwelling_activation') must carry the chosen direction
    # ('cw' clockwise / 'ccw' counter-clockwise). The field is only validated when
    # present here; the "required for a black_hole tap" check is in the dwelling tap
    # branch of player_play (refinery/laboratory taps carry no direction and stay valid).
    if 'rotation' in message and message['rotation'] is not None:
        if message['rotation'] not in ('cw', 'ccw'):
            return False, "'rotation' must be 'cw' or 'ccw'"

    # optional swap target: the POSITION (1..5) of
    # the OTHER entry of the player's OWN trip chain that the card being played
    # swaps places with (a play, a board placeholder or a rooted card). The field is
    # only validated when present here; the "target must exist at that position"
    # check is in player_play (it needs the player's chain). A swap_cards play
    # WITHOUT 'swap_with' is valid (the card simply advances as usual).
    if 'swap_with' in message and message['swap_with'] is not None:
        sw = message['swap_with']
        if isinstance(sw, bool) or not isinstance(sw, int) or not (1 <= sw <= 5):
            return False, "'swap_with' must be a position between 1 and 5"

    return True, "Message is valid"

def player_play(first_second: str, player: PlayerState, current_game: GameState):
    success = True
    if player.message['mode'] == 'pass':    # Player passed his turn
        if first_second == 'first':
            current_game.first_player_passed = True
            current_game.state = f"turn {current_game.turn} - waiting for second player ({current_game.turn_order[1]}) to play"
        elif first_second == 'second':
            current_game.second_player_passed = True
            current_game.state = f"turn {current_game.turn} - waiting for first player ({current_game.turn_order[0]}) to play"
        return player, current_game, True, f"Player {player.name} just passed"
    
    # --- DWELLING actions --------------------------
    # 'to': 'dwelling' with 1 card   -> PLACE the dwelling card (refinery) from the
    #                                  hand into the dedicated zone (one at a time).
    # 'to': 'dwelling' + mode 'dwelling_activation' (no card) -> TAP the dwelling
    #                                  card: at most ONCE per turn, free, instant
    #                                  effect (refinery: draw 1). Untapped in the
    #                                  cleaning phase. Removed by wrecking_ball.
    # Neither goes into the trip chain (no resolution). PLACE alternates with the
    # opponent like a normal play; TAP is a QUICK ACTION — the state stays "waiting
    # for this player to play", so they may still play a card or pass afterwards.
    if player.message['to'] == 'dwelling':
        if player.message['mode'] == 'dwelling_activation':
            # TAP the dwelling card on the board
            if not player.dwelling:
                return player, current_game, False, f"{player.name} has no dwelling card to tap"
            if player.dwelling_tapped:
                return player, current_game, False, f"{player.name}'s dwelling card is already tapped this turn"
            if player.dwelling == 'refinery':
                drawn = _draw_cards(player, 1)
                player.dwelling_tapped = True
                if drawn:
                    message = f"{player.name} taps the refinery — draws {drawn} card(s)"
                    _append_instant(current_game, player.name, f'🏭 refinery tap — draws {drawn} card(s)')
                else:
                    message = f"{player.name} taps the refinery — nothing left to draw"
                    _append_instant(current_game, player.name, '🏭 refinery tap — nothing left to draw')
                print(f'\t\t\tDWELLING tap: {player.name} taps the refinery, drew {drawn}')
            elif player.dwelling == MAGE_BLACK_HOLE:
                # Mages black_hole: the tap ROTATES THE EARTH 3 CELLS
                # in the player's chosen direction (message 'rotation': 'cw' or 'ccw').
                # The 4 biomes shift position in current_game.earth; every token (both
                # players, pet_trap drops, engineer board drops) stays on its own cell
                # index. Free + once per turn (dwelling_tapped), a quick action like the
                # other dwelling taps (no alternation). The direction is validated in
                # message_check (a black_hole tap without a valid direction is rejected).
                _rot = player.message.get('rotation')
                if _rot == 'cw':
                    rotate_earth(current_game, +3)
                    message = f"{player.name} taps the black_hole — the earth rotates 3 cells clockwise"
                    _append_instant(current_game, player.name, '🕳️ black_hole tap — the earth rotates 3 cells clockwise')
                elif _rot == 'ccw':
                    rotate_earth(current_game, -3)
                    message = f"{player.name} taps the black_hole — the earth rotates 3 cells counter-clockwise"
                    _append_instant(current_game, player.name, '🕳️ black_hole tap — the earth rotates 3 cells counter-clockwise')
                else:
                    return player, current_game, False, (
                        f"{player.name}'s black_hole tap requires a direction (message 'rotation': 'cw' or 'ccw')")
                player.dwelling_tapped = True
                player.message['black_hole_rotation'] = _rot
                print(f'\t\t\tDWELLING tap: {player.name} taps the black_hole, earth rotated ({_rot})')
            elif player.dwelling == 'laboratory':
                # Doctors laboratory: tap adds an "epo" pending card to the pending zone
                if player.pendings is None:
                    player.pendings = []
                if player.pending_slots is None:
                    player.pending_slots = []
                player.pendings.append('epo')
                # the tap is a QUICK action — the 'epo' sits in the persistent
                # pendings zone but gets NO placeholder entry (pending_slots is the
                # per-turn placeholder list, so nothing is appended and play_count
                # is untouched).
                player.dwelling_tapped = True
                message = f"{player.name} taps the laboratory — adds an 'epo' pending card"
                _append_instant(current_game, player.name, "⚗️ laboratory tap — adds an 'epo' pending card")
                print(f'\t\t\tDWELLING tap: {player.name} taps the laboratory, adds epo pending')
            else:
                return player, current_game, False, f"dwelling card {player.dwelling} has no tap effect yet"
        else:
            # PLACE the dwelling card from the hand
            cards_id = player.message['cards']
            if len(cards_id) != 1:
                return player, current_game, False, "dwelling placement takes exactly 1 card"
            card_id = cards_id[0]
            cost = _play_cost(current_game, card_id)
            mana_available = len(player.mana) - (player.mana_spend or 0)
            if player.dwelling:
                return player, current_game, False, f"{player.name} already has a dwelling card on the board ({player.dwelling})"
            if card_id not in (player.hand or []):
                return player, current_game, False, f"{card_id} is not in {player.name}'s hand"
            # Accept the three support-faction dwelling cards
            valid_dwelling = {ENGINEER_DWELLING, DOCTOR_DWELLING, MAGE_BLACK_HOLE}
            if card_id not in valid_dwelling:
                return player, current_game, False, f"{card_id} is not a dwelling card"
            if cost > mana_available:
                return player, current_game, False, (f"not enough mana to place {card_id} "
                                                     f"(available {mana_available}, required {cost})")
            player.hand.remove(card_id)
            player.dwelling = card_id
            # stopover positions: the dwelling placeholder takes the NEXT free position
            # in this player's OWN chain — (rooted count) + (plays so far this turn) + 1
            # — recorded as a column index (5 - pos).
            played_before = player.play_count or 0
            player.dwelling_slot = int(_player_stopover(current_game, player.name, played_before).rsplit('_', 1)[-1])
            player.play_count = played_before + 1
            player.mana_spend = (player.mana_spend or 0) + cost
            print(f'\t\t\tDWELLING place: {player.name} places {card_id} on the board (cost {cost})')
            message = f"{player.name} places the dwelling card {card_id} on the board"

            # alternation (same as a play: control passes to the opponent unless they already passed)
            if first_second == 'first':
                current_game.state = f"turn {current_game.turn} - waiting for second player ({current_game.turn_order[1]}) to play"
                if current_game.second_player_passed:
                    current_game.state = f"turn {current_game.turn} - waiting for first player ({current_game.turn_order[0]}) to play"
            elif first_second == 'second':
                current_game.state = f"turn {current_game.turn} - waiting for first player ({current_game.turn_order[0]}) to play"
                if current_game.first_player_passed:
                    current_game.state = f"turn {current_game.turn} - waiting for second player ({current_game.turn_order[1]}) to play"
        # TAP: no alternation — the state already reads "waiting for <this player>
        # to play", so they keep the floor and may play a card or pass next.

        for p in current_game.players.values():
            if p.name == player.name:
                # sync EVERY zone the tap may have touched back to the authoritative
                # state: `player` is a copy (the WS layer passes model_copy()), so
                # _draw_cards() popped/copied the COPY's deck — forgetting p.deck here
                # kept the drawn card in the persisted deck (card duplicated across
                # zones, re-drawn on the next draw). _next_from_deck also reshuffles
                # the discard when the deck runs out, so p.discard must come along.
                p.hand = player.hand
                p.deck = player.deck
                p.discard = player.discard
                p.mana_spend = player.mana_spend
                p.dwelling = player.dwelling
                p.dwelling_slot = player.dwelling_slot
                p.dwelling_tapped = player.dwelling_tapped
                p.pendings = player.pendings
                p.pending_slots = player.pending_slots
                # stopover positions: a dwelling PLACE consumes a
                # position - sync play_count back or the next play re-reads 0 and lands
                # on the placeholder's own slot (same class as the refinery-TAP bug)
                p.play_count = player.play_count
                p.messages_history.append(player.message)
        return player, current_game, True, message

    # --- PENDING ZONE placement ---
    # Place a doctor pending card (epo/virus/bloodtest/mercurochrome) into the
    # pending zone. Costs its mana_cost. Creates a placeholder in the stopover
    # (like the refinery dwelling). The card stays in the zone until attached
    # to a main card (max 1 per main card). This is a play-phase action.
    if player.message['to'] == 'pending_zone':
        pzone_cards = player.message['cards']
        if len(pzone_cards) != 1:
            return player, current_game, False, "pending zone placement takes exactly 1 card"
        pz_card = pzone_cards[0]
        if pz_card not in DOCTOR_PENDING:
            return player, current_game, False, f"{pz_card} is not a pending card (must be one of {DOCTOR_PENDING})"
        pz_cost = _play_cost(current_game, pz_card)
        pz_mana = len(player.mana) - (player.mana_spend or 0)
        if pz_card not in (player.hand or []):
            return player, current_game, False, f"{pz_card} is not in {player.name}'s hand"
        if pz_cost > pz_mana:
            return player, current_game, False, f"not enough mana to place {pz_card} (available {pz_mana}, required {pz_cost})"
        player.hand.remove(pz_card)
        player.mana_spend = (player.mana_spend or 0) + pz_cost
        if player.pendings is None:
            player.pendings = []
        if player.pending_slots is None:
            player.pending_slots = []
        player.pendings.append(pz_card)
        # Create placeholder in the stopover (same logic as the dwelling placeholder).
        # The placeholder is a [card, slot] PAIR — pending_slots is the per-turn
        # placeholder display (cleared in the cleaning phase) and is NOT parallel to
        # the persistent `pendings` zone, so attachment removes the pair BY CARD NAME
        # (never by index — the two lists are not parallel across turns).
        played_before = player.play_count or 0
        slot = int(_player_stopover(current_game, player.name, played_before).rsplit('_', 1)[-1])
        player.pending_slots.append([pz_card, slot])
        player.play_count = played_before + 1
        print(f'\t\t\tPENDING ZONE: {player.name} places {pz_card} in the pending zone (cost {pz_cost})')
        pz_message = f"{player.name} places {pz_card} in the pending zone"
        # Alternation (same as a play)
        if first_second == 'first':
            current_game.state = f"turn {current_game.turn} - waiting for second player ({current_game.turn_order[1]}) to play"
            if current_game.second_player_passed:
                current_game.state = f"turn {current_game.turn} - waiting for first player ({current_game.turn_order[0]}) to play"
        elif first_second == 'second':
            current_game.state = f"turn {current_game.turn} - waiting for first player ({current_game.turn_order[0]}) to play"
            if current_game.first_player_passed:
                current_game.state = f"turn {current_game.turn} - waiting for second player ({current_game.turn_order[1]}) to play"
        for p in current_game.players.values():
            if p.name == player.name:
                p.hand = player.hand
                p.mana_spend = player.mana_spend
                p.pendings = player.pendings
                p.pending_slots = player.pending_slots
                p.play_count = player.play_count
                p.messages_history.append(player.message)
        return player, current_game, True, pz_message

    # check mana available (main cards: pool mana; support cards: their
    # mana_cost from the support table; unknown cards: 0)
    mana_available = len(player.mana) - (player.mana_spend or 0)
    cards_id = player.message['cards']
    total_cost = sum(_play_cost(current_game, cid) for cid in cards_id)

    # --- PENDING CARD attachment validation (doctors) ---
    # When playing a MAIN card in MOVE mode, the player can attach ONE pending
    # card from their pending zone. Validate BEFORE any state changes.
    pendings_list = player.message.get('pendings') or []
    if len(pendings_list) > 1:
        return player, current_game, False, "You can attach at most 1 pending card"
    if len(pendings_list) == 1:
        pcard = pendings_list[0]
        if player.message['mode'] != 'move':
            return player, current_game, False, "Pending cards can only be attached to move cards"
        if pcard not in (player.pendings or []):
            return player, current_game, False, f"{pcard} is not in your pending zone"
        if cards_id:
            main_card = cards_id[0]
            if main_card in DOCTOR_PENDING or main_card in DOCTOR_DWELLING or main_card in ENGINEER_DROPS or main_card == ENGINEER_DWELLING or main_card == MAGE_CELASTIAL_REVERSAL or main_card == MAGE_NOBODYMOVES:
                return player, current_game, False, "You cannot attach a pending card to a support card"

    # engineers' drops: a MOVE play of a drop card must carry
    # the target cell (message 'cell', 0..23) - the token is placed there at play time
    if player.message['mode'] == 'move':
        for cid in cards_id[:1]:
            if cid in ENGINEER_DROPS:
                cell = player.message.get('cell')
                if isinstance(cell, bool) or not isinstance(cell, int) or not (0 <= cell < 24):
                    return player, current_game, False, (
                        f"engineer drop {cid} must target a cell of the earth (message 'cell', 0..23)")

    # Mages Celestial_reversal: a MOVE play must carry the
    # chosen phase (message 'day_night': 'day' or 'night') - it is applied INSTANTLY
    # at play time (see _apply_instant_effects) and the card is a no-op on the chain.
    # A DEFEND play is a plain no-op (like every support card) - no choice required.
    if player.message['mode'] == 'move':
        for cid in cards_id[:1]:
            if cid == MAGE_CELASTIAL_REVERSAL and player.message.get('day_night') not in ('day', 'night'):
                return player, current_game, False, (
                    f"{cid} requires a day/night choice (message 'day_night': 'day' or 'night')")

    # Mages thermic_flux: a MOVE play must carry the chosen
    # direction (message 'temp_change': 'up' or 'down') - it is applied INSTANTLY at
    # play time (see _apply_instant_effects) and the card is a no-op on the chain.
    # A DEFEND play is a plain no-op (like every support card) - no choice required.
    if player.message['mode'] == 'move':
        for cid in cards_id[:1]:
            if cid == MAGE_THERMIC_FLUX and player.message.get('temp_change') not in ('up', 'down'):
                return player, current_game, False, (
                    f"{cid} requires a +4/−4 choice (message 'temp_change': 'up' or 'down')")

    # Mages Apocalypticritual: a MOVE play must carry the chosen
    # ORDER OF ALL 4 CATACLYSM BIOMES (message 'cataclysm_order': a permutation of
    # OC/MO/DE/JU, index 0 strikes next) - it is applied INSTANTLY at play time (see
    # _apply_instant_effects) and the card is a no-op on the chain. A DEFEND play is
    # a plain no-op (like every support card) - no order required.
    if player.message['mode'] == 'move':
        for cid in cards_id[:1]:
            if cid == MAGE_APOCALYPTICRITUAL:
                _order = player.message.get('cataclysm_order')
                if (not isinstance(_order, list) or len(_order) != len(BIOMES)
                        or any(not isinstance(b, str) or b not in BIOMES for b in _order)
                        or len(set(_order)) != len(_order)):
                    return player, current_game, False, (
                        f"{cid} requires a cataclysm order choice (message 'cataclysm_order': "
                        f"a permutation of the 4 biomes OC/MO/DE/JU)")

    # swap_cards: an OPTIONAL swap choice (message 'swap_with')
    # - the position (1..5) of the OTHER entry of this player's OWN chain that the
    # card being played swaps places with (a play, a board placeholder or a rooted
    # card). Validated BEFORE any state change: the target must EXIST at that
    # position (a swap with an empty position is rejected - no state change). A
    # swap_cards play WITHOUT 'swap_with' is valid (the card advances as usual).
    # A DEFEND play never swaps (defend plays no effect).
    if player.message['mode'] == 'move' and cards_id:
        _sw = player.message.get('swap_with')
        if _sw is not None:
            _sw_rows = CARDS_DB.filter(pl.col('card_id') == cards_id[0])
            if not _sw_rows.is_empty() and _sw_rows['effect'][0] == 'swap_cards':
                if isinstance(_sw, bool) or not isinstance(_sw, int) or not (1 <= _sw <= 5):
                    return player, current_game, False, (
                        f"swap_cards: 'swap_with' must be a position 1..5 (got {_sw!r})")
                if _find_swap_target(player, _sw, current_game) is None:
                    return player, current_game, False, (
                        f"swap_cards: no card or placeholder of your own chain at position {_sw} to swap with")

    if total_cost <= mana_available:
        for card_id in cards_id:
            if card_id not in (player.hand or []):
                return player, current_game, False, f"{card_id} is not in {player.name}'s hand"
            player.hand.remove(card_id)
        # stopover positions: the engine is the source of truth for the position -
        # the (rooted count) + (plays so far this turn) + 1-th position in this
        # player's OWN chain. The client's 'to' is for display only; it is
        # overwritten here.
        played_before = player.play_count or 0
        player.message['to'] = _player_stopover(current_game, player.name, played_before)
        player.play_count = played_before + 1
        player.action_chain.append(player.message) # Append to Trip chain (it is a list of dict of cards)
        if first_second == 'first':
            current_game.state = f"turn {current_game.turn} - waiting for second player ({current_game.turn_order[1]}) to play"
            if current_game.second_player_passed:       # if second player already passed
                current_game.state = f"turn {current_game.turn} - waiting for first player ({current_game.turn_order[0]}) to play"
        elif first_second == 'second':
            current_game.state = f"turn {current_game.turn} - waiting for first player ({current_game.turn_order[0]}) to play"
            if current_game.first_player_passed:       # if second player already passed
                current_game.state = f"turn {current_game.turn} - waiting for second player ({current_game.turn_order[1]}) to play"
        player.mana_spend = (player.mana_spend or 0) + total_cost       # update mana spend
        # --- Consume the pending card (doctors) ---
        pendings_list = player.message.get('pendings') or []
        if len(pendings_list) == 1:
            pcard = pendings_list[0]
            idx = player.pendings.index(pcard)
            player.pendings = player.pendings[:idx] + player.pendings[idx+1:]
            # the placeholder of the attached pending card STAYS IN PLACE (when it
            # was placed this turn — i.e. the [card, slot] pair is in pending_slots):
            # it marks the trip-chain position that the pending placement consumed.
            # pending_slots is unchanged. (An OLD pending, placed a previous turn,
            # has no placeholder — nothing to keep.)
            player.message['pending_card'] = pcard
            print(f'\t\t\tPENDING: {player.name} attaches {pcard} to {cards_id[0]}')
        message = f"Player {player.name} played {cards_id} successfully"
    else:
        success = False
        message = f"Player {player.name} tried to play {cards_id} but not enough mana available (available: {mana_available}, required: {total_cost})"

    # --- INSTANT effects ----------------------------------------------------------------
    # pet_trap (and, later, swap_cards / wrecking_ball) fire the moment the card is
    # PLAYED - not at trip-chain resolution - so they cannot be blocked, canceled or
    # condition-gated: the effect is already on the board before the chain starts.
    # pet_trap leaves a drop token on the player's current cell; it triggers when
    # ANY player's token later ARRIVES on that cell (knockback -1 per token).
    if success:
        cells = _apply_instant_effects(player, current_game, player.message)
        if cells:
            # engine annotation on the action (shared dict: also lands in
            # action_chain / messages_history): the turn log reads the exact cells
            player.message['drop_placed_on'] = cells

    for p in current_game.players.values():
        if p.name == player.name:
            p.hand = player.hand
            p.mana_spend = player.mana_spend
            p.action_chain = player.action_chain
            p.dwelling = player.dwelling
            p.dwelling_tapped = player.dwelling_tapped
            # swap_cards: a swapped dwelling placeholder rewrites
            # the copy's dwelling_slot - persist it (a dwelling PLACE does not change
            # it, so this is a no-op for those; without it the swap would be lost on
            # the model_copy() WS path)
            p.dwelling_slot = player.dwelling_slot
            # stopover positions: persist this turn's play count
            p.play_count = player.play_count
            # doctors: persist pending zone state
            p.pendings = player.pendings
            p.pending_slots = player.pending_slots
            # only record actions that actually happened (a rejected play, e.g.
            # "not enough mana", must not be stored: it would show up as a real
            # play in the analysis app / history)
            if success:
                p.messages_history.append(player.message)

    return player, current_game, success, message

def _play_cost(current_game, card_id):
    """ cost of playing ONE card: main cards cost their pool mana; support cards
    cost their mana_cost from the support table; unknown cards cost 0. """
    rows = CARDS_DB.filter(pl.col('card_id') == card_id)
    if not rows.is_empty():
        return int(rows['mana'][0])
    if card_id in SUPPORT_DB:
        return int(SUPPORT_DB[card_id]['mana_cost'])
    return 0

def _apply_instant_effects(player, current_game, msg):
    """ INSTANT effects: fire at PLAY TIME (right after the card is accepted),
     before the trip chain resolves - they cannot be blocked or canceled.
     pet_trap: leaves ONE drop token on the player's current
     cell (tokens stack on the same cell). Move mode only: a defend play plays
     no effect, so it places no token.
     wrecking_ball: REMOVES the opponent's dwelling card
     (PlayerState.dwelling, if set) - the card is sent to the opponent's discard
     pile and the dwelling slot is cleared. Move mode only. A no-op today (no
     implemented effect places a dwelling card yet) but the removal system is
     live so it works as soon as dwelling cards exist.
     engineers' drops: boost/trampoline/gluetrap/landmine leave
     a VISIBLE token on the EARTH cell chosen by the player (message 'cell',
     validated in player_play). The first token ARRIVING on that cell fires the
     drop (see _trigger_board_drops), then it is consumed. Placing it on a cell
     where a token already stands does not fire it.
     Returns the list of cells that received a drop token ([] if none). """
    if not msg or (msg.get('mode') or '') != 'move':
        return []
    placed = []
    for card_id in (msg.get('cards') or [])[:1]:
        # engineers' drops (support cards, not in the main pool): the token goes
        # on the cell chosen by the player at play time
        if card_id in ENGINEER_DROPS:
            cell = msg.get('cell')
            if isinstance(cell, int) and not isinstance(cell, bool) and 0 <= cell < 24:
                current_game.board_drops.append({'cell': cell, 'kind': card_id, 'owner': player.name})
                # engine annotation on the action (shared dict: also lands in
                # action_chain / messages_history): the replay reads it
                msg['drop_cell'] = cell
                msg['drop_kind'] = card_id
                # turn log: the 'instant' section (top of the turn, before the stopovers)
                _append_instant(current_game, player.name,
                                f'🧲 {card_id} — drop placed on cell {cell}')
                print(f'\t\t\tINSTANT engineer drop: {card_id} token placed on cell {cell} (owner {player.name})')
            continue
        # Mages Celestial_reversal: the day/night is
        # FIXED to the player's choice (message 'day_night', validated in player_play)
        # for the rest of the game. INSTANT - applied at play time, so it cannot be
        # blocked / canceled / conditioned. The card itself resolves as a no-op below.
        if card_id == MAGE_CELASTIAL_REVERSAL:
            choice = msg.get('day_night')
            if choice in ('day', 'night'):
                current_game.day_night = choice
                current_game.day_night_fixed = True
                _append_instant(current_game, player.name,
                                f'🔮 Celestial_reversal — day/night fixed to {choice}')
                print(f'\t\t\tINSTANT Celestial_reversal: day/night FIXED to {choice} (for the rest of the game)')
            continue
        # Mages thermic_flux: the planet temperature
        # changes by ±4 °C to the player's choice (message 'temp_change', validated in
        # player_play), CLAMPED to 1..20, PERMANENTLY (mutated at play time, so it
        # cannot be blocked / canceled / conditioned). The new value is read by the
        # temp_* conditions at resolution time. The card itself resolves as a no-op
        # below (advancing 0, like a placeholder).
        if card_id == MAGE_THERMIC_FLUX:
            direction = msg.get('temp_change')
            if direction in ('up', 'down'):
                old_temp = current_game.temperature
                delta = 4 if direction == 'up' else -4
                new_temp = max(1, min(20, (old_temp or 0) + delta))
                current_game.temperature = new_temp
                msg['thermic_flux_from'] = old_temp
                msg['thermic_flux_to'] = new_temp
                _append_instant(current_game, player.name,
                                f'🌡️ thermic_flux — temperature {old_temp} → {new_temp} °C')
                print(f'\t\t\tINSTANT thermic_flux: temperature {old_temp} -> {new_temp} ({direction} 4, clamped 1..20)')
            continue
        # Mages nobodymoves: LOCKS ALL PLAYERS'
        # MOVEMENT for the rest of the turn — their MOVE cards still resolve (condition
        # + non-movement effects fire) but their basic advancing + movement effects are
        # suppressed, except an "unstoppable" card whose condition is met (it still
        # moves). DEFEND cards are unaffected (they never advance). INSTANT - applied at
        # play time, so it cannot be blocked / canceled / conditioned. The lock is
        # game-level and cleared in the cleaning phase. The card itself resolves as a
        # no-op below (advancing 0, like a placeholder).
        if card_id == MAGE_NOBODYMOVES:
            current_game.nobodymoves_active = True
            _append_instant(current_game, player.name,
                            "🚫 nobodymoves — all players' movement locked")
            print(f'\t\t\tINSTANT nobodymoves: ALL PLAYERS MOVEMENT LOCKED for the rest of the turn (only unstoppable may move)')
            continue
        # Mages Apocalypticritual: the ORDER OF
        # ALL 4 CATACLYSM CARDS is set to the player's choice (message
        # 'cataclysm_order', validated in player_play) - a permutation of the 4
        # biomes, index 0 = the biome that strikes NEXT (trigger_cataclysm pops the
        # top and rotates it). PERMANENT - it is the pile order read at resolution
        # time until the next Apocalypticritual reorders it (mutated at play time,
        # so it cannot be blocked / canceled / conditioned). The card itself
        # resolves as a no-op below (advancing 0, like a placeholder).
        if card_id == MAGE_APOCALYPTICRITUAL:
            order = msg.get('cataclysm_order')
            if (isinstance(order, list) and len(order) == len(BIOMES)
                    and all(isinstance(b, str) and b in BIOMES for b in order)
                    and len(set(order)) == len(order)):
                current_game.cataclysm_pile = list(order)
                # engine annotation on the action (shared dict: also lands in
                # action_chain / messages_history): the replay reads it
                msg['cataclysm_order_set'] = list(order)
                # turn log: the 'instant' section (the replay parses this phrase to
                # reconstruct the cataclysm pile — keep the '☄️ Apocalypticritual' prefix)
                _append_instant(current_game, player.name,
                                '☄️ Apocalypticritual — cataclysm order set: ' + ' → '.join(order))
                print(f'\t\t\tINSTANT Apocalypticritual: cataclysm order SET to {order} (index 0 strikes next)')
            continue
        rows = CARDS_DB.filter(pl.col('card_id') == card_id)
        if rows.is_empty():
            continue
        r = rows.row(0, named=True)
        if r['effect'] == 'pet_trap':
            cell = player.current_position or 0
            current_game.drop_tokens[cell] = current_game.drop_tokens.get(cell, 0) + 1
            placed.append(cell)
            _append_instant(current_game, player.name,
                            f'🪤 pet_trap — drop token placed on cell {cell}')
            print(f'\t\t\tINSTANT pet_trap: drop token placed on cell {cell} (total there: {current_game.drop_tokens[cell]})')
        elif r['effect'] == 'wrecking_ball':
            oppo = _get_oppo(player, current_game)
            if oppo is not None and oppo.dwelling:
                dwelling_card = oppo.dwelling
                oppo.discard = (oppo.discard or []) + [dwelling_card]
                oppo.dwelling = None
                # the placeholder STAYS IN PLACE — it marks the trip-chain position the
                # owner consumed (play_count is NOT released — the owner's next play
                # lands on the position AFTER the placeholder), and the slot stays
                # visually filled until the cleaning phase clears it (as for every
                # placeholder). The frontend renders the placeholder from p.dwelling_slot
                # alone (it does not require p.dwelling to be set).
                # (No annotation for the placeholder: the engine state itself carries
                # it — dwelling_slot is preserved while dwelling is cleared.)
                # engine annotation on the action (shared dict: also lands in
                # action_chain / messages_history): the replay reads it
                msg['dwelling_removed'] = dwelling_card
                msg['dwelling_removed_from'] = oppo.name
                note = f"💥 wrecking_ball — removed {oppo.name}'s dwelling card {dwelling_card}"
                _append_instant(current_game, player.name, note)
                print(f'\t\t\tINSTANT wrecking_ball: removed {oppo.name}\'s dwelling card {dwelling_card} (placeholder stays in place)')
        # swap_cards: the card just played SWAPS its trip-chain
        # POSITION with the entry chosen by the player (message 'swap_with': a
        # position 1..5 of the player's OWN chain — a play, a board placeholder
        # (doctor pending / dwelling) or a rooted card). The two entries exchange
        # their stopover columns: the play's recorded 'to' becomes the target's
        # stopover, and the target's slot becomes the play's original stopover.
        # INSTANT - applied at play time (the effect is already on the board before
        # the chain starts), so it cannot be blocked / canceled / conditioned. The
        # trip chain then resolves everything by the RECORDED columns (positions,
        # facing, blocking all read the 'to' values), so the swapped order is
        # applied automatically. The card still applies its condition/effect at
        # resolution (a swap_cards card with a condition can fail it - the swap is
        # already done, like every instant effect).
        elif r['effect'] == 'swap_cards':
            swap_with = msg.get('swap_with')
            if isinstance(swap_with, int) and not isinstance(swap_with, bool) and 1 <= swap_with <= 5:
                # the card's own (pre-swap) stopover: player_play overwrote msg['to']
                # with the card's position BEFORE the instant effects ran, so it is
                # here now (the client-sent 'to' was display-only)
                _m = re.match(r'stopover_(\d+)', msg.get('to') or '')
                own_col = int(_m.group(1)) if _m else None
                found = _find_swap_target(player, swap_with, current_game)
                if found is not None and own_col is not None:
                    kind, ref = found
                    if not (kind == 'play' and ref is msg):   # defensive: a self-swap is a no-op
                        target_col = 5 - swap_with
                        msg['to'] = f'stopover_{target_col}'   # the play takes the target's position
                        if kind == 'play':
                            target_name = (ref.get('cards') or ['?'])[0]
                            ref['to'] = f'stopover_{own_col}'
                        elif kind == 'pending':
                            target_name = ref[0]
                            ref[1] = own_col
                        elif kind == 'dwelling':
                            target_name = (player.dwelling or 'dwelling')
                            player.dwelling_slot = own_col
                        else:   # rooted
                            target_name = ref.get('card_id') or ref.get('card') or '?'
                            ref['stopover'] = f'stopover_{own_col}'
                        # engine annotation on the action (shared dict: also lands in
                        # action_chain / messages_history): the turn log + the replay
                        # read it (the replay mirrors the placeholder-slot rewrite)
                        msg['swapped_with'] = target_name
                        msg['swapped_with_kind'] = kind
                        msg['swapped_from'] = f'stopover_{own_col}'
                        _append_instant(current_game, player.name,
                                        f'\U0001F500 swap_cards — {card_id} swaps places with {target_name}')
                        print(f'\t\t\tINSTANT swap_cards: {card_id} (pos {5 - own_col}) <-> {target_name} (pos {swap_with})')
    return placed

def _cell_has_drop(current_game, cell_index):
    """ pet_trap: True if the cell carries at least one drop
     token (placed at play time by a pet_trap card). Empty for older games
     (drop_tokens defaults to {}), so this is a no-op for them. """
    return (current_game.drop_tokens or {}).get(cell_index, 0) > 0

# Movement effects: the effects whose
# apply_effect actually moves a token. During a nobodymoves turn these are
# SUPPRESSED (unless the card is unstoppable), along with the basic advancing.
# Non-movement effects (draw, ramp, discard, taxation, rooted-token, pet_trap,
# wrecking_ball, effect_canceled, unstoppable, copy_effect, grappling_hook) are
# unaffected. grappling_hook / copy_effect COPIES are gated separately in the trip
# chain (see apply_grappling_copy / apply_copy_effect).
MOVEMENT_EFFECTS = {'advancing', 'backward', 'jump', 'advancing_oppo', 'backward_oppo', 'avalanche'}

def _card_is_unstoppable(card_id, player, current_game, pcard=None):
    """ True when the card `card_id` (played with pending card `pcard`, if any) is
     UNSTOPPABLE for `player` right now: its effect is 'unstoppable' (or it carries
     a mercurochrome pending) AND its condition is met. A support card (not in the
     main pool) is never unstoppable. """
    rows = CARDS_DB.filter(pl.col('card_id') == card_id)
    if rows.is_empty():
        return False
    row = rows.row(0, named=True)
    return (row['effect'] == 'unstoppable' or pcard == 'mercurochrome') \
        and is_condition_met(row['condition'], player, current_game)

def _is_movement_locked(current_game, player, action):
    """ nobodymoves: True when the MOVE card in `action` (the
     player's message dict) has its MOVEMENT locked — i.e. nobodymoves is active
     this turn and the card is NOT unstoppable (an unstoppable card with its
     condition met still moves). Used to suppress the card's basic advancing, its
     movement effects (MOVEMENT_EFFECTS), its pending epo/virus, and its
     grappling/copy copies. Non-movement effects and the defend/block race are
     UNAFFECTED (the card still resolves them). """
    if not current_game.nobodymoves_active:
        return False
    cards = action.get('cards') or []
    if not cards:
        return True   # no card to check — treat as locked (defensive)
    return not _card_is_unstoppable(cards[0], player, current_game, action.get('pending_card'))

def _facing_effect_name(action):
    """ The `effect` column value of the card in `action` (the facing card's
     message dict), or None if it is a support card / has no card. Used to decide
     whether a copy_effect copy is movement (suppressed during a nobodymoves turn)
     or a zone effect (still fires). """
    cards = action.get('cards') or []
    if not cards:
        return None
    rows = CARDS_DB.filter(pl.col('card_id') == cards[0])
    if rows.is_empty():
        return None
    return rows.row(0, named=True)['effect']

def _player_blocked(player, current_game):
    """ landmine / nobodymoves: True while
     the player is FULLY BLOCKED (its move cards are CANCELED for the rest of the
     turn - except unstoppable cards with their condition met). Used for the
     FULL-CANCEL blocks (landmine) and for ROOTED cards on the trip chain (a rooted
     card is pure movement, so it is inert during a nobodymoves turn). nobodymoves
     is a GAME-LEVEL block (all players) set at play time and cleared in the cleaning
     phase; landmine is PER-PLAYER (the arriving player only).
     NOTE: for non-rooted move cards, nobodymoves now LOCKS movement only (the card
     still resolves its non-movement effects) — see _is_movement_locked. """
    return bool(current_game.nobodymoves_active) or bool(player.landmine_blocked)

def _trigger_board_drops(current_game, player, log_entry=None):
    """ engineers' drops: the FIRST token that ARRIVES
    on a cell (stepping through process_advancing, or a jump landing via _jump)
    fires ALL the engineer drop tokens on that cell, in placement order - then
    they are consumed (each drop fires once).
      boost      -> +2 advancing (stepped through the cells: win check, can chain
                    onto further drops; no faction biome bonus - it is the drop's
                    own value, not the card's)
      trampoline -> +2 jump (teleport, only the landing cell is checked)
      gluetrap   -> -1 knockback (direct, clamped at cell 0, no re-trigger)
      landmine   -> the arriving player is BLOCKED: landmine_blocked = True until
                    the cleaning phase (its move cards are canceled in process_card,
                    and the remaining steps of the CURRENT movement stop here)
    Placing a drop on an occupied cell does not fire it (placement happens at play
    time, this only fires on ARRIVAL). Knockbacks (cataclysm / avalanche /
    gluetrap / pet_trap recoil) do not trigger drops - same as the pet_trap rule.
    The caller must re-sync the earth array (the token may have moved). """
    cell = player.current_position or 0
    here = [d for d in (current_game.board_drops or []) if (d or {}).get('cell') == cell]
    if not here:
        return current_game
    was_blocked = bool(player.landmine_blocked)
    for d in here:
        # a previous drop may have moved the token OFF the cell (boost / trampoline /
        # gluetrap): the remaining drops stay for the next arrival (they fire only
        # when a token is on their cell)
        if (player.current_position or 0) != cell:
            break
        kind = d.get('kind')
        current_game.board_drops.remove(d)   # consumed on trigger
        if kind == 'boost':
            print(f'\t\t\tengineer drop BOOST on cell {cell}: {player.name} +2 advancing')
            if log_entry is not None and log_entry.get('player') == player.name:
                log_entry['notes'].append(f'🧲 boost on cell {cell} — +2 advancing')
            current_game = process_advancing(2, player, current_game, allow_bonus=False, log_entry=log_entry)
        elif kind == 'trampoline':
            print(f'\t\t\tengineer drop TRAMPOLINE on cell {cell}: {player.name} jumps +2')
            if log_entry is not None and log_entry.get('player') == player.name:
                log_entry['notes'].append(f'🦘 trampoline on cell {cell} — jump +2')
            current_game = _jump(player, 2, current_game, log_entry)
        elif kind == 'gluetrap':
            print(f'\t\t\tengineer drop GLUETRAP on cell {cell}: {player.name} knocked back -1')
            if log_entry is not None and log_entry.get('player') == player.name:
                log_entry['notes'].append(f'🩹 gluetrap on cell {cell} — knocked back -1')
            player.current_position = max(0, cell - 1)   # direct recoil (no re-trigger)
        elif kind == 'landmine':
            print(f'\t\t\tengineer drop LANDMINE on cell {cell}: {player.name} BLOCKED for the rest of the turn')
            if log_entry is not None and log_entry.get('player') == player.name:
                log_entry['notes'].append(f'💥 landmine on cell {cell} — {player.name} blocked')
            player.landmine_blocked = True
        else:
            print(f'\t\t	unknown engineer drop {kind} on cell {cell} (consumed, no effect)')
        # a landmine fired during this sequence: the player is blocked, the
        # remaining drops on the cell stay for the next arrival
        if current_game.state == "game over" or (player.landmine_blocked and not was_blocked):
            break
    return current_game

def grappling_copy_amount(game, facing_adv):
    """ grappling_hook: the forward movement a player copies from its facing card's
    total advancement (the net cells that facing card moved). 0 when the facing
    card did not advance forward (a grappling hook pulls forward, it never copies
    a recoil / net backward move). """
    return max(0, int(facing_adv or 0))


def apply_grappling_copy(game, player, amount, log_entry=None):
    """ grappling_hook: apply the copied forward advance (the facing card's total
    advancement). It is a pure copy of the facing card's movement, so it is applied
    WITHOUT the player's own faction biome bonus (allow_bonus=False) - but it still
    goes through process_advancing so the win condition and trap/drop checks apply. """
    if amount > 0:
        print(f'\t\t\tgrappling_hook: {player.name} copies the facing card advancement (+{amount})')
        if log_entry is not None:
            log_entry['notes'].append(f'grappling hook — copied +{amount}')
        return process_advancing(amount, player, game, allow_bonus=False, log_entry=log_entry)
    return game

def apply_copy_effect(game, copier, copier_action, facing_player, facing_action, log_entry=None):
    """ copy_effect: the copier copies the effect of its
    FACING card (the opponent card at the same trip-chain index / stopover), applied
    with the COPIER as the actor (so _oppo effects target the copier's opponent)
    and with the FACING card's data (effect_number + basic advancing).

    The trip chain calls it only when BOTH facing effects fired (condition met, not
    blocked, not effect_canceled). Guards kept here too:
    - the copier's card is not a copy_effect card -> no-op,
    - the facing card is a copy_effect itself -> no recursion, nothing to copy.
    Effects with no behavior in apply_effect (unstoppable, effect_canceled,
    grappling_hook, ...) are harmless no-ops when copied. """
    if not (copier_action or {}).get('cards') or (copier_action or {}).get('mode') != 'move':
        return game
    rows = CARDS_DB.filter(pl.col('card_id') == copier_action['cards'][0])
    if rows.is_empty() or rows.row(0, named=True)['effect'] != 'copy_effect':
        return game
    if not (facing_action or {}).get('cards') or (facing_action or {}).get('mode') != 'move':
        return game
    rows = CARDS_DB.filter(pl.col('card_id') == facing_action['cards'][0])
    if rows.is_empty():
        return game
    facing = rows.row(0, named=True)
    if facing['effect'] == 'copy_effect':      # no recursion: a facing copy_effect has nothing to copy
        return game

    print(f'\t\t\tcopy_effect: {copier.name} copies "{facing["effect"]}" from the facing card {facing["name"]}')
    if log_entry is not None:
        log_entry['notes'].append(f'copy_effect — copied "{facing["effect"]}" ({facing["name"]})')
    return apply_effect(facing['effect'], facing['effect_number'], int(facing['advancing']), copier, game, log_entry)


# ------------------------------------------------------------------ turn log
# Public per-turn recap of the resolution, persisted in GameState.log so the UI
# can show a collapsible history: turn -> stopover -> each player's line
# (card, condition met, effect, negative effects, notes, positions).
# Only PUBLIC information is recorded: played cards are public (they were on the
# stopovers), positions are public, condition met / effect / block / cancel are
# part of the public resolution. No hand/mana/deck content ever goes in here.

def _append_instant(current_game, player_name, phrase):
    """ Record an INSTANT effect (a play-time effect or a dwelling tap) in the
     CURRENT TURN's log entry — the 'instant' section the UI renders at the TOP of
     the turn, before the stopovers. Instant effects fire at PLAY TIME (before the
     trip chain) or on a TAP (a quick action, never part of the chain), so they do
     not belong to any stopover line.
     Creates the turn's log entry if it does not exist yet (instant effects fire
     before process_trip_chain runs, which normally creates the entry). """
    log_list = current_game.log
    if not isinstance(log_list, list):
        log_list = current_game.log = []
    turn_log = log_list[-1] if log_list else None
    if turn_log is None or turn_log.get('turn') != current_game.turn:
        turn_log = {'turn': current_game.turn, 'stopovers': [], 'instant': []}
        log_list.append(turn_log)
    turn_log.setdefault('instant', []).append({'player': player_name, 'what': phrase})
    return current_game

def new_log_entry(player, action, order):
    """ create an empty per-player log entry for one action of the trip chain.
     Filled during resolution: condition_met / effect / shield / negatives / notes / pos_after.
     (Instant effects — pet_trap, engineer drops, wrecking_ball, the Mages' instant
     cards — are NOT noted here: they fire at PLAY TIME and are recorded in the
     turn's 'instant' section, see _append_instant.) """
    entry = {
        'player': player.name,
        'order': order,                          # 1 = first player of the turn, 2 = second
        'mode': (action or {}).get('mode') or '',
        'to': (action or {}).get('to'),          # e.g. 'stopover_4'
        'cards': list((action or {}).get('cards') or []),   # move: 1 card, defend: 1-5
        'pos_before': player.current_position or 0,
        'pos_after': None,
        'condition_met': None,                   # None = defend (the condition is never evaluated)
        'effect': None,                          # the card's effect (from the pool)
        'shield': None,                          # defend only: total shield of the defended cards
        'negatives': [],                         # blocked, effect canceled, ...
        'notes': [],                             # cataclysm / avalanche / grappling copy / win, ...
    }
    return entry

def _log_stopover(sv, turn_log, resolved):
    """ finalize one stopover entry: set each resolved entry's pos_after (AFTER any
     grappling copies) and append the stopover to the turn log (only if a card was played) """
    for entry, player in resolved:
        if entry is not None:
            entry['pos_after'] = player.current_position or 0
    for entry, _ in resolved:
        if entry is not None:
            sv['entries'].append(entry)
    if sv['entries']:
        turn_log['stopovers'].append(sv)

def process_trip_chain(current_game, resume=None):
    """ process the action chain of the game, effect first then advancing.
     grappling_hook copies: after BOTH facing cards at an index have resolved, each
     validated grappling card advances by the total advancement of the facing card
     (see apply_grappling_copy / grappling_copy_amount).
     copy_effect copies: after BOTH facing cards at an index have resolved, each
     validated copy_effect card applies the effect of the facing card with itself as
     the actor (see apply_copy_effect) - only when the facing effect fired.

     Also records the public turn log in GameState.log (turn -> stopover -> entries).

     discard selection: when a discard / discard_oppo
     effect fires, the chain PAUSES (current_game.pending_discard is set by
     apply_effect) at the next step boundary. The resume context (index, stage,
     log entries, per-player flags) is saved in current_game.chain_resume and the
     state switches to "turn N - waiting for X to discard K card(s)". When the
     discarding player submits the choice (to: 'discard_pile'),
     handle_websocket_message calls process_trip_chain(current_game, resume=ctx)
     which continues EXACTLY where the chain stopped.
     Stages (the next step to execute, in order): first -> second -> grapple_f ->
     grapple_s -> copy_f -> copy_s -> log. `first` = before the first player's
     action at this index, `second` = before the second player's action, ...

     stopover positions: the chain is resolved BY
     POSITION (1->5), per-player. Each player's trip chain is that player's OWN
     rooted cards (from the previous turn, positions 1..R) followed by this turn's
     plays (positions R+1..). A rooted card is a REAL card: it applies its basic
     advancing (no condition, no effect) and is blockable. The facing (block /
     grappling / copy) is between the two players' entries at the SAME position. """
    print('Processing trip chain...')
    # Select first and 2nd player in the turn order
    first_player = current_game.players[current_game.turn_order[0]]
    second_player = current_game.players[current_game.turn_order[1]]

    chain_f = _player_chain(current_game, first_player.name)
    chain_s = _player_chain(current_game, second_player.name)
    # max POSITION (not len): the chain can have GAPS - a pending placeholder attached
    # to a play is removed from the chain, leaving its position empty. max_pos must
    # reach the farthest occupied position.
    _positions = [e['position'] for e in (chain_f or [])] + [e['position'] for e in (chain_s or [])]
    max_pos = max(_positions) if _positions else 0

    def _entry_at(chain, pos):
        for e in chain:
            if e['position'] == pos:
                return e
        return None

    if resume is not None:
        ctx = resume
        log_list = current_game.log
        if isinstance(log_list, list) and log_list:
            turn_log = log_list[-1]
        else:
            if not isinstance(log_list, list):
                current_game.log = []
            turn_log = {'turn': current_game.turn, 'stopovers': []}
            current_game.log.append(turn_log)
            log_list = current_game.log
        turn_log.setdefault('instant', [])
    else:
        log_list = current_game.log
        if not isinstance(log_list, list):
            current_game.log = log_list = []
        # REUSE the turn's entry if an INSTANT effect (a play-time effect or a
        # dwelling tap) already created it at play time — it carries the 'instant'
        # section that must survive the trip chain (a new entry would orphan it)
        turn_log = log_list[-1] if (log_list and log_list[-1].get('turn') == current_game.turn) else None
        if turn_log is None:
            turn_log = {'turn': current_game.turn, 'stopovers': []}
            log_list.append(turn_log)
        turn_log.setdefault('instant', [])
        ctx = {'p': 1, 'stage': 'first'}

    p = ctx['p']

    def flush_unresolved(ctx):
        """ The game just ended mid-chain (a win). The PLAYED cards of the UNRESOLVED
            actions have already left their owner's hand (player_play removes them at
            play time) but were never moved to the discard pile (process_card does
            that when the action resolves) -> without this flush they are lost from
            every zone and the final state no longer conserves the deck. Flush them
            to their owners' discard piles (each action once, in play order).
            Which actions count as "done" at position p is derived from ctx['stage']:
            first's action done iff stage != 'first'; second's action done iff the
            stage is past 'second'. (Rooted cards are NEVER flushed - they are
            already on the board and are not part of the hand/discard conservation.) """
        p = ctx['p']
        stage = ctx['stage']
        first_here = stage == 'first'
        second_here = stage in ('first', 'second')
        for player, chain, here_unprocessed in ((first_player, chain_f, first_here),
                                                (second_player, chain_s, second_here)):
            for entry in chain:
                pos = entry['position']
                unprocessed = pos > p or (pos == p and here_unprocessed)
                if not unprocessed:
                    continue
                if entry['kind'] != 'play':
                    continue
                cards = (entry['action'] or {}).get('cards') or []
                if not cards:
                    continue
                if player.discard is None:
                    player.discard = []
                player.discard.extend(cards)
                # doctors: flush the attached pending card too
                pcard = (entry['action'] or {}).get('pending_card')
                if pcard:
                    player.discard.append(pcard)
                print(f'\t\t\tgame over mid-chain: flushed {len(cards)} unresolved card(s) of {player.name} to discard')
            player.action_chain = []

    def pause_discard(ctx):
        """ The chain hit a discard the player must CHOOSE.
            Persist the resume context, switch the state machine to the waiting
            state and return: the chain resumes when the discarding player submits
            the choice (to: 'discard_pile' -> handle_websocket_message ->
            process_trip_chain(current_game, resume=ctx)). """
        pending = current_game.pending_discard or {}
        name = pending.get('player') or '?'
        n = int(pending.get('n') or 0)
        current_game.chain_resume = ctx
        current_game.state = f"turn {current_game.turn} - waiting for {name} to discard {n} card(s)"
        current_game.message = {'success': True, 'message': f'{name}: choose {n} card(s) to discard'}
        print(f'\t\tdiscard selection pending: {name} must choose {n} card(s) (chain paused, next stage {ctx["stage"]})')
        return current_game

    over = False
    while not over and p <= max_pos:
        if ctx['stage'] == 'first':
            print(f'\tProcessing position: {p}')
        # both entries are created BEFORE resolution (once per position, persisted in
        # ctx across a discard-selection pause) so cross-events (a block fired on
        # the opponent's card, a grappling copy, ...) can be attached to either line
        if 'sv' not in ctx:
            e_f = _entry_at(chain_f, p)
            e_s = _entry_at(chain_s, p)
            ctx['e_f'] = e_f
            ctx['e_s'] = e_s
            if e_f is not None and e_f['kind'] == 'rooted':
                entry_f = new_log_entry(first_player, {'to': e_f['stopover'], 'mode': 'rooted', 'cards': [e_f['card_id']]}, 1)
            elif e_f is not None and e_f['kind'] == 'play':
                entry_f = new_log_entry(first_player, e_f['action'], 1)
            else:
                entry_f = None   # placeholder: an empty position, nothing to log
            if e_s is not None and e_s['kind'] == 'rooted':
                entry_s = new_log_entry(second_player, {'to': e_s['stopover'], 'mode': 'rooted', 'cards': [e_s['card_id']]}, 2)
            elif e_s is not None and e_s['kind'] == 'play':
                entry_s = new_log_entry(second_player, e_s['action'], 2)
            else:
                entry_s = None   # placeholder
            ctx['entry_f'] = entry_f
            ctx['entry_s'] = entry_s
            ctx['sv'] = {
                'stopover': (e_f or e_s or {}).get('stopover') or f'stopover_{max(0, 4 - p)}',
                'entries': [],
            }
            ctx.setdefault('first_adv', 0)
            ctx.setdefault('first_grapple', False)
            ctx.setdefault('first_effect_ok', False)
            ctx.setdefault('second_adv', 0)
            ctx.setdefault('second_grapple', False)
            ctx.setdefault('second_effect_ok', False)
        entry_f, entry_s = ctx['entry_f'], ctx['entry_s']
        e_f, e_s = ctx['e_f'], ctx['e_s']
        sv = ctx['sv']
        stage = ctx['stage']

        # --- one step of the chain (the stage machine) ------------------------
        if stage == 'first':
            # Process the first player's entry at position p (a rooted card from the
            # previous turn, or this turn's play). A PLACEHOLDER is an empty
            # position: nothing resolves, and the flags stay at their reset values
            # (first_adv = 0) so a facing grappling hook copies nothing.
            if e_f is not None and e_f['kind'] == 'rooted':
                current_game, adv, _blocked = process_rooted_card(e_f['card_id'], first_player, current_game,
                                                                  log_entry=entry_f, oppo_entry=entry_s)
                ctx['first_adv'] = adv
                ctx['first_grapple'] = False
                ctx['first_effect_ok'] = False
            elif e_f is not None and e_f['kind'] == 'play':
                pos_before = first_player.current_position
                current_game, g, ok = process_card(e_f['action'], first_player, current_game,
                                                   log_entry=entry_f, oppo_entry=entry_s)
                ctx['first_adv'] = (first_player.current_position or 0) - pos_before
                ctx['first_grapple'], ctx['first_effect_ok'] = g, ok
            ctx['stage'] = 'second'
        elif stage == 'second':
            # Process the second player's entry at position p. A PLACEHOLDER
            # is an empty position: nothing resolves and second_adv stays 0, so a
            # facing grappling hook copies nothing.
            if e_s is not None and e_s['kind'] == 'rooted':
                current_game, adv, _blocked = process_rooted_card(e_s['card_id'], second_player, current_game,
                                                                  log_entry=entry_s, oppo_entry=entry_f)
                ctx['second_adv'] = adv
                ctx['second_grapple'] = False
                ctx['second_effect_ok'] = False
            elif e_s is not None and e_s['kind'] == 'play':
                pos_before = second_player.current_position
                current_game, g, ok = process_card(e_s['action'], second_player, current_game,
                                                   log_entry=entry_s, oppo_entry=entry_f)
                ctx['second_adv'] = (second_player.current_position or 0) - pos_before
                ctx['second_grapple'], ctx['second_effect_ok'] = g, ok
            ctx['stage'] = 'grapple_f'
        elif stage == 'grapple_f':
            # grappling_hook (first player): copies the total advancement of its
            # facing card (the second player's entry at this same position) - applied
            # AFTER both cards have resolved so the facing advancement is known.
            # A landmine-blocked player does not copy (its card
            # was canceled, so first_grapple is False). A nobodymoves-locked player
            # does not copy UNLESS the card is unstoppable.
            if ctx['first_grapple'] and not _is_movement_locked(current_game, first_player, e_f['action']):
                current_game = apply_grappling_copy(current_game, first_player,
                                                    grappling_copy_amount(current_game, ctx['second_adv']), entry_f)
            ctx['stage'] = 'grapple_s'
        elif stage == 'grapple_s':
            if ctx['second_grapple'] and not _is_movement_locked(current_game, second_player, e_s['action']):
                current_game = apply_grappling_copy(current_game, second_player,
                                                    grappling_copy_amount(current_game, ctx['first_adv']), entry_s)
            ctx['stage'] = 'copy_f'
        elif stage == 'copy_f':
            # copy_effect: a validated copy_effect card
            # copies the effect of its FACING card (the opponent entry at this same
            # position / stopover), applied with the copier as the actor. Only when
            # the facing card's effect actually fired (condition met, not blocked,
            # not effect_canceled) and the facing entry is a PLAY (a rooted card has
            # no effect to copy).
            # nobodymoves: a movement-locked copier copies ONLY
            # a non-movement (zone) effect; a movement-effect copy is suppressed.
            # An unstoppable copier (condition met) copies regardless.
            if ctx['first_effect_ok'] and ctx['second_effect_ok'] and e_s is not None and e_s['kind'] == 'play' \
                    and (not _is_movement_locked(current_game, first_player, e_f['action'])
                         or _facing_effect_name(e_s['action']) not in MOVEMENT_EFFECTS):
                current_game = apply_copy_effect(current_game, first_player, e_f['action'],
                                                 second_player, e_s['action'], entry_f)
            ctx['stage'] = 'copy_s'
        elif stage == 'copy_s':
            if ctx['second_effect_ok'] and ctx['first_effect_ok'] and e_f is not None and e_f['kind'] == 'play' \
                    and (not _is_movement_locked(current_game, second_player, e_s['action'])
                         or _facing_effect_name(e_f['action']) not in MOVEMENT_EFFECTS):
                current_game = apply_copy_effect(current_game, second_player, e_s['action'],
                                                 first_player, e_f['action'], entry_s)
            ctx['stage'] = 'log'
        elif stage == 'log':
            # finalize the stopover entry (positions are read AFTER the grappling
            # copies) and move to the next position
            _log_stopover(sv, turn_log, [(entry_f, first_player), (entry_s, second_player)])
            if p >= max_pos:
                over = True   # both players' chains are exhausted
            else:
                p += 1
                ctx['p'] = p
                ctx['stage'] = 'first'
                # reset the per-position flags (a player with NO entry at the new
                # position keeps the old flags unless we clear them here - the flags
                # are only assigned when an action actually resolved)
                ctx['first_adv'] = 0
                ctx['first_grapple'] = False
                ctx['first_effect_ok'] = False
                ctx['second_adv'] = 0
                ctx['second_grapple'] = False
                ctx['second_effect_ok'] = False
                del ctx['entry_f'], ctx['entry_s'], ctx['sv'], ctx['e_f'], ctx['e_s']
                continue   # the 'log' step changes nothing else: no boundary check needed
            break

        # --- boundary checks (after every state-changing step) ----------------
        if current_game.state == "game over":
            # a win: a pending discard (if any) is moot - the cards stay in the
            # hand (card conservation holds: they are still in a zone)
            if current_game.pending_discard is not None:
                current_game.pending_discard = None
            flush_unresolved(ctx)
            resolved = [(entry_f, first_player)] if stage == 'first' else [(entry_f, first_player), (entry_s, second_player)]
            _log_stopover(sv, turn_log, resolved)
            return current_game
        if current_game.pending_discard is not None:
            return pause_discard(ctx)

    # both players passed with no card this turn: nothing resolved -> drop the empty
    # entry — UNLESS the turn had INSTANT effects (e.g. a tap-only turn with no card
    # played): that entry is the only record of them, so it stays.
    if not turn_log.get('stopovers') and not turn_log.get('instant'):
        log_list.remove(turn_log)

    return current_game
def process_card(cards_dict, player, current_game, log_entry=None, oppo_entry=None):
    """ process a cards (list) that are in the trip chain, card format:
     log_entry: this player's log entry (filled with condition_met / effect / shield /
     negatives / notes during the resolution); oppo_entry: the opponent's entry for the
     SAME stopover (receives the cross-events: "blocked X's card", block-card effect).
     Returns (current_game, grappling_activated, effect_activated) - effect_activated
     is True when the card's effect actually fired (condition met, not canceled); the
     trip chain uses it for copy_effect (a copy only happens when the facing effect fired).
     {
        'cards': random.sample(p1.hand, 3),     # cards selected by user - LIST (if move mode == 1 card, if defend mode no max)
        'to': 'mana',                           # destination selected by user - STRING [stopover_x, mana, pending_zone, dwelling, discard_pile]
        'mode': '',                             # mode selected by user - STRING ['', move, defend, dwelling_activation, pending, pass]
        'pendings': []                          # cards in pendings zone that has to be added to a normal move card - LIST
    } """
    print(f'\t\t{player.name} processing cards: {cards_dict}')

    # move played cards to discard pile (so the deck can be reshuffled later)
    card_ids = cards_dict['cards']
    if player.discard is None:
        player.discard = []
    player.discard.extend(card_ids)

    stopover = cards_dict['to']

    # --- DEFEND mode: cards played sideways (90°) on the stopover.
    #     They do NOT advance and do NOT trigger their effect. The dedicated block
    #     cards (condition == 'block') have their effect ARMED here, but it fires later,
    #     only if they actually block an opponent card on this same stopover
    #     They block the opponent card on the SAME stopover (checked when that card resolves). ---
    if cards_dict['mode'] == 'defend':
        shield_total = 0
        for card_id in card_ids:
            row = CARDS_DB.filter(pl.col('card_id') == card_id)
            if row.is_empty():
                continue
            r = row.row(0, named=True)
            shield_total += int(r['shield'] or 0)
            if log_entry is not None and log_entry.get('effect') is None:
                log_entry['effect'] = r['effect']
            if r['condition'] == 'block':
                print(f'\t\t\tdefend card {card_id} (condition "block") armed on {stopover}: its effect fires only if it blocks an opponent card')
            else:
                print(f'\t\t\tdefend card {card_id} (shield {r["shield"]}) blocks the same stopover, no effect')
        if log_entry is not None:
            log_entry['shield'] = shield_total
        return current_game, False, False   # defend cards never advance / never grapple / never fire an effect

    # --- MOVE (normal) mode: resolve the card, but first check the opponent's
    #     defend cards on the SAME stopover (block / unstoppable / shields vs mana). ---
    grappling_activated = False
    effect_activated = False
    for card_id in card_ids[:1]:   # normal play: exactly 1 card
        rows = CARDS_DB.filter(pl.col('card_id') == card_id)
        if rows.is_empty():
            continue   # support cards (engineers' drops, ...) resolve as no-ops here
        row = rows.row(0, named=True)
        if log_entry is not None:
            log_entry['effect'] = row['effect']

        # doctors: the attached pending card may be
        # mercurochrome (unstoppable) — check it alongside the card's own effect
        _pending_card = cards_dict.get('pending_card')
        _is_unstoppable = _card_is_unstoppable(card_id, player, current_game, _pending_card)

        # landmine: a landmine-blocked player cannot advance for
        # the rest of the turn - its MOVE cards are CANCELED (no effect, no advancing).
        # The only exception is an "unstoppable" card whose condition is met (like the
        # unstoppable exception to the defend block). Defend cards are NOT affected
        # (they never advance - their shields / block effects work). The card is already
        # in the discard pile (top of process_card) - canceling just means it does nothing.
        if player.landmine_blocked:
            if _is_unstoppable:
                print(f'\t\t	card {card_id} is unstoppable (condition met) -> ignores the landmine block')
                if log_entry is not None:
                    log_entry['notes'].append('unstoppable — ignored the landmine block')
            else:
                print(f'\t\t	card {card_id} CANCELED by the landmine block (no effect, no advancing)')
                if log_entry is not None:
                    log_entry['negatives'].append('landmine — blocked')
                current_game.message = {'success': True, 'message': f'{player.name} is blocked by landmine'}
                continue

        # nobodymoves: GAME-LEVEL movement LOCK (all players) —
        # the card is NOT canceled. Its MOVEMENT is suppressed (basic advancing +
        # movement effects + pending epo/virus + grappling/copy copies), but it STILL
        # RESOLVES: its condition is checked, its non-movement effects (draw, ramp,
        # discard, taxation, rooted-token, ...) still fire, and the defend/block race
        # still happens. An UNSTOPPABLE card with its condition met ignores the lock
        # and still moves. (Unlike the landmine, this is a movement lock, not a cancel.)
        movement_locked = _is_movement_locked(current_game, player, cards_dict)
        if movement_locked:
            print(f'\t\t	card {card_id} is MOVEMENT-LOCKED by nobodymoves (no advancing / movement effects; non-movement effects still fire)')
            if log_entry is not None:
                log_entry['notes'].append('🚫 nobodymoves — movement locked')
        elif current_game.nobodymoves_active:
            # nobodymoves is active but this card is unstoppable (condition met) -> still moves
            print(f'\t\t	card {card_id} is unstoppable (condition met) -> ignores the nobodymoves movement lock (still advances)')
            if log_entry is not None:
                log_entry['notes'].append('unstoppable — ignored the nobodymoves lock')

        # BLOCK check: is the opponent playing defend card(s) on this same stopover?
        oppo = _get_oppo(player, current_game)
        if oppo is not None and _oppo_defend_actions(oppo, stopover):
            # exception 1: an "unstoppable" card whose condition is met is not affected
            #
            if _is_unstoppable:
                print(f'\t\t\tcard {card_id} is unstoppable (condition met) -> ignores the block')
                if log_entry is not None:
                    log_entry['notes'].append('unstoppable — ignored the opponent block')
            else:
                # exception 2: the block only holds if the total shields >= the card's mana
                shields = _oppo_defend_shields(oppo, stopover)
                if shields >= int(row['mana']):
                    print(f'\t\t\tcard {card_id} BLOCKED by {oppo.name} defend card(s) on {stopover} '
                          f'(shields {shields} >= mana {row["mana"]}) -> no effect, no advancing')
                    # the defender's dedicated block cards (condition == "block") now fire their effect
                    current_game = _fire_block_effects(oppo, stopover, current_game, defender_entry=oppo_entry)
                    current_game.message = {'success': True, 'message': f'Card blocked by {oppo.name} on {stopover}'}
                    if log_entry is not None:
                        log_entry['negatives'].append(f'blocked — shields {shields} ≥ cost {row["mana"]}')
                    if oppo_entry is not None:
                        oppo_entry['notes'].append(f'blocked {player.name}’s card on {stopover}')
                    continue
                print(f'\t\t\tblock failed (shields {shields} < mana {row["mana"]}), card {card_id} plays normally')
                if log_entry is not None:
                    log_entry['notes'].append(f'block broken — shields {shields} < cost {row["mana"]}')

        current_game, grappling_activated, effect_activated = _resolve_card(row, player, current_game, stopover, log_entry, movement_locked=movement_locked)

        # doctors: apply the attached pending card's effect.
        # Fires only if the main card's condition was met and its effect was not
        # canceled (effect_activated is True). mercurochrome is a no-op here (it
        # was already applied at block-check time as an unstoppable modifier).
        # nobodymoves: the pending epo (+1) / virus (-1) are
        # MOVEMENT and are suppressed when the card is movement-locked; bloodtest
        # (the OPPONENT discards 1, treated as discard_oppo — it pauses the chain
        # on pending_discard for the opponent's choice) still fires.
        _pcard = cards_dict.get('pending_card')
        if _pcard and effect_activated and current_game.state != "game over":
            current_game = _apply_pending_effect(_pcard, player, current_game, log_entry, movement_locked=movement_locked)

    # doctors: move the attached pending card to the discard
    # pile (it was consumed from the pending zone at play time; it is not in any
    # zone until now, so it must be flushed here for card conservation)
    _pcard_discard = cards_dict.get('pending_card')
    if _pcard_discard:
        if player.discard is None:
            player.discard = []
        player.discard.append(_pcard_discard)

    print(f'\t\t\tcurrent position: {player.current_position} (len(earth): {len(current_game.earth)})')

    return current_game, grappling_activated, effect_activated

def process_rooted_card(card_id, player, current_game, log_entry=None, oppo_entry=None):
    """ stopover positions: resolve a ROOTED card that is
     on the board (from the previous turn's rooted effect). It is a REAL card that
     occupies a stopover position and, when the trip chain resolves, APPLIES ITS
     BASIC ADVANCING (its 'advancing' value) — but NO condition check and NO effect
     (so the rooted effect does not re-trigger / become infinite). It is BLOCKABLE:
     the opponent's defend card(s) on the same stopover block it (shields >= mana),
     like a normal move card. Returns (current_game, advancing_delta, blocked). """
    print(f'\t\t{player.name} resolving rooted card on the board: {card_id}')
    rows = CARDS_DB.filter(pl.col('card_id') == card_id)
    if rows.is_empty():
        return current_game, 0, False
    row = rows.row(0, named=True)
    basic_advancing = int(row['advancing'] or 0)
    if log_entry is not None:
        log_entry['effect'] = row['effect']
        log_entry['condition_met'] = None
        log_entry['notes'].append('🌱 rooted card — basic advancing only')
    stopover = _rooted_stopover_of(current_game, card_id, player.name)
    _rb_source = None
    if current_game.nobodymoves_active:
        _rb_source = 'nobodymoves'
    elif player.landmine_blocked:
        _rb_source = 'landmine'
    if _rb_source:
        print(f'\t\t\trooted card {card_id} CANCELED by the {_rb_source} block (no advancing)')
        if log_entry is not None:
            log_entry['negatives'].append(f'{_rb_source} — rooted card blocked')
        return current_game, 0, False
    oppo = _get_oppo(player, current_game)
    if oppo is not None and _oppo_defend_actions(oppo, stopover):
        shields = _oppo_defend_shields(oppo, stopover)
        if shields >= int(row['mana'] or 0):
            print(f'\t\t\trooted card {card_id} BLOCKED by {oppo.name} defend card(s) on {stopover} (shields {shields} >= mana {row["mana"]}) -> no advancing')
            current_game = _fire_block_effects(oppo, stopover, current_game, defender_entry=oppo_entry)
            current_game.message = {'success': True, 'message': f'Rooted card blocked by {oppo.name} on {stopover}'}
            if log_entry is not None:
                log_entry['negatives'].append(f'blocked — shields {shields} ≥ cost {row["mana"]}')
            if oppo_entry is not None:
                oppo_entry['notes'].append(f'blocked {player.name}’s rooted card on {stopover}')
            return current_game, 0, True
        else:
            print(f'\t\t\trooted card {card_id} block failed (shields {shields} < mana {row["mana"]}) -> advances')
    pos_before = player.current_position or 0
    current_game = process_advancing(basic_advancing, player, current_game, log_entry=log_entry)
    delta = (player.current_position or 0) - pos_before
    return current_game, delta, False

def _rooted_stopover_of(current_game, card_id, owner_name):
    """ the stopover (position) of a rooted card on the board (for the block check) """
    for r in (current_game.rooted_on_board or []):
        if (r or {}).get('card_id') == card_id and (r or {}).get('owner') == owner_name:
            return r.get('stopover')
    return None
def _oppo_defend_actions(oppo, stopover):
    """ list of the opponent's defend actions played on the given stopover
     (each action: {'cards': [...], 'to': 'stopover_x', 'mode': 'defend', ...}) """
    return [a for a in (oppo.action_chain or [])
            if a and a.get('mode') == 'defend' and a.get('to') == stopover]

def _oppo_defend_shields(oppo, stopover):
    """ total shield value of the opponent's defend cards on the given stopover """
    total = 0
    for a in _oppo_defend_actions(oppo, stopover):
        rows = CARDS_DB.filter(pl.col('card_id').is_in(a.get('cards') or []))
        total += int(rows['shield'].sum()) if len(rows) else 0
    return total


def _oppo_has_valid_effect_canceled(oppo, stopover, current_game):
    """ effect_canceled: True if the opponent has a card
    with effect 'effect_canceled' played in MOVE mode on the given stopover whose
    condition is met (a 'valid' cancel card). Such a card CANCELS the effect of the
    facing card (the opponent's card on this same stopover) - checked in _resolve_card.
    Defend-mode cards never fire their effect, so only move-mode cancel cards count. """
    for a in (oppo.action_chain or []):
        if not a or a.get('to') != stopover or a.get('mode') != 'move':
            continue
        for card_id in (a.get('cards') or []):
            rows = CARDS_DB.filter(pl.col('card_id') == card_id)
            if rows.is_empty():
                continue
            r = rows.row(0, named=True)
            if r['effect'] == 'effect_canceled' and is_condition_met(r['condition'], oppo, current_game):
                return True
    return False


def _fire_block_effects(defender, stopover, current_game, defender_entry=None):
    """ Fire the effect of the defender's dedicated block cards (condition == 'block')
    played on this stopover. Called ONLY when an opponent card on this stopover was
    actually blocked (so a block card that did not block anything has no effect). """
    for action in _oppo_defend_actions(defender, stopover):
        for card_id in (action.get('cards') or []):
            rows = CARDS_DB.filter(pl.col('card_id') == card_id)
            if rows.is_empty():
                continue
            r = rows.row(0, named=True)
            if r['condition'] == 'block':
                print(f'\t\t\tblock card {card_id} triggered (it blocked an opponent card on {stopover}) -> applying effect: {r["effect"]}')
                if defender_entry is not None:
                    defender_entry['notes'].append(f'block card {r["name"]} — effect fired: {r["effect"]}')
                current_game = apply_effect(r['effect'], r['effect_number'], int(r['advancing']), defender, current_game, defender_entry)
    return current_game

def trigger_cataclysm(current_game, log_entry=None):
    """ Cataclysm trigger (condition 'cataclysm', fired once per resolved card in
     _resolve_card). Rule:
     1. Look at the TOP card of the cataclysm pile (4 cards, one per biome,
        shuffled at board init).
     2. ALL player tokens (both players) on a cell of that biome are knocked
        back to the FIRST cell of that biome (the start of the 6-cell segment).
        Tokens not on the biome are untouched.
     3. The drawn cataclysm card goes to the BOTTOM of the pile (the pile only rotates).
     A game without a pile (pre-cataclysm rules) is a safe no-op.

     nobodymoves: the knockback is a MOVEMENT, so while nobodymoves is active
     the trigger still FIRES (step 3: the pile rotates, the biome is announced)
     but step 2 is SUPPRESSED — no token is knocked back. """
    pile = current_game.cataclysm_pile or []
    if not pile:
        return current_game

    biome = pile.pop(0)
    pile.append(biome)   # drawn card goes to the bottom of the pile

    if log_entry is not None:
        log_entry['notes'].append(f'⚡ cataclysm — {biome} strikes')

    # nobodymoves: the cataclysm KNOCKBACK is a MOVEMENT, so it
    # is SUPPRESSED while nobodymoves is active (the trigger still fired — the pile
    # rotated above and the biome was announced — but no token moves).
    if current_game.nobodymoves_active:
        print(f'\t\t\tcataclysm knockback SUPPRESSED (nobodymoves movement lock)')
        if log_entry is not None:
            log_entry['notes'].append('nobodymoves — cataclysm knockback suppressed')
        return current_game

    return _knockback_biome(biome, current_game, f'cataclysm: {biome} strikes', log_entry)

def _knockback_biome(biome, current_game, label='strike', log_entry=None):
    """ ALL player tokens (both players) on cells of the given biome are knocked back
     to the FIRST cell of that biome (the start of the 6-cell segment). Tokens not on
     the biome are untouched. Shared by the cataclysm trigger (random biome from the
     pile) and the avalanche effect (fixed MO biome). """
    # first cell of the biome (biomes are contiguous segments of 6 cells)
    start = None
    for i, cell in enumerate(current_game.earth):
        if cell and cell[0] == biome:
            start = i
            break
    if start is None:
        return current_game

    for p in current_game.players.values():
        pos = p.current_position or 0
        cell = current_game.earth[pos]
        if cell and cell[0] == biome:
            print(f'\t\t\t{label}: {p.name} is knocked back from cell {pos} to cell {start} (start of the biome)')
            if log_entry is not None:
                log_entry['notes'].append(f'{label}: {p.name} knocked back {pos} → {start}')
            current_game.earth[pos].remove(p.name)
            p.current_position = start
            current_game.earth[start].append(p.name)
    return current_game

def rotate_earth(current_game, n, log_entry=None):
    """ black_hole: rotate the EARTH's biomes by `n` cells.
    Positive n = CLOCKWISE (the board's cell 0 → cell n direction, i.e. a feature at the
    top moves toward the right / 3 o'clock). The 4 biomes shift POSITION in
    current_game.earth — only the biome CODES at earth[i][0] are rotated; EVERY TOKEN
    (both players' current_position, the pet_trap drop tokens, the engineer board drops)
    STAYS on its own cell index, because all tokens are addressed BY CELL INDEX, so a
    rotation changes which BIOME a given cell belongs to (biome conditions, the faction
    biome bonus, and the cataclysm/avalanche knockback all re-read earth[i][0] and keep
    working) but NOT where the tokens are. The rotation is CUMULATIVE — each tap rotates
    from the current order (8 distinct states, 24/3 = 8). earth_rotation (signed
    cumulative cells) is updated so the frontend can rotate the background art by
    earth_rotation * 15deg (1 cell = 15deg on the 24-cell ring). """
    earth = current_game.earth
    if not earth or len(earth) != n_cells_by_biome * len(BIOMES):
        return current_game
    codes = [cell[0] for cell in earth if cell is not None]
    if len(codes) != n_cells_by_biome * len(BIOMES):
        return current_game
    # content moves clockwise by n: cell (j) → cell (j + n)  ⇒  new[i] = old[(i - n) mod 24]
    n24 = n_cells_by_biome * len(BIOMES)   # 24
    shifted = [codes[(i - n) % n24] for i in range(n24)]
    for i in range(n24):
        earth[i][0] = shifted[i]
    current_game.earth_rotation = (current_game.earth_rotation or 0) + n
    direction = 'clockwise' if n > 0 else 'counter-clockwise'
    print(f'\t\t\tblack_hole: earth rotated {n} cells {direction} (earth_rotation = {current_game.earth_rotation})')
    if log_entry is not None:
        log_entry['notes'].append(f'\U0001fa93 black_hole — earth rotated {abs(n)} cells {direction}')
    return current_game


def _resolve_card(row, player, current_game, stopover=None, log_entry=None, movement_locked=False):
    """ resolve ONE card of the trip chain: condition -> effect -> advancing
     (movement effects handle their own advancing inside apply_effect).
     stopover: the stopover this card was played on (e.g. 'stopover_4') - needed for
     the effect_canceled check (opponent cancel card on the SAME stopover).
     log_entry: the player's log entry (filled with condition_met / negatives / notes).
     movement_locked: nobodymoves - True when the card's MOVEMENT
     is locked (basic advancing + movement effects suppressed, non-movement effects
     and the condition still fire; an unstoppable card is NOT locked).
     Returns (current_game, grappling_activated, effect_activated) - grappling_activated
     is True when this is a grappling_hook card whose condition was met (rule active);
     the trip chain then applies the copy of the facing card's advancing after that card
     has resolved (the copy itself is gated on movement_locked in the trip chain).
     effect_activated is True when the card's effect actually fired (condition met, not
     canceled) - used by the trip chain for copy_effect. """
    condition = row['condition']
    effect = row['effect']
    basic_advancing = int(row['advancing'])
    print(f'\t\t\tadv: {basic_advancing}, mana: {row["mana"]}, condition: {condition}, effect: {effect}')

    # cataclysm trigger: fired EXACTLY ONCE here (not inside is_condition_met,
    # which may be called for evaluation only - e.g. by the replay or the
    # unstoppable check - and must stay side-effect free)
    if condition == 'cataclysm':
        current_game = trigger_cataclysm(current_game, log_entry)

    # check card condition
    condition_met = is_condition_met(condition, player, current_game)
    if log_entry is not None:
        log_entry['condition_met'] = condition_met

    # effect_canceled: before applying this card's
    # effect, check if the opponent has a VALID effect_canceled card (move mode,
    # condition met) on the SAME stopover - if so, this card's effect is
    # CANCELED: it does NOT fire (including any movement it would have caused),
    # but the card still advances by its basic value.
    effect_cancelled = False
    if condition_met and stopover:
        oppo = _get_oppo(player, current_game)
        if oppo is not None and _oppo_has_valid_effect_canceled(oppo, stopover, current_game):
            effect_cancelled = True
            print(f'\t\t\teffect {effect} CANCELED by {oppo.name} effect_canceled card on {stopover} -> no effect, basic advancing only')
            if log_entry is not None:
                log_entry['negatives'].append(f'effect canceled by {oppo.name}')

    # effect_activated: this card's effect actually fired (condition met, not
    # canceled). The trip chain uses it for copy_effect: a copy only happens when
    # the FACING card's effect fired (and the copier's own, of course).
    effect_activated = condition_met and not effect_cancelled
    # grappling_hook: the card's OWN advancing is applied below as usual; the
    # "copy of the facing card's advancing" is flagged here and applied by the trip
    # chain (it needs the facing card's movement, only known once that card resolved)
    grappling_activated = (effect == 'grappling_hook' and condition_met)

    if condition_met:
        if effect_cancelled:
            # the effect is canceled: only the basic advancing is applied
            # (suppressed if the card is movement-locked by nobodymoves)
            if movement_locked:
                if log_entry is not None:
                    log_entry['notes'].append('nobodymoves — basic advancing suppressed')
            else:
                current_game = process_advancing(basic_advancing, player, current_game, log_entry=log_entry)
        else:
            if movement_locked and effect in MOVEMENT_EFFECTS:
                # nobodymoves: the movement effect is SUPPRESSED (the card is
                # movement-locked). Non-movement effects still fire below.
                print(f'\t\t\teffect {effect} SUPPRESSED (nobodymoves movement lock)')
                if log_entry is not None:
                    log_entry['notes'].append(f'nobodymoves — {effect} suppressed')
            else:
                print(f'\t\t\tapplying effect: {effect}')
                current_game = apply_effect(effect, row['effect_number'], basic_advancing, player, current_game, log_entry)

            # rooted: grant the card a rooted token (it survives
            # the cleaning phase and stays on a free stopover). One-shot, 1-turn
            # cooldown. The token is NOT movement, so it is granted even during a
            # nobodymoves turn. The card still applies its basic advancing below
            # (unless movement-locked).
            if effect == 'rooted':
                current_game = _grant_rooted_token(row['card_id'], player, current_game, log_entry)

            # Apply basic advancing (movement effects already moved the player inside
            # apply_effect). Suppressed if the card is movement-locked by nobodymoves.
            if effect not in ('advancing', 'backward', 'jump'):
                if movement_locked:
                    if log_entry is not None:
                        log_entry['notes'].append('nobodymoves — basic advancing suppressed')
                else:
                    current_game = process_advancing(basic_advancing, player, current_game, log_entry=log_entry)
    else:
        # condition not met: reduced advancing (card mana - 1) — suppressed if
        # the card is movement-locked by nobodymoves
        reduced_advancing = int(row['mana']) - 1
        if movement_locked:
            print(f'\t\t\tcondition not met, reduced advancing {reduced_advancing} SUPPRESSED (nobodymoves)')
            if log_entry is not None:
                log_entry['notes'].append('nobodymoves — reduced advancing suppressed')
        else:
            print(f'\t\t\tcondition not met, reduced advancing: {reduced_advancing}')
            current_game = process_advancing(reduced_advancing, player, current_game, log_entry=log_entry)

    return current_game, grappling_activated, effect_activated

def _apply_pending_effect(pcard, player, current_game, log_entry=None, movement_locked=False):
    """ doctors: apply the effect of a pending card attached
     to a main card. Fires only when the main card's condition was met and its
     effect was not canceled. The pending card has already been consumed from
     the pending zone and is moved to the discard pile by the caller.
     mercurochrome is a no-op here (it was already applied at block-check time
     as an unstoppable modifier).
     nobodymoves: `movement_locked` suppresses the MOVEMENT
     pending effects (epo +1, virus -1); bloodtest (discard 1) is a zone effect
     and still fires.
     bloodtest is treated like a `discard_oppo` effect: it sets
     current_game.pending_discard targeting the OPPONENT — the trip chain pauses
     at the next step boundary and the opponent chooses the card (to:
     'discard_pile'), exactly like the main-faction discard_oppo. The attaching
     player's own hand is never touched. """
    print(f'\t\t\tapplied pending card effect: {pcard}')
    if pcard == 'epo':
        if movement_locked:
            if log_entry is not None:
                log_entry['notes'].append('nobodymoves — pending epo suppressed')
        else:
            current_game = process_advancing(1, player, current_game, allow_bonus=False, log_entry=log_entry)
            if log_entry is not None:
                log_entry['notes'].append('pending epo — +1 advancing')
    elif pcard == 'virus':
        if movement_locked:
            if log_entry is not None:
                log_entry['notes'].append('nobodymoves — pending virus suppressed')
        else:
            current_game = process_advancing(-1, player, current_game, allow_bonus=False, log_entry=log_entry)
            if log_entry is not None:
                log_entry['notes'].append('pending virus — -1 knockback')
    elif pcard == 'bloodtest':
        # TREATED AS A `discard_oppo` EFFECT (like the main-faction cards):
        # the OPPONENT chooses 1 card from their own hand. The chain pauses at
        # the next step boundary on pending_discard; the choice arrives as
        # to:'discard_pile' and resumes the chain (handle_websocket_message).
        oppo = _get_oppo(player, current_game)
        if oppo is not None and len(oppo.hand or []) > 0:
            current_game.pending_discard = {'player': oppo.name, 'n': 1}
            if log_entry is not None:
                log_entry['_pending_discard'] = 1
            print(f'\t\t\tpending bloodtest: PAUSED - {oppo.name} must choose 1 card to discard')
        else:
            if log_entry is not None:
                log_entry['notes'].append('pending bloodtest — opponent has no cards in hand')
    elif pcard == 'mercurochrome':
        # no-op: already applied at block-check time as an unstoppable modifier
        pass
    return current_game

def _grant_rooted_token(card_id, player, current_game, log_entry=None):
    """ rooted: grant a card a rooted token. The card
     will survive the cleaning phase (it is not discarded) and stays on the trip
     chain on a free stopover (placed at end of turn by _process_rooted_cards).
     The token is ONE-SHOT (the card loses it when it is placed). Cooldown: a card
     that earned a token the PREVIOUS turn cannot earn another this turn. """
    last = current_game.rooted_history.get(card_id)
    if last is not None and last == (current_game.turn or 0) - 1:
        print(f'\t\t\trooted: card {card_id} is on cooldown (rooted last turn) -> no token')
        if log_entry is not None:
            log_entry['negatives'].append('rooted on cooldown — no token')
        return current_game
    current_game.rooted_this_turn.append({'card_id': card_id, 'owner': player.name})
    current_game.rooted_history[card_id] = current_game.turn
    print(f'\t\t\trooted: card {card_id} earns a rooted token (survives the cleaning phase)')
    if log_entry is not None:
        log_entry['notes'].append('🌱 rooted — card survives onto the trip chain')
    return current_game

def _player_rooted_count(current_game, player_name):
    """ stopover positions: the number of THAT player's
     rooted cards on the board (from the previous turn's rooted effect). These cards
     are REAL cards that occupy the leading positions (1..R) of that player's OWN trip
     chain — they apply their basic advancing when the chain resolves and are
     blockable (see process_trip_chain). Per-player: a player's rooted cards never
     shift the opponent's positions. """
    return sum(1 for r in (current_game.rooted_on_board or []) if (r or {}).get('owner') == player_name)

def _player_stopover(current_game, player_name, played_before):
    """ SINGLE SOURCE OF TRUTH for stopover ordering (per-player): the stopover
     for the (played_before+1)-th play of
     `player_name`'s turn. A player's trip chain is filled in position order 1->5
     (columns 4, 3, 2, 1, 0); the first (rooted count) positions are occupied by
     that player's OWN rooted cards (from the previous turn). So the
     (played_before+1)-th play is at position (rooted count) + (played_before + 1),
     column 5 - position. PER-PLAYER: a player's rooted cards / dwelling placeholder
     do NOT shift the opponent's positions (the opponent's first play is always on
     the opponent's stopover 1 unless the opponent has their own rooted cards).
     Used by the engine (dwelling placeholder slot, rooted placement) and by the robot
     (ai_driver calls ge._player_stopover); the frontend mirrors it in JS
     (actions.mjs — it cannot import Python).
     Returns 'stopover_N' (clamped to stopover_0 when beyond the 5-stopover board). """
    rooted = _player_rooted_count(current_game, player_name)
    position = rooted + played_before + 1
    col = 5 - position
    if col < 0:
        col = 0
    return f'stopover_{col}'

def _find_swap_target(player, position, current_game):
    """ swap_cards: find the entry of the player's OWN trip
     chain at the given position (1..5): a PLAY (a move/defend action in the
     action_chain), a board PLACEHOLDER (a doctor pending [card, slot] pair, or
     the dwelling placeholder) or a ROOTED card on the board. Position -> column
     is 5 - position (position 1 -> stopover_4, …, position 5 -> stopover_0), the
     same mapping as _player_stopover. Returns (kind, ref) where kind is
     'play' | 'pending' | 'dwelling' | 'rooted' and ref is the STORED entry
     (the action dict / the [card, slot] pair / None / the rooted dict) so the
     caller can rewrite its stopover; or None if the position is empty (nothing
     to swap with there). The lookup is by the RECORDED stopover column (not by
     list order), so it works even with chain gaps (attached pending cards). """
    col = 5 - position
    if col < 0:
        return None
    # plays (move OR defend) in the action_chain at this position
    for a in (player.action_chain or []):
        if (a or {}).get('mode') in ('move', 'defend') and (a or {}).get('to') == f'stopover_{col}':
            return ('play', a)
    # doctor pending placeholders ([card, slot] pairs) at this position
    for e in (player.pending_slots or []):
        if isinstance(e, (list, tuple)) and len(e) == 2 and e[1] is not None and int(e[1]) == col:
            return ('pending', e)
    # the dwelling placeholder at this position
    if player.dwelling_slot is not None and int(player.dwelling_slot) == col:
        return ('dwelling', None)
    # a rooted card on the board at this position (leading positions of the chain)
    for r in (current_game.rooted_on_board or []):
        r = r or {}
        if r.get('owner') == player.name and r.get('stopover') == f'stopover_{col}':
            return ('rooted', r)
    return None

def _player_chain(current_game, player_name):
    """ stopover positions: THAT player's full trip chain for the current turn,
     as an ordered list of entries (position 1 first). Each entry is a dict
     {'kind': 'rooted'|'play'|'placeholder', 'card_id'|'action', 'stopover',
     'position'}.
     The leading entries are the player's OWN rooted cards (from the previous
     turn); the trailing entries are this turn's entries (board-furniture
     placeholders and plays). The trip chain resolves this per-player chain by
     position, and the facing (block / grappling / copy) is between the two
     players' entries at the SAME position.

     A board-furniture PLACEHOLDER (a doctor PENDING card, or a dwelling card)
     OCCUPIES a position in the chain as an empty 'placeholder' entry — no card,
     no effect, nothing to copy / block / cancel. A grappling_hook (or
     copy_effect) facing a placeholder therefore copies NOTHING (facing an empty
     position = no source to copy). Positions come from the RECORDED stopover
     column (position = 5 - col), so the chain always matches the board display
     and the placeholder actually sits between the plays it displaced. """
    def _col_of(stopover):
        m = re.match(r'stopover_(\d+)', stopover or '')
        return int(m.group(1)) if m else None

    rooted = [r for r in (current_game.rooted_on_board or []) if (r or {}).get('owner') == player_name]
    player = current_game.players.get(player_name)

    entries = []
    # leading: the player's OWN rooted cards (from the previous turn)
    for r in rooted:
        col = _col_of(r.get('stopover'))
        if col is None:
            continue
        entries.append({'kind': 'rooted', 'card_id': r.get('card_id'), 'stopover': r.get('stopover'), 'position': 5 - col})
    if player is not None:
        # board-furniture placeholders: EMPTY positions in the chain
        # (a doctor PENDING [card, slot] pair, or the dwelling placeholder)
        for entry in (player.pending_slots or []):
            col = entry[1] if isinstance(entry, (list, tuple)) and len(entry) == 2 else entry
            if col is None:
                continue
            c = int(col)
            entries.append({'kind': 'placeholder', 'stopover': f'stopover_{c}', 'position': 5 - c})
        if player.dwelling_slot is not None:
            c = int(player.dwelling_slot)
            entries.append({'kind': 'placeholder', 'stopover': f'stopover_{c}', 'position': 5 - c})
        # this turn's plays (action_chain, in play order)
        for action in (player.action_chain or []):
            if not action or action.get('mode') not in ('move', 'defend') or not action.get('cards'):
                continue
            col = _col_of(action.get('to'))
            if col is None:
                continue
            entries.append({'kind': 'play', 'action': action, 'stopover': action.get('to'), 'position': 5 - col})
    return entries

def _process_rooted_cards(current_game):
    """ rooted: end-of-turn placement. Called at the start of the next turn
     (before the action chains are cleared):
     1. last turn's rooted cards (rooted_on_board) have served their one extra
        turn -> they are DISCARDED now (card conservation);
     2. this turn's rooted cards (rooted_this_turn) survive: they are pulled out
        of the discard pile and placed onto their owner's LEADING positions in
        PLAY ORDER (the first rooted card -> stopover_4, the second ->
        stopover_3, ...) — per-player (each player's rooted cards are placed on
        their OWN positions, independently of the opponent) and they never share
        a stopover. """
    # 1. discard the previous turn's rooted cards (they are no longer on the board)
    for card in (current_game.rooted_on_board or []):
        owner = current_game.players.get(card.get('owner'))
        if owner is None:
            continue
        if not card.get('card_id'):
            continue
        if owner.discard is None:
            owner.discard = []
        owner.discard.append(card['card_id'])
        print(f'\t\trooted: card {card["card_id"]} ({owner.name}) served its turn -> discarded')
    current_game.rooted_on_board = []
    # 2. per-player placement: each player's rooted cards go on their OWN leading
    #    positions (stopover_4, stopover_3, ...), independently of the opponent
    by_owner = {}
    for entry in (current_game.rooted_this_turn or []):
        by_owner.setdefault(entry.get('owner'), []).append(entry)
    for owner_name, entries in by_owner.items():
        owner = current_game.players.get(owner_name)
        for k, entry in enumerate(entries):
            card_id = entry.get('card_id')
            if owner is not None and owner.discard and card_id in owner.discard:
                owner.discard.remove(card_id)   # the card survives: pulled out of the discard
            col = (4 - k) if k < 5 else 0
            stopover = f'stopover_{col}'
            current_game.rooted_on_board.append({'card_id': card_id, 'owner': owner_name, 'stopover': stopover})
            print(f'\t\trooted: card {card_id} ({owner_name}) stays on the trip chain -> {stopover} (position {k + 1})')
    # clear the per-turn buffer
    current_game.rooted_this_turn = []
    return current_game
def _get_oppo(player, current_game):
    """ return the opponent PlayerState (the other player in the game), or None if playing alone """
    for p in current_game.players.values():
        if p.name != player.name:
            return p
    return None

def _next_from_deck(p):
    """ pop the next card from p.deck, reshuffling the discard pile when the deck runs out.
     returns None if both deck and discard are empty """
    if p.discard is None:
        p.discard = []
    if not p.deck and p.discard:   # deck empty -> reshuffle discard pile into a new deck
        p.deck.extend(p.discard)
        random.shuffle(p.deck)
        p.discard.clear()
    return p.deck.pop(0) if p.deck else None

def _draw_cards(p, n):
    """ draw up to n cards from p.deck into p.hand (reshuffling the discard pile when needed).
     returns the number of cards actually drawn """
    drawn = 0
    while drawn < n:
        card = _next_from_deck(p)
        if card is None:
            break
        p.hand.append(card)
        drawn += 1
    return drawn

def _ramp_mana(p, n):
    """ move up to n cards from p.deck into p.mana (reshuffling the discard pile when needed).
     returns the number of cards actually moved """
    if p.mana is None:
        p.mana = []
    moved = 0
    while moved < n:
        card = _next_from_deck(p)
        if card is None:
            break
        p.mana.append(card)
        moved += 1
    return moved

def _tax_mana(p, n):
    """ move up to n cards from p.mana into p.discard (last cards of the mana zone first).
     returns the number of cards actually taxed """
    if p.discard is None:
        p.discard = []
    n = min(n, len(p.mana))
    taxed = p.mana[-n:] if n else []
    for card in taxed:
        p.mana.remove(card)
    p.discard.extend(taxed)
    return len(taxed)

def _player_faction(player):
    """ The faction the player is playing, derived from their deck (decks are
    single-faction by design; a mixed deck resolves to the majority faction).
    Returns None if no deck card can be found in the card pool. """
    ids = (player.deck or []) + (player.hand or []) + (player.mana or []) + (player.discard or [])
    if not ids:
        return None
    rows = CARDS_DB.filter(pl.col('card_id').is_in(ids))
    if rows.is_empty():
        return None
    counts = rows.group_by('faction').agg(pl.len().alias('n')).sort('n', descending=True)
    return counts['faction'][0]


def _on_home_biome(player, current_game):
    """ True if the player's token is on one of the two biomes of their faction. """
    home = FACTION_BIOMES.get(_player_faction(player) or '')
    if not home:
        return False
    cell = get_player_cell(player, current_game)
    return any(b in cell for b in home)


def is_condition_met(condition, player, current_game):
    # no condition required -> always met
    if condition == 'no_condition':
        return True

    # 'block' is the condition of defend cards: it is not a state to evaluate but a
    # marker handled in process_card. Defend plays return BEFORE any condition
    # evaluation, so reaching this line means the card was played in MOVE mode -
    # the block condition is NOT met there: no effect, reduced advancing (mana - 1)
    # (the effect of a block card fires only when played in defend mode and it
    # actually blocked an opponent card - see _fire_block_effects).
    if condition == 'block':
        return False

    # 'cataclysm' is a TRIGGER condition: the strike itself (knock back all tokens
    # on the struck biome to the biome start, rotate the pile) is fired exactly
    # once in _resolve_card; the condition itself is always treated as MET so the
    # card's effect fires (side-effect lives in _resolve_card, NOT here)
    if condition == 'cataclysm':
        return True

    # biome conditions: check the cell the player currently stands on
    if 'biome' in condition:
        cell = get_player_cell(player, current_game)
        home = FACTION_BIOMES.get(BIOME_CONDITION_FACTION.get(condition, ''), None)
        if home:
            return any(b in cell for b in home)
        # unknown biome -> not implemented, assume met (permissive)
        print(f'\t\t\tcondition {condition} not implemented, assuming met')
        return True

    # distance between players (ahead = I lead the opponent, behind = opponent leads me)
    dist_match = re.match(r'^dist_(ahead|behind)_sup_(\d+)$', condition)
    if dist_match:
        oppo = _get_oppo(player, current_game)
        if oppo is None:
            return False   # no opponent -> distance condition cannot be met
        gap = player.current_position - oppo.current_position      # >0 : I'm ahead, <0 : I'm behind
        threshold = int(dist_match.group(2))
        if dist_match.group(1) == 'ahead':
            return gap > threshold
        return -gap > threshold

    # mana conditions (own zone)
    if condition == 'mana_inf_6':
        return len(player.mana) < 6
    if condition == 'mana_sup_5':
        return len(player.mana) > 5

    # mana conditions (opponent zone)
    if condition in ('mana_inf_6_oppo', 'mana_sup_5_oppo'):
        oppo = _get_oppo(player, current_game)
        if oppo is None:
            return False   # no opponent -> cannot evaluate
        n = len(oppo.mana)
        return n < 6 if condition == 'mana_inf_6_oppo' else n > 5

    # cards in own hand
    if condition == 'cards_in_hand_inf_4':
        return len(player.hand) < 4
    if condition == 'cards_in_hand_sup_3':
        return len(player.hand) > 3

    # cards in opponent's hand (engine sees the full state; only hidden at API level)
    if condition in ('cards_in_hand_inf_4_oppo', 'cards_in_hand_sup_3_oppo'):
        oppo = _get_oppo(player, current_game)
        if oppo is None:
            return False   # no opponent -> cannot evaluate
        n = len(oppo.hand)
        return n < 4 if condition == 'cards_in_hand_inf_4_oppo' else n > 3

    # temperature conditions (planet temperature rolled at game start)
    temp_match = re.match(r'^temp_(inf|sup)_(\d+)$', condition)
    if temp_match:
        if current_game.temperature is None:
            return False   # temperature not rolled yet -> cannot evaluate
        threshold = int(temp_match.group(2))
        return current_game.temperature < threshold if temp_match.group(1) == 'inf' else current_game.temperature > threshold

    # day/night conditions (starts on "day", flips each turn)
    if condition in ('day', 'night'):
        return current_game.day_night == condition

    # drop_on_board: met if ANY drop or trap is on the
    # earth - a drop token (placed by a pet_trap card, in drop_tokens) or a
    # 'trap'/'drop' cell content.
    if condition == 'drop_on_board':
        if any(n > 0 for n in (current_game.drop_tokens or {}).values()):
            return True
        if (current_game.board_drops or []): # engineers' drops
            return True
        for cell in current_game.earth or []:
            if cell and ('trap' in cell or 'drop' in cell):
                return True
        return False

    # pending: met if the player has at least one pending card in their OWN
    # pending zone (the doctors' pending zone). Pure evaluation - no side effects.
    if condition == 'pending':
        return len(player.pendings or []) >= 1

    # any other not-yet-implemented condition: assume met so the card can still advance
    print(f'\t\t\tcondition {condition} not implemented, assuming met')
    return True

def apply_effect(effect, effect_number, basic_advancing, player, current_game, log_entry=None):
    """ apply the card effect (all effects are handled here)
     effect_number: quantitative parameter of the effect from CARDS_DB (e.g. N cards to draw, N cells to recoil)
     basic_advancing: base advancing value of the played card (needed by movement effects)
     log_entry: the playing player's log entry (receives notes for knockbacks / wins) """

    # --- movement effects (they handle the full movement themselves) ---
    if effect == 'advancing':
        bonus = effect_number   # extra forward cells on top of base
        print(f'\t\t\teffect advancing: +{bonus} extra cell(s)')
        return process_advancing(basic_advancing + bonus, player, current_game)

    if effect == 'backward':
        recoil = effect_number   # negative value (e.g. -1 or -2)
        current_game = process_advancing(basic_advancing, player, current_game)
        if current_game.state != "game over":     # no recoil after crossing the finish line
            print(f'\t\t\teffect backward: {recoil} cell(s) recoil')
            current_game = process_advancing(recoil, player, current_game)
        return current_game

    # --- opponent movement effects (the player's own basic advancing is still applied by process_card) ---
    if effect == 'advancing_oppo':
        oppo = _get_oppo(player, current_game)
        if oppo is not None and current_game.state != "game over":
            print(f'\t\t\teffect advancing_oppo: {oppo.name} advances {effect_number} cell(s)')
            current_game = process_advancing(effect_number, oppo, current_game)
        return current_game

    if effect == 'backward_oppo':
        oppo = _get_oppo(player, current_game)
        if oppo is not None and current_game.state != "game over":
            print(f'\t\t\teffect backward_oppo: {oppo.name} recoils {effect_number} cell(s)')
            current_game = process_advancing(effect_number, oppo, current_game)   # negative value (e.g. -1), clamped at position 0
        return current_game

    # --- card effects: draw / discard (own or opponent) ---
    if effect == 'draw':
        n = abs(effect_number)   # positive value (1-2)
        drawn = _draw_cards(player, n)
        print(f'\t\t\teffect draw: {player.name} draws {drawn}/{n} card(s), hand now {len(player.hand)}')
        return current_game

    if effect == 'draw_oppo':   # fatigue: opponent gets extra cards to manage
        oppo = _get_oppo(player, current_game)
        if oppo is not None:
            n = abs(effect_number)   # positive value (1-2)
            drawn = _draw_cards(oppo, n)
            print(f'\t\t\teffect draw_oppo: {oppo.name} draws {drawn}/{n} card(s), hand now {len(oppo.hand)}')
        return current_game

    if effect == 'discard':   # discard N cards from own hand
        n = min(abs(effect_number), len(player.hand))   # negative value (-1 to -2) -> number of cards
        if n > 0:
            # discard selection: the player CHOOSES which N cards to discard. The trip
            # chain PAUSES at the next step boundary (see process_trip_chain); the
            # choice arrives as a to:'discard_pile' message (handle_websocket_message)
            # and the chain resumes.
            current_game.pending_discard = {'player': player.name, 'n': n}
            if log_entry is not None:
                log_entry['_pending_discard'] = n
            print(f'\t\t\teffect discard: PAUSED - {player.name} must choose {n} card(s) to discard')
            return current_game
        # empty hand: nothing to discard
        print(f'\t\t\teffect discard: {player.name} discards 0 card(s) (empty hand)')
        return current_game

    if effect == 'discard_oppo':   # opponent discards N cards
        oppo = _get_oppo(player, current_game)
        if oppo is not None and len(oppo.hand) > 0:
            n = min(abs(effect_number), len(oppo.hand))   # -1 -> 1 card
            if n > 0:
                # discard selection: the OPPONENT chooses which card(s) to discard;
                # the chain pauses until their choice arrives (to: 'discard_pile') -
                # see process_trip_chain.
                current_game.pending_discard = {'player': oppo.name, 'n': n}
                if log_entry is not None:
                    log_entry['_pending_discard'] = n
                print(f'\t\t\teffect discard_oppo: PAUSED - {oppo.name} must choose {n} card(s) to discard')
                return current_game
        return current_game

    # --- resource effects (deck -> mana zone, mana zone -> discard) ---
    if effect == 'ramp':   # N cards from own deck to own mana zone
        n = abs(effect_number)   # positive value (1-2)
        moved = _ramp_mana(player, n)
        print(f'\t\t\teffect ramp: {player.name} moves {moved}/{n} card(s) from deck to mana, mana now {len(player.mana)}')
        return current_game

    if effect == 'ramp_oppo':   # 1 card from opponent's deck to opponent's mana zone
        oppo = _get_oppo(player, current_game)
        if oppo is not None:
            n = abs(effect_number)   # 1
            moved = _ramp_mana(oppo, n)
            print(f'\t\t\teffect ramp_oppo: {oppo.name} moves {moved}/{n} card(s) from deck to mana, mana now {len(oppo.mana)}')
        return current_game

    if effect == 'taxation':   # N cards from own mana zone to discard pile
        n = abs(effect_number)   # positive value (1-2)
        taxed = _tax_mana(player, n)
        print(f'\t\t\teffect taxation: {player.name} taxes {taxed}/{n} card(s) from mana to discard, mana now {len(player.mana)}')
        return current_game

    if effect == 'taxation_oppo':   # 1 card from opponent's mana zone to their discard pile
        oppo = _get_oppo(player, current_game)
        if oppo is not None:
            n = abs(effect_number)   # 1
            taxed = _tax_mana(oppo, n)
            print(f'\t\t\teffect taxation_oppo: {oppo.name} taxes {taxed}/{n} card(s) from mana to discard, mana now {len(oppo.mana)}')
        return current_game

    if effect == 'jump':   # jump directly to the destination cell (skipping intermediate cells)
        # the faction biome bonus applies to the jump distance as well
        jump_distance = basic_advancing
        if jump_distance > 0 and _on_home_biome(player, current_game):
            print(f'\t\t\tfaction biome bonus: +1 jump distance for {player.name} (token on home biome)')
            jump_distance += 1
            if log_entry is not None and log_entry.get('player') == player.name:
                log_entry['notes'].append('faction biome bonus +1')
        print(f'\t\t\teffect jump: {player.name} jumps {jump_distance} cell(s), skipping intermediate cells')
        return _jump(player, jump_distance, current_game, log_entry)

    # --- board effect: avalanche ---
    if effect == 'avalanche':
        # ALL player tokens on the Mountain (MO) biome are knocked back to the FIRST
        # cell of that biome (both players, the playing player included). The playing
        # player still applies its basic advancing afterwards (from the new position
        # if it was on MO). effect_number is 0 in the pool (unused).
        if log_entry is not None:
            log_entry['notes'].append('🏔 avalanche — MO strikes')
        current_game = _knockback_biome('MO', current_game, 'avalanche: MO strikes', log_entry)
        return current_game

    # --- grappling_hook: the card's own advancing is
    #     applied by _resolve_card as usual; the "copy of the facing card's advancing"
    #     is applied by the trip chain (apply_grappling_copy) after the facing card
    #     has resolved - so this branch is intentionally a no-op here. ---
    if effect == 'grappling_hook':
        return current_game

    # --- pet_trap: INSTANT effect - it ALREADY fired
    #     at play time (_apply_instant_effects placed the drop token on the
    #     player's cell). At resolution the card only advances by its basic
    #     value, so this branch is a no-op marker (like grappling_hook). ---
    if effect == 'pet_trap':
        return current_game

    # --- rooted: the token was ALREADY granted by
    #     _resolve_card (the card survives the cleaning phase and stays on a free
    #     stopover). At resolution the card only advances by its basic value, so
    #     this branch is a no-op marker (like grappling_hook / pet_trap). ---
    if effect == 'rooted':
        return current_game

    # --- wrecking_ball: INSTANT effect - it ALREADY
    #     fired at play time (_apply_instant_effects removed the opponent's
    #     dwelling card, if any). At resolution the card only advances by its
    #     basic value, so this branch is a no-op marker (like pet_trap). ---
    if effect == 'wrecking_ball':
        return current_game

    # --- board token trigger: a DROP token (placed by pet_trap) on this cell.
    #     Fired by process_advancing / _jump when a player's token ARRIVES on the
    #     cell: ALL tokens on the cell are consumed and the player is knocked
    #     back -1 per token (a direct recoil, clamped at cell 0). The recoil is
    #     NOT stepped through the cells, so it cannot re-trigger other drops
    #     (no chains / ping-pong). The caller appends the player's name to the
    #     final cell after the check (the name is not on the cell yet here). ---
    if effect == 'drop':
        cell = player.current_position or 0
        n = (current_game.drop_tokens or {}).get(cell, 0)
        if n:
            del current_game.drop_tokens[cell]
            print(f'\t\t\tdrop token(s) on cell {cell}: {player.name} knocked back -{n}')
            if log_entry is not None and log_entry.get('player') == player.name:
                log_entry['notes'].append(f'🪤 drop on cell {cell} — knocked back -{n}')
            player.current_position = max(0, cell - n)   # direct recoil (no per-step checks -> no re-trigger)
        return current_game

    # --- swap_cards: INSTANT effect - it ALREADY fired at
    #     play time (_apply_instant_effects swapped the card's trip-chain position
    #     with the entry chosen by the player). At resolution the card only applies
    #     its basic advancing, so this branch is a no-op marker (like pet_trap / wrecking_ball). ---
    if effect == 'swap_cards':
        return current_game

    # --- other effects not implemented yet ---
    return current_game

def _jump(player, n, current_game, log_entry=None):
    """ jump directly to the destination cell: intermediate cells are skipped entirely
     (no per-step trap/drop checks), only the landing cell is checked.
     win condition still applies on arrival. jump cards always move forward (advancing >= 2) """
    if n == 0 or current_game.state == "game over":
        return current_game

    new_position = player.current_position + n

    current_game.earth[player.current_position].remove(player.name)         # remove old player position in earth

    # check win condition - if the landing cell is at/past the end of the earth
    if new_position >= win_position:
        current_game.winner = player.name
        current_game.state = "game over"
        if log_entry is not None:
            log_entry['notes'].append(f'🏆 {player.name} reached cell 24 — win!')
        current_game.earth[0].append(player.name)                           # put it back to start showing crossing finish line
        return current_game

    player.current_position = new_position

    # the player's token must sit exactly on its landing cell BEFORE any drop can
    # fire (a trampoline / boost recurses into _jump / process_advancing and reads
    # the earth from the player's position)
    for cell_tokens in current_game.earth:
        while player.name in cell_tokens:
            cell_tokens.remove(player.name)
    current_game.earth[player.current_position].append(player.name)

    # check if landing on a trap or drop (intermediate cells were skipped, only the destination matters)
    cell = get_player_cell(player, current_game)
    if 'trap' in cell:
        current_game = apply_effect('trap', 0, 0, player, current_game, log_entry)
    if _cell_has_drop(current_game, player.current_position):
        current_game = apply_effect('drop', 0, 0, player, current_game, log_entry)
    # engineers' drops: fire on ARRIVAL at the landing cell
    # (boost +2 / trampoline +2 jump / gluetrap -1 / landmine blocks the player)
    current_game = _trigger_board_drops(current_game, player, log_entry)

    # a drop may have moved the player (the inner movement already synced the
    # earth) - (re)place the name once, on the final cell
    for cell_tokens in current_game.earth:
        while player.name in cell_tokens:
            cell_tokens.remove(player.name)
    current_game.earth[player.current_position].append(player.name)
    return current_game

def process_advancing(advancing_value, player, current_game, allow_bonus=True, log_entry=None):
    """ process advancing of a player on the earth, checking for traps and drops
     allow_bonus: apply the faction biome +1 for forward movement (default True).
     The grappling_hook copy (apply_grappling_copy) passes allow_bonus=False so it is
     a pure copy of the facing card's movement, not boosted by the player's own bonus.
     log_entry: the log entry of the player who played the card (receives the
     biome-bonus note only when it is the entry owner who moves, and the win note). """
    if advancing_value == 0:
        return current_game

    # Defensive: ensure the player's token is in the earth array at their current
    # position. A previous effect (cataclysm knockback, win-condition reset, etc.)
    # may have left the player in earth[0] while current_position points elsewhere.
    if player.name not in current_game.earth[player.current_position]:
        for _ct in current_game.earth:
            while player.name in _ct:
                _ct.remove(player.name)
        current_game.earth[player.current_position].append(player.name)

    # faction biome bonus: standing on one of the two biomes of your own faction
    # grants +1 to forward movement (recoil / backward movement is not boosted)
    if allow_bonus and advancing_value > 0 and _on_home_biome(player, current_game):
        print(f'\t\t\tfaction biome bonus: +1 advancing for {player.name} (token on home biome)')
        if log_entry is not None and log_entry.get('player') == player.name:
            log_entry['notes'].append('faction biome bonus +1')
        advancing_value += 1

    # +1 to move forward, -1 to move backward (single loop handles both directions)
    step = 1 if advancing_value > 0 else -1

    # landmine: if the player is ALREADY blocked (a previous
    # card / drop), this movement is not stopped by the stale flag - only a
    # landmine that fires DURING this movement cancels its remaining steps
    was_blocked = bool(player.landmine_blocked)

    for _ in range(abs(advancing_value)):
        new_position = player.current_position + step

        # boundary check: cannot go below position 0
        if new_position < 0:
            break

        current_game.earth[player.current_position].remove(player.name)         # remove old player position in earth

        # check win condition - if player is at the end of the earth
        if step > 0 and new_position >= win_position:
            # player has reached the end of the earth, set game state to "game over" and declare winner
            current_game.winner = player.name
            current_game.state = "game over"
            if log_entry is not None:
                log_entry['notes'].append(f'🏆 {player.name} reached cell 24 — win!')
            current_game.earth[0].append(player.name)                           # put it back to start showing crossing finish line
            break

        player.current_position = new_position

        # the player's token must sit exactly on its current cell BEFORE any drop
        # can fire (a boost / trampoline recurses into process_advancing / _jump
        # and reads the earth from the player's position)
        for cell_tokens in current_game.earth:
            while player.name in cell_tokens:
                cell_tokens.remove(player.name)
        current_game.earth[player.current_position].append(player.name)

        # check if stepping on a trap or drop (if yes apply effect)
        cell = get_player_cell(player, current_game)
        if 'trap' in cell:
            # apply trap effect
            current_game = apply_effect('trap', 0, 0, player, current_game)
        if _cell_has_drop(current_game, player.current_position):
            # apply drop effect (pet_trap: consume the token(s), knockback -1 each)
            current_game = apply_effect('drop', 0, 0, player, current_game, log_entry)
        # engineers' drops: boost / trampoline / gluetrap /
        # landmine on this cell fire on ARRIVAL (may move the player further)
        current_game = _trigger_board_drops(current_game, player, log_entry)

        # a drop (boost / trampoline / gluetrap / pet_trap recoil) may have moved
        # the player - the inner movements already synced the earth, so (re)place
        # the name once, on the final cell
        for cell_tokens in current_game.earth:
            while player.name in cell_tokens:
                cell_tokens.remove(player.name)
        current_game.earth[player.current_position].append(player.name)

        # a landmine fired on this step: the player is blocked for the rest of the
        # turn - the remaining steps of THIS movement are canceled (it "avoids the
        # player to advance"), like a win would stop the loop
        if current_game.state == "game over" or (player.landmine_blocked and not was_blocked):
            break

    return current_game

def get_player_cell(player, current_game):
    return current_game.earth[player.current_position]