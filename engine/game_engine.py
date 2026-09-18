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
# and resolve as 0-cost no-ops unless a rule covers them (engine_version 12 covers
# the Engineers: 4 drop cards + the refinery dwelling; the others are still pending).
SUPPORT_DB_PATH = os.path.join(os.path.dirname(__file__), '../cards/support_factions.parquet')
SUPPORT_DB = {}
if os.path.exists(SUPPORT_DB_PATH):
    for _r in pl.read_parquet(SUPPORT_DB_PATH).iter_rows(named=True):
        SUPPORT_DB[_r['card_name']] = _r

# ---------------------------------------------------------------------------
# Engineers (support faction, rule of engine_version 12)
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

# Doctors (support faction, engine_version 16)
# Pending cards (placed in the pending zone, attachable to a main card — max 1):
#   epo           +1 advancing (added to the main card's effect)
#   virus          -1 knockback (added to the main card's effect)
#   bloodtest      discard 1 card from the player's hand
#   mercurochrome  unstoppable (the main card ignores blocks — checked at block time)
# Dwelling card (placed in the dwelling slot, tap 1x/turn):
#   laboratory     when tapped, adds an "epo" pending card to the player's pending zone
DOCTOR_PENDING = ('epo', 'virus', 'bloodtest', 'mercurochrome')
DOCTOR_DWELLING = 'laboratory'   # tap effect: add an 'epo' pending card

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
    #    12 = + Engineers (support faction): 4 drop cards (boost +2 / trampoline +2 jump /
    #         gluetrap -1 / landmine blocks the arriving player for the rest of the turn,
    #         unstoppable cards with their condition met excepted) + the refinery dwelling
    #         (one card at a time, permanent, tapped once per turn to draw 1, untapped in
    #         the cleaning phase, removed by wrecking_ball); support cards cost their
    #         mana_cost from the support table when played
    #    11 = + wrecking_ball effect (INSTANT play-time effect: removes the opponent's
    #         dwelling card - the card is sent to the opponent's discard, the dwelling
    #         slot is cleared)
    #    10 = + rooted effect (card earns a rooted token -> survives the cleaning phase on a
    #         free stopover, one-shot, 1-turn cooldown; inert on the board, never a blocker)
    #    9 = + drop_on_board condition (met if any drop or trap is on the earth)
    #    8 = + pet_trap effect (INSTANT play-time effect: drop token on the player's cell,
    #        triggered when any token arrives on the cell, knockback -1 per token)
    #    7 = + copy_effect effect (copy the facing card's effect, applied with the copier as actor)
    #    6 = + effect_canceled effect (cancel the facing card's effect)
    #    5 = + grappling_hook effect (copy the facing card's total advancement)
    #    4 = + avalanche effect (knockback of all tokens on the MO biome)
    #    13 = + discard selection: the discard / discard_oppo effects PAUSE the trip
    #         chain and the discarding player CHOOSES the card(s) (to: 'discard_pile');
    #         games < 13 keep the auto-discard (last card(s) of the hand)
    #    14 = + skip-occupied-stopover rule (shared, by column)
    #    15 = + PER-PLAYER stopovers: each player has their OWN 5 stopover slots; the
    #         trip chain is position-based (a player's rooted cards occupy the first
    #         positions, then their plays); play_count tracks each player's plays this
    #         turn; games < 15 keep the shared stopover columns (v14) / action-index
    #         chain (v13 and below)
    current_game.engine_version = 20   # rule version: + the placeholder of a pending card that is ATTACHED to a main card this same turn STAYS IN PLACE (v19 and below removed it — the freed position desynced the frontend's next-slot suggestion, which offered the main card's own slot for the next play). pending_slots is a list of [card, slot] pairs (v19: attachment removed the pair BY CARD NAME; the laboratory TAP's 'epo' gets no slot entry, v19+). Board-furniture placeholders (doctor pending / dwelling) OCCUPY a trip-chain position (v17)

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
    # rooted (engine_version 10): settle the rooted cards NOW (after the
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
    current_game.day_night = 'night' if current_game.day_night == 'day' else 'day'   # flip day/night each new turn

    for p in current_game.players.values():                     # reset necessary players state
        p.mana_spend = 0
        p.action_chain = []
        # stopover positions (engine_version 15): each player's plays this turn reset
        p.play_count = 0
        # landmine (engine_version 12): the block lasts until the end of the turn
        p.landmine_blocked = False
        # dwelling (engine_version 12): the dwelling card can be tapped again next turn
        p.dwelling_tapped = False
        # dwelling placeholder (engine_version 12): the placeholder image only shows
        # during the placement turn — clear the stored slot here (cleaning phase) so it
        # does not reappear at every new turn (the frontend renders it iff the slot is set)
        p.dwelling_slot = None
        # pending placeholders (engine_version 16): same logic — the stopover
        # placeholders only show during the placement turn
        if (current_game.engine_version or 0) >= 16:
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

        # 2.0 Discard selection (rule of engine_version 13): the trip chain is
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

    # dwelling actions (engine_version 12, Engineers): place the dwelling card
    # (to 'dwelling', exactly 1 card) or tap it (mode 'dwelling_activation', no card)
    if message['to'] == 'dwelling':
        if message['mode'] == 'dwelling_activation':
            if len(message['cards']) != 0:
                return False, "When mode is 'dwelling_activation', 'cards' must be empty (the tap uses the card on the board)"
        elif len(message['cards']) != 1:
            return False, "When 'to' is 'dwelling', 'cards' must contain exactly 1 card (the dwelling card to place)"

    # optional target cell (engineers' drop placement, engine_version 12):
    # an engineer drop played in MOVE mode must carry the earth cell it is placed on
    if 'cell' in message and message['cell'] is not None:
        cell = message['cell']
        if isinstance(cell, bool) or not isinstance(cell, int) or not (0 <= cell < 24):
            return False, "'cell' must be an integer between 0 and 23"

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
    
    # --- DWELLING actions (engine_version 12, Engineers) --------------------------
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
        if (current_game.engine_version or 0) < 12:
            return player, current_game, False, "dwelling actions are not available in this game (engine_version < 12)"
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
                else:
                    message = f"{player.name} taps the refinery — nothing left to draw"
                print(f'\t\t\tDWELLING tap: {player.name} taps the refinery, drew {drawn}')
            elif player.dwelling == 'laboratory' and (current_game.engine_version or 0) >= 16:
                # Doctors laboratory: tap adds an "epo" pending card to the pending zone
                if player.pendings is None:
                    player.pendings = []
                if player.pending_slots is None:
                    player.pending_slots = []
                player.pendings.append('epo')
                if (current_game.engine_version or 0) >= 19:
                    # v19: the tap is a QUICK action — the 'epo' sits in the persistent
                    # pendings zone but gets NO placeholder entry (pending_slots is the
                    # per-turn placeholder list, so nothing is appended and play_count
                    # is untouched). v18 recorded a None entry; v19 records nothing.
                    pass
                elif (current_game.engine_version or 0) == 18:
                    # v18: the tap is a QUICK action — the 'epo' sits in the pending zone
                    # but occupies NO trip-chain position (no placeholder, play_count
                    # untouched). pending_slots stays PARALLEL to pendings: None = no
                    # placeholder (board.mjs / actions.mjs / _player_chain all skip it).
                    player.pending_slots.append(None)
                elif (current_game.engine_version or 0) >= 15:
                    played_before = player.play_count or 0
                    slot = int(_player_stopover(current_game, player.name, played_before).rsplit('_', 1)[-1])
                    player.pending_slots.append(slot)
                    player.play_count = played_before + 1
                else:
                    player.pending_slots.append(4 - min(len(player.pending_slots), 4))
                player.dwelling_tapped = True
                message = f"{player.name} taps the laboratory — adds an 'epo' pending card"
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
            # Accept engineer dwelling (refinery) or doctor dwelling (laboratory, v16+)
            valid_dwelling = {ENGINEER_DWELLING}
            if (current_game.engine_version or 0) >= 16:
                valid_dwelling.add(DOCTOR_DWELLING)
            if card_id not in valid_dwelling:
                return player, current_game, False, f"{card_id} is not a dwelling card"
            if cost > mana_available:
                return player, current_game, False, (f"not enough mana to place {card_id} "
                                                     f"(available {mana_available}, required {cost})")
            player.hand.remove(card_id)
            player.dwelling = card_id
            if (current_game.engine_version or 0) >= 15:
                # stopover positions (engine_version 15): the dwelling placeholder takes
                # the NEXT free position in this player's OWN chain — (rooted count)
                # + (plays so far this turn) + 1 — recorded as a column index (5 - pos).
                played_before = player.play_count or 0
                player.dwelling_slot = int(_player_stopover(current_game, player.name, played_before).rsplit('_', 1)[-1])
                player.play_count = played_before + 1
            else:
                # compute the stopover column the placeholder should occupy (v<15 legacy):
                # same convention as a normal play — 1st card → col 4, 2nd → col 3, …
                played_count = sum(1 for a in (player.action_chain or [])
                                   if a.get('mode') in ('move', 'defend') and a.get('cards'))
                player.dwelling_slot = 4 - min(played_count, 4)
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
                # stopover positions (engine_version 15): a dwelling PLACE consumes a
                # position - sync play_count back or the next play re-reads 0 and lands
                # on the placeholder's own slot (same class as the refinery-TAP bug)
                p.play_count = player.play_count
                p.messages_history.append(player.message)
        return player, current_game, True, message

    # --- PENDING ZONE placement (doctors, engine_version 16) ---
    # Place a doctor pending card (epo/virus/bloodtest/mercurochrome) into the
    # pending zone. Costs its mana_cost. Creates a placeholder in the stopover
    # (like the refinery dwelling). The card stays in the zone until attached
    # to a main card (max 1 per main card). This is a play-phase action.
    if player.message['to'] == 'pending_zone':
        if (current_game.engine_version or 0) < 16:
            return player, current_game, False, "pending zone is not available in this game (engine_version < 16)"
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
        # engine_version 19: the placeholder is a [card, slot] PAIR — the list is the
        # per-turn placeholder display (cleared in the cleaning phase) and is NOT
        # parallel to the persistent `pendings` zone, so attachment must remove the
        # pair BY CARD NAME, not by index (the v18-and-below index splice deleted the
        # wrong placeholder once the two lists diverged across turns).
        if (current_game.engine_version or 0) >= 15:
            played_before = player.play_count or 0
            slot = int(_player_stopover(current_game, player.name, played_before).rsplit('_', 1)[-1])
            if (current_game.engine_version or 0) >= 19:
                player.pending_slots.append([pz_card, slot])
            else:
                player.pending_slots.append(slot)
            player.play_count = played_before + 1
        else:
            player.pending_slots.append(4 - min(len(player.pending_slots), 4))
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

    # check mana available (main cards: pool mana; support cards, engine_version 12:
    # their mana_cost from the support table; unknown cards: 0)
    mana_available = len(player.mana) - (player.mana_spend or 0)
    cards_id = player.message['cards']
    total_cost = sum(_play_cost(current_game, cid) for cid in cards_id)

    # --- PENDING CARD attachment validation (doctors, engine_version 16) ---
    # When playing a MAIN card in MOVE mode, the player can attach ONE pending
    # card from their pending zone. Validate BEFORE any state changes.
    if (current_game.engine_version or 0) >= 16:
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
                if main_card in DOCTOR_PENDING or main_card in DOCTOR_DWELLING or main_card in ENGINEER_DROPS or main_card == ENGINEER_DWELLING:
                    return player, current_game, False, "You cannot attach a pending card to a support card"

    # engineers' drops (engine_version 12): a MOVE play of a drop card must carry
    # the target cell (message 'cell', 0..23) - the token is placed there at play time
    if (current_game.engine_version or 0) >= 12 and player.message['mode'] == 'move':
        for cid in cards_id[:1]:
            if cid in ENGINEER_DROPS:
                cell = player.message.get('cell')
                if isinstance(cell, bool) or not isinstance(cell, int) or not (0 <= cell < 24):
                    return player, current_game, False, (
                        f"engineer drop {cid} must target a cell of the earth (message 'cell', 0..23)")

    if total_cost <= mana_available:
        for card_id in cards_id:
            if card_id not in (player.hand or []):
                return player, current_game, False, f"{card_id} is not in {player.name}'s hand"
            player.hand.remove(card_id)
        if (current_game.engine_version or 0) >= 15:
            # stopover positions (engine_version 15): the engine is the source of
            # truth for the position - the (rooted count) + (plays so far this turn)
            # + 1-th position in this player's OWN chain. The client's 'to' is for
            # display only; it is overwritten here.
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
        # --- Consume the pending card (doctors, engine_version 16) ---
        if (current_game.engine_version or 0) >= 16:
            pendings_list = player.message.get('pendings') or []
            if len(pendings_list) == 1:
                pcard = pendings_list[0]
                idx = player.pendings.index(pcard)
                player.pendings = player.pendings[:idx] + player.pendings[idx+1:]
                if (current_game.engine_version or 0) >= 20:
                    # v20: the placeholder of the attached pending card STAYS IN PLACE
                    # (when it was placed this turn — i.e. the [card, slot] pair is in
                    # pending_slots): it marks the trip-chain position that the pending
                    # placement consumed. Removing it freed the position while the
                    # engine's play_count kept it, so the frontend's next-slot mirror
                    # (visible action_chain + placeholders) offered the main card's own
                    # slot for the next play. An OLD pending (placed a previous turn)
                    # has no placeholder — nothing to keep. pending_slots is unchanged.
                    pass
                elif (current_game.engine_version or 0) >= 19:
                    # v19: pending_slots is the per-turn placeholder list ([card, slot]
                    # pairs) — NOT parallel to the persistent `pendings` zone (the slots
                    # are cleared every turn, the pendings persist). Remove the attached
                    # card's placeholder BY CARD NAME (the first matching pair). The
                    # v18-and-below index splice below deleted the WRONG placeholder —
                    # attaching an old pending (low index in `pendings`) wiped the slot
                    # of a card placed THIS turn (game 26_09_17_21_33_39_d5oEd, turn 4:
                    # attaching 'virus' deleted the mercurochrome placeholder).
                    _new_slots, _removed = [], False
                    for _e in (player.pending_slots or []):
                        if (not _removed and isinstance(_e, (list, tuple)) and len(_e) == 2
                                and _e[0] == pcard):
                            _removed = True
                            continue
                        _new_slots.append(_e)
                    player.pending_slots = _new_slots
                else:
                    player.pending_slots = player.pending_slots[:idx] + player.pending_slots[idx+1:]
                player.message['pending_card'] = pcard
                print(f'\t\t\tPENDING: {player.name} attaches {pcard} to {cards_id[0]}')
        message = f"Player {player.name} played {cards_id} successfully"
    else:
        success = False
        message = f"Player {player.name} tried to play {cards_id} but not enough mana available (available: {mana_available}, required: {total_cost})"

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
            p.dwelling = player.dwelling
            p.dwelling_tapped = player.dwelling_tapped
            # stopover positions (engine_version 15): persist this turn's play count
            p.play_count = player.play_count
            # doctors (engine_version 16): persist pending zone state
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
    (engine_version 12) cost their mana_cost from the support table; unknown
    cards (e.g. support cards in games < 12) cost 0. """
    rows = CARDS_DB.filter(pl.col('card_id') == card_id)
    if not rows.is_empty():
        return int(rows['mana'][0])
    if (current_game.engine_version or 0) >= 12 and card_id in SUPPORT_DB:
        return int(SUPPORT_DB[card_id]['mana_cost'])
    return 0

def _apply_instant_effects(player, current_game, msg):
    """ INSTANT effects: fire at PLAY TIME (right after the card is accepted),
     before the trip chain resolves - they cannot be blocked or canceled.
     pet_trap (engine_version 8): leaves ONE drop token on the player's current
     cell (tokens stack on the same cell). Move mode only: a defend play plays
     no effect, so it places no token.
     wrecking_ball (engine_version 11): REMOVES the opponent's dwelling card
     (PlayerState.dwelling, if set) - the card is sent to the opponent's discard
     pile and the dwelling slot is cleared. Move mode only. A no-op today (no
     implemented effect places a dwelling card yet) but the removal system is
     live so it works as soon as dwelling cards exist.
     engineers' drops (engine_version 12): boost/trampoline/gluetrap/landmine leave
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
        if card_id in ENGINEER_DROPS and (current_game.engine_version or 0) >= 12:
            cell = msg.get('cell')
            if isinstance(cell, int) and not isinstance(cell, bool) and 0 <= cell < 24:
                current_game.board_drops.append({'cell': cell, 'kind': card_id, 'owner': player.name})
                # engine annotation on the action (shared dict: also lands in
                # action_chain / messages_history): the turn log reads it
                msg['drop_cell'] = cell
                msg['drop_kind'] = card_id
                print(f'\t\t\tINSTANT engineer drop: {card_id} token placed on cell {cell} (owner {player.name})')
            continue
        rows = CARDS_DB.filter(pl.col('card_id') == card_id)
        if rows.is_empty():
            continue
        r = rows.row(0, named=True)
        if r['effect'] == 'pet_trap':
            cell = player.current_position or 0
            current_game.drop_tokens[cell] = current_game.drop_tokens.get(cell, 0) + 1
            placed.append(cell)
            print(f'\t\t\tINSTANT pet_trap: drop token placed on cell {cell} (total there: {current_game.drop_tokens[cell]})')
        elif r['effect'] == 'wrecking_ball' and (current_game.engine_version or 0) >= 11:
            oppo = _get_oppo(player, current_game)
            if oppo is not None and oppo.dwelling:
                dwelling_card = oppo.dwelling
                oppo.discard = (oppo.discard or []) + [dwelling_card]
                oppo.dwelling = None
                oppo.dwelling_slot = None
                # engine annotation on the action (shared dict: also lands in
                # action_chain / messages_history): the turn log reads it
                msg['dwelling_removed'] = dwelling_card
                msg['dwelling_removed_from'] = oppo.name
                print(f'\t\t\tINSTANT wrecking_ball: removed {oppo.name}\'s dwelling card {dwelling_card}')
    return placed

