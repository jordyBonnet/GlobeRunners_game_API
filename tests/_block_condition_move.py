# Manual smoke test: the 'block' CONDITION must NOT be met when the card is
# played in MOVE mode (bug in game 26_09_22_20_55_31_VESGf, turn 1).
#
# BUG: is_condition_met('block') returned True unconditionally, so a
# block-condition card played NORMALLY (move mode) fired its effect
# (e.g. Dem33_fa4b4f 'Phyrra Yana', effect discard -> the player was forced
# to discard a card).
#
# FIX: is_condition_met('block') now returns False - it is only ever evaluated
# on the MOVE path (defend plays return before condition evaluation). A
# block-condition card in move mode: condition NOT met -> no effect, reduced
# advancing (mana - 1). Its effect fires ONLY when played in defend mode and it
# actually blocks (unchanged - _fire_block_effects).
#
# Scenario 1 (the bug, move mode):
#   A (Demons)  : Dem33_fa4b4f (block / discard, mana 3) played in MOVE mode
#   B (Dwarves) : pass
#   EXPECT (fixed): condition_met=False, NO discard selection (chain completes,
#                   pending_discard stays None), A advances by mana-1 = 2.
#   (BUGGY would be: condition_met=True, chain PAUSES on the discard selection.)
#
# Scenario 2 (defend regression):
#   A (Dwarves) : defend Dwa22_20e631 (block / draw 1, shield 2) on stopover_4
#   B (Demons)  : move Dem10_0d945f (mana 1) on stopover_4
#   EXPECT: shield race 2 >= 1 -> B BLOCKED (canceled), A's block card effect
#           FIRES (draw 1) - the defend path is unaffected by the fix.
#
# Run from the project root:  uv run python tests/_block_condition_move.py
# (deletes its own games)
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

# A1 (Demons): the user's card - block condition, discard effect, mana 3
MOVE_CARD = 'Dem33_fa4b4f'
# A2 (Dwarves): block condition, draw effect, shield 2 (defend scenario)
DEFEND_CARD = 'Dwa22_20e631'
# B2 (Demons): plain card, mana 1 (loses the shield race 2 >= 1)
B_MOVE = 'Dem10_0d945f'

for cid in (MOVE_CARD, DEFEND_CARD, B_MOVE):
    row = DB.filter(pl.col('card_id') == cid)
    assert row.height == 1, cid
    r = row.row(0, named=True)
    print(f"  {cid}: faction={r['faction']} adv={r['advancing']} eff={r['effect']} "
          f"cond={r['condition']} mana={r['mana']} shield={r['shield']}")

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

def new_game(a_cards, b_cards):
    FA = [c for c in DB.filter(pl.col('faction') == 'Demons')['card_id'].to_list() if c not in (a_cards, B_MOVE, MOVE_CARD)][:12]
    FB = [c for c in DB.filter(pl.col('faction') == 'Dwarves')['card_id'].to_list() if c not in (b_cards, DEFEND_CARD)][:12]
    p1 = PlayerState(name='A', deck=a_cards + FA)
    p2 = PlayerState(name='B', deck=b_cards + FB)
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    # JU is a home biome for neither Demons (OC/DE) nor Dwarves (MO/OC) -> no biome bonus
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
    while len(p.mana) - (p.mana_spend or 0) < 6 and p.deck:
        p.mana.append(p.deck.pop(0))
    p.mana_spend = 0
    p.message = dict(msg)
    p.message.setdefault('pendings', [])
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, ok, txt = ge.player_play(first_second, p, gs)
    assert ok, f"rejected: {txt} (msg={p.message})"
    return gs

def entry_of(turn_log, name, card):
    for sv in (turn_log or {}).get('stopovers', []):
        for e in sv.get('entries', []):
            if e.get('player') == name and card in (e.get('cards') or []):
                return e
    return None

# ---------------------------------------------------------------------------
print("\n=== Scenario 1: block card played in MOVE mode -> condition NOT met ===")
gs, gid = new_game([MOVE_CARD], [])
a, b = gs.players['A'], gs.players['B']
force_hand(gs, 'A', [MOVE_CARD])
a0 = a.current_position or 0

gs = act(gs, 'A', 'first', cards=[MOVE_CARD], to='stopover_4', mode='move')
gs = act(gs, 'B', 'second', cards=[], to='', mode='pass')
gs = ge.process_trip_chain(gs)

a_delta = (a.current_position or 0) - a0
turn_log = gs.log[-1] if gs.log else None
e = entry_of(turn_log, 'A', MOVE_CARD)
a_hand = list(a.hand)
pd = gs.pending_discard
_delete(gid)
print(f"  A delta: {a_delta:+d}  pending_discard: {pd}")
print(f"  entry: condition_met={e and e.get('condition_met')} negatives={e and e.get('negatives')} notes={e and e.get('notes')}")
check(e is not None, "log entry exists for the move play")
check(e and e.get('condition_met') is False, f"condition 'block' NOT met in move mode: {e and e.get('condition_met')}")
check(pd is None, f"no discard selection paused the chain: pending_discard={pd}")
check(a_delta == 2, f"reduced advancing (mana 3 - 1 = 2), no effect: got {a_delta:+d}")
check(not (e and any('discard' in str(n) for n in e.get('notes', []))), "no discard note in the log")

# ---------------------------------------------------------------------------
print("\n=== Scenario 2: block card in DEFEND mode that blocks -> effect FIRES ===")
gs, gid = new_game([], [B_MOVE])
a, b = gs.players['A'], gs.players['B']
force_hand(gs, 'A', [DEFEND_CARD])
force_hand(gs, 'B', [B_MOVE])
b0 = b.current_position or 0

gs = act(gs, 'A', 'first', cards=[DEFEND_CARD], to='stopover_4', mode='defend')
gs = act(gs, 'B', 'second', cards=[B_MOVE], to='stopover_4', mode='move')
gs = act(gs, 'A', 'first', cards=[], to='', mode='pass')
gs = act(gs, 'B', 'second', cards=[], to='', mode='pass')
gs = ge.process_trip_chain(gs)

b_delta = (b.current_position or 0) - b0
turn_log = gs.log[-1] if gs.log else None
ea = entry_of(turn_log, 'A', DEFEND_CARD)
eb = entry_of(turn_log, 'B', B_MOVE)
_delete(gid)
print(f"  B delta: {b_delta:+d}")
print(f"  A entry: notes={ea and ea.get('notes')} shield={ea and ea.get('shield')}")
print(f"  B entry: negatives={eb and eb.get('negatives')}")
check(ea is not None and eb is not None, "log entries exist for both plays")
check(eb and any('blocked' in str(n) for n in eb.get('negatives', [])), "B's card BLOCKED (shield race 2 >= 1)")
check(b_delta == 0, f"B canceled: no advancing: got {b_delta:+d}")
check(ea and any('effect fired: draw' in str(n) for n in ea.get('notes', [])),
      f"A's block-card effect FIRED (defend path unaffected): {ea and ea.get('notes')}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
