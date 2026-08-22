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
    cond_cards = ge.CARDS_DB.filter(pl.col('condition') == 'dist_ahead_sup_3')['card_id'].to_list()[:2]
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
    test_reduced_advancing_when_condition_not_met()
    test_full_games()
    print('ALL TESTS PASSED')
