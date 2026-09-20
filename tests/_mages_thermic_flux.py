# Manual smoke test: Mages support faction, THIRD card — thermic_flux (engine_version 24)
#  - thermic_flux (mana_cost 2): an INSTANT play-time effect — the player CHOOSES +4 °C
#    or −4 °C (message 'temp_change': 'up' or 'down') and the planet temperature
#    changes by that amount, CLAMPED to 1..20, PERMANENTLY (game.temperature mutated).
#  - The new value is read by the temp_inf_6 / temp_inf_11 / temp_sup_9 / temp_sup_15
#    conditions at resolution time.
#  - The card is played in MOVE mode on the trip chain and resolves as a NO-OP
#    (occupies a stopover position, costs its mana_cost, no advancing / no effect).
#  - A MOVE play WITHOUT the +4/−4 choice is REJECTED.
#  - Old games (engine_version < 24): thermic_flux is a no-op, temperature never changes.
# Run from the project root:  uv run python tests/_mages_thermic_flux.py
# NOTE: writes to games.db (like _mages_celestial.py / _mages_nobodymoves.py)
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

def q(**kw):
    df = DB
    for k, v in kw.items():
        df = df.filter(pl.col(k) == v)
    return df

# a no_condition advancing card (for a normal move play)
adv1 = q(condition='no_condition', advancing=2, mana=1)['card_id'].to_list()
adv1 = adv1[0] if adv1 else q(condition='no_condition')['card_id'].to_list()[0]

# filler cards
filler = [c for c in q(faction='Miaous')['card_id'].to_list() if c != adv1][:16]

THERMIC = ge.MAGE_THERMIC_FLUX   # 'thermic_flux'

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()

def new_game(a_cards, b_cards, version=None):
    p1 = PlayerState(name='A', deck=list(a_cards) + filler)
    p2 = PlayerState(name='B', deck=list(b_cards) + filler)
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    if version is not None:
        gs.engine_version = version
    return gs, gid

def set_biomes(gs, biome='OC'):
    for cell in gs.earth:
        cell[0] = biome

def set_temp(gs, n):
    # simulate a game whose INITIAL temperature is n (set both the live value and the
    # recorded initial value, as create_new_game would have)
    gs.temperature = n
    gs.temperature_initial = n

def force_hand(gs, name, cards):
    p = gs.players[name]
    for c in cards:
        if c not in p.hand:
            if c in p.deck:
                p.deck.remove(c)
            p.hand.append(c)

def ensure_mana(gs, name, n):
    p = gs.players[name]
    while len(p.mana) - (p.mana_spend or 0) < n and p.deck:
        p.mana.append(p.deck.pop(0))

def play(gs, name, first_second, card, mode='move', to='stopover_4', temp_change=None):
    p = gs.players[name]
    cost = max(ge._play_cost(gs, card), 1)
    ensure_mana(gs, name, cost)
    p.mana_spend = 0
    msg = {'cards': [card], 'to': to, 'mode': mode, 'pendings': []}
    if temp_change is not None:
        msg['temp_change'] = temp_change
    p.message = msg
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    return gs, okk, msgtxt

