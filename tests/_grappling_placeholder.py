# Manual smoke test: grappling_hook vs a PLACEHOLDER (engine_version 17)
#
# BUG (engine_version <= 16): board furniture (a doctor PENDING placeholder or a
# dwelling placeholder) consumes a display position (player_play advances
# play_count, so the next play renders one stopover column later), but the
# trip-chain resolution (ge._player_chain) IGNORED the placeholder. So a
# grappling_hook that visually faces a placeholder still copied the facing
# advancement of the card in the NEXT row (and vice versa).
#
# FIX (engine_version 17): _player_chain includes placeholder entries at their
# recorded stopover positions - a card facing a placeholder faces an EMPTY
# position: no grappling copy, no copy_effect. The display and the resolution
# now agree on positions.
#
# Scenario (mirrors game 26_09_17_18_39_29_78u8l, turn 3):
#   A (Dwarves, first)  : pending 'epo' (NOT attached) -> position 1 (stopover_4)
#                         grappling card (adv 1)       -> position 2 (stopover_3)
#   B (Demons, second)  : grappling card (adv 1)       -> position 1 (stopover_4)
#                         plain card (net 4)           -> position 2 (stopover_3)
#
#   v16 (buggy)  : placeholder ignored -> both grappling cards face EACH OTHER
#                  at chain position 1 and copy each other's base (+1).
#   v17 (fixed)  : B's grappling faces A's PLACEHOLDER -> copies nothing.
#                  A's grappling (pos 2) faces B's plain card -> copies +4.
#
# Run from the project root:  uv run python tests/_grappling_placeholder.py
# (deletes its own games)
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

# A (Dwarves): grappling_hook, no_condition, advancing 1
A_GRAP = 'Dwa34_2cd1a7'
# B (Demons): grappling_hook, no_condition, advancing 1
B_GRAP = 'Dem32_b94652'
# B (Demons): plain card, no_condition, advancing 3 + effect 'advancing' +1 = net 4
B_PLAIN = 'Dem32_029034'

for cid in (A_GRAP, B_GRAP, B_PLAIN):
    row = DB.filter(pl.col('card_id') == cid)
    assert row.height == 1, cid
    r = row.row(0, named=True)
    print(f"  {cid}: faction={r['faction']} adv={r['advancing']} eff={r['effect']} cond={r['condition']} mana={r['mana']}")

# filler per faction (the player's faction is derived from the deck - the filler
# must NOT change it, or the biome bonus math below is wrong)
FILLER_A = [c for c in DB.filter(pl.col('faction') == 'Dwarves')['card_id'].to_list()
            if c != A_GRAP][:12]
FILLER_B = [c for c in DB.filter(pl.col('faction') == 'Demons')['card_id'].to_list()
            if c not in (B_GRAP, B_PLAIN)][:12]

passed = 0
failed = 0

def check(condition, msg):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()

def new_game(version):
    p1 = PlayerState(name='A', deck=['epo', A_GRAP] + FILLER_A)
    p2 = PlayerState(name='B', deck=[B_GRAP, B_PLAIN] + FILLER_B)
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    gs.engine_version = version   # pin: 16 = buggy behavior, 17 = fixed
    # JU is a home biome for neither Dwarves (MO/OC) nor Demons (OC/DE) -> no biome bonus
    for cell in gs.earth:
        cell[0] = 'JU'
    return gs, gid

def force_hand(gs, name, cards):
    p = gs.players[name]
    for c in cards:
        if c not in p.hand:
            if c in p.deck:
                p.deck.remove(c)
            p.hand.append(c)

def act(gs, name, first_second, **msg):
    p = gs.players[name]
    while len(p.mana) - (p.mana_spend or 0) < 5 and p.deck:
        p.mana.append(p.deck.pop(0))
    p.mana_spend = 0
    p.message = dict(msg)
    p.message.setdefault('pendings', [])
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, ok, txt = ge.player_play(first_second, p, gs)
    assert ok, f"rejected: {txt} (msg={p.message})"
    return gs

