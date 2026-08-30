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
    #    5 = + grappling_hook effect (copy the facing card's total advancement)
    #    4 = + avalanche effect (knockback of all tokens on the MO biome)
    current_game.engine_version = 6

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

    # ToDo: manage instant actions here (support faction, pendings, drops, defense cards, etc.)

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

def grappling_copy_amount(game, facing_adv):
    """ grappling_hook: the forward movement a player copies from its facing card's
    total advancement (the net cells that facing card moved). 0 when the rule is not
    active (engine_version < 5) or when the facing card did not advance forward
    (a grappling hook pulls forward, it never copies a recoil / net backward move). """
    if (game.engine_version or 0) < 5:
        return 0
    return max(0, int(facing_adv or 0))


def apply_grappling_copy(game, player, amount):
    """ grappling_hook: apply the copied forward advance (the facing card's total
    advancement). It is a pure copy of the facing card's movement, so it is applied
    WITHOUT the player's own faction biome bonus (allow_bonus=False) - but it still
    goes through process_advancing so the win condition and trap/drop checks apply. """
    if amount > 0:
        print(f'\t\t\tgrappling_hook: {player.name} copies the facing card advancement (+{amount})')
        return process_advancing(amount, player, game, allow_bonus=False)
    return game


def process_trip_chain(current_game):
    """ process the action chain of the game, effect first then advancing.
     grappling_hook copies: after BOTH facing cards at an index have resolved, each
     validated grappling card advances by the total advancement of the facing card
     (see apply_grappling_copy / grappling_copy_amount). """
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
        first_grapple = False
        first_adv = 0
        if i < len(first_player.action_chain):
            pos_before = first_player.current_position
            current_game, first_grapple = process_card(first_player.action_chain[i], first_player, current_game)
            first_adv = (first_player.current_position or 0) - pos_before
        else:
            first_p_over = True

        if current_game.state == "game over":
            return current_game

        # Process action for second player at index i
        second_grapple = False
        second_adv = 0
        if i < len(second_player.action_chain):
            pos_before = second_player.current_position
            current_game, second_grapple = process_card(second_player.action_chain[i], second_player, current_game)
            second_adv = (second_player.current_position or 0) - pos_before
        else:
            second_p_over = True

        if current_game.state == "game over":
            return current_game

        # grappling_hook copies: each validated grappling card copies the total
        # advancement of its facing card (the opponent card at this same index).
        # Applied AFTER both cards have resolved so the facing advancement is known.
        if first_grapple:
            current_game = apply_grappling_copy(current_game, first_player,
                                                grappling_copy_amount(current_game, second_adv))
        if current_game.state == "game over":
            return current_game
        if second_grapple:
            current_game = apply_grappling_copy(current_game, second_player,
                                                grappling_copy_amount(current_game, first_adv))

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

    stopover = cards_dict['to']

    # --- DEFEND mode: cards played sideways (90°) on the stopover.
    #     They do NOT advance and do NOT trigger their effect. The dedicated block
    #     cards (condition == 'block') have their effect ARMED here, but it fires later,
    #     only if they actually block an opponent card on this same stopover
    #     They block the opponent card on the SAME stopover (checked when that card resolves). ---
    if cards_dict['mode'] == 'defend':
        for card_id in card_ids:
            row = CARDS_DB.filter(pl.col('card_id') == card_id)
            if row.is_empty():
                continue
            r = row.row(0, named=True)
            if r['condition'] == 'block':
                print(f'\t\t\tdefend card {card_id} (condition "block") armed on {stopover}: its effect fires only if it blocks an opponent card')
            else:
                print(f'\t\t\tdefend card {card_id} (shield {r["shield"]}) blocks the same stopover, no effect')
        return current_game, False   # defend cards never advance / never grapple

    # --- MOVE (normal) mode: resolve the card, but first check the opponent's
    #     defend cards on the SAME stopover (block / unstoppable / shields vs mana). ---
    grappling_activated = False
    for card_id in card_ids[:1]:   # normal play: exactly 1 card
        rows = CARDS_DB.filter(pl.col('card_id') == card_id)
        if rows.is_empty():
            continue
        row = rows.row(0, named=True)

        # BLOCK check: is the opponent playing defend card(s) on this same stopover?
        oppo = _get_oppo(player, current_game)
        if oppo is not None and _oppo_defend_actions(oppo, stopover):
            # exception 1: an "unstoppable" card whose condition is met is not affected
            if row['effect'] == 'unstoppable' and is_condition_met(row['condition'], player, current_game):
                print(f'\t\t\tcard {card_id} is unstoppable (condition met) -> ignores the block')
            else:
                # exception 2: the block only holds if the total shields >= the card's mana
                shields = _oppo_defend_shields(oppo, stopover)
                if shields >= int(row['mana']):
                    print(f'\t\t\tcard {card_id} BLOCKED by {oppo.name} defend card(s) on {stopover} '
                          f'(shields {shields} >= mana {row["mana"]}) -> no effect, no advancing')
                    # the defender's dedicated block cards (condition == "block") now fire their effect
                    current_game = _fire_block_effects(oppo, stopover, current_game)
                    current_game.message = {'success': True, 'message': f'Card blocked by {oppo.name} on {stopover}'}
                    continue
                print(f'\t\t\tblock failed (shields {shields} < mana {row["mana"]}), card {card_id} plays normally')

        current_game, grappling_activated = _resolve_card(row, player, current_game, stopover)

    print(f'\t\t\tcurrent position: {player.current_position} (len(earth): {len(current_game.earth)})')

    return current_game, grappling_activated

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


