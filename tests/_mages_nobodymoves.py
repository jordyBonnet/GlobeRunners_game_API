# Manual smoke test: Mages support faction, SECOND card — nobodymoves (engine_version 23)
#  - nobodymoves (mana_cost 3): an INSTANT play-time effect — it LOCKS ALL PLAYERS'
#    MOVEMENT for the rest of the turn. Their MOVE cards still RESOLVE (condition
#    check + non-movement effects like draw/ramp/discard/taxation still fire, and the
#    defend/block race still happens) but their MOVEMENT is suppressed: no basic
#    advancing, no movement effects (advancing/backward/jump/advancing_oppo/
#    backward_oppo/avalanche), no pending epo/virus, no grappling/copy copies. The only
#    exception is an "unstoppable" card whose condition is met (it still moves).
#    DEFEND cards are unaffected (they never advance).
#  - The lock is GAME-LEVEL (game.nobodymoves_active), set at play time (cannot be
#    blocked / canceled / conditioned) and cleared in the cleaning phase.
#  - The card is played in MOVE mode on the trip chain and resolves as a NO-OP
#    (occupies a stopover position, costs its mana_cost, no advancing / no effect).
#  - A DEFEND play of nobodymoves is a plain no-op (no lock).
#  - Old games (engine_version < 23): nobodymoves is a no-op, there is no lock.
# Run from the project root:  uv run python tests/_mages_nobodymoves.py
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
adv1 = 'Dwa23_79c784'   # adv 2, mana 2, no_condition, effect advancing (movement)
UNSTOPPABLE = 'Dwa34_316a8c'   # adv 1, mana 3, no_condition, effect unstoppable
DRAWS = 'Dwa23_755151'   # adv 0, mana 2, no_condition, effect draw x1 (non-movement)

# filler cards
filler = [c for c in q(faction='Miaous')['card_id'].to_list() if c not in (adv1, UNSTOPPABLE, DRAWS)][:16]

NOBODY = ge.MAGE_NOBODYMOVES   # 'nobodymoves'

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

def play(gs, name, first_second, card, mode='move', to='stopover_4'):
    p = gs.players[name]
    cost = max(ge._play_cost(gs, card), 1)
    ensure_mana(gs, name, cost)
    p.mana_spend = 0
    msg = {'cards': [card], 'to': to, 'mode': mode, 'pendings': []}
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
print("\n=== Test 1: nobodymoves LOCKS the player's movement (v23) — no advancing ===")
# ============================================================
gs, gid = new_game([NOBODY, adv1], [adv1], version=23)
set_biomes(gs)
a = gs.players['A']

check(gs.nobodymoves_active is False, f"nobodymoves_active starts False: {gs.nobodymoves_active}")

force_hand(gs, 'A', [adv1, NOBODY])
pos_before = a.current_position
# A plays a normal move card, then nobodymoves (the movement lock is set at play time)
gs, okk, msgtxt = play(gs, 'A', 'first', adv1, mode='move', to='stopover_4')
check(okk, f"play adv1 accepted (msg: {msgtxt})")
gs, okk, msgtxt = play(gs, 'A', 'first', NOBODY, mode='move', to='stopover_3')
check(okk, f"play nobodymoves accepted (msg: {msgtxt})")
check(gs.nobodymoves_active is True, f"nobodymoves_active is now True (movement lock active): {gs.nobodymoves_active}")

# B passes, resolve the chain — A's adv1 movement is LOCKED (no advancing)
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
check(a.current_position == pos_before,
      f"adv1 movement was LOCKED (position unchanged {a.current_position} == {pos_before})")
check(NOBODY not in a.hand and adv1 not in a.hand, f"both cards left A's hand: {a.hand}")

_delete(gid)

# ============================================================
print("\n=== Test 2: nobodymoves LOCKS BOTH players' movement (v23) ===")
# ============================================================
gs, gid = new_game([NOBODY, adv1], [adv1], version=23)
set_biomes(gs)
a = gs.players['A']; b = gs.players['B']
force_hand(gs, 'A', [adv1, NOBODY])
force_hand(gs, 'B', [adv1])
pos_a = a.current_position; pos_b = b.current_position
# A plays adv1, B plays adv1, A plays nobodymoves (block set), then resolve
gs = play(gs, 'A', 'first', adv1, mode='move', to='stopover_4')[0]
gs = play(gs, 'B', 'second', adv1, mode='move', to='stopover_3')[0]
gs, okk, msgtxt = play(gs, 'A', 'first', NOBODY, mode='move', to='stopover_2')
check(okk, f"play nobodymoves accepted (msg: {msgtxt})")
check(gs.nobodymoves_active is True, f"lock active: {gs.nobodymoves_active}")
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
check(a.current_position == pos_a, f"A's adv1 canceled (position unchanged {a.current_position} == {pos_a})")
check(b.current_position == pos_b, f"B's adv1 canceled (position unchanged {b.current_position} == {pos_b})")