def pas(gs, name, first_second):
    p = gs.players[name]
    p.message = {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    assert okk, f'pass rejected: {msgtxt}'
    return gs

passed = 0
failed = 0

def check(condition, msg):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {msg}")
    else:
        failed += 1
        print(f"  ✗ {msg}")

# ============================================================
print("\n=== Test 1: thermic_flux +4 °C (v24) — temperature increases ===")
# ============================================================
gs, gid = new_game([THERMIC, adv1], [adv1], version=24)
set_biomes(gs)
set_temp(gs, 10)
a = gs.players['A']

check(gs.temperature == 10, f"temperature starts at 10: {gs.temperature}")
check(gs.temperature_initial == 10, f"temperature_initial recorded at game start: {gs.temperature_initial}")

force_hand(gs, 'A', [THERMIC])
pos_before = a.current_position
gs, okk, msgtxt = play(gs, 'A', 'first', THERMIC, mode='move', to='stopover_4', temp_change='up')
check(okk, f"play thermic_flux choosing 'up' (+4) accepted (msg: {msgtxt})")
check(gs.temperature == 14, f"temperature now 14 (10 + 4): {gs.temperature}")
check(THERMIC not in a.hand, f"card left A's hand: {a.hand}")
check(len(a.action_chain) == 1, f"card is on the trip chain (1 action): {a.action_chain}")

# B passes, resolve the chain — the card is a NO-OP (no advancing)
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
check(a.current_position == pos_before,
      f"the card is a NO-OP on the chain (position unchanged {a.current_position} == {pos_before})")

_delete(gid)

# ============================================================
print("\n=== Test 2: thermic_flux −4 °C (v24) — temperature decreases ===")
# ============================================================
gs, gid = new_game([THERMIC, adv1], [adv1], version=24)
set_biomes(gs)
set_temp(gs, 15)
a = gs.players['A']

force_hand(gs, 'A', [THERMIC])
gs, okk, msgtxt = play(gs, 'A', 'first', THERMIC, mode='move', to='stopover_4', temp_change='down')
check(okk, f"play thermic_flux choosing 'down' (−4) accepted (msg: {msgtxt})")
check(gs.temperature == 11, f"temperature now 11 (15 − 4): {gs.temperature}")

_delete(gid)

# ============================================================
print("\n=== Test 3: clamping — up at high temp caps at 20 ===")
# ============================================================
gs, gid = new_game([THERMIC], [adv1], version=24)
set_biomes(gs)
set_temp(gs, 18)
a = gs.players['A']
force_hand(gs, 'A', [THERMIC])
gs, okk, msgtxt = play(gs, 'A', 'first', THERMIC, mode='move', to='stopover_4', temp_change='up')
check(okk, f"play accepted (msg: {msgtxt})")
check(gs.temperature == 20, f"18 + 4 = 22 clamped to 20: {gs.temperature}")

_delete(gid)

# ============================================================
print("\n=== Test 4: clamping — down at low temp floors at 1 ===")
# ============================================================
gs, gid = new_game([THERMIC], [adv1], version=24)
set_biomes(gs)
set_temp(gs, 3)
a = gs.players['A']
force_hand(gs, 'A', [THERMIC])
gs, okk, msgtxt = play(gs, 'A', 'first', THERMIC, mode='move', to='stopover_4', temp_change='down')
check(okk, f"play accepted (msg: {msgtxt})")
check(gs.temperature == 1, f"3 − 4 = −1 clamped to 1: {gs.temperature}")

_delete(gid)

# ============================================================
print("\n=== Test 5: the new temperature persists across turns (permanent) ===")
# ============================================================
gs, gid = new_game([THERMIC, adv1], [adv1], version=24)
set_biomes(gs)
set_temp(gs, 10)
a = gs.players['A']
force_hand(gs, 'A', [THERMIC])
gs, okk, msgtxt = play(gs, 'A', 'first', THERMIC, mode='move', to='stopover_4', temp_change='up')
check(okk, f"play accepted (msg: {msgtxt})")
check(gs.temperature == 14, f"temperature now 14: {gs.temperature}")

# simulate several turn-ends — the temperature must stay 14 every time (no reset)
stayed = True
for _ in range(4):
    gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
    gs, _s, _m = ge._end_turn(gs, "ok")
    if gs.temperature != 14:
        stayed = False
        break
check(stayed, f"temperature stayed 14 across 4 turn-ends (final: {gs.temperature})")
check(gs.temperature_initial == 10, f"temperature_initial still records the rolled value (10): {gs.temperature_initial}")

_delete(gid)

# ============================================================
print("\n=== Test 6: temp_* condition evaluates against the CHANGED value ===")
# ============================================================
gs, gid = new_game([THERMIC], [adv1], version=24)
set_biomes(gs)
a = gs.players['A']
# set the temperature to 12, then play thermic_flux 'down' (−4) -> 8
set_temp(gs, 12)
# at 12, 'temp_inf_11' (temp < 11) is NOT met; after −4 -> 8, it IS met
check(ge.is_condition_met('temp_inf_11', a, gs) is False, f"at temp 12, 'temp_inf_11' is NOT met")
force_hand(gs, 'A', [THERMIC])
gs, okk, msgtxt = play(gs, 'A', 'first', THERMIC, mode='move', to='stopover_4', temp_change='down')
check(okk, f"play accepted (msg: {msgtxt})")
check(gs.temperature == 8, f"temperature now 8: {gs.temperature}")
check(ge.is_condition_met('temp_inf_11', a, gs) is True, f"at temp 8, 'temp_inf_11' IS met")
check(ge.is_condition_met('temp_inf_6', a, gs) is False, f"at temp 8, 'temp_inf_6' is NOT met")

_delete(gid)

# ============================================================
print("\n=== Test 7: MOVE play without a +4/−4 choice is REJECTED ===")
# ============================================================
gs, gid = new_game([THERMIC], [adv1], version=24)
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [THERMIC])
set_temp(gs, 10)
# no temp_change field -> must be rejected
gs, okk, msgtxt = play(gs, 'A', 'first', THERMIC, mode='move', to='stopover_4', temp_change=None)
check(not okk, f"play without a choice is rejected (msg: {msgtxt})")
check(THERMIC in a.hand, f"card still in hand after rejection: {a.hand}")
check(len(a.action_chain) == 0, f"nothing added to the trip chain: {a.action_chain}")
check(gs.temperature == 10, f"temperature unchanged after rejection: {gs.temperature}")

