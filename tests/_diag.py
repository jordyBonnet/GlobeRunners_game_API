""" Diagnostic / verification script for the game engine.
Run from project root:  .venv\\Scripts\\python.exe tests/_diag.py """

import os, sys, random, json
# make project root importable regardless of where the script is launched from
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from engine import game_engine as ge
import polars as pl
from models import GameState, PlayerState


# --- unit test of process_advancing ---
def fresh_game():
    p1 = PlayerState(name='A', deck=[f'c{i}' for i in range(15)])
    p2 = PlayerState(name='B', deck=[f'd{i}' for i in range(15)])
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, c, gs = ge.get_current_game(gid)
    conn.close()
    return gs


def test_process_advancing():
    print('=== unit tests: process_advancing ===')
    gs = fresh_game()
    p = gs.players['A']
    assert p.current_position == 0

    ge.process_advancing(5, p, gs)
    print(f'after +5: {p.current_position} | earth[5]: {gs.earth[5]}')
    assert p.current_position == 5 and 'A' in gs.earth[5] and 'A' not in gs.earth[0]

    ge.process_advancing(-3, p, gs)
    print(f'after -3: {p.current_position} | earth[2]: {gs.earth[2]}')
    assert p.current_position == 2 and 'A' in gs.earth[2] and 'A' not in gs.earth[5]

    ge.process_advancing(-10, p, gs)   # should stop at 0
    print(f'after -10 (clamped): {p.current_position}')
    assert p.current_position == 0 and 'A' in gs.earth[0]

    ge.process_advancing(0, p, gs)     # no-op
    assert p.current_position == 0

    # win condition: move from pos 23 -> game over
    gs = fresh_game()
    p = gs.players['A']
    ge.process_advancing(23, p, gs)    # now at position 23
    print(f'at 23: {p.current_position} | state: {gs.state}')
    assert p.current_position == 23 and gs.state != 'game over'

    ge.process_advancing(1, p, gs)     # one more step -> win
    print(f'after +1 from 23: winner = {gs.winner} | state: {gs.state}')
    assert gs.winner == 'A' and gs.state == 'game over'

    print('unit tests PASSED\n')


# --- full game simulation with PlayerAI ---
def make_player(name):
    faction = random.choice(ge.CARDS_DB['faction'].unique().to_list())
    ids = random.sample(ge.CARDS_DB.filter(pl.col('faction') == faction)['card_id'].to_list(), 15)
    return PlayerState(name=name, deck=ids)


def simulate_game(seed):
    import player_ai.playerai as pai

    random.seed(seed)
    p1 = make_player('Anne')
    p2 = make_player('Boris')
    a1 = pai.PlayerAI(p1)
    a2 = pai.PlayerAI(p2)

    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

    def refresh():
        gs = GameState.model_validate(json.loads(ge.get_game(gid)))
        p1 = PlayerState.model_validate(gs.players['Anne'])
        p2 = PlayerState.model_validate(gs.players['Boris'])
        a1.update_player_state(p1, p2, gs)
        a2.update_player_state(p2, p1, gs)
        return gs, p1, p2

    # initial mana phase: each player sends exactly ONE message of 3 cards to mana.
    # (sending a second round would land in the turns phase and be treated as a play action!)
    gs, p1, p2 = refresh()
    for pp, ai in [(p1, a1), (p2, a2)]:
        msg = ai.put_mana(num_cards=3)
        if isinstance(msg, list):
            pp.message = {'cards': msg, 'to': 'mana', 'mode': '', 'pendings': []}
        ge.handle_websocket_message(game_id=gid, player=pp)

    # main game loop
    actions = 0
    turn_history = []   # (turn, day_night) to verify day/night flips each turn
    while True:
        gs, p1, p2 = refresh()
        if not turn_history or turn_history[-1][0] != gs.turn:
            turn_history.append((gs.turn, gs.day_night))
        if 'game over' in gs.state:
            for (t1, dn1), (t2, dn2) in zip(turn_history, turn_history[1:]):
                assert t2 == t1 + 1 and dn1 != dn2, f'day/night should flip each turn: {turn_history}'
            return f"GAME OVER after {actions} actions - winner: {gs.winner}"
        if actions > 500:
            return f'STALLED at action {actions}'

        if gs.state == 'waiting for both players to mana or pass':
            for pp, ai in [(p1, a1), (p2, a2)]:
                pp.message = ai.put_mana(num_cards=1, in_turn=True)
                ge.handle_websocket_message(game_id=gid, player=pp)
            continue

        if f'({p1.name}) to play' in gs.state:
            pp, ai = p1, a1
        elif f'({p2.name}) to play' in gs.state:
            pp, ai = p2, a2
        else:
            return 'UNEXPECTED STATE: ' + gs.state

        pp.message = ai.play_card()
        ge.handle_websocket_message(game_id=gid, player=pp)
        actions += 1