def _fire_block_effects(defender, stopover, current_game):
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
                current_game = apply_effect(r['effect'], r['effect_number'], int(r['advancing']), defender, current_game)
    return current_game

def trigger_cataclysm(current_game):
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

    return _knockback_biome(biome, current_game, f'cataclysm: {biome} strikes')

def _knockback_biome(biome, current_game, label='strike'):
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
            current_game.earth[pos].remove(p.name)
            p.current_position = start
            current_game.earth[start].append(p.name)
    return current_game


def _resolve_card(row, player, current_game, stopover=None):
    """ resolve ONE card of the trip chain: condition -> effect -> advancing
     (movement effects handle their own advancing inside apply_effect).
     stopover: the stopover this card was played on (e.g. 'stopover_4') - needed for
     the effect_canceled check (opponent cancel card on the SAME stopover).
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
        current_game = trigger_cataclysm(current_game)

    # check card condition
    condition_met = is_condition_met(condition, player, current_game)

    # grappling_hook: the card's OWN advancing is applied below as usual; the
    # "copy of the facing card's advancing" is flagged here and applied by the trip
    # chain (it needs the facing card's movement, only known once that card resolved)
    grappling_activated = (effect == 'grappling_hook' and condition_met
                           and (current_game.engine_version or 0) >= 5)

    if condition_met:
        # effect_canceled (rule of engine_version 6): before applying this card's
        # effect, check if the opponent has a VALID effect_canceled card (move mode,
        # condition met) on the SAME stopover - if so, this card's effect is
        # CANCELED: it does NOT fire (including any movement it would have caused),
        # but the card still advances by its basic value.
        effect_cancelled = False
        if (current_game.engine_version or 0) >= 6 and stopover:
            oppo = _get_oppo(player, current_game)
            if oppo is not None and _oppo_has_valid_effect_canceled(oppo, stopover, current_game):
                effect_cancelled = True
                print(f'\t\t\teffect {effect} CANCELED by {oppo.name} effect_canceled card on {stopover} -> no effect, basic advancing only')

        if effect_cancelled:
            # the effect is canceled: only the basic advancing is applied
            current_game = process_advancing(basic_advancing, player, current_game)
        else:
            print(f'\t\t\tapplying effect: {effect}')
            current_game = apply_effect(effect, row['effect_number'], basic_advancing, player, current_game)

            # Apply basic advancing (movement effects already moved the player inside apply_effect)
            if effect not in ('advancing', 'backward', 'jump'):
                current_game = process_advancing(basic_advancing, player, current_game)
    else:
        # condition not met: reduced advancing (card mana - 1)
        basic_advancing = int(row['mana']) - 1
        print(f'\t\t\tcondition not met, reduced advancing: {basic_advancing}')
        current_game = process_advancing(basic_advancing, player, current_game)

    return current_game, grappling_activated

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

    # any other not-yet-implemented condition: assume met so the card can still advance
    print(f'\t\t\tcondition {condition} not implemented, assuming met')
    return True

def apply_effect(effect, effect_number, basic_advancing, player, current_game):
    """ apply the card effect (all effects are handled here)
     effect_number: quantitative parameter of the effect from CARDS_DB (e.g. N cards to draw, N cells to recoil)
     basic_advancing: base advancing value of the played card (needed by movement effects) """

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
        print(f'\t\t\teffect jump: {player.name} jumps {jump_distance} cell(s), skipping intermediate cells')
        return _jump(player, jump_distance, current_game)

    # --- board effect: avalanche (rule of engine_version 4) ---
    if effect == 'avalanche':
        # ALL player tokens on the Mountain (MO) biome are knocked back to the FIRST
        # cell of that biome (both players, the playing player included). The playing
        # player still applies its basic advancing afterwards (from the new position
        # if it was on MO). effect_number is 0 in the pool (unused).
        # Games with engine_version < 4 keep the old no-op behavior (replay pinning).
        if (current_game.engine_version or 0) >= 4:
            current_game = _knockback_biome('MO', current_game, 'avalanche: MO strikes')
        else:
            print(f'\t\t\teffect avalanche: no-op (engine_version < 4)')
        return current_game

    # --- grappling_hook (rule of engine_version 5): the card's own advancing is
    #     applied by _resolve_card as usual; the "copy of the facing card's advancing"
    #     is applied by the trip chain (apply_grappling_copy) after the facing card
    #     has resolved - so this branch is intentionally a no-op here. ---
    if effect == 'grappling_hook':
        return current_game

    # --- other effects not implemented yet (wrecking_ball, copy_effect, ...) ---
    return current_game

def _jump(player, n, current_game):
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
        current_game.earth[0].append(player.name)                           # put it back to start showing crossing finish line
        return current_game

    player.current_position = new_position

    # check if landing on a trap or drop (intermediate cells were skipped, only the destination matters)
    cell = get_player_cell(player, current_game)
    if 'trap' in cell:
        current_game = apply_effect('trap', 0, 0, player, current_game)
    if 'drop' in cell:
        current_game = apply_effect('drop', 0, 0, player, current_game)

    current_game.earth[player.current_position].append(player.name)     # update new player position in earth
    return current_game

def process_advancing(advancing_value, player, current_game, allow_bonus=True):
    """ process advancing of a player on the earth, checking for traps and drops
     allow_bonus: apply the faction biome +1 for forward movement (default True).
     The grappling_hook copy (apply_grappling_copy) passes allow_bonus=False so it is
     a pure copy of the facing card's movement, not boosted by the player's own bonus. """
    if advancing_value == 0:
        return current_game

    # faction biome bonus: standing on one of the two biomes of your own faction
    # grants +1 to forward movement (recoil / backward movement is not boosted)
    if allow_bonus and advancing_value > 0 and _on_home_biome(player, current_game):
        print(f'\t\t\tfaction biome bonus: +1 advancing for {player.name} (token on home biome)')
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
            current_game.earth[0].append(player.name)                           # put it back to start showing crossing finish line
            break

        player.current_position = new_position

        # check if stepping on a trap or drop (if yes apply effect)
        cell = get_player_cell(player, current_game)
        if 'trap' in cell:
            # apply trap effect
            current_game = apply_effect('trap', 0, 0, player, current_game)
        if 'drop' in cell:
            # apply drop effect
            current_game = apply_effect('drop', 0, 0, player, current_game)

        current_game.earth[player.current_position].append(player.name)     # update new player position in earth

    return current_game

def get_player_cell(player, current_game):
    return current_game.earth[player.current_position]