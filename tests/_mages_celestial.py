# Manual smoke test: Mages support faction, FIRST card — Celestial_reversal (engine_version 22)
#  - Celestial_reversal (mana_cost 2): an INSTANT play-time effect — the player CHOOSES
#    day or night (message 'day_night') and it is FIXED for the rest of the game
#    (game.day_night set to the choice, game.day_night_fixed = True).
#  - The day/night no longer flips each turn (the cleaning phase skips the flip).
#  - The card is played in MOVE mode on the trip chain and resolves as a NO-OP
#    (occupies a stopover position, costs its mana_cost, no advancing / no effect).
#  - A MOVE play WITHOUT the day/night choice is REJECTED.
#  - Old games (engine_version < 22): Celestial_reversal is a no-op, day/night still flips.
# Run from the project root:  uv run python tests/_mages_celestial.py
# NOTE: writes to games.db (like _engineers.py / _doctors.py)
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

CELESTIAL = ge.MAGE_CELASTIAL_REVERSAL   # 'Celestial_reversal'

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

def play(gs, name, first_second, card, mode='move', to='stopover_4', day_night=None):
    p = gs.players[name]
    cost = max(ge._play_cost(gs, card), 1)
    ensure_mana(gs, name, cost)
    p.mana_spend = 0
    msg = {'cards': [card], 'to': to, 'mode': mode, 'pendings': []}
    if day_night is not None:
        msg['day_night'] = day_night
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
print("\n=== Test 1: Celestial_reversal FIXES the day/night (v22) ===")
# ============================================================
gs, gid = new_game([CELESTIAL, adv1], [adv1])
set_biomes(gs)
a = gs.players['A']

check(gs.day_night == 'day', f"game starts on day: {gs.day_night}")
check(gs.day_night_fixed is False, f"day_night_fixed starts False: {gs.day_night_fixed}")

force_hand(gs, 'A', [CELESTIAL])
pos_before = a.current_position
gs, okk, msgtxt = play(gs, 'A', 'first', CELESTIAL, mode='move', to='stopover_4', day_night='night')
check(okk, f"play Celestial_reversal choosing 'night' accepted (msg: {msgtxt})")
check(gs.day_night == 'night', f"day/night now FIXED to 'night': {gs.day_night}")
check(gs.day_night_fixed is True, f"day_night_fixed is True: {gs.day_night_fixed}")
check(CELESTIAL not in a.hand, f"card left A's hand: {a.hand}")
check(len(a.action_chain) == 1, f"card is on the trip chain (1 action): {a.action_chain}")

# B passes, resolve the chain — the card is a NO-OP (no advancing)
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
check(a.current_position == pos_before,
      f"the card is a NO-OP on the chain (position unchanged {a.current_position} == {pos_before})")

# --- the day/night must NOT flip at the end of the turn ---
gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
# run the real end-of-turn block (rooted settlement + next-turn prep incl. the flip)
gs, _s, _m = ge._end_turn(gs, "ok")
check(gs.day_night == 'night', f"day/night did NOT flip after the turn (still 'night'): {gs.day_night}")
check(gs.day_night_fixed is True, f"day_night_fixed stays True: {gs.day_night_fixed}")

_delete(gid)

# ============================================================
print("\n=== Test 2: the fixed day/night persists across several turns ===")
# ============================================================
gs, gid = new_game([CELESTIAL, adv1], [adv1])
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [CELESTIAL])
gs, okk, msgtxt = play(gs, 'A', 'first', CELESTIAL, mode='move', to='stopover_4', day_night='day')
check(okk, f"play Celestial_reversal choosing 'day' accepted (msg: {msgtxt})")
check(gs.day_night == 'day' and gs.day_night_fixed is True, f"fixed to 'day': {gs.day_night}, {gs.day_night_fixed}")

# simulate several turn-ends — the day/night must stay 'day' every time
stayed = True
for _ in range(4):
    gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
    gs, _s, _m = ge._end_turn(gs, "ok")
    if gs.day_night != 'day':
        stayed = False
        break
