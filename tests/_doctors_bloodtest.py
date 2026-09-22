# Manual smoke test: DOCTORS bloodtest pending card — the discard effect is
# applied to the OPPONENT (treated as a `discard_oppo` effect, like the main
# faction cards), not to the player who attached the pending card.
#
#  - the trip chain PAUSES on pending_discard targeting the opponent
#    ("turn N - waiting for OPPONENT to discard 1 card(s)"); the opponent
#    CHOOSES the card (to: 'discard_pile') and the chain resumes — exactly
#    the discard-selection flow of the main-card discard_oppo.
#  - the attaching player's own hand is NEVER touched (regression: game
#    26_09_22_17_12_20_qs6EM, turn 3, the old code discarded from the
#    attacker's own hand).
#  - only the opponent may answer (the caster / wrong count / not-in-hand
#    are rejected, chain stays paused).
#  - opponent with an empty hand: no pause, log note, nothing discarded.
#  - card conservation holds in every end state.
#
# NOTE: like _choose_discard.py this drives every action through
# ge.handle_websocket_message() with a model_copy() - exactly like API.py /
# ai_driver.py - so the WS-layer pause/resume path is what is exercised.
#
# Run from the project root:  uv run python tests/_doctors_bloodtest.py
# NOTE: writes to games.db (like the other smoke tests); the games are deleted after.
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

MAIN = 'Twi11_68cfac'   # no_condition, mana 1, advancing 0, effect 'advancing' +2 (Twigs)

_db = ge.CARDS_DB
FILLER = [c for c in _db.filter((pl.col('faction') == 'Miaous') & (pl.col('mana') == 1))['card_id'].to_list()
          if c != MAIN]
assert len(FILLER) >= 64, f'need >= 64 distinct Miaous mana-1 fillers, got {len(FILLER)}'

# disjoint filler pools (no card shared between A/B or hand/mana/deck)
A_HAND = [0, 1, 2, 3, 4, 5]        # A hand fillers (up to 6)
A_MANA = [20, 21, 22, 23, 24, 25]  # A mana (unused)
A_DECK = [26, 27, 28, 29, 30, 31]  # A deck
B_HAND = [40, 41, 42, 43, 44, 45]  # B hand fillers
B_MANA = [46, 47, 48, 49, 50, 51]  # B mana (unused)
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

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()

def new_game():
    """ deterministic game. Mana starts EMPTY; init() places exactly 3 per player
       (the init check requires len(mana)==3). After init, A's hand =
       [bloodtest, MAIN, f(3), f(4), f(5)] and the mana pool is 3 (turn-1
       budget - exactly enough: bloodtest 2 + MAIN 1). B = 6 hand fillers
       (f40..f45), mana f40..f42 after init, deck. Biomes = OC (no home-biome
       bonus asserted: we check hands/zones, not positions). A first. """
    a_hand = ['bloodtest', MAIN] + [f(i) for i in A_HAND]
    a_deck = [f(i) for i in A_DECK]
    a_full = a_hand + a_deck
    b_hand = [f(i) for i in B_HAND]
    b_deck = [f(i) for i in B_DECK]
    b_full = b_hand + b_deck
    p1 = PlayerState(name='A', deck=list(a_full))
    p2 = PlayerState(name='B', deck=list(b_full))
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

    c, _, gs = ge.get_current_game(gid)
    gs.turn_order = ['A', 'B']
    for cell in gs.earth:
        cell[0] = 'OC'
    ga, gb = gs.players['A'], gs.players['B']
    ga.hand, ga.mana, ga.deck, ga.discard = a_hand, [], a_deck, []
    gb.hand, gb.mana, gb.deck, gb.discard = b_hand, [], b_deck, []
    c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    c.commit()
    c.close()
    return gid

def init(gid):
    """ both players place 3 cards in mana -> turn 1 play phase """
    gs, _ = send(gid, 'A', {'cards': [f(0), f(1), f(2)], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [f(40), f(41), f(42)], 'to': 'mana', 'mode': '', 'pendings': []})
    assert 'turn 1' in gs.state and 'to play' in gs.state, f"init should reach turn 1 play phase, state={gs.state}"
    return gs

B_CHOICE = f(43)   # a B hand filler that stays in hand after init
PAUSE_STATE = "turn 1 - waiting for B to discard 1 card(s)"

