# Manual smoke test: pending condition (rule of engine_version 21)
#  - condition met iff the player has >= 1 pending card in their OWN pending zone
#  - empty pending zone -> NOT met (effect does not fire, reduced advancing = mana-1)
#  - >= 1 pending card in the zone -> met (effect fires, full advancing)
#  - OLD games (engine_version < 21) keep the canonical default: condition MET
#  - AI mirror (PlayerAI._condition_met) agrees with the engine
# Run from the project root:  uv run python tests/_pending_condition.py
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

# a real pending card with an OBSERVABLE non-movement effect (draw 1, advancing 1, mana 2)
pend = q(condition='pending', effect='draw', effect_number=1, advancing=1, mana=2)['card_id'].to_list()
assert len(pend) >= 1, 'need a pending/draw/1/adv1/mana2 card'
pend = pend[0]

# neutral card for the opponent (rooted = self-effect only, advancing 1)
adv1 = q(effect='rooted', condition='no_condition', advancing=1)['card_id'].to_list()[0]

# 13 Dwarves filler cards (A's faction) - distinct from the two test cards
filler = [c for c in q(faction='Dwarves')['card_id'].to_list() if c not in {pend, adv1}][:13]
assert len(filler) == 13, 'need 13 Dwarves filler cards'
assert {pend, adv1}, 'need real pool cards'

def new_game():
    p1 = PlayerState(name='A', deck=[pend] + filler)
    p2 = PlayerState(name='B', deck=[adv1] + filler)
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    return gs

def set_biomes(gs, biome='JU'):
    # JU (jungle): NOT a home biome for Dwarves (A's faction) -> the faction +1
    # bonus never applies, so every position/hand assertion stays exact
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

# 1) v21, EMPTY pending zone -> NOT met (both engine and AI)
gs = new_game()
set_biomes(gs, 'JU')
gs.players['A'].pendings = []
engine_met = ge.is_condition_met('pending', gs.players['A'], gs)
ai_met = ai_condition_met(gs, 'A', 'pending')
print(f"1) empty pending zone (v21): engine={engine_met} ai={ai_met} (both expected False)")
assert engine_met is False and ai_met is False, (engine_met, ai_met)
ok += 1

# 2) v21, >= 1 pending card in the zone -> MET (both engine and AI)
gs.players['A'].pendings = ['epo']
engine_met = ge.is_condition_met('pending', gs.players['A'], gs)
ai_met = ai_condition_met(gs, 'A', 'pending')
gs.players['A'].pendings = []
print(f"2) one pending card in zone (v21): engine={engine_met} ai={ai_met} (both expected True)")
assert engine_met is True and ai_met is True, (engine_met, ai_met)
ok += 1

# 3) v21, multiple pending cards -> MET
gs.players['A'].pendings = ['epo', 'virus', 'mercurochrome']
engine_met = ge.is_condition_met('pending', gs.players['A'], gs)
ai_met = ai_condition_met(gs, 'A', 'pending')
gs.players['A'].pendings = []
print(f"3) several pending cards in zone (v21): engine={engine_met} ai={ai_met} (both expected True)")
assert engine_met is True and ai_met is True, (engine_met, ai_met)
ok += 1

# 4) OLD game (engine_version 20), empty pending zone -> canonical default: MET
gs.engine_version = 20
engine_met = ge.is_condition_met('pending', gs.players['A'], gs)
ai_met = ai_condition_met(gs, 'A', 'pending')
gs.engine_version = 21
print(f"4) old game v20, empty zone: engine={engine_met} ai={ai_met} (both expected True)")
assert engine_met is True and ai_met is True, (engine_met, ai_met)
ok += 1

# 5) OLD game (engine_version 15), empty pending zone -> canonical default: MET
gs.engine_version = 15
engine_met = ge.is_condition_met('pending', gs.players['A'], gs)
ai_met = ai_condition_met(gs, 'A', 'pending')
gs.engine_version = 21
print(f"5) old game v15, empty zone: engine={engine_met} ai={ai_met} (both expected True)")
assert engine_met is True and ai_met is True, (engine_met, ai_met)
ok += 1

# 6) FULL RESOLUTION (v21), empty pending zone -> NOT met: no draw, reduced advancing (mana-1 = 1)
gs = new_game()
set_biomes(gs, 'JU')
gs.players['A'].pendings = []
force_hand(gs, 'A', [pend]); force_hand(gs, 'B', [adv1])
gs = play(gs, 'A', 'first', pend)
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

# 7) FULL RESOLUTION (v21), pending card in zone -> MET: draw 1, full advancing (adv=1)
gs = new_game()
set_biomes(gs, 'JU')
gs.players['A'].pendings = ['epo']   # at least one pending card in A's zone
force_hand(gs, 'A', [pend]); force_hand(gs, 'B', [adv1])
gs = play(gs, 'A', 'first', pend)
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

print(f"\nALL {ok} pending TESTS PASSED")
