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

    # 5. Roll planet temperature (2x d20, keep value closest to 10); day/night always starts on "day" and flips each turn
    current_game.temperature = roll_temperature()
    current_game.day_night = 'day'
    print(f'Planet initialized: temperature = {current_game.temperature}, {current_game.day_night}')

    # 6. Change game state
    current_game.state = f"waiting for both players to put {start_cards_in_mana} cards in hand"

    # Update the game state in the database
    c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (current_game.to_json(), game_id))
    conn.commit()
    conn.close()
    # current_game.to_json()
    return current_game_json(player['name'], current_game)

def current_game_json(player_name: str, current_game: GameState):
    """ return the current game to player, but hides the hand, mana and deck of the opponent """
    for p in current_game.players.values():
        if p.name != player_name:
            p.hand = []
            p.mana = []
            p.deck = []
    return current_game.to_json()

def get_game(game_id=None):
    """ get all informations about a game returns it as a json """
    _, _, current_game = get_current_game(game_id)

    return current_game.to_json()

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
    
            if current_game.state == "game over":
                success = True
                message = f"Game over! Winner: {current_game.winner}"
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

def get_cardpool() -> pl.DataFrame:
    return CARDS_DB

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
        if len(message['cards']) > 1:
            return False, "When mode is 'move', 'cards' list must contain at most 1 card"
        if not message['to'].startswith('stopover_'):
            return False, "When mode is 'move', 'to' must be in the format 'stopover_x' (where x is an integer)"
    

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

    # ToDo: manage instant actions here (support faction, pendings, drops, defense cards, etc.)

    for p in current_game.players.values():
        if p.name == player.name:
            p.hand = player.hand
            p.mana_spend = player.mana_spend
            p.action_chain = player.action_chain
            p.messages_history.append(player.message)

    return player, current_game, success, message

def process_trip_chain(current_game):
    """ process the action chain of the game, effect first then advancing """
    print('Processing trip chain...')
    # Select first and 2nd player in the turn order
    first_player = current_game.players[current_game.turn_order[0]]
    second_player = current_game.players[current_game.turn_order[1]]
    first_p_over = False
    second_p_over = False

    over = False
    i = 0
    while not over:
        print(f'\tProcessing action index: {i}')
        # Process action for first player at index i
        if i < len(first_player.action_chain):
            current_game = process_card(first_player.action_chain[i], first_player, current_game)
        else:
            first_p_over = True

        if current_game.state == "game over":
            return current_game

        # Process action for second player at index i
        if i < len(second_player.action_chain):
            current_game = process_card(second_player.action_chain[i], second_player, current_game)
        else:
            second_p_over = True

        if current_game.state == "game over":
            return current_game

        # Check if both players are over
        if first_p_over and second_p_over:
            # If both players are over, we can end the action chain processing
            over = True

        i += 1

    return current_game

def process_card(cards_dict, player, current_game):
    """ process a cards (list) that are in the trip chain, card format:
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

    # if mode is defend do nothing
    if cards_dict['mode'] == 'defend':
        # GERER CONDITION BLOCK
        return current_game

    # filter cards from CARDS_DB based on card_ids in cards_dict['cards']
    card_df = CARDS_DB.filter(pl.col('card_id').is_in(card_ids))
    print(f'\t\t\tadv: {card_df["advancing"].item()}, mana: {card_df["mana"].item()}')

    # check card condition (extract scalar values: each action plays exactly 1 card)
    condition = card_df['condition'].item()
    effect = card_df['effect'].item()
    condition_met = is_condition_met(condition, player, current_game)

    # if condition met apply effect and advancing
    if condition_met:
        print(f'\t\t\tapplying effect: {effect}')
        current_game = apply_effect(effect, player, current_game)

        # Apply basic advancing
        basic_advancing = card_df['advancing'].sum()
        current_game = process_advancing(basic_advancing, player, current_game)
    else:
        # condition not met: reduced advancing (card mana - 1)
        basic_advancing = card_df['mana'].sum() - 1
        print(f'\t\t\tcondition not met, reduced advancing: {basic_advancing}')
        current_game = process_advancing(basic_advancing, player, current_game)

    print(f'\t\t\tcurrent position: {player.current_position} (len(earth): {len(current_game.earth)})')

    return current_game

def _get_oppo(player, current_game):
    """ return the opponent PlayerState (the other player in the game), or None if playing alone """
    for p in current_game.players.values():
        if p.name != player.name:
            return p
    return None

def is_condition_met(condition, player, current_game):
    # no condition required -> always met
    if condition == 'no_condition':
        return True

    # biome conditions: check the cell the player currently stands on
    if 'biome' in condition:
        cell = get_player_cell(player, current_game)
        biome_map = {
            'biome_Dwa': ('MO', 'OC'),
            'biome_Dem': ('OC', 'DE'),
            'biome_Twi': ('JU', 'OC'),
            'biome_Mia': ('DE', 'JU'),
            'biome_Orc': ('MO', 'JU'),
            'biome_Mum': ('DE', 'MO'),
        }
        if condition in biome_map:
            return any(b in cell for b in biome_map[condition])
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

    # any other not-yet-implemented condition: assume met so the card can still advance
    print(f'\t\t\tcondition {condition} not implemented, assuming met')
    return True

def apply_effect(effect, player, current_game):

    return current_game

def process_advancing(advancing_value, player, current_game):
    """ process advancing of a player on the earth, checking for traps and drops """
    if advancing_value == 0:
        return current_game

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
            current_game.earth[0].append(player.name)                           # put it back to start showing crossing finish line
            break

        player.current_position = new_position

        # check if stepping on a trap or drop (if yes apply effect)
        cell = get_player_cell(player, current_game)
        if 'trap' in cell:
            # apply trap effect
            current_game = apply_effect('trap', player, current_game)
        if 'drop' in cell:
            # apply drop effect
            current_game = apply_effect('drop', player, current_game)

        current_game.earth[player.current_position].append(player.name)     # update new player position in earth

    return current_game

def get_player_cell(player, current_game):
    return current_game.earth[player.current_position]