def _cell_has_drop(current_game, cell_index):
    """ pet_trap (engine_version 8): True if the cell carries at least one drop
     token (placed at play time by a pet_trap card). Empty for older games
     (drop_tokens defaults to {}), so this is a no-op for them. """
    return (current_game.drop_tokens or {}).get(cell_index, 0) > 0

def _player_blocked(player, current_game):
    """ landmine (engine_version 12): True while the player is blocked by a
     landmine (its move cards, grappling copies and effect copies are canceled
     for the rest of the turn - except unstoppable cards with their condition met). """
    return (current_game.engine_version or 0) >= 12 and bool(player.landmine_blocked)

def _trigger_board_drops(current_game, player, log_entry=None):
    """ engineers' drops (rule of engine_version 12): the FIRST token that ARRIVES
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
    if (current_game.engine_version or 0) < 12:
        return current_game
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
                log_entry['notes'].append(f'💥 landmine on cell {cell} — {player.name} blocked for the rest of the turn')
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
    # engineers' drops (engine_version 12): the token was placed at PLAY TIME on the
    # cell CHOSEN by the player (the engine annotated it on the action)
    if (action or {}).get('drop_kind'):
        entry['notes'].append(
            f'🧲 {action["drop_kind"]} — drop placed on cell {action.get("drop_cell")} '
            f'(fires when any token arrives, then is consumed)')
    # wrecking_ball (engine_version 11): the dwelling card was removed at PLAY TIME
    # (before the trip chain started) - the engine annotated which card was wrecked
    if (action or {}).get('dwelling_removed'):
        entry['notes'].append(
            f'💥 wrecking_ball — removed {((action or {}).get("dwelling_removed_from") or "opponent")}\'s '
            f'dwelling card {action["dwelling_removed"]}')
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

     discard selection (rule of engine_version 13): when a discard / discard_oppo
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

     stopover positions (rule of engine_version 15): the chain is resolved BY
     POSITION (1->5), per-player. Each player's trip chain is that player's OWN
     rooted cards (from the previous turn, positions 1..R) followed by this turn's
     plays (positions R+1..). A rooted card is a REAL card: it applies its basic
     advancing (no condition, no effect) and is blockable. The facing (block /
     grappling / copy) is between the two players' entries at the SAME position.
     Games with engine_version < 15 keep the legacy action-index iteration
     (no rooted cards on the board). """
    print('Processing trip chain...')
    # Select first and 2nd player in the turn order
    first_player = current_game.players[current_game.turn_order[0]]
    second_player = current_game.players[current_game.turn_order[1]]

    use_positions = (current_game.engine_version or 0) >= 15
    if use_positions:
        chain_f = _player_chain(current_game, first_player.name)
        chain_s = _player_chain(current_game, second_player.name)
        # max POSITION (not len): since engine_version 17 the chain can have GAPS -
        # a pending placeholder attached to a play is removed from the chain, leaving
        # its position empty. max_pos must reach the farthest occupied position.
        _positions = [e['position'] for e in (chain_f or [])] + [e['position'] for e in (chain_s or [])]
        max_pos = max(_positions) if _positions else 0
    else:
        def _legacy_chain(player):
            out = []
            for idx, a in enumerate(player.action_chain or []):
                if not a:
                    continue
                if a.get('mode') not in ('move', 'defend'):
                    continue
                if not a.get('cards'):
                    continue
                out.append({'kind': 'play', 'action': a, 'stopover': a.get('to'), 'position': idx + 1})
            return out
        chain_f = _legacy_chain(first_player)
        chain_s = _legacy_chain(second_player)
        max_pos = max(len(chain_f), len(chain_s))

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
    else:
        log_list = current_game.log
        if not isinstance(log_list, list):
            current_game.log = log_list = []
        turn_log = {'turn': current_game.turn, 'stopovers': []}
        log_list.append(turn_log)
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
                # doctors (engine_version 16): flush the attached pending card too
                pcard = (entry['action'] or {}).get('pending_card')
                if pcard:
                    player.discard.append(pcard)
                print(f'\t\t\tgame over mid-chain: flushed {len(cards)} unresolved card(s) of {player.name} to discard')
            player.action_chain = []

    def pause_discard(ctx):
        """ The chain hit a discard the player must CHOOSE (rule of engine_version 13).
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
                entry_f = None   # placeholder (v17): an empty position, nothing to log
            if e_s is not None and e_s['kind'] == 'rooted':
                entry_s = new_log_entry(second_player, {'to': e_s['stopover'], 'mode': 'rooted', 'cards': [e_s['card_id']]}, 2)
            elif e_s is not None and e_s['kind'] == 'play':
                entry_s = new_log_entry(second_player, e_s['action'], 2)
            else:
                entry_s = None   # placeholder (v17)
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
            # previous turn, or this turn's play). A PLACEHOLDER (v17) is an empty
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
            # Process the second player's entry at position p. A PLACEHOLDER (v17)
            # is an empty position: nothing resolves and second_adv stays 0, so a
            # facing grappling hook copies nothing (the v17 fix).
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
            # A landmine-blocked player (engine_version 12) does not copy.
            if ctx['first_grapple'] and not _player_blocked(first_player, current_game):
                current_game = apply_grappling_copy(current_game, first_player,
                                                    grappling_copy_amount(current_game, ctx['second_adv']), entry_f)
            ctx['stage'] = 'grapple_s'
        elif stage == 'grapple_s':
            if ctx['second_grapple'] and not _player_blocked(second_player, current_game):
                current_game = apply_grappling_copy(current_game, second_player,
                                                    grappling_copy_amount(current_game, ctx['first_adv']), entry_s)
            ctx['stage'] = 'copy_f'
        elif stage == 'copy_f':
            # copy_effect (rule of engine_version 7): a validated copy_effect card
            # copies the effect of its FACING card (the opponent entry at this same
            # position / stopover), applied with the copier as the actor. Only when
            # the facing card's effect actually fired (condition met, not blocked,
            # not effect_canceled) and the facing entry is a PLAY (a rooted card has
            # no effect to copy) and the copier is not landmine-blocked.
            if ctx['first_effect_ok'] and ctx['second_effect_ok'] and e_s is not None and e_s['kind'] == 'play' \
                    and not _player_blocked(first_player, current_game):
                current_game = apply_copy_effect(current_game, first_player, e_f['action'],
                                                 second_player, e_s['action'], entry_f)
            ctx['stage'] = 'copy_s'
        elif stage == 'copy_s':
            if ctx['second_effect_ok'] and ctx['first_effect_ok'] and e_f is not None and e_f['kind'] == 'play' \
                    and not _player_blocked(second_player, current_game):
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
            continue   # support cards (engineers' drops, ...) resolve as no-ops here
        row = rows.row(0, named=True)
        if log_entry is not None:
            log_entry['effect'] = row['effect']

        # doctors (engine_version 16): the attached pending card may be
        # mercurochrome (unstoppable) — check it alongside the card's own effect
        _pending_card = cards_dict.get('pending_card')
        _is_unstoppable = (
            (row['effect'] == 'unstoppable' or _pending_card == 'mercurochrome')
            and is_condition_met(row['condition'], player, current_game)
        )

        # LANDMINE block (engine_version 12): a player blocked by a landmine cannot
        # advance for the rest of the turn - its MOVE cards are canceled (no effect,
        # no advancing). The only exception is an "unstoppable" card whose condition
        # is met (like the unstoppable exception to the defend block). Defend cards
        # are NOT affected (they never advance - their shields / block effects work).
        # The card is already in the discard pile (top of process_card) - canceling
        # just means it does nothing.
        if (current_game.engine_version or 0) >= 12 and player.landmine_blocked:
            if _is_unstoppable:
                print(f'\t\t	card {card_id} is unstoppable (condition met) -> ignores the landmine block')
                if log_entry is not None:
                    log_entry['notes'].append('unstoppable — ignored the landmine block')
            else:
                print(f'\t\t	card {card_id} CANCELED by the landmine block (no effect, no advancing)')
                if log_entry is not None:
                    log_entry['negatives'].append('landmine — blocked (no effect, no advancing)')
                current_game.message = {'success': True, 'message': f'{player.name} is blocked by a landmine'}
                continue

        # BLOCK check: is the opponent playing defend card(s) on this same stopover?
        oppo = _get_oppo(player, current_game)
        if oppo is not None and _oppo_defend_actions(oppo, stopover):
            # exception 1: an "unstoppable" card whose condition is met is not affected
            # (includes mercurochrome pending card, engine_version 16)
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

        current_game, grappling_activated, effect_activated = _resolve_card(row, player, current_game, stopover, log_entry)

        # doctors (engine_version 16): apply the attached pending card's effect.
        # Fires only if the main card's condition was met and its effect was not
        # canceled (effect_activated is True). mercurochrome is a no-op here (it
        # was already applied at block-check time as an unstoppable modifier).
        _pcard = cards_dict.get('pending_card')
        if _pcard and effect_activated and (current_game.engine_version or 0) >= 16 and current_game.state != "game over":
            current_game = _apply_pending_effect(_pcard, player, current_game, log_entry)

    # doctors (engine_version 16): move the attached pending card to the discard
    # pile (it was consumed from the pending zone at play time; it is not in any
    # zone until now, so it must be flushed here for card conservation)
    _pcard_discard = cards_dict.get('pending_card')
    if _pcard_discard and (current_game.engine_version or 0) >= 16:
        if player.discard is None:
            player.discard = []
        player.discard.append(_pcard_discard)

    print(f'\t\t\tcurrent position: {player.current_position} (len(earth): {len(current_game.earth)})')

    return current_game, grappling_activated, effect_activated

def process_rooted_card(card_id, player, current_game, log_entry=None, oppo_entry=None):
    """ stopover positions (rule of engine_version 15): resolve a ROOTED card that is
     on the board (from the previous turn's rooted effect). It is a REAL card that
     occupies a stopover position and, when the trip chain resolves, APPLIES ITS
     BASIC ADVANCING (its 'advancing' value) — but NO condition check and NO effect
     (so the rooted effect does not re-trigger / become infinite). It is BLOCKABLE:
     the opponent's defend card(s) on the same stopover block it (shields >= mana),
     like a normal move card. Returns (current_game, advancing_delta, blocked). """
    print(f'\t\t{player.name} resolving rooted card on the board: {card_id}')
    if (current_game.engine_version or 0) < 15:
        return current_game, 0, False
    rows = CARDS_DB.filter(pl.col('card_id') == card_id)
    if rows.is_empty():
        return current_game, 0, False
    row = rows.row(0, named=True)
    basic_advancing = int(row['advancing'] or 0)
    if log_entry is not None:
        log_entry['effect'] = row['effect']
        log_entry['condition_met'] = None
        log_entry['notes'].append('🌱 rooted card — basic advancing only (no condition, no effect)')
    stopover = _rooted_stopover_of(current_game, card_id, player.name)
    if (current_game.engine_version or 0) >= 12 and player.landmine_blocked:
        print(f'\t\t\trooted card {card_id} CANCELED by the landmine block (no advancing)')
        if log_entry is not None:
            log_entry['negatives'].append('landmine — rooted card blocked (no advancing)')
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

            # rooted (engine_version 10): grant the card a rooted token (it survives
            # the cleaning phase and stays on a free stopover). One-shot, 1-turn
            # cooldown. The card still applies its basic advancing below.
            if effect == 'rooted' and (current_game.engine_version or 0) >= 10:
                current_game = _grant_rooted_token(row['card_id'], player, current_game, log_entry)

            # Apply basic advancing (movement effects already moved the player inside apply_effect)
            if effect not in ('advancing', 'backward', 'jump'):
                current_game = process_advancing(basic_advancing, player, current_game, log_entry=log_entry)
    else:
        # condition not met: reduced advancing (card mana - 1)
        basic_advancing = int(row['mana']) - 1
        print(f'\t\t\tcondition not met, reduced advancing: {basic_advancing}')
        current_game = process_advancing(basic_advancing, player, current_game, log_entry=log_entry)

    return current_game, grappling_activated, effect_activated

def _apply_pending_effect(pcard, player, current_game, log_entry=None):
    """ doctors (engine_version 16): apply the effect of a pending card attached
     to a main card. Fires only when the main card's condition was met and its
     effect was not canceled. The pending card has already been consumed from
     the pending zone and is moved to the discard pile by the caller.
     mercurochrome is a no-op here (it was already applied at block-check time
     as an unstoppable modifier). """
    print(f'\t\t\tapplied pending card effect: {pcard}')
    if pcard == 'epo':
        current_game = process_advancing(1, player, current_game, allow_bonus=False, log_entry=log_entry)
        if log_entry is not None:
            log_entry['notes'].append('pending epo — +1 advancing')
    elif pcard == 'virus':
        current_game = process_advancing(-1, player, current_game, allow_bonus=False, log_entry=log_entry)
        if log_entry is not None:
            log_entry['notes'].append('pending virus — -1 knockback')
    elif pcard == 'bloodtest':
        if player.hand:
            discarded = player.hand.pop()
            if player.discard is None:
                player.discard = []
            player.discard.append(discarded)
            print(f'\t\t\t  bloodtest: discarded {discarded}')
            if log_entry is not None:
                log_entry['notes'].append(f'pending bloodtest — discarded {discarded}')
        else:
            if log_entry is not None:
                log_entry['notes'].append('pending bloodtest — no cards in hand to discard')
    elif pcard == 'mercurochrome':
        # no-op: already applied at block-check time as an unstoppable modifier
        pass
    return current_game

def _grant_rooted_token(card_id, player, current_game, log_entry=None):
    """ rooted (rule of engine_version 10): grant a card a rooted token. The card
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
    """ stopover positions (rule of engine_version 15): the number of THAT player's
     rooted cards on the board (from the previous turn's rooted effect). These cards
     are REAL cards that occupy the leading positions (1..R) of that player's OWN trip
     chain — they apply their basic advancing when the chain resolves and are
     blockable (see process_trip_chain). Per-player: a player's rooted cards never
     shift the opponent's positions. """
    return sum(1 for r in (current_game.rooted_on_board or []) if (r or {}).get('owner') == player_name)

def _player_stopover(current_game, player_name, played_before):
    """ SINGLE SOURCE OF TRUTH for stopover ordering (per-player, rule of
     engine_version 15): the stopover for the (played_before+1)-th play of
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

def _player_chain(current_game, player_name):
    """ stopover positions (rule of engine_version 15): THAT player's full trip chain
     for the current turn, as an ordered list of entries (position 1 first). Each
     entry is a dict {'kind': 'rooted'|'play'|'placeholder', 'card_id'|'action',
     'stopover', 'position'}.
     The leading entries are the player's OWN rooted cards (from the previous turn);
     the trailing entries are this turn's entries (plays and, since engine_version
     17, board-furniture placeholders). The trip chain resolves this per-player
     chain by position, and the facing (block / grappling / copy) is between the
     two players' entries at the SAME position.

     engine_version 17: a board-furniture PLACEHOLDER (a doctor PENDING card, or a
     dwelling card) OCCUPIES a position in the chain as an empty 'placeholder'
     entry — no card, no effect, nothing to copy / block / cancel. A grappling_hook
     (or copy_effect) facing a placeholder therefore copies NOTHING (facing an
     empty position = no source to copy). Positions come from the RECORDED
     stopover column (position = 5 - col), so the chain always matches the board
     display and the placeholder actually sits between the plays it displaced.

     engine_version < 17: legacy chain (rooted + plays, dense sequential positions,
     placeholders NOT in the chain) — kept so old games replay exactly as played. """
    def _col_of(stopover):
        m = re.match(r'stopover_(\d+)', stopover or '')
        return int(m.group(1)) if m else None

    rooted = [r for r in (current_game.rooted_on_board or []) if (r or {}).get('owner') == player_name]
    player = current_game.players.get(player_name)

    if (current_game.engine_version or 0) >= 17:
        entries = []
        # leading: the player's OWN rooted cards (from the previous turn)
        for r in rooted:
            col = _col_of(r.get('stopover'))
            if col is None:
                continue
            entries.append({'kind': 'rooted', 'card_id': r.get('card_id'), 'stopover': r.get('stopover'), 'position': 5 - col})
        if player is not None:
            # board-furniture placeholders: EMPTY positions in the chain (v17)
            # entry shapes: v19+ = [card, slot] pair; v18 = bare int or None (a None
            # lab-tap entry = no placeholder, skipped); v16-17 = bare int. A None
            # slot is skipped in every shape.
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

    # legacy (engine_version < 17): rooted + plays, dense sequential positions
    chain = []
    for r in rooted:
        chain.append({'kind': 'rooted', 'card_id': r.get('card_id'), 'stopover': r.get('stopover')})
    if player is not None:
        for action in (player.action_chain or []):
            if not action:
                continue
            if action.get('mode') not in ('move', 'defend'):
                continue
            if not action.get('cards'):
                continue
            chain.append({'kind': 'play', 'action': action, 'stopover': action.get('to')})
    # assign positions 1..N
    for i, entry in enumerate(chain):
        entry['position'] = i + 1
    return chain

def _process_rooted_cards(current_game):
    """ rooted (rule of engine_version 10): end-of-turn placement. Called at the
     start of the next turn (before the action chains are cleared):
     1. last turn's rooted cards (rooted_on_board) have served their one extra
        turn -> they are DISCARDED now (card conservation);
     2. this turn's rooted cards (rooted_this_turn) survive: they are pulled out
        of the discard pile and placed onto FREE stopovers in PLAY ORDER (the
        first rooted card -> stopover_4, the second -> stopover_3, ...) - they
        never share a stopover.

     engine_version 15 (per-player): each player's rooted cards are placed on their
     OWN leading positions (stopover_4, stopover_3, ...) independently of the
     opponent. engine_version 14 (shared skip): the free-stopover computation is
     shared across both players (skipping rooted + dwelling columns). Legacy (v10-13):
     sequential placement on stopover_4..0 in play order. """
    if (current_game.engine_version or 0) < 10:
        return current_game
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
    if (current_game.engine_version or 0) >= 15:
        # v15: per-player placement (each player's rooted cards on their own positions)
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
    elif (current_game.engine_version or 0) >= 14:
        # v14: shared skip (rooted + dwelling columns reserved across both players)
        for i, entry in enumerate(current_game.rooted_this_turn or []):
            card_id = entry.get('card_id')
            owner_name = entry.get('owner')
            owner = current_game.players.get(owner_name)
            if owner is not None and owner.discard and card_id in owner.discard:
                owner.discard.remove(card_id)   # the card survives: pulled out of the discard
            stopover = _next_free_stopover_v14(current_game, i, owner_name)
            current_game.rooted_on_board.append({'card_id': card_id, 'owner': owner_name, 'stopover': stopover})
            print(f'\t\trooted: card {card_id} ({owner_name}) stays on the trip chain -> {stopover}')
    else:
        # legacy (v10-13): sequential placement on stopover_4..0 in play order
        free_stopovers = ['stopover_4', 'stopover_3', 'stopover_2', 'stopover_1', 'stopover_0']
        for i, entry in enumerate(current_game.rooted_this_turn or []):
            card_id = entry.get('card_id')
            owner_name = entry.get('owner')
            owner = current_game.players.get(owner_name)
            if owner is not None and owner.discard and card_id in owner.discard:
                owner.discard.remove(card_id)   # the card survives: pulled out of the discard
            stopover = free_stopovers[i] if i < len(free_stopovers) else f'stopover_extra_{i}'
            current_game.rooted_on_board.append({'card_id': card_id, 'owner': owner_name, 'stopover': stopover})
            print(f'\t\trooted: card {card_id} ({owner_name}) stays on the trip chain -> {stopover}')
    # clear the per-turn buffer
    current_game.rooted_this_turn = []
    return current_game
def _next_free_stopover_v14(current_game, played_count, owner_name):
    """ engine_version 14 ONLY (superseded by the v15 per-player rule): the
     (played_count+1)-th FREE stopover, skipping columns reserved by board furniture
     (rooted-on-board cards + the two players' dwelling placeholders — shared, by
     column). Kept so v14 games replay exactly as they were played. """
    occupied = set()
    for r in (current_game.rooted_on_board or []):
        m = re.match(r'stopover_(\d+)', (r or {}).get('stopover') or '')
        if m:
            occupied.add(int(m.group(1)) % 5)
    for p in current_game.players.values():
        if p.dwelling and p.dwelling_slot is not None:
            occupied.add(int(p.dwelling_slot) % 5)
    free = [c for c in (4, 3, 2, 1, 0) if c not in occupied]
    if not free:
        return f'stopover_{4 - min(played_count, 4)}'
    col = free[played_count] if played_count < len(free) else free[-1]
    return f'stopover_{col}'
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
        if (current_game.board_drops or []):   # engineers' drops (engine_version 12)
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

    if effect == 'discard':   # discard N cards from own hand
        n = min(abs(effect_number), len(player.hand))   # negative value (-1 to -2) -> number of cards
        if n > 0 and (current_game.engine_version or 0) >= 13:
            # discard selection (rule of engine_version 13): the player CHOOSES which
            # N cards to discard. The trip chain PAUSES at the next step boundary
            # (see process_trip_chain); the choice arrives as a to:'discard_pile'
            # message (handle_websocket_message) and the chain resumes.
            current_game.pending_discard = {'player': player.name, 'n': n}
            if log_entry is not None:
                log_entry['_pending_discard'] = n
            print(f'\t\t\teffect discard: PAUSED - {player.name} must choose {n} card(s) to discard')
            return current_game
        # engine_version < 13 (or empty hand): legacy auto-discard (last cards of the hand)
        discarded = player.hand[-n:] if n else []
        for card in discarded:
            player.hand.remove(card)
        if player.discard is None:
            player.discard = []
        player.discard.extend(discarded)
        print(f'\t\t\teffect discard: {player.name} discards {len(discarded)} card(s), hand now {len(player.hand)}')
        return current_game

    if effect == 'discard_oppo':   # opponent discards N cards
        oppo = _get_oppo(player, current_game)
        if oppo is not None and len(oppo.hand) > 0:
            n = min(abs(effect_number), len(oppo.hand))   # -1 -> 1 card
            if n > 0 and (current_game.engine_version or 0) >= 13:
                # discard selection (rule of engine_version 13): the OPPONENT chooses
                # which card(s) to discard; the chain pauses until their choice
                # arrives (to: 'discard_pile') - see process_trip_chain.
                current_game.pending_discard = {'player': oppo.name, 'n': n}
                if log_entry is not None:
                    log_entry['_pending_discard'] = n
                print(f'\t\t\teffect discard_oppo: PAUSED - {oppo.name} must choose {n} card(s) to discard')
                return current_game
            # engine_version < 13: legacy auto-discard (last card(s) of their hand)
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

    # --- rooted (rule of engine_version 10): the token was ALREADY granted by
    #     _resolve_card (the card survives the cleaning phase and stays on a free
    #     stopover). At resolution the card only advances by its basic value, so
    #     this branch is a no-op marker (like grappling_hook / pet_trap). ---
    if effect == 'rooted':
        return current_game

    # --- wrecking_ball (rule of engine_version 11): INSTANT effect - it ALREADY
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

    # --- other effects not implemented yet (swap_cards, ...) ---
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
    # engineers' drops (engine_version 12): fire on ARRIVAL at the landing cell
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
            log_entry['notes'].append('faction biome bonus +1 (token on home biome)')
        advancing_value += 1

    # +1 to move forward, -1 to move backward (single loop handles both directions)
    step = 1 if advancing_value > 0 else -1

    # landmine (engine_version 12): if the player is ALREADY blocked (a previous
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
        # engineers' drops (engine_version 12): boost / trampoline / gluetrap /
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