def run_scenario(version):
    """ A: pending epo (NOT attached), then grappling | B: grappling, then plain. """
    gs, gid = new_game(version)
    a, b = gs.players['A'], gs.players['B']
    force_hand(gs, 'A', ['epo', A_GRAP])
    force_hand(gs, 'B', [B_GRAP, B_PLAIN])
    a0, b0 = a.current_position, b.current_position

    gs = act(gs, 'A', 'first', cards=['epo'], to='pending_zone', mode='')
    gs = act(gs, 'A', 'first', cards=[A_GRAP], to='stopover_3', mode='move')
    gs = act(gs, 'B', 'second', cards=[B_GRAP], to='stopover_4', mode='move')
    gs = act(gs, 'B', 'second', cards=[B_PLAIN], to='stopover_3', mode='move')
    gs = act(gs, 'A', 'first', cards=[], to='', mode='pass')
    gs = act(gs, 'B', 'second', cards=[], to='', mode='pass')
    # the WS layer (handle_websocket_message) runs the chain once both have
    # passed - the test drives it directly
    gs = ge.process_trip_chain(gs)

    a_delta = (a.current_position or 0) - a0
    b_delta = (b.current_position or 0) - b0

    # locate the log entries for this turn
    turn_log = gs.log[-1] if gs.log else None
    def entry(name, card):
        for sv in (turn_log or {}).get('stopovers', []):
            for e in sv.get('entries', []):
                if e.get('player') == name and card in (e.get('cards') or []):
                    return e
        return None

    a_entry = entry('A', A_GRAP)
    b_entry = entry('B', B_GRAP)
    a_pend = list((gs.players['A'].pendings or []))
    a_slots = list((gs.players['A'].pending_slots or []))
    _delete(gid)
    return {
        'a_delta': a_delta, 'b_delta': b_delta,
        'a_to': a_entry and a_entry.get('to'), 'b_to': b_entry and b_entry.get('to'),
        'a_notes': (a_entry or {}).get('notes', []),
        'b_notes': (b_entry or {}).get('notes', []),
        'a_pend': a_pend, 'a_slots': a_slots,
    }

print("\n=== v16 (pre-fix): grappling vs placeholder copies the NEXT row (bug) ===")
r16 = run_scenario(16)
print(f"  A (grappling)      delta: {r16['a_delta']:+d}  to={r16['a_to']}  notes={r16['a_notes']}")
print(f"  B (grappling)      delta: {r16['b_delta']:+d}  to={r16['b_to']}  notes={r16['b_notes']}")
# v16 buggy expectation (placeholder ignored -> both grappling face each other
# at chain position 1 and copy each other's base +1):
#   A = 1 base + 1 (copies B grap base 1) = 2
#   B = (1 base + 1 (copies A grap base 1)) + 4 (plain card 3+1) = 6
check(r16['a_delta'] == 2, f"v16 A delta == 2 (mutual copy): got {r16['a_delta']:+d}")
check(r16['b_delta'] == 6, f"v16 B delta == 6 (mutual copy + plain 4): got {r16['b_delta']:+d}")
check(any('copied' in n for n in r16['b_notes']), "v16 B grappling COPIED (the reported bug)")
check(r16['a_pend'] == ['epo'] and r16['a_slots'] == [4],
      f"v16 placeholder on stopover_4 (pending zone intact): {r16['a_pend']} {r16['a_slots']}")

print("\n=== v17 (fixed): grappling vs placeholder copies NOTHING ===")
r17 = run_scenario(17)
print(f"  A (grappling)      delta: {r17['a_delta']:+d}  to={r17['a_to']}  notes={r17['a_notes']}")
print(f"  B (grappling)      delta: {r17['b_delta']:+d}  to={r17['b_to']}  notes={r17['b_notes']}")
# v17 fixed expectation: B = 1 base, NO copy (faces A's placeholder)
#                        A = 1 base + 4 (copies B plain net 4) = 5
check(r17['a_to'] == 'stopover_3' and r17['b_to'] == 'stopover_4',
      f"display positions: A on stopover_3, B grap on stopover_4: {r17['a_to']}/{r17['b_to']}")
