# Manual smoke test (REPLAY): Mages black_hole — the replay must mirror the
# engine's earth rotation:
#   * the TAP (a quick action, not part of the trip chain) is applied in the
#     play-phase message loop, BEFORE the trip chain resolves — mirroring the engine
#     (so the rotation is in place for the chain's biome conditions / biome bonus).
#   * the replay reconstructs the INITIAL earth (the stored final earth reversed by the
#     total rotation) so the per-turn earth starts correct and each tap advances it.
#   * the final replayed earth matches the stored final earth (no "earth biomes
#     diverge" warning).
#   * the black_hole_tap event is recorded with the correct direction.
# Run from the project root:  uv run python tests/_mages_black_hole_replay.py
# NOTE: writes one game to games.db (like the other smoke tests); the game is deleted after.
import sys, io, os, sqlite3, json, re, contextlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
# 24 DISTINCT low-advancing no_condition cards with a harmless effect (predictable,
# no movement / no chain pause / no copy, so the positions stay small and the game
# never wins — the replay focus is the earth rotation, not the trip chain).
low = DB.filter((pl.col('condition') == 'no_condition')
                & (pl.col('advancing') >= 1) & (pl.col('advancing') <= 2)
                & (pl.col('mana') <= 3)
                & ~pl.col('effect').is_in(['advancing', 'backward', 'jump',
                                          'discard', 'discard_oppo'])
                & (pl.col('effect') != 'grappling_hook'))
CARDS = low['card_id'].to_list()
assert len(CARDS) >= 24, f"need 24 distinct low-advancing cards, got {len(CARDS)}"
BLACK = ge.MAGE_BLACK_HOLE

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

def codes(gs):
    return [cell[0] for cell in gs.earth]

# --- build the game: A has black_hole + 19; B has 24 -------------------------
a_deck = [BLACK] + CARDS[0:19]
b_deck = CARDS[6:24] + CARDS[0:2]   # distinct enough

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

# --- deterministic zones + a KNOWN initial earth order ----------------------
c, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
order = ['OC', 'MO', 'DE', 'JU']
for i, biome in enumerate(order):
    for j in range(6):
        gs.earth[i * 6 + j][0] = biome
gs.earth[0].append('A')
gs.earth[0].append('B')
gs.earth_initial_b0 = order[0]
gs.earth_rotation = 0
ga, gb = gs.players['A'], gs.players['B']
ga.hand = [BLACK, CARDS[0], CARDS[1], CARDS[2], CARDS[3], CARDS[4]]
ga.deck = a_deck[6:]
gb.hand = list(b_deck[:6])
gb.deck = b_deck[6:]
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.mana, gb.discard, gb.dwelling = [], [], None
c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
c.commit(); c.close()
print(f"game {gid} created (engine_version={gs.engine_version})")

def mana_pass(gs):
    """ both players pass the mana phase (no card to mana) -> play phase begins """
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    return gs

