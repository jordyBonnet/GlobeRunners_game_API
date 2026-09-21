# Manual smoke test: MULTI-CARD DEFENSE (the engine contract the new defend popup
# relies on). The engine already supports defending 1-5 cards in ONE action:
#   - cost   = SUM of the selected cards' mana   (player_play, total_cost)
#   - shield = SUM of their shields              (_oppo_defend_shields, block race)
# This test drives the real WS entry point (handle_websocket_message + model_copy,
# like API.py / ai_driver.py) to verify:
#   1) a defend message with 2 cards is accepted as ONE defend action (cost = sum,
#      both cards leave the hand);
#   2) the block race uses the SUMMED shield (2 cards shield 3+3=6 blocks a cost-5
#      move card);
#   3) a single card (shield 3) does NOT block a cost-4 move card (3 < 4) - i.e. the
#      sum (not a per-card max) is what matters.
#
# Run from the project root:  uv run python tests/_multi_defend.py
# NOTE: writes to games.db; the games are deleted after.
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

# A's defend cards (Dwarves, mana 2, shield 3, no_condition):
AD1 = 'Dwa23_79c784'   # Hanna Zanna   (mana 2, shield 3, adv 2)
AD2 = 'Dwa23_5cd6c5'   # Filda Rurik   (mana 2, shield 3, adv 1)
# B's move cards (Dwarves, no_condition, advancing):
BM5 = 'Dwa56_4f1b44'   # Helgar Thorin (mana 5, adv 3) -> shield 6 >= 5  -> BLOCKED
BM4 = 'Dwa45_bda3fa'   # Ilsa Grak     (mana 4, adv 2) -> shield 3 <  4 -> NOT blocked
# mana-1 fillers (shield 0)
FILL = [c for c in ge.CARDS_DB.filter((pl.col('mana')==1) & (pl.col('shield')==0))['card_id'].to_list()[:80]]
assert len(FILL) >= 44, f'need >= 44 distinct mana-1 shield-0 fillers, got {len(FILL)}'
def f(i): return FILL[i]
# disjoint index ranges (no card shared between any zone / player):
# A: hand f0..f5, deck f6..f11, mana-boost f12..f21
# B: hand f22..f27 (+ move specials), deck f28..f33, mana-boost f34..f43
A_BOOST = [f(i) for i in range(12, 22)]
B_BOOST = [f(i) for i in range(34, 44)]

ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

def send(gid, name, message):
    conn, _, gs = ge.get_current_game(gid)
    player = gs.players[name].model_copy()
    player.message = message
    conn.close()
    resp = ge.handle_websocket_message(gid, player)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    return gs, resp

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit(); conn.close()

def new_game(a_hand_specials):
    # A: defend specials + 6 hand fillers (f0..f5) + deck (f6..f11)
    # B: move specials + 6 hand fillers (f22..f27) + deck (f28..f33)
    a_hand = list(a_hand_specials) + [f(i) for i in range(0, 6)]
    a_deck = [f(i) for i in range(6, 12)]
    b_hand = [BM5, BM4] + [f(i) for i in range(22, 28)]
    b_deck = [f(i) for i in range(28, 34)]
    p1 = PlayerState(name='A', deck=list(a_hand + a_deck))
    p2 = PlayerState(name='B', deck=list(b_hand + b_deck))
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    c, _, gs = ge.get_current_game(gid)
    gs.turn_order = ['A', 'B']
    for cell in gs.earth: cell[0] = 'OC'     # OC: not a Dwarves home biome (no +1)
    ga, gb = gs.players['A'], gs.players['B']
    ga.hand, ga.mana, ga.deck, ga.discard = a_hand, [], a_deck, []
    gb.hand, gb.mana, gb.deck, gb.discard = b_hand, [], b_deck, []
    c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    c.commit(); c.close()
    return gid