# a bad value must be rejected by message_check too
ok, msg = ge.message_check({'cards': [THERMIC], 'to': 'stopover_4', 'mode': 'move',
                            'pendings': [], 'temp_change': 'hot'})
check(not ok, f"message_check rejects a bad 'temp_change' value (msg: {msg})")

_delete(gid)

# ============================================================
print("\n=== Test 8: a DEFEND play is a plain no-op (no choice required, no change) ===")
# ============================================================
gs, gid = new_game([THERMIC], [adv1], version=24)
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [THERMIC])
set_temp(gs, 10)
gs, okk, msgtxt = play(gs, 'A', 'first', THERMIC, mode='defend', to='stopover_4')
check(okk, f"defend play accepted (no choice needed) (msg: {msgtxt})")
check(gs.temperature == 10, f"temperature NOT changed by a defend play: {gs.temperature}")

_delete(gid)

# ============================================================
print("\n=== Test 9: old-game pinning (v23) — thermic_flux is a no-op, temp unchanged ===")
# ============================================================
gs, gid = new_game([THERMIC], [adv1], version=23)   # pre-thermic_flux
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [THERMIC])
set_temp(gs, 10)
# v23: the card is a no-op support card; the 'temp_change' field is ignored
gs, okk, msgtxt = play(gs, 'A', 'first', THERMIC, mode='move', to='stopover_4', temp_change='up')
check(okk, f"v23 play accepted (the card is a no-op support card) (msg: {msgtxt})")
check(gs.temperature == 10, f"v23: temperature NOT changed (still 10): {gs.temperature}")

_delete(gid)

# ============================================================
print("\n=== Test 10: turn-log note on the thermic_flux line ===")
# ============================================================
gs, gid = new_game([THERMIC, adv1], [adv1], version=24)
set_biomes(gs)
a = gs.players['A']
set_temp(gs, 10)
force_hand(gs, 'A', [THERMIC])
gs, okk, msgtxt = play(gs, 'A', 'first', THERMIC, mode='move', to='stopover_4', temp_change='up')
check(okk, f"play accepted (msg: {msgtxt})")
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
# find the log entry for A's action and check for the thermic_flux note
found_note = False
for turn in gs.log:
    for sv in turn.get('stopovers', []):
        for e in sv.get('entries', []):
            for note in e.get('notes', []):
                if 'thermic_flux' in note and '14' in note:
                    found_note = True
check(found_note, f"turn log has a thermic_flux note: {gs.log}")

_delete(gid)

# ============================================================
print(f"\n{'='*50}")
print(f"RESULTS: {passed} passed, {failed} failed")
print(f"{'='*50}")

if failed == 0:
    print("All tests passed!")
else:
    print(f"{failed} test(s) failed!")
    sys.exit(1)
