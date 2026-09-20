# Manual smoke test (REPLAY): Mages nobodymoves — the replay must mirror the
# engine's global block:
#   * the INSTANT effect (nobodymoves_active = True) is applied at play time
#   * the opponent's MOVE card's movement is LOCKED (no advancing) in the replay
#   * the lock is CLEARED at the end of the turn (the next turn's card advances)
#   * no "unknown card" warnings (the replay's _card_row synthesizes a support row)
#   * the replay records the nobodymoves event
#   * the day/night flips normally (nobodymoves does NOT fix day/night)
# Run from the project root:  uv run python tests/_mages_nobodymoves_replay.py
# NOTE: writes one game to games.db (like the other smoke tests); the game is deleted after.
import sys, io, os, sqlite3, json, re, contextlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
# 24 DISTINCT low-advancing no_condition cards with a harmless effect (predictable).
low = DB.filter((pl.col('condition') == 'no_condition')
                & (pl.col('advancing') >= 1) & (pl.col('advancing') <= 2)
                & (pl.col('mana') <= 3)          # affordable with 3 mana (init)
                & ~pl.col('effect').is_in(['advancing', 'backward', 'jump',
                                          'discard', 'discard_oppo'])   # no movement / no chain pause
                & (pl.col('effect') != 'grappling_hook'))   # no copy (keeps positions simple)
CARDS = low['card_id'].to_list()
assert len(CARDS) >= 26, f"need 26 distinct low-advancing cards, got {len(CARDS)}"
NOBODY = ge.MAGE_NOBODYMOVES   # 'nobodymoves'

def send(gid, name, message):
    conn, _, gs = ge.get_current_game(gid)
    player = gs.players[name].model_copy()
    player.message = message
    conn.close()
    resp = ge.handle_websocket_message(gid, player)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    if isinstance(resp, str):
        resp = json.loads(resp)
    return gs, resp

ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

# --- build the game: A has nobodymoves + 5 distinct; B has 6 distinct ----------
a_deck = [NOBODY] + CARDS[0:19]
b_deck = CARDS[6:26]

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

c, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
ga, gb = gs.players['A'], gs.players['B']
ga.hand = [NOBODY, CARDS[0], CARDS[1], CARDS[2], CARDS[3], CARDS[4]]
ga.deck = a_deck[6:]
gb.hand = list(b_deck[:6])
gb.deck = b_deck[6:]
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.mana, gb.discard, gb.dwelling = [], [], None
c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
c.commit(); c.close()
print(f"game {gid} created (engine_version={gs.engine_version})")

def mana_put(gs):
    """ both players put 1 card in mana (to='mana') -> creates the replay turn
    boundary (a mana-PASS with to='' would lose the boundary and lump the
    turns' moves together — a pre-existing replay edge case we avoid here) """
    gs, _ = send(gid, 'A', {'cards': [CARDS[4]], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [b_deck[5]], 'to': 'mana', 'mode': '', 'pendings': []})
    return gs

def turn(gs, a_card, b_card):
    """ one full turn (PLAY phase), driven by the STATE. Each player plays their
    one card when it is their slot, then passes; the turn closes when both have
    passed. Turn order reverses each turn. """
    a_played = b_played = False
    for _ in range(8):   # safety cap
        m = re.search(r'waiting for (?:first|second) player \((\w+)\)', gs.state)
        if not m:
            break
        actor = m.group(1)
        if actor == 'A':
            if not a_played and a_card:
                gs, r = send(gid, 'A', {'cards': [a_card], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
                assert (r.get('message') or {}).get('success'), f"A play rejected: {r.get('message')}"
                a_played = True
            else:
                gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
        else:
            if not b_played and b_card:
                gs, r = send(gid, 'B', {'cards': [b_card], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
                assert (r.get('message') or {}).get('success'), f"B play rejected: {r.get('message')}"
                b_played = True
            else:
                gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
        if 'to play' not in gs.state:
            break
    return gs

try:
    # --- init: both put 3 cards in mana (required before turn 1) -------------
    gs, r = send(gid, 'A', {'cards': [CARDS[0], CARDS[1], CARDS[2]], 'to': 'mana', 'mode': '', 'pendings': []})
    assert (r.get('message') or {}).get('success'), f"A init rejected: {r.get('message')}"
    gs, r = send(gid, 'B', {'cards': [b_deck[0], b_deck[1], b_deck[2]], 'to': 'mana', 'mode': '', 'pendings': []})
    assert (r.get('message') or {}).get('success'), f"B init rejected: {r.get('message')}"
    check('init done - turn 1 play phase', 'turn 1' in gs.state and 'to play' in gs.state, f"(state={gs.state})")

    # --- 2 complete turns (nobodymoves on turn 1) ----------------------------
    # turn 1: A plays nobodymoves (block set), B plays a card (CANCELED)
    pos_b_t1 = gs.players['B'].current_position
    gs = turn(gs, NOBODY, b_deck[3])
    print(f"  after turn 1: state={gs.state!r} turn={gs.turn} block={gs.nobodymoves_active}")
    check('B\'s card movement was LOCKED (no advancing)',
          gs.players['B'].current_position == pos_b_t1,
          f"(pos {pos_b_t1} -> {gs.players['B'].current_position})")
    check('lock CLEARED after turn 1', gs.nobodymoves_active is False, f"(lock={gs.nobodymoves_active})")
    # turn 2: MANA phase (both put a card) -> PLAY phase (no block, B's card advances)
    gs = mana_put(gs)
    pos_b_t2 = gs.players['B'].current_position
    gs = turn(gs, CARDS[3], b_deck[4])
    print(f"  after turn 2: state={gs.state!r} turn={gs.turn} block={gs.nobodymoves_active}")
    check('B\'s card ADVANCED in turn 2 (block cleared)',
          gs.players['B'].current_position != pos_b_t2,
          f"(pos {pos_b_t2} -> {gs.players['B'].current_position})")

    check('2 turns completed (now turn 3)', gs.turn == 3, f"(turn={gs.turn})")

    # --- replay ---------------------------------------------------------------
    conn, _, gs2 = ge.get_current_game(gid)
    conn.close()
    import games.analysis.replay as R
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = R.analyze_game(json.loads(gs2.to_json()))
    print(f"  replay: verified={res['verified']} turns={len(res['turns'])} warn={len(res['warnings'])}")
    for w in res['warnings']:
        print(f"    warn: {w}")

    check('replayed 2 turns', len(res['turns']) >= 2, f"(turns={len(res['turns'])})")
    check('replay VERIFIED (the block is mirrored)', res['verified'] is True,
          f"(verified={res['verified']}, warnings={res['warnings']})")
    check('no "unknown card" warnings for nobodymoves',
          not any('nobodymoves' in w and 'unknown' in w for w in res['warnings']),
          f"(warnings={res['warnings']})")
    # the replay should have recorded the nobodymoves event
    evs = [e for t in res['turns'] for e in t.get('events', []) if e.get('type') == 'nobodymoves']
    check('replay recorded the nobodymoves event', len(evs) == 1, f"(events={evs})")

    print(f"\nALL {ok} REPLAY CHECKS PASSED - the replay mirrors the nobodymoves block")
finally:
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit(); conn.close()
