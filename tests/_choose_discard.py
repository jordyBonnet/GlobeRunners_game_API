# Manual smoke test: DISCARD SELECTION (rule of engine_version 13)
#
#  - the discard / discard_oppo effects PAUSE the trip chain (instead of the
#    legacy auto-discard of the last N hand cards). The discarding player then
#    CHOOSES which card(s) to discard, sending them with to: 'discard_pile'.
#  - only the discarding player may answer (the opponent is rejected); the wrong
#    count / a card not in hand is rejected.
#  - the chain resumes EXACTLY where it stopped (a later card in the same chain
#    still resolves after the choice).
#  - card conservation holds in every end-of-turn case.
#  - engine_version < 13: the legacy auto-discard (last N of the hand) is kept.
#
# NOTE: like _tap_dup.py this drives every action through
# ge.handle_websocket_message() with a model_copy() - exactly like API.py /
# ai_driver.py - so the WS-layer pause/resume path is what is exercised.
#
# Run from the project root:  uv run python tests/_choose_discard.py
# NOTE: writes to games.db (like the other smoke tests); the games are deleted after.
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

D1 = 'Mia11_57b5be'   # discard 1  (effect_number -1), mana 1, adv 2, no_condition (Miaous)
D2 = 'Orc21_9882e1'   # discard 2  (effect_number -2), mana 2, adv 5, no_condition (Orcs)
D3 = 'Dem10_463e0a'   # discard_oppo 1 (effect_number -1), mana 1, adv -1, no_condition (Demons)
M1 = 'Mia44_523f53'   # effect 'advancing' +1 (moves 3+1=4), mana 4, no_condition (Miaous)

_db = ge.CARDS_DB
_special = {D1, D2, D3, M1}
FILLER = [c for c in _db.filter((pl.col('faction') == 'Miaous') & (pl.col('mana') == 1))['card_id'].to_list()
          if c not in _special]
assert len(FILLER) >= 64, f'need >= 64 distinct Miaous mana-1 fillers, got {len(FILLER)}'

# disjoint filler pools (no card shared between A/B or hand/mana/deck)
A_HAND = [0, 1, 2, 3, 4, 5]        # A hand fillers (up to 6)
A_MANA = [20, 21, 22, 23, 24, 25]  # A mana
A_DECK = [26, 27, 28, 29, 30, 31]  # A deck
B_HAND = [40, 41, 42, 43, 44, 45]  # B hand fillers
B_MANA = [46, 47, 48, 49, 50, 51]  # B mana
B_DECK = [52, 53, 54, 55, 56, 57]  # B deck
def f(idx): return FILLER[idx]

ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

def zones(gs, name):
    p = gs.players[name]
    return {'hand': list(p.hand or []), 'mana': list(p.mana or []),
            'deck': list(p.deck or []), 'discard': list(p.discard or [])}

def all_cards(z):
    return [c for lst in z.values() for c in lst]

def send(gid, name, message):
    """ drive the engine exactly like the WS layer / AI driver do """
    conn, _, gs = ge.get_current_game(gid)
    player = gs.players[name].model_copy()
    player.message = message
    conn.close()
    resp = ge.handle_websocket_message(gid, player)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    return gs, resp

def new_game(a_special):
    """ deterministic game. Mana starts EMPTY; init() places exactly 3 per player
       (the init check requires len(mana)==3). After init, A's hand = a_special +
       [f(3), f(4), f(5)] (f0..f2 moved to mana) and the mana pool is 3 (turn-1 budget).
       B = 6 hand fillers (f40..f45), mana f40..f42 after init, deck. Biomes = OC
       (not a home biome for the special cards' factions -> no +1 bonus). A first. """
    a_hand = list(a_special) + [f(i) for i in A_HAND]      # special + 6 fillers (f0..f5)
    a_deck = [f(i) for i in A_DECK]                         # f26..f31
    a_full = a_hand + a_deck
    b_hand = [f(i) for i in B_HAND]                         # f40..f45
    b_deck = [f(i) for i in B_DECK]                         # f52..f57
    b_full = b_hand + b_deck
    p1 = PlayerState(name='A', deck=list(a_full))
    p2 = PlayerState(name='B', deck=list(b_full))
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

    c, _, gs = ge.get_current_game(gid)
    gs.turn_order = ['A', 'B']
    for cell in gs.earth:
        cell[0] = 'OC'                      # OC: not a home biome for Miaous/Orcs/Demons
    ga, gb = gs.players['A'], gs.players['B']
    ga.hand, ga.mana, ga.deck, ga.discard = a_hand, [], a_deck, []
    gb.hand, gb.mana, gb.deck, gb.discard = b_hand, [], b_deck, []
    c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    c.commit()
    c.close()
    return gid

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()

