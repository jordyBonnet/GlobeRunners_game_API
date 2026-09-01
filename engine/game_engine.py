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
DB_PATH = os.path.join(os.path.dirname(__file__), '../games/games.db')

start_cards_in_hand = 6
start_cards_in_mana = 3
turn_n_draw_cards = 3
n_cells_by_biome = 6
BIOMES = ['OC', 'MO', 'DE', 'JU']
win_position = 24

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
    
    game = GameState(
        id=game_id,
        players={player['name']: player},
    )

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

    # 1. Add the new player to the game
    current_game.players[player['name']] = PlayerState.model_validate(player)
    
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

    # 4.5 Cataclysm pile: one card per biome (4 cards), shuffled at board init.
    #     Top = first element. Each cataclysm trigger takes the top card, strikes
    #     its biome and puts it at the BOTTOM of the pile (see trigger_cataclysm).
    current_game.cataclysm_pile = random.sample(BIOMES, len(BIOMES))
    print(f'Cataclysm pile initialized: {current_game.cataclysm_pile}')

    # 5. Roll planet temperature (2x d20, keep value closest to 10); day/night always starts on "day" and flips each turn
    current_game.temperature = roll_temperature()
    current_game.day_night = 'day'
    print(f'Planet initialized: temperature = {current_game.temperature}, {current_game.day_night}')

    # 6. Change game state
    current_game.state = f"waiting for both players to put {start_cards_in_mana} cards in hand"

    # 7. Mark the rules version this game is played with (the replay/analysis
    #    tooling uses it to pin older games to the rules they were actually played under)
    #    9 = + drop_on_board condition (met if any drop or trap is on the earth)
    #    8 = + pet_trap effect (INSTANT play-time effect: drop token on the player's cell,
    #        triggered when any token arrives on the cell, knockback -1 per token)
    #    7 = + copy_effect effect (copy the facing card's effect, applied with the copier as actor)
    #    6 = + effect_canceled effect (cancel the facing card's effect)
    #    5 = + grappling_hook effect (copy the facing card's total advancement)
    #    4 = + avalanche effect (knockback of all tokens on the MO biome)
    current_game.engine_version = 9

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
    
    success = True
    message = f"Action successful for {player.name}"

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

            # if a card was blocked in defend mode during resolution, propagate the
            # info to the client (otherwise the local "action successful" message overwrites it)
            if isinstance(current_game.message, dict) and "blocked" in current_game.message.get("message", "").lower():
                message = current_game.message["message"]

            if current_game.state == "game over":
                success = True
                message = f"Game over! Winner: {current_game.winner}"
            elif _check_deadlock(current_game):
                # all cards stuck in mana zones -> nobody can move anymore, end the game
                if current_game.winner is not None:
                    message = f"Deadlock - no more playable cards for both players. Winner (furthest ahead): {current_game.winner}"
                else:
                    message = "Deadlock - no more playable cards for both players. Draw!"
            else:
                # ... then prepare for next turn    
                current_game.turn_order = current_game.turn_order[::-1]     # change turn order
                current_game.first_player_passed = False                    # reset first player passed
                current_game.second_player_passed = False                   # reset second player passed
                current_game.turn += 1                                      # increment turn
                current_game.day_night = 'night' if current_game.day_night == 'day' else 'day'   # flip day/night each new turn
                for p in current_game.players.values():                     # reset necessary players state
                    p.mana_spend = 0
                    p.action_chain = []
                    
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
    
    # check mana available
    mana_available = len(player.mana) - player.mana_spend
    cards_id = player.message['cards']
    cards_rows = CARDS_DB.filter(pl.col('card_id').is_in(cards_id))
    if cards_rows['mana'].sum() <= mana_available:
        for card_id in cards_id:
            player.hand.remove(card_id)
        player.action_chain.append(player.message) # Append to Trip chain (it is a list of dict of cards)
        if first_second == 'first':
            current_game.state = f"turn {current_game.turn} - waiting for second player ({current_game.turn_order[1]}) to play"
            if current_game.second_player_passed:       # if second player already passed
                current_game.state = f"turn {current_game.turn} - waiting for first player ({current_game.turn_order[0]}) to play"
        elif first_second == 'second':
            current_game.state = f"turn {current_game.turn} - waiting for first player ({current_game.turn_order[0]}) to play"
            if current_game.first_player_passed:       # if second player already passed
                current_game.state = f"turn {current_game.turn} - waiting for second player ({current_game.turn_order[1]}) to play"
        player.mana_spend += cards_rows['mana'].sum()       # update mana spend
        message = f"Player {player.name} played {cards_id} successfully"
    else:
        success = False
        message = f"Player {player.name} tried to play {cards_id} but not enough mana available (available: {mana_available}, required: {cards_rows['mana'].sum()})"

    # --- INSTANT effects (rule of engine_version 8) -----------------------------------
    # pet_trap (and, later, swap_cards / wrecking_ball) fire the moment the card is
    # PLAYED - not at trip-chain resolution - so they cannot be blocked, canceled or
    # condition-gated: the effect is already on the board before the chain starts.
    # pet_trap leaves a drop token on the player's current cell; it triggers when
    # ANY player's token later ARRIVES on that cell (knockback -1 per token).
    if success and (current_game.engine_version or 0) >= 8:
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
            # only record actions that actually happened (a rejected play, e.g.
            # "not enough mana", must not be stored: it would show up as a real
            # play in the analysis app / history)
            if success:
                p.messages_history.append(player.message)

    return player, current_game, success, message