def turn(gs, a_action, b_card):
    """ one full turn (PLAY phase), driven by the STATE. `a_action` is either a card
    id (a move play), 'black_hole_place' (place the dwelling), 'black_hole_cw' /
    'black_hole_ccw' (tap the dwelling), or None (pass). `b_card` is B's move play. """
    a_done = b_done = False
    for _ in range(10):   # safety cap
        m = re.search(r'waiting for (?:first|second) player \((\w+)\)', gs.state)
        if not m:
            break
        actor = m.group(1)
        if actor == 'A':
            if not a_done and a_action:
                if a_action == 'black_hole_place':
                    gs, r = send(gid, 'A', {'cards': [BLACK], 'to': 'dwelling', 'mode': '', 'pendings': []})
                elif a_action in ('black_hole_cw', 'black_hole_ccw'):
                    gs, r = send(gid, 'A', {'cards': [], 'to': 'dwelling', 'mode': 'dwelling_activation',
                                            'pendings': [], 'rotation': 'cw' if a_action == 'black_hole_cw' else 'ccw'})
                else:
                    gs, r = send(gid, 'A', {'cards': [a_action], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
                assert (r.get('message') or {}).get('success'), f"A action rejected: {r.get('message')}"
                a_done = True
            else:
                gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
        else:
            if not b_done and b_card:
                gs, r = send(gid, 'B', {'cards': [b_card], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
                assert (r.get('message') or {}).get('success'), f"B play rejected: {r.get('message')}"
                b_done = True
            else:
                gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
        if 'to play' not in gs.state:
            break
    return gs

try:
    # --- init: both put 3 cards in mana (A keeps the black_hole in hand) -----
    gs, r = send(gid, 'A', {'cards': [CARDS[0], CARDS[1], CARDS[2]], 'to': 'mana', 'mode': '', 'pendings': []})
    assert (r.get('message') or {}).get('success'), f"A init rejected: {r.get('message')}"
    gs, r = send(gid, 'B', {'cards': b_deck[:3], 'to': 'mana', 'mode': '', 'pendings': []})
    assert (r.get('message') or {}).get('success'), f"B init rejected: {r.get('message')}"
    check('init done - turn 1 play phase', 'turn 1' in gs.state and 'to play' in gs.state, f"(state={gs.state})")
    check('initial earth_rotation is 0', gs.earth_rotation == 0, f"({gs.earth_rotation})")
    check('initial earth_initial_b0 is OC', gs.earth_initial_b0 == 'OC', f"({gs.earth_initial_b0})")

    # --- turn 1: A PLACES the black_hole; B plays a card ---------------------
    gs = turn(gs, 'black_hole_place', b_deck[3])
    print(f"  after turn 1: state={gs.state!r} turn={gs.turn} rot={gs.earth_rotation} dwelling={gs.players['A'].dwelling}")
    check('A placed the black_hole (turn 1)', gs.players['A'].dwelling == BLACK, f"({gs.players['A'].dwelling})")
    check('earth_rotation still 0 (placement does not rotate)', gs.earth_rotation == 0, f"({gs.earth_rotation})")

    # --- turn 2: A TAPS the black_hole CW; B plays a card --------------------
    gs = mana_pass(gs)
    gs = turn(gs, 'black_hole_cw', b_deck[4])
    print(f"  after turn 2: state={gs.state!r} turn={gs.turn} rot={gs.earth_rotation}")
    check('A tapped the black_hole CW (turn 2)', gs.earth_rotation == 3, f"({gs.earth_rotation})")
    base = ['OC']*6 + ['MO']*6 + ['DE']*6 + ['JU']*6
    check('earth rotated 3 cells CW (stored final earth)',
          codes(gs) == [base[(i - 3) % 24] for i in range(24)], f"(earth={codes(gs)})")

    # --- turn 3: both play normal cards (the rotation is now permanent) ------
    gs = mana_pass(gs)
    gs = turn(gs, CARDS[3], b_deck[5])
    print(f"  after turn 3: state={gs.state!r} turn={gs.turn} rot={gs.earth_rotation}")
    check('rotation is PERMANENT (still 3 after turn 3)', gs.earth_rotation == 3, f"({gs.earth_rotation})")
    check('earth still rotated 3 cells CW',
          codes(gs) == [base[(i - 3) % 24] for i in range(24)], f"(earth={codes(gs)})")

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

    check('replayed >= 2 turns', len(res['turns']) >= 2, f"(turns={len(res['turns'])})")
    # THE KEY CHECK: the earth biomes are mirrored (no divergence) — the replay
    # reconstructed the initial earth and applied the tap, ending at the stored earth.
    check('NO "earth biomes diverge" warning (the earth IS mirrored)',
          not any('earth' in w and 'biome' in w and 'diverge' in w for w in res['warnings']),
          f"(warnings={res['warnings']})")
    check('no "unknown card" warnings for black_hole',
          not any('black_hole' in w and 'unknown' in w for w in res['warnings']),
          f"(warnings={res['warnings']})")
    # the replay should have recorded the black_hole_tap event with the direction
    evs = [e for t in res['turns'] for e in t.get('events', []) if e.get('type') == 'black_hole_tap']
    check('replay recorded the black_hole_tap event', len(evs) == 1, f"(events={evs})")
    check('replay black_hole_tap event: rotation=cw',
          len(evs) == 1 and evs[0].get('rotation') == 'cw', f"(event={evs})")

    print(f"\nALL {ok} REPLAY CHECKS PASSED - the replay mirrors the black_hole earth rotation")
finally:
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit(); conn.close()