check(r17['b_delta'] == 5, f"v17 B delta == 5 (grap 1 NO-copy + plain 4): got {r17['b_delta']:+d}")
check(not any('copied' in n for n in r17['b_notes']), "v17 B grappling did NOT copy")
check(r17['a_delta'] == 5, f"v17 A delta == 5 (base 1 + copy 4): got {r17['a_delta']:+d}")
check(any('copied +4' in n for n in r17['a_notes']), f"v17 A copied +4 from B's plain card: {r17['a_notes']}")
check(r17['a_pend'] == ['epo'] and r17['a_slots'] == [4],
      f"v17 placeholder on stopover_4 (pending zone intact): {r17['a_pend']} {r17['a_slots']}")

# ---------------------------------------------------------------------------
# Scenario 2: pending card ATTACHED to the play -> the placeholder is consumed,
# leaving a GAP in the chain (A has no entry at position 1, only at position 2).
# This exercises the max_pos fix: max_pos must be the max POSITION (2), not
# max(len(chain)) (1), or A's play past the gap is never resolved.
#   A (Dwarves, first)  : pending 'epo' attached to the grappling -> gap@1, grappling@2 (base 1 + 1 epo = 2)
#   B (Demons, second)  : one plain card (net 4) -> position 1
# ---------------------------------------------------------------------------

def run_gap_scenario(version):
    gs, gid = new_game(version)
    a, b = gs.players['A'], gs.players['B']
    force_hand(gs, 'A', ['epo', A_GRAP])
    force_hand(gs, 'B', [B_PLAIN])
    a0, b0 = a.current_position, b.current_position

    # A places the pending (position 1), then attaches it to the grappling (position 2 -> gap@1)
    gs = act(gs, 'A', 'first', cards=['epo'], to='pending_zone', mode='')
    gs = act(gs, 'A', 'first', cards=[A_GRAP], to='stopover_3', mode='move', pendings=['epo'])
    gs = act(gs, 'B', 'second', cards=[B_PLAIN], to='stopover_4', mode='move')
    gs = act(gs, 'A', 'first', cards=[], to='', mode='pass')
    gs = act(gs, 'B', 'second', cards=[], to='', mode='pass')
    gs = ge.process_trip_chain(gs)

    a_delta = (a.current_position or 0) - a0
    b_delta = (b.current_position or 0) - b0
    turn_log = gs.log[-1] if gs.log else None
    def entry(name, card):
        for sv in (turn_log or {}).get('stopovers', []):
            for e in sv.get('entries', []):
                if e.get('player') == name and card in (e.get('cards') or []):
                    return e
        return None
    a_entry = entry('A', A_GRAP)
    a_pend = list((gs.players['A'].pendings or []))
    a_slots = list((gs.players['A'].pending_slots or []))
    _delete(gid)
    return {
        'a_delta': a_delta, 'b_delta': b_delta,
        'a_to': a_entry and a_entry.get('to'),
        'a_notes': (a_entry or {}).get('notes', []),
        'a_pend': a_pend, 'a_slots': a_slots,
    }

print("\n=== GAP (attached pending) - max_pos must reach the play past the gap ===")
g17 = run_gap_scenario(17)
print(f"  A (grappling+epo)  delta: {g17['a_delta']:+d}  to={g17['a_to']}  notes={g17['a_notes']}")
print(f"  B (plain)          delta: {g17['b_delta']:+d}")
# v17 fixed: A's grappling is at position 2 (gap@1 from the attached epo). max_pos
# must be 2 (max position), not 1 (max len), or A's play is never resolved (A=0).
# A = 1 base + 1 (epo) = 2, NO copy (faces nothing at position 2). B = 4 (plain net).
check(g17['a_pend'] == [] and g17['a_slots'] == [],
      f"pending consumed (attached): pendings={g17['a_pend']} slots={g17['a_slots']}")
check(g17['a_delta'] == 2, f"v17 A delta == 2 (base 1 + epo 1, RESOLVED past the gap): got {g17['a_delta']:+d} (0 = max_pos bug)")
check(any('pending epo' in n for n in g17['a_notes']), f"v17 A epo applied: {g17['a_notes']}")
check(not any('copied' in n for n in g17['a_notes']), "v17 A grappling did NOT copy (faces nothing at pos 2)")
check(g17['b_delta'] == 4, f"v17 B delta == 4 (plain net): got {g17['b_delta']:+d}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
