# Manual smoke test: drop_on_board (rule of engine_version 9)
#  - condition met iff ANY drop or trap is on the earth (drop token or 'trap'/'drop' cell)
#  - no drop/trap -> NOT met (effect does not fire, reduced advancing = mana-1)
#  - drop token present -> met (effect fires, full advancing)
#  - 'trap' / 'drop' cell content -> met
#  - old games (engine_version < 9) keep the canonical default: condition MET
#  - AI mirror (PlayerAI._condition_met) agrees with the engine
# Run from the project root:  uv run python tests/_drop_on_board.py
# NOTE: writes to games.db (like _diag.py)
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState
from player_ai.playerai import PlayerAI

DB = ge.CARDS_DB

def q(**kw):
    df = DB
    for k, v in kw.items():
        df = df.filter(pl.col(k) == v)
    return df

# a real drop_on_board card with an OBSERVABLE non-movement effect (draw 1)
dob = q(condition='drop_on_board', effect='draw', effect_number=1, advancing=1, mana=2)['card_id'].to_list()
assert len(dob) >= 1, 'need a drop_on_board/draw card'
dob = dob[0]

# neutral card for the opponent (rooted = no-op effect -> plain advancing only)
adv1 = q(effect='rooted', condition='no_condition', advancing=1)['card_id'].to_list()[0]
assert {dob, adv1}, 'need real pool cards'

filler = [c for c in q(faction='Miaous')['card_id'].to_list() if c not in {dob, adv1}][:13]
assert len(filler) == 13, 'need 13 Miaous filler cards'

def new_game(a_card, b_card):
    p1 = PlayerState(name='A', deck=[a_card] + filler)
    p2 = PlayerState(name='B', deck=[b_card] + filler)
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    return gs

def set_biomes(gs, biome='OC'):
    # OC (ocean): NOT a home biome for Miaous (the deck's dominant faction = the
    # player's faction) -> the faction +1 bonus never applies, so every
    # position/hand assertion stays exact
    for cell in gs.earth:
        cell[0] = biome

def force_hand(gs, name, cards):
    p = gs.players[name]
    for c in cards:
        if c not in p.hand:
            if c in p.deck:
                p.deck.remove(c)
            p.hand.append(c)

def play(gs, name, first_second, card, mode='move'):
    p = gs.players[name]
    mana = int(DB.filter(pl.col('card_id') == card)['mana'][0])
    p.mana = [filler[0]] * mana
    p.mana_spend = 0
    p.message = {'cards': [card], 'to': 'stopover_4', 'mode': mode, 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, ok, msg = ge.player_play(first_second, p, gs)
    assert ok, f'play rejected: {msg}'
    return gs

def ai_condition_met(gs, name, condition):
    ai = PlayerAI(gs.players[name])
    ai.update_player_state(gs.players[name], gs.players['B' if name == 'A' else 'A'], gs)
    return ai._condition_met(condition)

ok = 0

# 1) v9, CLEAN board -> NOT met (both engine and AI)
gs = new_game(dob, adv1)
set_biomes(gs, 'OC')
engine_met = ge.is_condition_met('drop_on_board', gs.players['A'], gs)
ai_met = ai_condition_met(gs, 'A', 'drop_on_board')
print(f"1) clean board (v9): engine={engine_met} ai={ai_met} (both expected False)")
assert engine_met is False and ai_met is False, (engine_met, ai_met)
ok += 1

# 2) v9, drop token on the board -> MET (both engine and AI)
gs.drop_tokens = {20: 1}
engine_met = ge.is_condition_met('drop_on_board', gs.players['A'], gs)
ai_met = ai_condition_met(gs, 'A', 'drop_on_board')
print(f"2) drop token on board (v9): engine={engine_met} ai={ai_met} (both expected True)")
assert engine_met is True and ai_met is True, (engine_met, ai_met)
ok += 1

# 3) v9, 'trap' cell content -> MET
gs.drop_tokens = {}
gs.earth[10].append('trap')
engine_met = ge.is_condition_met('drop_on_board', gs.players['A'], gs)
ai_met = ai_condition_met(gs, 'A', 'drop_on_board')
gs.earth[10].remove('trap')
print(f"3) 'trap' cell content (v9): engine={engine_met} ai={ai_met} (both expected True)")
assert engine_met is True and ai_met is True, (engine_met, ai_met)
ok += 1

# 4) v9, 'drop' cell content -> MET
gs.earth[12].append('drop')
engine_met = ge.is_condition_met('drop_on_board', gs.players['A'], gs)
ai_met = ai_condition_met(gs, 'A', 'drop_on_board')
gs.earth[12].remove('drop')
print(f"4) 'drop' cell content (v9): engine={engine_met} ai={ai_met} (both expected True)")
assert engine_met is True and ai_met is True, (engine_met, ai_met)
ok += 1

# 5) OLD game (engine_version 8) -> canonical default: MET even on a clean board
gs = new_game(dob, adv1)
set_biomes(gs, 'OC')
gs.engine_version = 8
engine_met = ge.is_condition_met('drop_on_board', gs.players['A'], gs)
ai_met = ai_condition_met(gs, 'A', 'drop_on_board')
print(f"5) old game v8, clean board: engine={engine_met} ai={ai_met} (both expected True)")
assert engine_met is True and ai_met is True, (engine_met, ai_met)
ok += 1

# 6) FULL RESOLUTION, no drop/trap -> NOT met: no draw, reduced advancing (mana-1 = 1)
gs = new_game(dob, adv1)
set_biomes(gs, 'OC')
force_hand(gs, 'A', [dob]); force_hand(gs, 'B', [adv1])
gs = play(gs, 'A', 'first', dob)
gs = play(gs, 'B', 'second', adv1)
a_hand_before = len(gs.players['A'].hand)
a_pos_before = gs.players['A'].current_position
gs = ge.process_trip_chain(gs)
a_hand_after = len(gs.players['A'].hand)
a_pos_after = gs.players['A'].current_position
print(f"6) not met: A hand {a_hand_before}->{a_hand_after} (expected +0, no draw), "
      f"A pos {a_pos_before}->{a_pos_after} (expected +1 = mana-1)")
assert a_hand_after == a_hand_before, (a_hand_before, a_hand_after)   # no draw (condition not met)
assert a_pos_after == a_pos_before + 1, (a_pos_before, a_pos_after)    # reduced advancing = mana-1 = 1
ok += 1

# 7) FULL RESOLUTION, drop token present -> MET: draw 1, full advancing (adv=1)
gs = new_game(dob, adv1)
set_biomes(gs, 'OC')
gs.drop_tokens = {20: 1}   # a drop token on the board (not on A's path)
force_hand(gs, 'A', [dob]); force_hand(gs, 'B', [adv1])
gs = play(gs, 'A', 'first', dob)
gs = play(gs, 'B', 'second', adv1)
a_hand_before = len(gs.players['A'].hand)
a_pos_before = gs.players['A'].current_position
gs = ge.process_trip_chain(gs)
a_hand_after = len(gs.players['A'].hand)
a_pos_after = gs.players['A'].current_position
print(f"7) met: A hand {a_hand_before}->{a_hand_after} (expected +1, draw 1), "
      f"A pos {a_pos_before}->{a_pos_after} (expected +1 = full advancing adv=1)")
assert a_hand_after == a_hand_before + 1, (a_hand_before, a_hand_after)   # draw 1 (condition met)
assert a_pos_after == a_pos_before + 1, (a_pos_before, a_pos_after)        # full advancing = adv 1
ok += 1

print(f"\nALL {ok} drop_on_board TESTS PASSED")