# ============================================================
# 1) WS layer: A attaches bloodtest -> the chain PAUSES for B (the
#    OPPONENT) to choose 1 card; A's own hand is never touched.
# ============================================================
gid = new_game()
try:
    init(gid)
    # A places bloodtest in the pending zone (cost 2)
    gs, _ = send(gid, 'A', {'cards': ['bloodtest'], 'to': 'pending_zone', 'mode': '', 'pendings': []})
    check('bloodtest placed in A\'s pending zone',
          'bloodtest' in (gs.players['A'].pendings or []), f"(pendings={gs.players['A'].pendings})")
    # A plays MAIN with bloodtest attached (cost 1)
    gs, _ = send(gid, 'A', {'cards': [MAIN], 'to': 'stopover_4', 'mode': 'move', 'pendings': ['bloodtest']})
    check('bloodtest consumed from A\'s pending zone at play time',
          'bloodtest' not in (gs.players['A'].pendings or []))
    # B passes, A passes -> trip chain resolves -> PAUSE on bloodtest
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})

    check('chain PAUSED waiting for B (the opponent) to discard',
          gs.state == PAUSE_STATE, f"(state={gs.state})")
    check('pending_discard targets B', gs.pending_discard == {'player': 'B', 'n': 1},
          f"(pending={gs.pending_discard})")
    check('chain_resume saved (paused mid-chain)', gs.chain_resume is not None)
    check('B\'s hand still intact while paused', B_CHOICE in zones(gs, 'B')['hand'])
    check('A\'s OWN hand untouched by bloodtest (the reported bug): only MAIN left the hand',
          sorted(zones(gs, 'A')['hand']) == sorted([f(3), f(4), f(5)]),
          f"(A hand={zones(gs, 'A')['hand']})")

    # A (the caster) must NOT answer (f(3) is in A's hand - still rejected)
    gs, _ = send(gid, 'A', {'cards': [f(3)], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    check('A (the caster) REJECTED - it is B who discards',
          gs.state == PAUSE_STATE and gs.pending_discard == {'player': 'B', 'n': 1})

    # wrong count rejected
    gs, _ = send(gid, 'B', {'cards': [B_CHOICE, f(44)], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    check('wrong count (2 instead of 1) REJECTED', gs.state == PAUSE_STATE)

    # a card not in B's hand rejected (MAIN is not in B hand)
    gs, _ = send(gid, 'B', {'cards': [MAIN], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    check('card-not-in-hand REJECTED (MAIN is not in B hand)', gs.state == PAUSE_STATE)

    # valid choice: B discards B_CHOICE
    zB = zones(gs, 'B')
    gs, _ = send(gid, 'B', {'cards': [B_CHOICE], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    zB2 = zones(gs, 'B')
    check('turn advanced after the choice', gs.state.startswith('turn 2') or 'mana or pass' in gs.state,
          f"(state={gs.state})")
    check('pending_discard cleared', gs.pending_discard is None)
    check('chain_resume cleared', gs.chain_resume is None)
    check('B\'s chosen card left B\'s hand', B_CHOICE not in zB2['hand'])
    check('B\'s chosen card is in B\'s discard pile', B_CHOICE in zB2['discard'])
    check('card conservation for B (12 cards)', len(all_cards(zB2)) == 12, f"({len(all_cards(zB2))})")
    # A's hand after the turn-end draw: the 3 fillers + up to 3 drawn cards.
    # bloodtest discarded NONE of them (the paused-state check above already
    # proved the hand was [f3, f4, f5] before the choice/draw).
    zA = zones(gs, 'A')
    check('A\'s hand = the 3 fillers + the turn-end draw (6 cards)',
          len(zA['hand']) == 6 and all(c in zA['hand'] for c in (f(3), f(4), f(5))),
          f"(A hand={zA['hand']})")
    check('A\'s discard holds exactly the 2 played cards (MAIN + bloodtest)',
          sorted(zA['discard']) == sorted([MAIN, 'bloodtest']), f"(A discard={zA['discard']})")
    # turn log: the choice note is on MAIN's line
    t1 = [t for t in (gs.log or []) if t.get('turn') == 1]
    main_notes = []
    for t in t1:
        for s in (t.get('stopovers') or []):
            for e in (s.get('entries') or []):
                if MAIN in (e.get('cards') or []):
                    main_notes = e.get('notes') or []
    check('turn log notes the opponent\'s choice on the card\'s line',
          any('discard selection' in n and 'B' in n for n in main_notes), f"(notes={main_notes})")
    print(f"1) WS-layer bloodtest -> opponent discard selection -> PASS")
    ok += 1
finally:
    _delete(gid)

# ============================================================
# 2) engine level: the OPPONENT chooses (pending_discard set, log flag),
#    and an EMPTY opponent hand -> no pause, note, nothing discarded.
# ============================================================
gid = new_game()
try:
    gs = init(gid)
    a, b = gs.players['A'], gs.players['B']
    entry = {'notes': [], 'negatives': []}

    # normal: B has cards in hand -> pause for B, log flag set
    gs = ge._apply_pending_effect('bloodtest', a, gs, log_entry=entry)
    check('engine-level: pending_discard set for B', gs.pending_discard == {'player': 'B', 'n': 1})
    check('engine-level: log entry marked _pending_discard=1', entry.get('_pending_discard') == 1)
    check('engine-level: A\'s hand untouched', len(a.hand) == 5, f"(A hand={a.hand})")
    check('engine-level: B\'s hand untouched until the choice', len(b.hand) == 3, f"(B hand={b.hand})")

    # empty opponent hand: no pause, explanatory note, nothing discarded
    gs.pending_discard = None
    b.hand, b.discard = [], []
    entry2 = {'notes': [], 'negatives': []}
    gs = ge._apply_pending_effect('bloodtest', a, gs, log_entry=entry2)
    check('empty opponent hand: NO pause (pending_discard stays None)', gs.pending_discard is None)
    check('empty opponent hand: note recorded',
          any('no cards in hand' in n for n in entry2['notes']), f"(notes={entry2['notes']})")
    print(f"2) engine-level pause + empty-hand branch -> PASS")
    ok += 1
finally:
    _delete(gid)

print(f"\nALL {ok} CHECKS PASSED")