check(stayed, f"day/night stayed 'day' across 4 turn-ends (final: {gs.day_night})")

_delete(gid)

# ============================================================
print("\n=== Test 3: day/night condition evaluates against the FIXED value ===")
# ============================================================
gs, gid = new_game([CELESTIAL], [adv1])
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [CELESTIAL])
gs, okk, msgtxt = play(gs, 'A', 'first', CELESTIAL, mode='move', to='stopover_4', day_night='night')
check(okk, f"play accepted (msg: {msgtxt})")
# now the 'night' condition must be met and the 'day' condition NOT met
check(ge.is_condition_met('night', a, gs) is True, f"'night' condition is met (day_night={gs.day_night})")
check(ge.is_condition_met('day', a, gs) is False, f"'day' condition is NOT met (day_night={gs.day_night})")

_delete(gid)

# ============================================================
print("\n=== Test 4: MOVE play without a day/night choice is REJECTED ===")
# ============================================================
gs, gid = new_game([CELESTIAL], [adv1])
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [CELESTIAL])
# no day_night field -> must be rejected
gs, okk, msgtxt = play(gs, 'A', 'first', CELESTIAL, mode='move', to='stopover_4', day_night=None)
check(not okk, f"play without a choice is rejected (msg: {msgtxt})")
check(CELESTIAL in a.hand, f"card still in hand after rejection: {a.hand}")
check(len(a.action_chain) == 0, f"nothing added to the trip chain: {a.action_chain}")
check(gs.day_night_fixed is False, f"day_night_fixed still False: {gs.day_night_fixed}")

# a bad value must be rejected by message_check too
ok, msg = ge.message_check({'cards': [CELESTIAL], 'to': 'stopover_4', 'mode': 'move',
                            'pendings': [], 'day_night': 'dawn'})
check(not ok, f"message_check rejects a bad 'day_night' value (msg: {msg})")

_delete(gid)

# ============================================================
print("\n=== Test 5: a DEFEND play is a plain no-op (no choice required, no fix) ===")
# ============================================================
gs, gid = new_game([CELESTIAL], [adv1])
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [CELESTIAL])
gs, okk, msgtxt = play(gs, 'A', 'first', CELESTIAL, mode='defend', to='stopover_4')
check(okk, f"defend play accepted (no choice needed) (msg: {msgtxt})")
check(gs.day_night_fixed is False, f"day/night NOT fixed by a defend play: {gs.day_night_fixed}")
check(gs.day_night == 'day', f"day/night unchanged: {gs.day_night}")

_delete(gid)

# ============================================================
print("\n=== Test 6: cannot attach a pending card to Celestial_reversal (support card) ===")
# ============================================================
gs, gid = new_game([CELESTIAL], [adv1])
set_biomes(gs)
gs.players['A'].pendings = ['epo']   # pretend A has a pending card
p = gs.players['A']
ensure_mana(gs, 'A', 3)
p.mana_spend = 0
p.message = {'cards': [CELESTIAL], 'to': 'stopover_4', 'mode': 'move', 'pendings': ['epo'], 'day_night': 'day'}
gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
p, gs, okk, msgtxt = ge.player_play('first', p, gs)
check(not okk, f"attaching a pending card to Celestial_reversal is rejected (msg: {msgtxt})")

_delete(gid)

# ============================================================
print("\n=== Test 8: turn-log note on the Celestial_reversal line ===")
# ============================================================
gs, gid = new_game([CELESTIAL, adv1], [adv1])
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [CELESTIAL])
gs, okk, msgtxt = play(gs, 'A', 'first', CELESTIAL, mode='move', to='stopover_4', day_night='night')
check(okk, f"play accepted (msg: {msgtxt})")
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
# find the Celestial_reversal note in the turn's 'instant' section (play-time effect)
found_note = False
for turn in gs.log:
    for it in (turn.get('instant') or []):
        if 'Celestial_reversal' in str(it.get('what')) and 'night' in str(it.get('what')):
            found_note = True
check(found_note, f"turn log has a Celestial_reversal note: {gs.log}")

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