def _apply_instant_effects(player, current_game, msg):
    """ INSTANT effects: fire at PLAY TIME (right after the card is accepted),
     before the trip chain resolves - they cannot be blocked or canceled.
     pet_trap (engine_version 8): leaves ONE drop token on the player's current
     cell (tokens stack on the same cell). Move mode only: a defend play plays
     no effect, so it places no token.
     Returns the list of cells that received a drop token ([] if none). """
    if not msg or (msg.get('mode') or '') != 'move':
        return []
    placed = []
    for card_id in (msg.get('cards') or [])[:1]:
        rows = CARDS_DB.filter(pl.col('card_id') == card_id)
        if rows.is_empty():
            continue
        r = rows.row(0, named=True)
        if r['effect'] == 'pet_trap':
            cell = player.current_position or 0
            current_game.drop_tokens[cell] = current_game.drop_tokens.get(cell, 0) + 1
            placed.append(cell)
            print(f'\t\t\tINSTANT pet_trap: drop token placed on cell {cell} (total there: {current_game.drop_tokens[cell]})')
    return placed

def _cell_has_drop(current_game, cell_index):
    """ pet_trap (engine_version 8): True if the cell carries at least one drop
     token (placed at play time by a pet_trap card). Empty for older games
     (drop_tokens defaults to {}), so this is a no-op for them. """
    return (current_game.drop_tokens or {}).get(cell_index, 0) > 0

def grappling_copy_amount(game, facing_adv):
    """ grappling_hook: the forward movement a player copies from its facing card's
    total advancement (the net cells that facing card moved). 0 when the rule is not
    active (engine_version < 5) or when the facing card did not advance forward
    (a grappling hook pulls forward, it never copies a recoil / net backward move). """
    if (game.engine_version or 0) < 5:
        return 0
    return max(0, int(facing_adv or 0))


def apply_grappling_copy(game, player, amount, log_entry=None):
    """ grappling_hook: apply the copied forward advance (the facing card's total
    advancement). It is a pure copy of the facing card's movement, so it is applied
    WITHOUT the player's own faction biome bonus (allow_bonus=False) - but it still
    goes through process_advancing so the win condition and trap/drop checks apply. """
    if amount > 0:
        print(f'\t\t\tgrappling_hook: {player.name} copies the facing card advancement (+{amount})')
        if log_entry is not None:
            log_entry['notes'].append(f'grappling hook — copied +{amount} from the facing card')
        return process_advancing(amount, player, game, allow_bonus=False, log_entry=log_entry)
    return game

