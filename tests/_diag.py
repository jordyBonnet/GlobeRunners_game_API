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


def _bonus(p, gs):
    """ +1 if p stands on one of its two faction biomes (engine rule), else 0.
    Must be captured BEFORE the move (the bonus depends on the starting cell). """
    return 1 if ge._on_home_biome(p, gs) else 0


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

    # both at position 0 -> condition NOT met -> advancing should be mana - 1 (+1 if A is on a home biome)
    bonus1 = _bonus(a, gs)
    msg = {'cards': [cond_cards[0]], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    base = row['mana'] - 1
    expected = base + (bonus1 if base > 0 else 0)
    print(f"card {row['card_id']} (adv={row['advancing']}, mana={row['mana']}), condition not met -> position: {a.current_position}")
    assert a.current_position == expected, f'expected {expected}, got {a.current_position}'

    # now make the condition met (move A ahead of B by > 3) and play the second such card
    ge.process_advancing(5, a, gs)   # gap = (mana-1)+5 - 0 > 3
    row2 = ge.CARDS_DB.filter(pl.col('card_id') == cond_cards[1]).row(0, named=True)
    pos_before = a.current_position
    bonus2 = _bonus(a, gs)
    msg2 = {'cards': [cond_cards[1]], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg2, a, gs)
    expected2 = pos_before + row2['advancing'] + (bonus2 if row2['advancing'] > 0 else 0)
    print(f"condition met -> position: {a.current_position}")
    assert a.current_position == expected2, f'expected {expected2}, got {a.current_position}'

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

    # effect=advancing: total movement = base advancing + effect_number bonus (+1 if on a home biome)
    bonus1 = _bonus(a, gs)
    msg = {'cards': [adv_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    base = adv_card['advancing'] + adv_card['effect_number']
    expected = base + (bonus1 if base > 0 else 0)
    print(f"advancing effect card (base={adv_card['advancing']}, bonus={adv_card['effect_number']}) -> position: {a.current_position}")
    assert a.current_position == expected, f'expected {expected}, got {a.current_position}'

    # effect=backward: move forward base advancing (+1 if on a home biome), then recoil by effect_number (negative)
    pos_before = a.current_position
    bonus2 = _bonus(a, gs)
    msg2 = {'cards': [back_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg2, a, gs)
    expected2 = pos_before + back_card['advancing'] + (bonus2 if back_card['advancing'] > 0 else 0) + back_card['effect_number']
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

    # effect=advancing_oppo: A moves its base advancing (+1 if on a home biome), B is pushed forward by effect_number
    bonus1 = _bonus(a, gs)
    msg = {'cards': [adv_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    expected_a = adv_oppo_card['advancing'] + (bonus1 if adv_oppo_card['advancing'] > 0 else 0)
    print(f"advancing_oppo card (A base={adv_oppo_card['advancing']}, oppo push={adv_oppo_card['effect_number']}): A at {a.current_position}, B at {b.current_position}")
    assert a.current_position == expected_a, f'A should move its base advancing, got {a.current_position}'
    assert b.current_position == adv_oppo_card['effect_number'], f'B should be pushed by effect_number, got {b.current_position}'

    # effect=backward_oppo: A moves its base advancing (+1 if on a home biome), B recoils by 1 (clamped at position 0)
    pos_a_before = a.current_position
    pos_b_before = b.current_position
    bonus2 = _bonus(a, gs)
    msg2 = {'cards': [back_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg2, a, gs)
    expected_b = max(pos_b_before + back_oppo_card['effect_number'], 0)   # recoil clamped at position 0
    expected_a2 = pos_a_before + back_oppo_card['advancing'] + (bonus2 if back_oppo_card['advancing'] > 0 else 0)
    print(f"backward_oppo card (A base={back_oppo_card['advancing']}, oppo recoil={back_oppo_card['effect_number']}): A at {a.current_position}, B at {b.current_position}")
    assert a.current_position == expected_a2, f'A should move its base advancing, got {a.current_position}'
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


def _apply_pending_discard(gs):
    """ v13 (discard selection): process_card PAUSED on pending_discard instead of
    auto-discarding -> apply the choice (the last n cards, like the legacy
    auto-discard) and clear the pause marker, exactly like
    handle_websocket_message does for a to:'discard_pile' message. """
    pd = gs.pending_discard
    if pd is None:
        return
    t = gs.players[pd['player']]
    n = min(pd['n'], len(t.hand or []))
    chosen = (t.hand or [])[-n:]
    for c in chosen:
        t.hand.remove(c)
    t.discard = (t.discard or []) + chosen
    gs.pending_discard = None


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
    _apply_pending_discard(gs)   # v13: the engine paused on the discard selection
    print(f"discard card (N={n_disc}): A hand {hand_a1} -> {len(a.hand)}, discard pile: {len(a.discard)}")
    assert len(a.hand) == hand_a1 - n_disc
    assert len(a.discard) == 2 + n_disc   # played draw card + played discard card + discarded cards

    # effect=draw_oppo (fatigue): B's hand grows, B's deck shrinks; A still moves its base advancing (+1 if on a home biome)
    n_draw_o = abs(draw_oppo_card['effect_number'])
    pos_a2 = a.current_position
    bonus_o = _bonus(a, gs)
    hand_b0, deck_b0 = len(b.hand), len(b.deck)
    msg3 = {'cards': [draw_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg3, a, gs)
    print(f"draw_oppo card (N={n_draw_o}): B hand {hand_b0} -> {len(b.hand)}, deck {deck_b0} -> {len(b.deck)}; A at {a.current_position}")
    assert len(b.hand) == hand_b0 + n_draw_o and len(b.deck) == deck_b0 - n_draw_o
    assert a.current_position == pos_a2 + draw_oppo_card['advancing'] + (bonus_o if draw_oppo_card['advancing'] > 0 else 0)   # own base advancing still applied

    # effect=discard_oppo: B's hand shrinks by 1, card lands in B's discard pile
    hand_b1 = len(b.hand)
    msg4 = {'cards': [discard_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg4, a, gs)
    _apply_pending_discard(gs)   # v13: the engine paused on the discard selection
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

    # effect=ramp: N cards from A's deck to A's mana zone (hand untouched, base advancing still applied, +1 if on a home biome)
    n_ramp = abs(ramp_card['effect_number'])
    deck_a0, mana_a0, hand_a0 = len(a.deck), len(a.mana), len(a.hand)
    bonus1 = _bonus(a, gs)
    msg = {'cards': [ramp_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    print(f"ramp card (N={n_ramp}): A deck {deck_a0} -> {len(a.deck)}, mana {mana_a0} -> {len(a.mana)}, hand {hand_a0} -> {len(a.hand)}")
    assert len(a.mana) == mana_a0 + n_ramp and len(a.deck) == deck_a0 - n_ramp
    assert len(a.hand) == hand_a0   # ramp does not touch the hand
    assert a.current_position == ramp_card['advancing'] + (bonus1 if ramp_card['advancing'] > 0 else 0)   # base advancing still applied

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

    # effect=ramp_oppo: 1 card from B's deck to B's mana zone; A still moves its base advancing (+1 if on a home biome)
    pos_a2 = a.current_position
    bonus_o = _bonus(a, gs)
    deck_b0, mana_b0 = len(b.deck), len(b.mana)
    msg3 = {'cards': [ramp_oppo_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.process_card(msg3, a, gs)
    print(f"ramp_oppo card: B deck {deck_b0} -> {len(b.deck)}, mana {mana_b0} -> {len(b.mana)}; A at {a.current_position}")
    assert len(b.mana) == mana_b0 + 1 and len(b.deck) == deck_b0 - 1
    assert a.current_position == pos_a2 + ramp_oppo_card['advancing'] + (bonus_o if ramp_oppo_card['advancing'] > 0 else 0)   # own base advancing still applied

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
    bonus1 = _bonus(a, gs)   # the faction biome bonus can add +1 to the jump distance
    msg = {'cards': [jump_card['card_id']], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
    ge.apply_effect = spy
    try:
        ge.process_card(msg, a, gs)
    finally:
        ge.apply_effect = orig_apply
    expected = n_adv + bonus1
    print(f"jump card (adv={n_adv}, bonus={bonus1}): A at {a.current_position}, effect calls: {calls}")
    assert a.current_position == expected, f'A should land directly on cell {expected}, got {a.current_position}'
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


def test_faction_biome_bonus():
    print('=== unit tests: faction biome bonus (+1 on home biome) ===')
    import polars as pl

    faction = 'Dwarves'   # home biomes: MO and OC
    home_biome = ge.FACTION_BIOMES[faction][0]
    foreign_biome = next(b for b in ge.BIOMES if b not in ge.FACTION_BIOMES[faction])
    ids = ge.CARDS_DB.filter(pl.col('faction') == faction)['card_id'].to_list()

    def make_game():
        p1 = PlayerState(name='A', deck=ids[:15])
        p2 = PlayerState(name='B', deck=[f'x{i}' for i in range(15)])
        gid = ge.create_new_game(player=p1.model_dump())
        ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
        conn, c, gs = ge.get_current_game(gid)
        conn.close()
        return gs, gs.players['A']

    # the player's faction is resolved from its deck
    gs, a = make_game()
    assert ge._player_faction(a) == faction

    # on a home biome: +1 to forward advancing
    gs.earth[a.current_position][0] = home_biome
    assert ge._on_home_biome(a, gs) is True
    ge.process_advancing(3, a, gs)
    print(f'on home biome: advanced 3 -> position {a.current_position} (expected 4)')
    assert a.current_position == 4

    # on a foreign biome: no bonus
    gs, a = make_game()
    gs.earth[a.current_position][0] = foreign_biome
    assert ge._on_home_biome(a, gs) is False
    ge.process_advancing(3, a, gs)
    print(f'on foreign biome: advanced 3 -> position {a.current_position} (expected 3)')
    assert a.current_position == 3

    # backward movement is never boosted
    gs, a = make_game()
    gs.earth[a.current_position][0] = foreign_biome
    ge.process_advancing(5, a, gs)            # A at position 5 (no bonus)
    gs.earth[a.current_position][0] = home_biome
    assert ge._on_home_biome(a, gs) is True
    ge.process_advancing(-2, a, gs)           # recoil on home biome: no bonus
    print(f'recoil on home biome: -2 -> position {a.current_position} (expected 3)')
    assert a.current_position == 3

    print('faction biome bonus tests PASSED\n')


def test_block_card_effect():
    print('=== unit tests: block card effect (fires only when a block happens) ===')
    import polars as pl

    # a dedicated block card (condition == 'block'): shield 3, effect 'draw' +1
    block_id = 'Dwa33_79a683'
    block_row = ge.CARDS_DB.filter(pl.col('card_id') == block_id).row(0, named=True)
    assert block_row['condition'] == 'block' and block_row['shield'] == 3
    assert block_row['effect'] == 'draw' and block_row['effect_number'] == 1

    # mover cards with a no-op effect (not implemented in the engine): position = plain advancing
    noop = ['avalanche', 'copy_effect', 'effect_canceled', 'grappling_hook',
            'pet_trap', 'rooted', 'swap_cards', 'wrecking_ball']
    pool = ge.CARDS_DB.filter((pl.col('condition') == 'no_condition')
                              & pl.col('effect').is_in(noop) & (pl.col('advancing') > 0))
    low = pool.filter(pl.col('mana') <= block_row['shield']).sort('mana').row(0, named=True)
    high = pool.filter(pl.col('mana') > block_row['shield']).sort('mana').row(0, named=True)

    def setup():
        filler = [c for c in ge.CARDS_DB['card_id'].to_list()
                  if c not in (low['card_id'], high['card_id'], block_id)][:12]
        p1 = PlayerState(name='A', deck=[low['card_id'], high['card_id']] + filler)
        p2 = PlayerState(name='B', deck=[block_id] + [c for c in ge.CARDS_DB['card_id'].to_list()
                                                    if c not in (low['card_id'], high['card_id'])][:14])
        gid = ge.create_new_game(player=p1.model_dump())
        ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
        conn, c, gs = ge.get_current_game(gid)
        conn.close()
        a, b = gs.players['A'], gs.players['B']
        # make sure the block card is in B's hand (the setup draw may have dealt it there already)
        if block_id not in b.hand:
            b.hand.append(block_id)
            b.deck.remove(block_id)
        # disable the faction biome bonus for A: start on a foreign biome
        fac = ge._player_faction(a)
        foreign = next(bi for bi in ge.BIOMES if bi not in ge.FACTION_BIOMES.get(fac, ()))
        gs.earth[0][0] = foreign
        return gs, a, b

    # 1) block holds (shields >= mana) -> A does not move, B's block effect (draw 1) fires
    gs, a, b = setup()
    b.action_chain = [{'cards': [block_id], 'to': 'stopover_2', 'mode': 'defend', 'pendings': []}]
    hand_b0, deck_b0 = len(b.hand), len(b.deck)
    msg = {'cards': [low['card_id']], 'to': 'stopover_2', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    print(f'blocked: A at {a.current_position} (expected 0), B hand {hand_b0} -> {len(b.hand)}, B deck {deck_b0} -> {len(b.deck)}')
    assert a.current_position == 0, 'A must not advance when blocked'
    assert len(b.hand) == hand_b0 + 1 and len(b.deck) == deck_b0 - 1, "block card 'draw' effect must fire"

    # 2) block fails (shields < mana) -> A moves normally, no block effect
    gs, a, b = setup()
    b.action_chain = [{'cards': [block_id], 'to': 'stopover_2', 'mode': 'defend', 'pendings': []}]
    hand_b1, deck_b1 = len(b.hand), len(b.deck)
    msg = {'cards': [high['card_id']], 'to': 'stopover_2', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    print(f'block failed: A at {a.current_position} (expected {high["advancing"]}), B hand {hand_b1} -> {len(b.hand)}')
    assert a.current_position == high['advancing'], 'A must move normally when the block fails'
    assert len(b.hand) == hand_b1 and len(b.deck) == deck_b1, "block card effect must NOT fire when the block fails"

    # 3) defend on a different stopover -> no block, no effect
    gs, a, b = setup()
    b.action_chain = [{'cards': [block_id], 'to': 'stopover_9', 'mode': 'defend', 'pendings': []}]
    hand_b2 = len(b.hand)
    msg = {'cards': [low['card_id']], 'to': 'stopover_2', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, a, gs)
    assert a.current_position == low['advancing'], 'A must move when the defend is on another stopover'
    assert len(b.hand) == hand_b2, "block card effect must NOT fire on another stopover"

    print('block card effect tests PASSED\n')


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


def test_cataclysm():
    print('=== unit tests: cataclysm condition ===')
    gs = fresh_game()
    a, b = gs.players['A'], gs.players['B']

    # 1) the pile exists at board init: one card per biome, shuffled
    assert gs.cataclysm_pile is not None
    assert sorted(gs.cataclysm_pile) == sorted(ge.BIOMES)

    # 2) trigger: ALL tokens on the struck biome go back to its first cell,
    #    tokens elsewhere are untouched, pile rotates (top -> bottom)
    top = gs.cataclysm_pile[0]
    start = next(i for i, cell in enumerate(gs.earth) if cell and cell[0] == top)
    mid = start + 3
    gs.earth[0].remove('A')
    gs.earth[mid].append('A')
    a.current_position = mid
    before = list(gs.cataclysm_pile)

    ge.trigger_cataclysm(gs)

    assert a.current_position == start and 'A' in gs.earth[start] and 'A' not in gs.earth[mid]
    assert b.current_position == 0 and 'B' in gs.earth[0]
    assert gs.cataclysm_pile == before[1:] + before[:1]
    print(f'  trigger: A {mid} -> {start} (biome {top}), pile {before} -> {gs.cataclysm_pile}')

    # 3) is_condition_met('cataclysm') is True for everyone, and side-effect FREE
    #    (the strike lives in _resolve_card, so evaluation must not rotate the pile)
    pile_before = list(gs.cataclysm_pile)
    assert ge.is_condition_met('cataclysm', a, gs) is True
    assert ge.is_condition_met('cataclysm', b, gs) is True
    assert gs.cataclysm_pile == pile_before

    # 4) token not on the struck biome: nothing happens (pile still rotates)
    gs2 = fresh_game()
    other_biome = [bi for bi in ge.BIOMES if bi != gs2.cataclysm_pile[0]][0]
    other_cell = next(i for i, cell in enumerate(gs2.earth) if cell and cell[0] == other_biome) + 2
    gs2.earth[0].remove('A'); gs2.earth[other_cell].append('A')
    gs2.players['A'].current_position = other_cell
    pile2 = list(gs2.cataclysm_pile)
    ge.trigger_cataclysm(gs2)
    assert gs2.players['A'].current_position == other_cell
    assert gs2.cataclysm_pile == pile2[1:] + pile2[:1]

    # 5) game without a pile (pre-cataclysm rules) -> safe no-op
    gs3 = fresh_game()
    gs3.cataclysm_pile = None
    ge.trigger_cataclysm(gs3)
    assert gs3.players['A'].current_position == 0

    # 6) _resolve_card fires the trigger EXACTLY ONCE for a cataclysm-condition card
    gs4 = fresh_game()
    p4 = gs4.players['A']
    cid = ge.CARDS_DB.filter(pl.col('condition') == 'cataclysm')['card_id'][0]
    top4 = gs4.cataclysm_pile[0]
    start4 = next(i for i, cell in enumerate(gs4.earth) if cell and cell[0] == top4)
    mid4 = start4 + 3
    gs4.earth[0].remove('A'); gs4.earth[mid4].append('A')
    p4.current_position = mid4
    pile4 = list(gs4.cataclysm_pile)
    msg = {'cards': [cid], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}
    ge.process_card(msg, p4, gs4)
    assert gs4.cataclysm_pile == pile4[1:] + pile4[:1], 'pile must rotate exactly once per resolved cataclysm card'
    print(f'  resolve: pile rotated once, A at {p4.current_position} after card {cid} resolved')

    print('cataclysm tests PASSED\n')


def test_avalanche():
    print('=== unit tests: avalanche effect ===')
    gs = fresh_game()
    a, b = gs.players['A'], gs.players['B']
    # avalanche rule is active in every new game (engine_version "1.0")

    mo_cells = [i for i, cell in enumerate(gs.earth) if cell and cell[0] == 'MO']
    assert len(mo_cells) == 6
    mo_start, mo_mid = mo_cells[0], mo_cells[3]

    # 1) BOTH tokens on MO -> both knocked back to the first cell of the MO segment
    gs.earth[0].remove('A'); gs.earth[mo_start + 2].append('A')
    a.current_position = mo_start + 2
    gs.earth[0].remove('B'); gs.earth[mo_mid].append('B')
    b.current_position = mo_mid
    ge.apply_effect('avalanche', 0, 0, a, gs)
    assert a.current_position == mo_start and b.current_position == mo_start
    assert 'A' in gs.earth[mo_start] and 'B' in gs.earth[mo_start]
    assert 'A' not in gs.earth[mo_start + 2] and 'B' not in gs.earth[mo_mid]
    print(f'  both on MO: A@{mo_start + 2} B@{mo_mid} -> both {mo_start}')

    # 2) tokens NOT on MO are untouched
    gs2 = fresh_game()
    a2, b2 = gs2.players['A'], gs2.players['B']
    other = [i for i, cell in enumerate(gs2.earth) if cell and cell[0] != 'MO'][3]
    gs2.earth[0].remove('B'); gs2.earth[other].append('B')
    b2.current_position = other
    ge.apply_effect('avalanche', 0, 0, a2, gs2)
    assert b2.current_position == other and a2.current_position == 0
    print(f'  tokens off MO (cell {other}) untouched')

    # 3) full resolution: the playing player is on MO -> knocked back FIRST, then
    #    its basic advancing is applied from the new position (avalanche is NOT a
    #    movement-handling effect)
    # an avalanche card with no_condition, advancing > 0 and a faction that is NOT
    # at home on MO (Miaous: DE/JU) -> no faction biome bonus, deterministic result
    cid_row = ge.CARDS_DB.filter((pl.col('effect') == 'avalanche')
                                & (pl.col('condition') == 'no_condition')
                                & (pl.col('advancing') > 0)
                                & ~pl.col('faction').is_in(['Dwarves', 'Orcs', 'Mummies']))
    cid = cid_row['card_id'][0]
    adv = int(cid_row['advancing'][0])
    gs3 = fresh_game()
    p3 = gs3.players['A']
    mo3 = [i for i, cell in enumerate(gs3.earth) if cell and cell[0] == 'MO'][0]
    gs3.earth[0].remove('A'); gs3.earth[mo3 + 1].append('A')
    p3.current_position = mo3 + 1
    ge.process_card({'cards': [cid], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}, p3, gs3)
    assert p3.current_position == mo3 + adv, f'{mo3 + 1} -> {mo3} then +{adv} = {mo3 + adv}'
    print(f'  resolve: A@{mo3 + 1} knocked to {mo3}, then advances +{adv} -> {p3.current_position}')

    print('avalanche tests PASSED\n')


def test_grappling_hook():
    print('=== unit tests: grappling_hook effect ===')
    # cards from the pool (all Miaous: OC is NOT a home biome, so a token on cell 0
    # gets NO faction biome bonus -> deterministic net advancing for the facing card)
    GH      = 'Mia44_fa1db6'    # grappling_hook, no_condition, adv 1
    GH_FAIL = 'Mia33_a963c7'    # grappling_hook, dist_ahead_sup_1 (not met at equal position), adv 1, mana 3
    FAC     = 'Mia11_57b5be'    # discard x1, adv 2 (non-movement effect -> net advancing = 2)

    def msg(cid):
        return {'cards': [cid], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}

    GH_BASE = 1      # Mia44_fa1db6 advancing
    FAC_ADV = 2      # Mia11_57b5be advancing (Miaous on OC -> no faction bonus)
    FAIL_MANA = 3    # Mia33_a963c7 mana -> reduced advancing = mana - 1 = 2

    # isolate the grappling logic from the (board-random) faction biome bonus for
    # tests 1-5; test 6 turns it back on explicitly
    saved_on_home = ge._on_home_biome
    ge._on_home_biome = lambda p, g: False

    # 1) grappling card is the 2nd player's, facing card is the 1st player's
    gs = fresh_game()
    fp, sp = gs.players[gs.turn_order[0]], gs.players[gs.turn_order[1]]
    fp.action_chain = [msg(FAC)]
    sp.action_chain = [msg(GH)]
    ge.process_trip_chain(gs)
    assert fp.current_position == FAC_ADV, f'facing: {fp.current_position} != {FAC_ADV}'
    assert sp.current_position == GH_BASE + FAC_ADV, f'grapple: {sp.current_position} != {GH_BASE + FAC_ADV}'
    print(f'  2nd-player grapple: base {GH_BASE} + copy {FAC_ADV} = {sp.current_position} (facing at {fp.current_position})')

    # 2) grappling card is the 1st player's, facing card is the 2nd player's
    gs = fresh_game()
    fp, sp = gs.players[gs.turn_order[0]], gs.players[gs.turn_order[1]]
    fp.action_chain = [msg(GH)]
    sp.action_chain = [msg(FAC)]
    ge.process_trip_chain(gs)
    assert sp.current_position == FAC_ADV, f'facing: {sp.current_position} != {FAC_ADV}'
    assert fp.current_position == GH_BASE + FAC_ADV, f'grapple: {fp.current_position} != {GH_BASE + FAC_ADV}'
    print(f'  1st-player grapple: base {GH_BASE} + copy {FAC_ADV} = {fp.current_position} (facing at {sp.current_position})')

    # 3) grappling card with NO facing card -> base advancing only (nothing to copy)
    gs = fresh_game()
    fp = gs.players[gs.turn_order[0]]
    sp = gs.players[gs.turn_order[1]]
    fp.action_chain = [msg(GH)]
    sp.action_chain = []
    ge.process_trip_chain(gs)
    assert fp.current_position == GH_BASE, f'no facing: {fp.current_position} != {GH_BASE}'
    print(f'  no facing card: base {GH_BASE} only = {fp.current_position}')

    # 4) grappling card condition NOT met -> no copy, reduced advancing (mana - 1)
    gs = fresh_game()
    fp, sp = gs.players[gs.turn_order[0]], gs.players[gs.turn_order[1]]
    fp.action_chain = [msg(FAC)]
    sp.action_chain = [msg(GH_FAIL)]   # dist_ahead_sup_1, both at 0 -> not met
    ge.process_trip_chain(gs)
    reduced = FAIL_MANA - 1
    assert fp.current_position == FAC_ADV
    assert sp.current_position == reduced, f'not met: {sp.current_position} != {reduced}'
    print(f'  condition not met: reduced {reduced}, no copy = {sp.current_position}')

    ge._on_home_biome = saved_on_home   # restore before test 6 (it manages its own patch)

    # 6) the faction biome bonus is NOT applied to the copy (it is a pure copy),
    #    but IS applied to the card's own base advancing
    gs = fresh_game()
    fp, sp = gs.players[gs.turn_order[0]], gs.players[gs.turn_order[1]]
    saved = ge._on_home_biome
    ge._on_home_biome = lambda p, g: True    # everyone "on a home biome"
    try:
        fp.action_chain = [msg(FAC)]         # facing: adv 2 + bonus 1 = 3
        sp.action_chain = [msg(GH)]          # grapple: base 1 + bonus 1 = 2, copy = facing net 3 (no bonus)
        ge.process_trip_chain(gs)
    finally:
        ge._on_home_biome = saved
    assert fp.current_position == FAC_ADV + 1, f'facing(+bonus): {fp.current_position} != {FAC_ADV + 1}'
    # sp = base (1+1) + copy (3, no bonus) = 5 ; would be 6 if the copy got the bonus
    assert sp.current_position == (GH_BASE + 1) + (FAC_ADV + 1), f'copy no bonus: {sp.current_position}'
    print(f'  bonus: facing at {fp.current_position} (+1), grapple base+1 then copy {FAC_ADV + 1} (no bonus) = {sp.current_position}')

    print('grappling_hook tests PASSED\n')


def test_effect_canceled():
    print('=== unit tests: effect_canceled ===')
    # cards from the pool
    FAC         = 'Dwa23_79c784'  # advancing, no_condition, adv 2, effect_number 1 (advances 3 when NOT canceled)
    CANCEL      = 'Dem32_fb1112'  # effect_canceled, no_condition, adv 1 (always a valid canceler)
    CANCEL_FAIL = 'Dem21_ef97e9'  # effect_canceled, dist_ahead_sup_1 (NOT met at equal position -> invalid), adv 1
    DRAW        = 'Dwa45_b171d2'  # draw 1, no_condition, adv 2 (non-movement effect, used to verify a non-movement effect is canceled)

    def msg(cid, to='stopover_4'):
        return {'cards': [cid], 'to': to, 'mode': 'move', 'pendings': []}

    FAC_BASE, FAC_BONUS = 2, 1   # Dwa23_79c784: basic advancing 2, effect bonus 1
    CANCEL_ADV = 1                # Dem32_fb1112: basic advancing 1
    DRAW_ADV = 2                  # Dwa45_b171d2: basic advancing 2

    # isolate from the (board-random) faction biome bonus for deterministic positions
    saved_on_home = ge._on_home_biome
    ge._on_home_biome = lambda p, g: False

    # 1) valid canceler on the SAME stopover -> facing card's effect CANCELED (basic advancing only)
    gs = fresh_game()
    fp, sp = gs.players[gs.turn_order[0]], gs.players[gs.turn_order[1]]
    fp.action_chain = [msg(FAC)]
    sp.action_chain = [msg(CANCEL)]
    ge.process_trip_chain(gs)
    assert fp.current_position == FAC_BASE, f'canceled: {fp.current_position} != {FAC_BASE}'
    assert sp.current_position == CANCEL_ADV, f'canceler: {sp.current_position} != {CANCEL_ADV}'
    print(f'  1) valid cancel (same stopover): facing advances {FAC_BASE} (not {FAC_BASE + FAC_BONUS}), canceler advances {CANCEL_ADV}')

    # 2) control: NO canceler on the stopover -> facing card's effect fires (full advancing)
    gs = fresh_game()
    fp, sp = gs.players[gs.turn_order[0]], gs.players[gs.turn_order[1]]
    fp.action_chain = [msg(FAC)]
    sp.action_chain = [msg(DRAW)]     # a normal (non-canceler) card on the same stopover
    ge.process_trip_chain(gs)
    assert fp.current_position == FAC_BASE + FAC_BONUS, f'control: {fp.current_position} != {FAC_BASE + FAC_BONUS}'
    print(f'  2) control (no canceler): facing advances {FAC_BASE + FAC_BONUS} (full)')

    # 3) canceler with condition NOT met -> invalid, facing card's effect fires
    gs = fresh_game()
    fp, sp = gs.players[gs.turn_order[0]], gs.players[gs.turn_order[1]]
    fp.action_chain = [msg(FAC)]
    sp.action_chain = [msg(CANCEL_FAIL)]   # dist_ahead_sup_1, both at 0 -> not met
    ge.process_trip_chain(gs)
    assert fp.current_position == FAC_BASE + FAC_BONUS, f'not met: {fp.current_position} != {FAC_BASE + FAC_BONUS}'
    print(f'  3) canceler condition not met: facing advances {FAC_BASE + FAC_BONUS} (full)')

    # 4) canceler on a DIFFERENT stopover -> no cancel (cancel is same-stopover only)
    gs = fresh_game()
    fp, sp = gs.players[gs.turn_order[0]], gs.players[gs.turn_order[1]]
    fp.action_chain = [msg(FAC, to='stopover_4')]
    sp.action_chain = [msg(CANCEL, to='stopover_3')]
    ge.process_trip_chain(gs)
    assert fp.current_position == FAC_BASE + FAC_BONUS, f'diff stopover: {fp.current_position} != {FAC_BASE + FAC_BONUS}'
    print(f'  4) canceler on different stopover: facing advances {FAC_BASE + FAC_BONUS} (full)')

    # 6) a non-movement effect (draw) is also canceled: the hand does NOT grow from the draw
    #    (the test hand holds fake ids, so the played card was never in it; only the draw
    #    would change the hand count: +1 if the effect fired, 0 if canceled)
    gs = fresh_game()
    fp, sp = gs.players[gs.turn_order[0]], gs.players[gs.turn_order[1]]
    hand_before = len(fp.hand)
    fp.action_chain = [msg(DRAW)]
    sp.action_chain = [msg(CANCEL)]
    ge.process_trip_chain(gs)
    assert len(fp.hand) == hand_before, f'draw canceled: hand {len(fp.hand)} != {hand_before} (draw must not fire)'
    assert fp.current_position == DRAW_ADV, f'draw canceled adv: {fp.current_position} != {DRAW_ADV}'
    print(f'  6) draw effect canceled: hand {len(fp.hand)} (unchanged, draw did not fire), advances {DRAW_ADV}')

    # 6b) control for 6: with NO canceler, the draw effect fires and the hand grows by 1
    gs = fresh_game()
    fp, sp = gs.players[gs.turn_order[0]], gs.players[gs.turn_order[1]]
    hand_before = len(fp.hand)
    fp.action_chain = [msg(DRAW)]
    sp.action_chain = [msg(CANCEL_FAIL)]   # invalid canceler (condition not met) -> draw fires
    ge.process_trip_chain(gs)
    assert len(fp.hand) == hand_before + 1, f'draw fires: hand {len(fp.hand)} != {hand_before + 1}'
    print(f'  6b) control: draw fires, hand {len(fp.hand)} (= {hand_before} + 1)')

    ge._on_home_biome = saved_on_home
    print('effect_canceled tests PASSED\n')


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
    test_faction_biome_bonus()
    test_block_card_effect()
    test_cataclysm()
    test_avalanche()
    test_grappling_hook()
    test_effect_canceled()
    test_full_games()
    print('ALL TESTS PASSED')