def test_reduced_advancing_when_condition_not_met():
    print('=== unit tests: reduced advancing when condition not met ===')
    import polars as pl

    # build a game where A's deck contains two real dist_ahead_sup_3 cards (fresh_game uses fake ids)
    # exclude movement-effect cards so expected advancing stays simply the base value
    cond_cards = ge.CARDS_DB.filter(
        (pl.col('condition') == 'dist_ahead_sup_3') & (~pl.col('effect').is_in(['advancing', 'backward']))
    )['card_id'].to_list()[:2]
    filler = [c for c in ge.CARDS_DB['card_id'].to_list() if c not in cond_cards][:13]
    p1 = PlayerState(name='A', deck=cond_cards + filler)
    p2 = PlayerState(name='B', deck=[f'x{i}' for i in range(15)])
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, c, gs = ge.get_current_game(gid)
    conn.close()
    a, b = gs.players['A'], gs.players['B']

    row = ge.CARDS_DB.filter(pl.col('card_id') == cond_cards[0]).row(0, named=True)

    # both at position 0 -> condition NOT met -> advancing should be mana - 1
    msg = {'cards': [cond_cards[0]], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    expected = row['mana'] - 1
    print(f"card {row['card_id']} (adv={row['advancing']}, mana={row['mana']}), condition not met -> position: {a.current_position}")
    assert a.current_position == expected, f'expected {expected}, got {a.current_position}'

    # now make the condition met (move A ahead of B by > 3) and play the second such card
    ge.process_advancing(5, a, gs)   # gap = (mana-1)+5 - 0 > 3
    row2 = ge.CARDS_DB.filter(pl.col('card_id') == cond_cards[1]).row(0, named=True)
    pos_before = a.current_position
    msg2 = {'cards': [cond_cards[1]], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg2, a, gs)
    print(f"condition met -> position: {a.current_position}")
    assert a.current_position == pos_before + row2['advancing'], \
        f'expected full advancing ({pos_before}+{row2["advancing"]}), got {a.current_position}'

    print('reduced advancing tests PASSED\n')


def test_temperature_and_day_night():
    print('=== unit tests: temperature & day/night init ===')
    # roll_temperature: always in 1..20, and the kept value is one of the two rolls closest to 10
    for _ in range(500):
        t = ge.roll_temperature()
        assert 1 <= t <= 20

    gs = fresh_game()
    print(f'new game: temperature = {gs.temperature}, day_night = {gs.day_night}')
    assert gs.temperature is not None and 1 <= gs.temperature <= 20
    assert gs.day_night in ('day', 'night')

    # distribution sanity check: over many rolls, values should cluster around 10 (no extreme-only results)
    import collections
    counts = collections.Counter(ge.roll_temperature() for _ in range(4000))
    assert counts[10] > 0 and counts[9] + counts[10] + counts[11] > len(counts) * 0.3, 'temperature should cluster near 10'

    # temperature conditions (evaluated against current_game.temperature)
    a = gs.players['A']
    gs.temperature = 5
    assert ge.is_condition_met('temp_inf_6', a, gs) is True     # 5 < 6
    assert ge.is_condition_met('temp_sup_9', a, gs) is False
    gs.temperature = 10
    assert ge.is_condition_met('temp_inf_6', a, gs) is False    # 10 < 6 false
    assert ge.is_condition_met('temp_sup_9', a, gs) is True     # 10 > 9
    assert ge.is_condition_met('temp_inf_11', a, gs) is True    # 10 < 11
    assert ge.is_condition_met('temp_sup_15', a, gs) is False
    gs.temperature = 16
    assert ge.is_condition_met('temp_sup_15', a, gs) is True

    # day/night conditions (game always starts on "day")
    assert gs.day_night == 'day'
    assert ge.is_condition_met('day', a, gs) is True
    assert ge.is_condition_met('night', a, gs) is False
    gs.day_night = 'night'
    assert ge.is_condition_met('day', a, gs) is False
    assert ge.is_condition_met('night', a, gs) is True

    print('temperature/day-night tests PASSED\n')


def test_movement_effects():
    print('=== unit tests: advancing & backward effects ===')
    import polars as pl

    # pick real no_condition cards: one with effect=advancing, one with effect=backward
    adv_card = ge.CARDS_DB.filter((pl.col('effect') == 'advancing') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    back_card = ge.CARDS_DB.filter((pl.col('effect') == 'backward') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    filler = [c for c in ge.CARDS_DB['card_id'].to_list() if c not in (adv_card['card_id'], back_card['card_id'])][:13]

    p1 = PlayerState(name='A', deck=[adv_card['card_id'], back_card['card_id']] + filler)
    p2 = PlayerState(name='B', deck=[f'x{i}' for i in range(15)])
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, c, gs = ge.get_current_game(gid)
    conn.close()
    a = gs.players['A']

    # effect=advancing: total movement = base advancing + effect_number bonus
    msg = {'cards': [adv_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    expected = adv_card['advancing'] + adv_card['effect_number']
    print(f"advancing effect card (base={adv_card['advancing']}, bonus={adv_card['effect_number']}) -> position: {a.current_position}")
    assert a.current_position == expected, f'expected {expected}, got {a.current_position}'

    # effect=backward: move forward base advancing, then recoil by effect_number (negative)
    pos_before = a.current_position
    msg2 = {'cards': [back_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg2, a, gs)
    expected2 = pos_before + back_card['advancing'] + back_card['effect_number']
    print(f"backward effect card (base={back_card['advancing']}, recoil={back_card['effect_number']}) -> position: {a.current_position}")
    assert a.current_position == expected2, f'expected {expected2}, got {a.current_position}'

    # no recoil after crossing the finish line: put A at 23 and play a backward card with base >= 1
    gs = fresh_game()
    a = gs.players['A']
    ge.process_advancing(23, a, gs)   # at position 23, one step away from winning
    msg3 = {'cards': [back_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg3, a, gs)
    print(f"backward card at position 23 -> winner: {gs.winner}, state: {gs.state}, position: {a.current_position}")
    assert gs.state == 'game over' and gs.winner == 'A'
    assert a.current_position == 23   # no recoil applied after winning

    print('movement effects tests PASSED\n')


def test_oppo_movement_effects():
    print('=== unit tests: advancing_oppo & backward_oppo effects ===')
    import polars as pl

    # pick real no_condition cards for both opponent-movement effects
    adv_oppo_card = ge.CARDS_DB.filter((pl.col('effect') == 'advancing_oppo') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    back_oppo_card = ge.CARDS_DB.filter((pl.col('effect') == 'backward_oppo') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    filler = [c for c in ge.CARDS_DB['card_id'].to_list() if c not in (adv_oppo_card['card_id'], back_oppo_card['card_id'])][:13]

    p1 = PlayerState(name='A', deck=[adv_oppo_card['card_id'], back_oppo_card['card_id']] + filler)
    p2 = PlayerState(name='B', deck=[f'x{i}' for i in range(15)])
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, c, gs = ge.get_current_game(gid)
    conn.close()
    a, b = gs.players['A'], gs.players['B']

    # effect=advancing_oppo: A moves its base advancing, B is pushed forward by effect_number
    msg = {'cards': [adv_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    print(f"advancing_oppo card (A base={adv_oppo_card['advancing']}, oppo push={adv_oppo_card['effect_number']}): A at {a.current_position}, B at {b.current_position}")
    assert a.current_position == adv_oppo_card['advancing'], f'A should move its base advancing, got {a.current_position}'
    assert b.current_position == adv_oppo_card['effect_number'], f'B should be pushed by effect_number, got {b.current_position}'

    # effect=backward_oppo: A moves its base advancing, B recoils by 1 (clamped at position 0)
    pos_a_before = a.current_position
    pos_b_before = b.current_position
    msg2 = {'cards': [back_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg2, a, gs)
    expected_b = max(pos_b_before + back_oppo_card['effect_number'], 0)   # recoil clamped at position 0
    print(f"backward_oppo card (A base={back_oppo_card['advancing']}, oppo recoil={back_oppo_card['effect_number']}): A at {a.current_position}, B at {b.current_position}")
    assert a.current_position == pos_a_before + back_oppo_card['advancing'], f'A should move its base advancing, got {a.current_position}'
    assert b.current_position == expected_b, f'B should recoil (clamped at 0), got {b.current_position}, expected {expected_b}'

    # B not at 0: verify a real recoil of exactly 1 cell
    gs = fresh_game()
    a, b = gs.players['A'], gs.players['B']
    ge.process_advancing(3, b, gs)   # B at position 3
    msg3 = {'cards': [back_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg3, a, gs)
    print(f"backward_oppo with B at 3: A at {a.current_position}, B at {b.current_position}")
    assert b.current_position == 2, f'B should recoil from 3 to 2, got {b.current_position}'

    print('oppo movement effects tests PASSED\n')


def test_draw_discard_effects():
    print('=== unit tests: draw / discard effects ===')
    import polars as pl

    # pick real no_condition cards for the four card effects
    draw_card = ge.CARDS_DB.filter((pl.col('effect') == 'draw') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    discard_card = ge.CARDS_DB.filter((pl.col('effect') == 'discard') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    draw_oppo_card = ge.CARDS_DB.filter((pl.col('effect') == 'draw_oppo') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    discard_oppo_card = ge.CARDS_DB.filter((pl.col('effect') == 'discard_oppo') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    all_ids = {draw_card['card_id'], discard_card['card_id'], draw_oppo_card['card_id'], discard_oppo_card['card_id']}
    filler = [c for c in ge.CARDS_DB['card_id'].to_list() if c not in all_ids][:11]

    p1 = PlayerState(name='A', deck=[draw_card['card_id'], discard_card['card_id'], draw_oppo_card['card_id'], discard_oppo_card['card_id']] + filler)
    p2 = PlayerState(name='B', deck=[f'x{i}' for i in range(15)])
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, c, gs = ge.get_current_game(gid)
    conn.close()
    a, b = gs.players['A'], gs.players['B']

    # effect=draw: A's hand grows by effect_number, deck shrinks by the same amount (base advancing still applied)
    n_draw = abs(draw_card['effect_number'])
    hand_a0, deck_a0 = len(a.hand), len(a.deck)
    msg = {'cards': [draw_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    print(f"draw card (N={n_draw}): A hand {hand_a0} -> {len(a.hand)}, deck {deck_a0} -> {len(a.deck)}")
    assert len(a.hand) == hand_a0 + n_draw and len(a.deck) == deck_a0 - n_draw

    # effect=discard: A's hand shrinks by |effect_number|, cards land in discard pile (played card already there)
    n_disc = abs(discard_card['effect_number'])
    hand_a1 = len(a.hand)
    msg2 = {'cards': [discard_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg2, a, gs)
    print(f"discard card (N={n_disc}): A hand {hand_a1} -> {len(a.hand)}, discard pile: {len(a.discard)}")
    assert len(a.hand) == hand_a1 - n_disc
    assert len(a.discard) == 2 + n_disc   # played draw card + played discard card + discarded cards

    # effect=draw_oppo (fatigue): B's hand grows, B's deck shrinks; A still moves its base advancing
    n_draw_o = abs(draw_oppo_card['effect_number'])
    pos_a2 = a.current_position
    hand_b0, deck_b0 = len(b.hand), len(b.deck)
    msg3 = {'cards': [draw_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg3, a, gs)
    print(f"draw_oppo card (N={n_draw_o}): B hand {hand_b0} -> {len(b.hand)}, deck {deck_b0} -> {len(b.deck)}; A at {a.current_position}")
    assert len(b.hand) == hand_b0 + n_draw_o and len(b.deck) == deck_b0 - n_draw_o
    assert a.current_position == pos_a2 + draw_oppo_card['advancing']   # own base advancing still applied

    # effect=discard_oppo: B's hand shrinks by 1, card lands in B's discard pile
    hand_b1 = len(b.hand)
    msg4 = {'cards': [discard_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg4, a, gs)
    print(f"discard_oppo card: B hand {hand_b1} -> {len(b.hand)}, B discard pile: {len(b.discard)}")
    assert len(b.hand) == hand_b1 - 1
    assert len(b.discard) == 1   # only the discarded card (B never played anything)

    # reshuffle when deck runs out: empty deck + cards in discard -> draw from reshuffled pile, stops when exhausted
    gs = fresh_game()
    a = gs.players['A']
    hand_a2 = len(a.hand)
    a.deck = []
    a.discard = ['r1', 'r2', 'r3']
    drawn = ge._draw_cards(a, 5)   # only 3 available after reshuffle
    print(f'reshuffle draw: drew {drawn}/3, hand {hand_a2} -> {len(a.hand)}, deck: {a.deck}, discard: {a.discard}')
    assert drawn == 3 and len(a.hand) == hand_a2 + 3 and a.deck == [] and a.discard == []

    print('draw/discard effects tests PASSED\n')


def test_resource_effects():
    print('=== unit tests: ramp / taxation effects ===')
    import polars as pl

    # pick real no_condition cards for the four resource effects
    ramp_card = ge.CARDS_DB.filter((pl.col('effect') == 'ramp') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    tax_card = ge.CARDS_DB.filter((pl.col('effect') == 'taxation') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    ramp_oppo_card = ge.CARDS_DB.filter((pl.col('effect') == 'ramp_oppo') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    tax_oppo_card = ge.CARDS_DB.filter((pl.col('effect') == 'taxation_oppo') & (pl.col('condition') == 'no_condition')).row(0, named=True)
    all_ids = {ramp_card['card_id'], tax_card['card_id'], ramp_oppo_card['card_id'], tax_oppo_card['card_id']}
    filler = [c for c in ge.CARDS_DB['card_id'].to_list() if c not in all_ids][:11]

    p1 = PlayerState(name='A', deck=[ramp_card['card_id'], tax_card['card_id'], ramp_oppo_card['card_id'], tax_oppo_card['card_id']] + filler)
    p2 = PlayerState(name='B', deck=[f'x{i}' for i in range(15)])
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, c, gs = ge.get_current_game(gid)
    conn.close()
    a, b = gs.players['A'], gs.players['B']

    # effect=ramp: N cards from A's deck to A's mana zone (hand untouched, base advancing still applied)
    n_ramp = abs(ramp_card['effect_number'])
    deck_a0, mana_a0, hand_a0 = len(a.deck), len(a.mana), len(a.hand)
    msg = {'cards': [ramp_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    print(f"ramp card (N={n_ramp}): A deck {deck_a0} -> {len(a.deck)}, mana {mana_a0} -> {len(a.mana)}, hand {hand_a0} -> {len(a.hand)}")
    assert len(a.mana) == mana_a0 + n_ramp and len(a.deck) == deck_a0 - n_ramp
    assert len(a.hand) == hand_a0   # ramp does not touch the hand
    assert a.current_position == ramp_card['advancing']   # base advancing still applied

    # effect=taxation: N cards from A's mana zone to A's discard pile (played card already in discard)
    n_tax = abs(tax_card['effect_number'])
    for _ in range(3):   # simulate the initial mana phase: put 3 hand cards into mana
        a.mana.append(a.hand.pop())
    mana_a1, disc_a0 = len(a.mana), len(a.discard)
    msg2 = {'cards': [tax_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg2, a, gs)
    print(f"taxation card (N={n_tax}): A mana {mana_a1} -> {len(a.mana)}, discard {disc_a0} -> {len(a.discard)}")
    assert len(a.mana) == mana_a1 - n_tax
    assert len(a.discard) == disc_a0 + 1 + n_tax   # played card + taxed cards

    # effect=ramp_oppo: 1 card from B's deck to B's mana zone; A still moves its base advancing
    pos_a2 = a.current_position
    deck_b0, mana_b0 = len(b.deck), len(b.mana)
    msg3 = {'cards': [ramp_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg3, a, gs)
    print(f"ramp_oppo card: B deck {deck_b0} -> {len(b.deck)}, mana {mana_b0} -> {len(b.mana)}; A at {a.current_position}")
    assert len(b.mana) == mana_b0 + 1 and len(b.deck) == deck_b0 - 1
    assert a.current_position == pos_a2 + ramp_oppo_card['advancing']   # own base advancing still applied

    # effect=taxation_oppo: 1 card from B's mana zone to B's discard pile
    disc_b0 = len(b.discard)
    msg4 = {'cards': [tax_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg4, a, gs)
    print(f"taxation_oppo card: B mana {len(b.mana)}, discard {disc_b0} -> {len(b.discard)}")
    assert len(b.mana) == 0
    assert len(b.discard) == disc_b0 + 1   # only the taxed card (B never played anything)

    # edge cases: ramp reshuffles an empty deck, stops when exhausted; taxation on empty mana is a no-op
    gs = fresh_game()
    a = gs.players['A']
    a.deck = []
    a.discard = ['r1', 'r2']
    moved = ge._ramp_mana(a, 3)   # only 2 available after reshuffle
    print(f'ramp edge case: moved {moved}/2, mana: {a.mana}, deck: {a.deck}')
    assert moved == 2 and len(a.mana) == 2 and a.deck == [] and a.discard == []
    taxed = ge._tax_mana(gs.players['B'], 1)   # B has empty mana zone
    print(f'taxation edge case: taxed {taxed}/0 from empty mana')
    assert taxed == 0

    print('resource effects tests PASSED\n')


def test_jump_effect():
    print('=== unit tests: jump effect ===')
    import polars as pl

    # pick a real no_condition jump card with advancing >= 4 (so it can skip over an intermediate cell)
    jump_card = ge.CARDS_DB.filter((pl.col('effect') == 'jump') & (pl.col('condition') == 'no_condition') & (pl.col('advancing') >= 4)).row(0, named=True)
    filler = [c for c in ge.CARDS_DB['card_id'].to_list() if c != jump_card['card_id']][:14]

    p1 = PlayerState(name='A', deck=[jump_card['card_id']] + filler)
    p2 = PlayerState(name='B', deck=[f'x{i}' for i in range(15)])
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, c, gs = ge.get_current_game(gid)
    conn.close()
    a = gs.players['A']

    # spy on apply_effect to see which trap/drop checks fire during the move
    calls = []
    orig_apply = ge.apply_effect
    def spy(effect, *args):
        calls.append(effect)
        return orig_apply(effect, *args)

    # jump over an intermediate 'trap' cell: it must NOT be triggered
    gs.earth[2].append('trap')   # trap on cell 2 (intermediate for any jump of >= 4 from position 0)
    n_adv = jump_card['advancing']
    msg = {'cards': [jump_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.apply_effect = spy
    try:
        ge.process_card(msg, a, gs)
    finally:
        ge.apply_effect = orig_apply
    print(f"jump card (adv={n_adv}): A at {a.current_position}, effect calls: {calls}")
    assert a.current_position == n_adv, f'A should land directly on cell {n_adv}, got {a.current_position}'
    assert 'trap' not in calls, f'intermediate trap should be skipped by jump, got calls {calls}'

    # contrast: normal movement over the same intermediate trap DOES trigger it
    gs = fresh_game()
    a = gs.players['A']
    gs.earth[2].append('trap')
    calls.clear()
    ge.apply_effect = spy
    try:
        ge.process_advancing(3, a, gs)   # passes through cell 2 on the way to 3
    finally:
        ge.apply_effect = orig_apply
    print(f"normal +3 over trap at cell 2: A at {a.current_position}, effect calls: {calls}")
    assert 'trap' in calls, f'normal movement should trigger intermediate traps, got calls {calls}'

    # landing cell trap IS triggered by jump (you land there)
    gs = fresh_game()
    a = gs.players['A']
    gs.earth[3].append('trap')
    calls.clear()
    ge.apply_effect = spy
    try:
        ge._jump(a, 3, gs)   # jump directly onto the trap cell
    finally:
        ge.apply_effect = orig_apply
    print(f"jump onto trap at landing cell 3: A at {a.current_position}, effect calls: {calls}")
    assert a.current_position == 3 and 'trap' in calls, f'landing trap should be triggered, got position {a.current_position}, calls {calls}'

    # jump can win the game: from position 22, a jump of >= 2 lands past the finish line (24)
    gs = fresh_game()
    a = gs.players['A']
    ge.process_advancing(22, a, gs)   # at position 22
    msg2 = {'cards': [jump_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg2, a, gs)
    print(f"jump from 22 (adv={n_adv}): winner: {gs.winner}, state: {gs.state}")
    assert gs.state == 'game over' and gs.winner == 'A'

    print('jump effect tests PASSED\n')


def test_full_games():
    print('=== full game simulations (3 seeds) ===')
    for seed in range(3):
        result = simulate_game(seed)
        print(f'seed {seed}: {result}')
        assert 'GAME OVER' in result, f'Game with seed {seed} did not finish: {result}'
    print('simulations PASSED\n')


def test_distance_conditions():
    print('=== unit tests: distance conditions ===')
    gs = fresh_game()
    a, b = gs.players['A'], gs.players['B']

    # both at position 0 -> gap 0, nothing met
    assert ge.is_condition_met('dist_ahead_sup_1', a, gs) is False
    assert ge.is_condition_met('dist_behind_sup_1', a, gs) is False

    ge.process_advancing(2, a, gs)   # A at 2, B at 0 -> gap +2 for A
    assert ge.is_condition_met('dist_ahead_sup_1', a, gs) is True     # A leads by 2 > 1
    assert ge.is_condition_met('dist_ahead_sup_3', a, gs) is False    # 2 is not > 3
    assert ge.is_condition_met('dist_behind_sup_1', b, gs) is True    # B trails by 2 > 1
    assert ge.is_condition_met('dist_behind_sup_3', b, gs) is False

    ge.process_advancing(2, a, gs)   # A at 4 -> gap +4
    assert ge.is_condition_met('dist_ahead_sup_3', a, gs) is True
    assert ge.is_condition_met('dist_behind_sup_3', b, gs) is True

    print('distance tests PASSED\n')


def test_oppo_mana_conditions():
    print('=== unit tests: oppo mana conditions ===')
    gs = fresh_game()
    a, b = gs.players['A'], gs.players['B']

    # both start with 0 cards in mana -> inf_6 met for both, sup_5 not met
    assert ge.is_condition_met('mana_inf_6_oppo', a, gs) is True   # B has 0 < 6
    assert ge.is_condition_met('mana_sup_5_oppo', a, gs) is False

    b.mana = ['m1', 'm2', 'm3']
    assert ge.is_condition_met('mana_inf_6_oppo', a, gs) is True   # 3 < 6
    assert ge.is_condition_met('mana_sup_5_oppo', a, gs) is False

    b.mana = ['m1', 'm2', 'm3', 'm4', 'm5']                        # exactly 5: inf_6 met (5<6), sup_5 not (5>5 false)
    assert ge.is_condition_met('mana_inf_6_oppo', a, gs) is True
    assert ge.is_condition_met('mana_sup_5_oppo', a, gs) is False

    b.mana = ['m1', 'm2', 'm3', 'm4', 'm5', 'm6']                  # 6: inf_6 not met (6<6 false), sup_5 met
    assert ge.is_condition_met('mana_inf_6_oppo', a, gs) is False
    assert ge.is_condition_met('mana_sup_5_oppo', a, gs) is True

    print('oppo mana tests PASSED\n')


def test_hand_conditions():
    print('=== unit tests: hand conditions ===')
    gs = fresh_game()
    a, b = gs.players['A'], gs.players['B']

    # both start with 6 cards in hand -> inf_4 not met (6<4 false), sup_3 met (6>3 true)
    assert ge.is_condition_met('cards_in_hand_inf_4', a, gs) is False
    assert ge.is_condition_met('cards_in_hand_sup_3', a, gs) is True

    # shrink A's hand to 3 -> inf_4 met (3<4), sup_3 not met (3>3 false)
    a.hand = ['h1', 'h2', 'h3']
    assert ge.is_condition_met('cards_in_hand_inf_4', a, gs) is True
    assert ge.is_condition_met('cards_in_hand_sup_3', a, gs) is False

    # opponent hand: B still has 6 -> from A's view inf_4_oppo not met, sup_3_oppo met
    assert ge.is_condition_met('cards_in_hand_inf_4_oppo', a, gs) is False
    assert ge.is_condition_met('cards_in_hand_sup_3_oppo', a, gs) is True

    b.hand = ['h1']   # B has 1 -> inf_4_oppo met (1<4), sup_3_oppo not met (1>3 false)
    assert ge.is_condition_met('cards_in_hand_inf_4_oppo', a, gs) is True
    assert ge.is_condition_met('cards_in_hand_sup_3_oppo', a, gs) is False

    print('hand tests PASSED\n')


if __name__ == '__main__':
    test_process_advancing()
    test_distance_conditions()
    test_oppo_mana_conditions()
    test_hand_conditions()
    test_temperature_and_day_night()
    test_movement_effects()
    test_oppo_movement_effects()
    test_draw_discard_effects()
    test_resource_effects()
    test_jump_effect()
    test_reduced_advancing_when_condition_not_met()
    test_full_games()
    print('ALL TESTS PASSED')