def apply_copy_effect(game, copier, copier_action, facing_player, facing_action, log_entry=None):
    """ copy_effect (rule of engine_version 7): the copier copies the effect of its
    FACING card (the opponent card at the same trip-chain index / stopover), applied
    with the COPIER as the actor (so _oppo effects target the copier's opponent)
    and with the FACING card's data (effect_number + basic advancing).

    The trip chain calls it only when BOTH facing effects fired (condition met, not
    blocked, not effect_canceled). Guards kept here too:
    - rule not active (engine_version < 7) -> no-op (replay pinning of older games),
    - the copier's card is not a copy_effect card -> no-op,
    - the facing card is a copy_effect itself -> no recursion, nothing to copy.
    Effects with no behavior in apply_effect (unstoppable, effect_canceled,
    grappling_hook, ...) are harmless no-ops when copied. """
    if (game.engine_version or 0) < 7:
        return game
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
        log_entry['notes'].append(f'copy_effect — copied "{facing["effect"]}" from the facing card ({facing["name"]})')
    return apply_effect(facing['effect'], facing['effect_number'], int(facing['advancing']), copier, game, log_entry)


# ------------------------------------------------------------------ turn log
# Public per-turn recap of the resolution, persisted in GameState.log so the UI
# can show a collapsible history: turn -> stopover -> each player's line
# (card, condition met, effect, negative effects, notes, positions).
# Only PUBLIC information is recorded: played cards are public (they were on the
# stopovers), positions are public, condition met / effect / block / cancel are
# part of the public resolution. No hand/mana/deck content ever goes in here.

def new_log_entry(player, action, order):
    """ create an empty per-player log entry for one action of the trip chain.
     Filled during resolution: condition_met / effect / shield / negatives / notes / pos_after """
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
    # pet_trap (engine_version 8): the drop token was placed at PLAY TIME (before
    # the trip chain started) - the engine annotated the exact cell(s) on the action
    for cell in ((action or {}).get('drop_placed_on') or []):
        entry['notes'].append(f'🪤 pet_trap — drop token placed on cell {cell} (fires when any token arrives)')
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