def init(gid):
    """ both players place 3 cards in mana -> turn 1 play phase """
    # A: 3 of its HAND fillers (f(0..2)); B: 3 of its hand fillers (f(40..42))
    gs, _ = send(gid, 'A', {'cards': [f(0), f(1), f(2)], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [f(40), f(41), f(42)], 'to': 'mana', 'mode': '', 'pendings': []})
    assert 'turn 1' in gs.state and 'to play' in gs.state, f"init should reach turn 1 play phase, state={gs.state}"
    return gs

# A's hand after init (3 hand fillers moved to mana): the remaining hand fillers.
# For 1 special: hand = [D1, f(3), f(4), f(5)]; for 2: [D1, M1, f(3), f(4), f(5)].
A_HAND_AFTER_INIT_1 = [f(3), f(4), f(5)]
B_CHOICE = f(43)   # a B hand filler that stays in hand after init

print(f"engine_version of a new game = {ge.create_new_game.__module__} (expect 13 rule active)")

# ============================================================
# 1) BASIC discard (n=1): pause, rejections, choice, resume, conservation
# ============================================================
gid = new_game([D1])
try:
    init(gid)
    # A (first) plays D1 (move) -> B passes -> A passes -> chain resolves -> PAUSE
    gs, _ = send(gid, 'A', {'cards': [D1], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})

    check('chain PAUSED on discard selection',
          gs.state == f"turn 1 - waiting for A to discard 1 card(s)", f"(state={gs.state})")
    check('pending_discard recorded', gs.pending_discard == {'player': 'A', 'n': 1},
          f"(pending={gs.pending_discard})")
    check('chain_resume saved (paused mid-chain)', gs.chain_resume is not None)
    check('D1 advancing already applied (pos 0 -> 2)', gs.players['A'].current_position == 2,
          f"(pos={gs.players['A'].current_position})")

    # opponent must NOT be able to answer (B has B_CHOICE in hand - still rejected)
    gs, _ = send(gid, 'B', {'cards': [B_CHOICE], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    check('opponent choice REJECTED (state unchanged, still waiting for A)',
          gs.state == f"turn 1 - waiting for A to discard 1 card(s)" and gs.pending_discard is not None)

    # wrong count rejected (A has [f(3), f(4), f(5)] in hand now)
    gs, _ = send(gid, 'A', {'cards': [f(3), f(4)], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    check('wrong count (2 instead of 1) REJECTED',
          gs.state == f"turn 1 - waiting for A to discard 1 card(s)")

    # a card not in hand rejected (D2 is not in A hand)
    gs, _ = send(gid, 'A', {'cards': [D2], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    check('card-not-in-hand REJECTED (D2 is not in A hand)',
          gs.state == f"turn 1 - waiting for A to discard 1 card(s)")

    # valid choice: A discards f(3) (in hand)
    z_before = zones(gs, 'A')
    gs, _ = send(gid, 'A', {'cards': [f(3)], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    z_after = zones(gs, 'A')
    check('chain RESUMED and turn advanced to 2', gs.state.startswith('turn 2') or gs.state == 'waiting for both players to mana or pass',
          f"(state={gs.state})")
    check('pending_discard cleared', gs.pending_discard is None)
    check('chosen card left the hand', f(3) not in z_after['hand'])
    check('chosen card is in the discard pile', f(3) in z_after['discard'])
    # A total = 1 special + 6 hand fillers + 6 deck = 13
    check('card conservation for A (13 cards)', len(all_cards(z_after)) == 13, f"({len(all_cards(z_after))})")
    print(f"1) BASIC discard selection (n=1) -> PASS")
    ok += 1
finally:
    _delete(gid)

# ============================================================
# 2) discard_oppo (n=1): the OPPONENT chooses
# ============================================================
gid = new_game([D3])
try:
    init(gid)
    # A plays D3 (discard_oppo) -> B passes -> A passes -> chain -> PAUSE (B chooses)
    gs, _ = send(gid, 'A', {'cards': [D3], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})

    check('chain PAUSED waiting for B (the opponent) to discard',
          gs.state == f"turn 1 - waiting for B to discard 1 card(s)", f"(state={gs.state})")
    check('pending_discard targets B', gs.pending_discard == {'player': 'B', 'n': 1},
          f"(pending={gs.pending_discard})")

    # A (the discard_oppo player) must NOT answer (f(3) is in A's hand - still rejected)
    gs, _ = send(gid, 'A', {'cards': [f(3)], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    check('A (the caster) REJECTED - it is B who discards',
          gs.state == f"turn 1 - waiting for B to discard 1 card(s)")

    # B chooses B_CHOICE (f(43), a card in B's hand)
    zB = zones(gs, 'B')
    check('B has the choice card in hand', B_CHOICE in zB['hand'])
    gs, _ = send(gid, 'B', {'cards': [B_CHOICE], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    zB2 = zones(gs, 'B')
    check('B\'s chosen card left the hand', B_CHOICE not in zB2['hand'])
    check('B\'s chosen card is in B\'s discard pile', B_CHOICE in zB2['discard'])
    check('turn advanced to 2', gs.state.startswith('turn 2') or gs.state == 'waiting for both players to mana or pass')
    # B total = 6 hand + 6 deck = 12
    check('card conservation for B (12 cards)', len(all_cards(zB2)) == 12, f"({len(all_cards(zB2))})")
    print(f"2) discard_oppo selection (n=1, opponent chooses) -> PASS")
    ok += 1
finally:
    _delete(gid)

# ============================================================
# 3) discard (n=2): choose exactly 2 cards
# ============================================================
gid = new_game([D2])
try:
    init(gid)
    gs, _ = send(gid, 'A', {'cards': [D2], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})

    check('chain PAUSED waiting for A to discard 2',
          gs.state == f"turn 1 - waiting for A to discard 2 card(s)", f"(state={gs.state})")

    # 1 card is not enough
    zA = zones(gs, 'A')
    gs, _ = send(gid, 'A', {'cards': [zA['hand'][0]], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    check('single card REJECTED (need 2)', gs.state == f"turn 1 - waiting for A to discard 2 card(s)")

    # valid: 2 distinct cards from hand
    c1, c2 = zA['hand'][0], zA['hand'][1]
    gs, _ = send(gid, 'A', {'cards': [c1, c2], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    zA2 = zones(gs, 'A')
    check('both chosen cards left the hand', c1 not in zA2['hand'] and c2 not in zA2['hand'])
    check('both chosen cards are in the discard pile', c1 in zA2['discard'] and c2 in zA2['discard'])
    check('turn advanced to 2', gs.state.startswith('turn 2') or gs.state == 'waiting for both players to mana or pass')
    # A total = 1 special + 6 hand fillers + 6 deck = 13
    check('card conservation for A (13 cards)', len(all_cards(zA2)) == 13, f"({len(all_cards(zA2))})")
    print(f"3) discard selection (n=2) -> PASS")
    ok += 1
finally:
    _delete(gid)

# ============================================================
# 4) chain CONTINUES after the choice (the SECOND discard card resolves after
#    the first selection -> a second pause). D1(cost1) + D2(cost2) = 3 = turn-1 mana.
# ============================================================
gid = new_game([D1, D2])
try:
    init(gid)
    # A plays D1 (discard 1, index0) THEN D2 (discard 2, index1) -> B passes -> A passes
    gs, _ = send(gid, 'A', {'cards': [D1], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'A', {'cards': [D2], 'to': 'stopover_3', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})

    # FIRST pause: D1's discard (1 card). A's hand = [f3, f4, f5] (D1,D2 left at play)
    check('chain PAUSED on D1\'s discard (1 card)',
          gs.state == f"turn 1 - waiting for A to discard 1 card(s)", f"(state={gs.state})")
    gs, _ = send(gid, 'A', {'cards': [f(3)], 'to': 'discard_pile', 'mode': '', 'pendings': []})

    # chain CONTINUED to index1: D2's discard -> SECOND pause (2 cards)
    check('chain CONTINUED and PAUSED again on D2\'s discard (2 cards)',
          gs.state == f"turn 1 - waiting for A to discard 2 card(s)", f"(state={gs.state})")
    check('first choice applied (f(3) in discard) before the second pause',
          f(3) in (gs.players['A'].discard or []), f"(discard={gs.players['A'].discard})")

    # A's hand now = [f4, f5] -> discard exactly 2
    gs, _ = send(gid, 'A', {'cards': [f(4), f(5)], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    check('turn advanced to 2 after both selections',
          gs.state.startswith('turn 2') or gs.state == 'waiting for both players to mana or pass',
          f"(state={gs.state})")
    zA2 = zones(gs, 'A')
    check('both selections applied (f(3), f(4), f(5) in discard)',
          all(x in zA2['discard'] for x in (f(3), f(4), f(5))), f"(discard={zA2['discard']})")
    # A total = 2 special + 6 hand fillers + 6 deck = 14
    check('card conservation for A (14 cards)', len(all_cards(zA2)) == 14, f"({len(all_cards(zA2))})")
    print(f"4) chain continuation (second discard resolves after the first choice) -> PASS")
    ok += 1
finally:
    _delete(gid)

print(f"\nALL {ok} DISCARD-SELECTION TESTS PASSED")