_delete(gid)

# ============================================================
print("\n=== Test 3: unstoppable card IGNORES the movement lock (v23) ===")
# ============================================================
gs, gid = new_game([NOBODY, UNSTOPPABLE], [adv1], version=23)
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [UNSTOPPABLE, NOBODY])
pos_before = a.current_position
gs = play(gs, 'A', 'first', UNSTOPPABLE, mode='move', to='stopover_4')[0]
gs, okk, msgtxt = play(gs, 'A', 'first', NOBODY, mode='move', to='stopover_3')
check(okk, f"play nobodymoves accepted (msg: {msgtxt})")
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
# the unstoppable card (no_condition, always met) advances despite the movement lock
check(a.current_position != pos_before,
      f"unstoppable card advanced despite the lock ({a.current_position} != {pos_before})")
# confirm it was the unstoppable card (adv 1) that moved, not a cancel
# find the log entry note for the unstoppable card
found_unstoppable_note = False
for turn in gs.log:
    for sv in turn.get('stopovers', []):
        for e in sv.get('entries', []):
            for note in e.get('notes', []):
                if 'unstoppable' in note and 'nobodymoves' in note:
                    found_unstoppable_note = True
check(found_unstoppable_note, f"log shows unstoppable ignored the nobodymoves block: {gs.log}")

_delete(gid)

# ============================================================
print("\n=== Test 4: the movement lock is CLEARED in the cleaning phase (v23) ===")
# ============================================================
gs, gid = new_game([NOBODY, adv1, adv1], [adv1], version=23)
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [adv1, NOBODY])
# turn 1: play adv1 + nobodymoves -> adv1 canceled
pos_t1 = a.current_position
gs = play(gs, 'A', 'first', adv1, mode='move', to='stopover_4')[0]
gs = play(gs, 'A', 'first', NOBODY, mode='move', to='stopover_3')[0]
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
check(gs.nobodymoves_active is True, f"lock active during the turn: {gs.nobodymoves_active}")
check(a.current_position == pos_t1, f"turn 1: adv1 canceled (position unchanged)")
# end of turn -> block cleared
gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
gs, _s, _m = ge._end_turn(gs, "ok")
check(gs.nobodymoves_active is False, f"block CLEARED after the turn: {gs.nobodymoves_active}")
# turn 2: play adv1 -> it ADVANCES (block cleared)
force_hand(gs, 'A', [adv1])
pos_t2 = a.current_position
gs = play(gs, 'A', 'first', adv1, mode='move', to='stopover_4')[0]
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
check(a.current_position != pos_t2,
      f"turn 2: adv1 advanced (block cleared, {a.current_position} != {pos_t2})")

_delete(gid)

# ============================================================
print("\n=== Test 5: a DEFEND play of nobodymoves is a no-op (no block) ===")
# ============================================================
gs, gid = new_game([NOBODY, adv1], [adv1], version=23)
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [NOBODY])
gs, okk, msgtxt = play(gs, 'A', 'first', NOBODY, mode='defend', to='stopover_4')
check(okk, f"defend play accepted (msg: {msgtxt})")
check(gs.nobodymoves_active is False, f"nobodymoves_active stays False (no block from a defend play): {gs.nobodymoves_active}")

_delete(gid)

# ============================================================
print("\n=== Test 6: cannot attach a pending card to nobodymoves (support card) ===")
# ============================================================
gs, gid = new_game([NOBODY], [adv1], version=23)
set_biomes(gs)
gs.players['A'].pendings = ['epo']   # pretend A has a pending card
p = gs.players['A']
ensure_mana(gs, 'A', 3)
p.mana_spend = 0
p.message = {'cards': [NOBODY], 'to': 'stopover_4', 'mode': 'move', 'pendings': ['epo']}
gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
p, gs, okk, msgtxt = ge.player_play('first', p, gs)
check(not okk, f"attaching a pending card to nobodymoves is rejected (msg: {msgtxt})")

_delete(gid)

# ============================================================
print("\n=== Test 7: old-game pinning (v22) — nobodymoves is a no-op, no block ===")
# ============================================================
gs, gid = new_game([NOBODY, adv1], [adv1], version=22)   # pre-nobodymoves
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [adv1, NOBODY])
# v22: the card is a no-op support card; the movement lock does not fire
pos_before = a.current_position
gs = play(gs, 'A', 'first', adv1, mode='move', to='stopover_4')[0]
gs, okk, msgtxt = play(gs, 'A', 'first', NOBODY, mode='move', to='stopover_3')
check(okk, f"v22 play accepted (the card is a no-op support card) (msg: {msgtxt})")
check(gs.nobodymoves_active is False, f"v22: nobodymoves_active stays False: {gs.nobodymoves_active}")
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
check(a.current_position != pos_before,
      f"v22: adv1 ADVANCED (no block, {a.current_position} != {pos_before})")