def process_trip_chain(current_game):
    """ process the action chain of the game, effect first then advancing.
     grappling_hook copies: after BOTH facing cards at an index have resolved, each
     validated grappling card advances by the total advancement of the facing card
     (see apply_grappling_copy / grappling_copy_amount).
     copy_effect copies: after BOTH facing cards at an index have resolved, each
     validated copy_effect card applies the effect of the facing card with itself as
     the actor (see apply_copy_effect) - only when the facing effect fired.

     Also records the public turn log in GameState.log (turn -> stopover -> entries).
     """
    print('Processing trip chain...')
    # Select first and 2nd player in the turn order
    first_player = current_game.players[current_game.turn_order[0]]
    second_player = current_game.players[current_game.turn_order[1]]
    first_p_over = False
    second_p_over = False

    def flush_unresolved(first_resolved, second_resolved):
        """ The game just ended mid-chain (a win). The PLAYED cards of the UNRESOLVED
            actions have already left their owner's hand (player_play removes them at
            play time) but were never moved to the discard pile (process_card does
            that when the action resolves) -> without this flush they are lost from
            every zone and the final state no longer conserves the deck. Flush them
            to their owners' discard piles (each action once, in play order). """
        for p, done in ((first_player, first_resolved), (second_player, second_resolved)):
            chain = p.action_chain or []
            for a in chain[(i + 1) if done else i:]:
                cards = (a or {}).get('cards') or []
                if not cards:
                    continue
                if p.discard is None:
                    p.discard = []
                p.discard.extend(cards)
                print(f'\t\t\tgame over mid-chain: flushed {len(cards)} unresolved card(s) of {p.name} to discard')

    # turn log: appended up-front so a mid-chain win ("game over" early return)
    # still keeps the partial turn in the log
    log_list = current_game.log
    if not isinstance(log_list, list):
        current_game.log = log_list = []
    turn_log = {'turn': current_game.turn, 'stopovers': []}
    log_list.append(turn_log)

    over = False
    i = 0
    while not over:
        print(f'\tProcessing action index: {i}')
        # both entries are created BEFORE resolution so cross-events (a block fired
        # on the opponent's card, a grappling copy, ...) can be attached to either line
        entry_f = new_log_entry(first_player, first_player.action_chain[i], 1) if i < len(first_player.action_chain) else None
        entry_s = new_log_entry(second_player, second_player.action_chain[i], 2) if i < len(second_player.action_chain) else None
        sv = {
            'stopover': (entry_f or entry_s or {}).get('to') or f'stopover_{max(0, 4 - i)}',
            'entries': [],
        }
        # Process action for first player at index i
        first_grapple = False
        first_adv = 0
        first_effect_ok = False
        if i < len(first_player.action_chain):
            pos_before = first_player.current_position
            current_game, first_grapple, first_effect_ok = process_card(first_player.action_chain[i], first_player, current_game,
                                                       log_entry=entry_f, oppo_entry=entry_s)
            first_adv = (first_player.current_position or 0) - pos_before
        else:
            first_p_over = True

        if current_game.state == "game over":
            flush_unresolved(True, False)
            _log_stopover(sv, turn_log, [(entry_f, first_player)])
            return current_game

        # Process action for second player at index i
        second_grapple = False
        second_adv = 0
        second_effect_ok = False
        if i < len(second_player.action_chain):
            pos_before = second_player.current_position
            current_game, second_grapple, second_effect_ok = process_card(second_player.action_chain[i], second_player, current_game,
                                                        log_entry=entry_s, oppo_entry=entry_f)
            second_adv = (second_player.current_position or 0) - pos_before
        else:
            second_p_over = True

        if current_game.state == "game over":
            flush_unresolved(True, True)
            _log_stopover(sv, turn_log, [(entry_f, first_player), (entry_s, second_player)])
            return current_game

        # grappling_hook copies: each validated grappling card copies the total
        # advancement of its facing card (the opponent card at this same index).
        # Applied AFTER both cards have resolved so the facing advancement is known.
        if first_grapple:
            current_game = apply_grappling_copy(current_game, first_player,
                                                grappling_copy_amount(current_game, second_adv), entry_f)
        if current_game.state == "game over":
            flush_unresolved(True, True)
            _log_stopover(sv, turn_log, [(entry_f, first_player), (entry_s, second_player)])
            return current_game
        if second_grapple:
            current_game = apply_grappling_copy(current_game, second_player,
                                                grappling_copy_amount(current_game, first_adv), entry_s)
        if current_game.state == "game over":
            flush_unresolved(True, True)
            _log_stopover(sv, turn_log, [(entry_f, first_player), (entry_s, second_player)])
            return current_game

        # copy_effect copies (rule of engine_version 7): a validated copy_effect card
        # copies the effect of its FACING card (the opponent card at this same index /
        # stopover), applied with the copier as the actor. It only happens when the
        # facing card's effect actually fired (condition met, not blocked, not
        # effect_canceled) and the facing card is not a copy_effect itself (no
        # recursion) - enforced by apply_copy_effect.
        if first_effect_ok and second_effect_ok:
            current_game = apply_copy_effect(current_game, first_player, first_player.action_chain[i],
                                             second_player, second_player.action_chain[i], entry_f)
        if current_game.state == "game over":
            flush_unresolved(True, True)
            _log_stopover(sv, turn_log, [(entry_f, first_player), (entry_s, second_player)])
            return current_game
        if second_effect_ok and first_effect_ok:
            current_game = apply_copy_effect(current_game, second_player, second_player.action_chain[i],
                                             first_player, first_player.action_chain[i], entry_s)
        if current_game.state == "game over":
            flush_unresolved(True, True)
            _log_stopover(sv, turn_log, [(entry_f, first_player), (entry_s, second_player)])
            return current_game

        # finalize the stopover entry (positions are read AFTER the grappling copies)
        _log_stopover(sv, turn_log, [(entry_f, first_player), (entry_s, second_player)])

        # Check if both players are over
        if first_p_over and second_p_over:
            # If both players are over, we can end the action chain processing
            over = True

        i += 1

    # both players passed with no card this turn: nothing resolved -> drop the empty entry
    if not turn_log['stopovers']:
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
            continue
        row = rows.row(0, named=True)
        if log_entry is not None:
            log_entry['effect'] = row['effect']

        # BLOCK check: is the opponent playing defend card(s) on this same stopover?
        oppo = _get_oppo(player, current_game)
        if oppo is not None and _oppo_defend_actions(oppo, stopover):
            # exception 1: an "unstoppable" card whose condition is met is not affected
            if row['effect'] == 'unstoppable' and is_condition_met(row['condition'], player, current_game):
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

        current_game, grappling_activated, effect_activated = _resolve_card(row, player, current_game, stopover, log_entry)

    print(f'\t\t\tcurrent position: {player.current_position} (len(earth): {len(current_game.earth)})')

    return current_game, grappling_activated, effect_activated

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
    """ effect_canceled (rule of engine_version 6): True if the opponent has a card
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
     A game without a pile (pre-cataclysm rules) is a safe no-op. """
    pile = current_game.cataclysm_pile or []
    if not pile:
        return current_game

    biome = pile.pop(0)
    pile.append(biome)   # drawn card goes to the bottom of the pile

    if log_entry is not None:
        log_entry['notes'].append(f'⚡ cataclysm — {biome} strikes')
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