def init(gid, boost_mana=10):
    # both place 3 in mana -> turn 1 play phase; then top up the mana pools so the
    # cost limit (turn-1 budget = 3) does not constrain the defend/move under test.
    gs, _ = send(gid, 'A', {'cards': [f(0), f(1), f(2)], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [f(22), f(23), f(24)], 'to': 'mana', 'mode': '', 'pendings': []})
    assert 'turn 1' in gs.state and 'to play' in gs.state, f"init -> {gs.state}"
    # top up the mana pools (cost limit = turn-1 budget of 3) so the defend/move
    # under test is not cost-constrained; the boost cards are in no other zone.
    conn, _, gs = ge.get_current_game(gid)
    ga, gb = gs.players['A'], gs.players['B']
    ga.mana = list(ga.mana) + A_BOOST
    gb.mana = list(gb.mana) + B_BOOST
    conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    conn.commit(); conn.close()
    return gs

# ============================================================
# 1) MULTI-DEFEND is accepted as ONE action; cost = SUM of mana; both leave hand
# ============================================================
gid = new_game([AD1, AD2])
try:
    gs = init(gid)
    zA0 = list(gs.players['A'].hand)
    check('A has both defend cards in hand', AD1 in zA0 and AD2 in zA0)
    spend0 = gs.players['A'].mana_spend
    # A defends BOTH cards in a single message (to stopover_4 = position 1)
    gs, resp = send(gid, 'A', {'cards': [AD1, AD2], 'to': 'stopover_4', 'mode': 'defend', 'pendings': []})
    chainA = gs.players['A'].action_chain or []
    defend_actions = [a for a in chainA if a.get('mode') == 'defend']
    check('exactly ONE defend action recorded', len(defend_actions) == 1, f"(chain={chainA})")
    check('that defend action holds BOTH cards',
          len(defend_actions) == 1 and sorted(defend_actions[0]['cards']) == sorted([AD1, AD2]),
          f"(action={defend_actions[:1]})")
    check('cost = SUM of mana (2+2=4) -> mana_spend increased by 4',
          (gs.players['A'].mana_spend or 0) - (spend0 or 0) == 4,
          f"(spend0={spend0}, spend={gs.players['A'].mana_spend})")
    zA1 = list(gs.players['A'].hand)
    check('both defend cards left the hand', AD1 not in zA1 and AD2 not in zA1, f"(hand={zA1})")
    print(f"1) multi-defend accepted as ONE action, cost = sum, cards leave hand -> PASS")
    ok += 1
finally:
    _delete(gid)

# ============================================================
# 2) SHIELD SUM blocks a higher-cost move (3+3=6 >= cost 5)
# ============================================================
gid = new_game([AD1, AD2])
try:
    gs = init(gid)
    # A defends both (shield 6) on position 1; B moves a cost-5 card on position 1.
    gs, _ = send(gid, 'A', {'cards': [AD1, AD2], 'to': 'stopover_4', 'mode': 'defend', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [BM5], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    posB_before = gs.players['B'].current_position
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    # B's move card was BLOCKED -> no advancing (stays on cell 0)
    check('B cost-5 move BLOCKED by summed shield (6 >= 5) -> no advancing',
          gs.players['B'].current_position == posB_before,
          f"(pos {posB_before} -> {gs.players['B'].current_position})")
    # the log should show a block on B's line
    logstr = str(gs.log)
    check('log records a block on B\'s card', 'blocked' in logstr.lower(), "(log lacks 'blocked')")
    print(f"2) shield SUM (6) blocks the cost-5 move -> PASS")
    ok += 1
finally:
    _delete(gid)

# ============================================================
# 3) SHIELD SUM does NOT block (single card shield 3 < cost 4) - proves SUM, not max
# ============================================================
gid = new_game([AD1])   # only ONE defend card (shield 3)
try:
    gs = init(gid)
    gs, _ = send(gid, 'A', {'cards': [AD1], 'to': 'stopover_4', 'mode': 'defend', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [BM4], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    posB_before = gs.players['B'].current_position
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    # shield 3 < cost 4 -> NOT blocked -> B advances (adv 2, OC no bonus) 0 -> 2
    check('B cost-4 move NOT blocked by shield 3 (3 < 4) -> advancing applied',
          gs.players['B'].current_position > posB_before,
          f"(pos {posB_before} -> {gs.players['B'].current_position})")
    print(f"3) shield 3 does NOT block the cost-4 move (sum, not max) -> PASS")
    ok += 1
finally:
    _delete(gid)

print(f"\nALL {ok} MULTI-DEFEND TESTS PASSED (engine contract confirmed for the defend popup)")