_delete(gid)

# ============================================================
print("\n=== Test 8b: turn-log note on the nobodymoves line (MOVEMENT LOCKED text) ===")
# ============================================================
gs, gid = new_game([NOBODY, adv1], [adv1], version=23)
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [adv1, NOBODY])
gs = play(gs, 'A', 'first', adv1, mode='move', to='stopover_4')[0]
gs, okk, msgtxt = play(gs, 'A', 'first', NOBODY, mode='move', to='stopover_3')
check(okk, f"play accepted (msg: {msgtxt})")
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
# the MOVEMENT LOCKED note lives in the turn's 'instant' section (play-time effect)
found_note = False
for turn in gs.log:
    for it in (turn.get('instant') or []):
        w = str(it.get('what'))
        if 'nobodymoves' in w and 'movement locked' in w.lower():
            found_note = True
check(found_note, f"turn log has a nobodymoves MOVEMENT LOCKED note")

_delete(gid)

# ============================================================
print("\n=== Test 9: a NON-MOVEMENT effect (draw) STILL FIRES during nobodymoves (v23) ===")
# ============================================================
# The KEY difference from the old "cancel" semantics: a card with a non-movement
# effect (draw) still resolves that effect, but its MOVEMENT is suppressed.
gs, gid = new_game([NOBODY, DRAWS], [adv1], version=23)
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [DRAWS, NOBODY])
pos_before = a.current_position
hand_before = len(a.hand)
# the draw card will draw 1; to measure the net hand change, account for the
# card leaving the hand and the draw adding one
n_drawn = a.deck[0] if a.deck else None
gs = play(gs, 'A', 'first', DRAWS, mode='move', to='stopover_4')[0]
# the draw card resolves NOW? No — it resolves at the trip chain. Play nobodymoves first.
gs, okk, msgtxt = play(gs, 'A', 'first', NOBODY, mode='move', to='stopover_3')
check(okk, f"play nobodymoves accepted (msg: {msgtxt})")
check(gs.nobodymoves_active is True, f"lock active: {gs.nobodymoves_active}")
# hand after playing both cards (both left the hand), before the trip chain draws
hand_pre_chain = len(a.hand)
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
# the draw card's effect fired (drew 1) but its movement was suppressed (adv 0 anyway)
# net: hand went from hand_pre_chain (after both plays) to hand_pre_chain + 1 (the draw)
check(a.current_position == pos_before,
      f"draw card's movement suppressed (position unchanged {a.current_position} == {pos_before})")
check(len(a.hand) == hand_pre_chain + 1,
      f"draw card's effect FIRED (+1 card, hand {hand_pre_chain} -> {len(a.hand)})")

_delete(gid)

# ============================================================
print("\n=== Test 10: a MOVEMENT effect (advancing) is SUPPRESSED during nobodymoves (v23) ===")
# ============================================================
# Contrast with Test 9: an advancing card (a movement effect) does NOT fire — no
# extra cells — and its basic advancing is suppressed too.
gs, gid = new_game([NOBODY, adv1], [adv1], version=23)
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [adv1, NOBODY])
pos_before = a.current_position
gs = play(gs, 'A', 'first', adv1, mode='move', to='stopover_4')[0]
gs, okk, msgtxt = play(gs, 'A', 'first', NOBODY, mode='move', to='stopover_3')
check(okk, f"play nobodymoves accepted (msg: {msgtxt})")
hand_pre = len(a.hand)
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
check(a.current_position == pos_before,
      f"advancing card's movement suppressed (position unchanged {a.current_position} == {pos_before})")
check(len(a.hand) == hand_pre,
      f"advancing card's effect did NOT add cards (no draw; hand {hand_pre} -> {len(a.hand)})")

_delete(gid)

# ============================================================
print("\n=== Test 11: unstoppable STILL MOVES while a normal card is locked (v23) ===")
# ============================================================
# Two cards in the same chain: a normal adv card (locked) + an unstoppable card
# (still moves). Confirms the per-card exception.
gs, gid = new_game([adv1, UNSTOPPABLE, NOBODY], [adv1], version=23)
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [adv1, UNSTOPPABLE, NOBODY])
pos_before = a.current_position
gs = play(gs, 'A', 'first', adv1, mode='move', to='stopover_4')[0]
gs = play(gs, 'A', 'first', UNSTOPPABLE, mode='move', to='stopover_3')[0]
gs, okk, msgtxt = play(gs, 'A', 'first', NOBODY, mode='move', to='stopover_2')
check(okk, f"play nobodymoves accepted (msg: {msgtxt})")
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
# the unstoppable card (adv 1) moved; the normal adv card (adv 2) did not.
# Net: position should be pos_before + 1 (only the unstoppable card advanced)
check(a.current_position == pos_before + 1,
      f"only the unstoppable card moved (+1, {pos_before} -> {a.current_position})")

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