def _resolve_card(row, player, current_game, stopover=None, log_entry=None):
    """ resolve ONE card of the trip chain: condition -> effect -> advancing
     (movement effects handle their own advancing inside apply_effect).
     stopover: the stopover this card was played on (e.g. 'stopover_4') - needed for
     the effect_canceled check (opponent cancel card on the SAME stopover).
     log_entry: the player's log entry (filled with condition_met / negatives / notes).
     Returns (current_game, grappling_activated) - grappling_activated is True when
     this is a grappling_hook card whose condition was met (rule active); the trip
     chain then applies the copy of the facing card's advancing after that card
     has resolved. """
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

    # effect_canceled (rule of engine_version 6): before applying this card's
    # effect, check if the opponent has a VALID effect_canceled card (move mode,
    # condition met) on the SAME stopover - if so, this card's effect is
    # CANCELED: it does NOT fire (including any movement it would have caused),
    # but the card still advances by its basic value.
    effect_cancelled = False
    if condition_met and (current_game.engine_version or 0) >= 6 and stopover:
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
    grappling_activated = (effect == 'grappling_hook' and condition_met
                           and (current_game.engine_version or 0) >= 5)

    if condition_met:
        if effect_cancelled:
            # the effect is canceled: only the basic advancing is applied
            current_game = process_advancing(basic_advancing, player, current_game, log_entry=log_entry)
        else:
            print(f'\t\t\tapplying effect: {effect}')
            current_game = apply_effect(effect, row['effect_number'], basic_advancing, player, current_game, log_entry)

            # Apply basic advancing (movement effects already moved the player inside apply_effect)
            if effect not in ('advancing', 'backward', 'jump'):
                current_game = process_advancing(basic_advancing, player, current_game, log_entry=log_entry)
    else:
        # condition not met: reduced advancing (card mana - 1)
        basic_advancing = int(row['mana']) - 1
        print(f'\t\t\tcondition not met, reduced advancing: {basic_advancing}')
        current_game = process_advancing(basic_advancing, player, current_game, log_entry=log_entry)

    return current_game, grappling_activated, effect_activated

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
    # marker handled in process_card (a block-condition card only triggers its effect
    # when played in defend mode)
    if condition == 'block':
        return True

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

    # drop_on_board (rule of engine_version 9): met if ANY drop or trap is on the
    # earth - a drop token (placed by a pet_trap card, in drop_tokens) or a
    # 'trap'/'drop' cell content. Old games (< 9) keep the canonical default:
    # an unimplemented condition is treated as met.
    if condition == 'drop_on_board':
        if (current_game.engine_version or 0) < 9:
            return True
        if any(n > 0 for n in (current_game.drop_tokens or {}).values()):
            return True
        for cell in current_game.earth or []:
            if cell and ('trap' in cell or 'drop' in cell):
                return True
        return False

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

    if effect == 'discard':   # discard N cards from own hand (auto: last cards of the hand)
        n = min(abs(effect_number), len(player.hand))   # negative value (-1 to -2) -> number of cards
        discarded = player.hand[-n:] if n else []
        for card in discarded:
            player.hand.remove(card)
        if player.discard is None:
            player.discard = []
        player.discard.extend(discarded)
        print(f'\t\t\teffect discard: {player.name} discards {len(discarded)} card(s), hand now {len(player.hand)}')
        return current_game

    if effect == 'discard_oppo':   # opponent discards 1 card (auto: last card of their hand)
        oppo = _get_oppo(player, current_game)
        if oppo is not None and len(oppo.hand) > 0:
            n = min(abs(effect_number), len(oppo.hand))   # -1 -> 1 card
            discarded = oppo.hand[-n:]
            for card in discarded:
                oppo.hand.remove(card)
            if oppo.discard is None:
                oppo.discard = []
            oppo.discard.extend(discarded)
            print(f'\t\t\teffect discard_oppo: {oppo.name} discards {len(discarded)} card(s), hand now {len(oppo.hand)}')
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
                log_entry['notes'].append('faction biome bonus +1 (token on home biome)')
        print(f'\t\t\teffect jump: {player.name} jumps {jump_distance} cell(s), skipping intermediate cells')
        return _jump(player, jump_distance, current_game, log_entry)

    # --- board effect: avalanche (rule of engine_version 4) ---
    if effect == 'avalanche':
        # ALL player tokens on the Mountain (MO) biome are knocked back to the FIRST
        # cell of that biome (both players, the playing player included). The playing
        # player still applies its basic advancing afterwards (from the new position
        # if it was on MO). effect_number is 0 in the pool (unused).
        # Games with engine_version < 4 keep the old no-op behavior (replay pinning).
        if (current_game.engine_version or 0) >= 4:
            if log_entry is not None:
                log_entry['notes'].append('🏔 avalanche — MO strikes')
            current_game = _knockback_biome('MO', current_game, 'avalanche: MO strikes', log_entry)
        else:
            print(f'\t\t\teffect avalanche: no-op (engine_version < 4)')
        return current_game

    # --- grappling_hook (rule of engine_version 5): the card's own advancing is
    #     applied by _resolve_card as usual; the "copy of the facing card's advancing"
    #     is applied by the trip chain (apply_grappling_copy) after the facing card
    #     has resolved - so this branch is intentionally a no-op here. ---
    if effect == 'grappling_hook':
        return current_game

    # --- pet_trap (rule of engine_version 8): INSTANT effect - it ALREADY fired
    #     at play time (_apply_instant_effects placed the drop token on the
    #     player's cell). At resolution the card only advances by its basic
    #     value, so this branch is a no-op marker (like grappling_hook). ---
    if effect == 'pet_trap':
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

    # --- other effects not implemented yet (wrecking_ball, swap_cards, rooted, ...) ---
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

    # check if landing on a trap or drop (intermediate cells were skipped, only the destination matters)
    cell = get_player_cell(player, current_game)
    if 'trap' in cell:
        current_game = apply_effect('trap', 0, 0, player, current_game, log_entry)
    if _cell_has_drop(current_game, player.current_position):
        current_game = apply_effect('drop', 0, 0, player, current_game, log_entry)

    current_game.earth[player.current_position].append(player.name)     # update new player position in earth
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

    # faction biome bonus: standing on one of the two biomes of your own faction
    # grants +1 to forward movement (recoil / backward movement is not boosted)
    if allow_bonus and advancing_value > 0 and _on_home_biome(player, current_game):
        print(f'\t\t\tfaction biome bonus: +1 advancing for {player.name} (token on home biome)')
        if log_entry is not None and log_entry.get('player') == player.name:
            log_entry['notes'].append('faction biome bonus +1 (token on home biome)')
        advancing_value += 1

    # +1 to move forward, -1 to move backward (single loop handles both directions)
    step = 1 if advancing_value > 0 else -1

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

        # check if stepping on a trap or drop (if yes apply effect)
        cell = get_player_cell(player, current_game)
        if 'trap' in cell:
            # apply trap effect
            current_game = apply_effect('trap', 0, 0, player, current_game)
        if _cell_has_drop(current_game, player.current_position):
            # apply drop effect (pet_trap: consume the token(s), knockback -1 each)
            current_game = apply_effect('drop', 0, 0, player, current_game, log_entry)

        current_game.earth[player.current_position].append(player.name)     # update new player position in earth

    return current_game

def get_player_cell(player, current_game):
    return current_game.earth[player.current_position]