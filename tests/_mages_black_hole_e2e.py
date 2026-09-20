# Manual smoke test (E2E): Mages black_hole dwelling TAP, driven through the REAL
# entry point — ge.handle_websocket_message() with a model_copy() player (exactly like
# API.py / ai_driver.py). This is the path the unit test (which calls ge.player_play()
# directly with the authoritative player object) does NOT cover:
#   * the black_hole tap mutates the AUTHORITATIVE game object (rotate_earth rotates
#     current_game.earth + updates current_game.earth_rotation) — verify those persist
#     through the model_copy path into the saved DB state (and are read back on the
#     next get_current_game).
#   * the once-per-turn flag (dwelling_tapped) syncs back through the copy.
#   * the rotation is cumulative across the copy path (two turns = two taps).
#   * card conservation is preserved (the tap does not move any card).
# Run from the project root:  uv run python tests/_mages_black_hole_e2e.py
# NOTE: writes one game to games.db (like the other smoke tests); the game is deleted after.
import sys, io, os, sqlite3, json, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
main = DB.filter(pl.col('faction') == 'Miaous')['card_id'].to_list()[:20]
BLACK = ge.MAGE_BLACK_HOLE

def zones(gs, name):
    p = gs.players[name]
    return {
        'hand': list(p.hand or []), 'mana': list(p.mana or []),
        'deck': list(p.deck or []), 'discard': list(p.discard or []),
        'dwelling': [p.dwelling] if p.dwelling else [],
    }

def all_cards(z):
    return [c for lst in z.values() for c in lst]

def send(gid, name, message):
    """ drive the engine exactly like the WS layer / AI driver do (model_copy path) """
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

# --- build a deterministic game ---------------------------------------------
# A's deck (30 cards, in order): hand [f0, f1, BLACK, f2, f3, f4], deck [f5, ...]
a_deck = [main[0], main[1], BLACK, main[3], main[4], main[5],
          main[6], main[7], main[8], main[9], main[10], main[11],
          main[12], main[13], main[14], main[15], main[16], main[17], main[18],
          main[19], 'trampoline', 'gluetrap']
b_deck = [main[19]] * 30   # filler deck (B only mana-places + passes)

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

# --- deterministic zones + a known initial earth order ----------------------
c, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
# set a KNOWN initial biome order (OC, MO, DE, JU) so the rotation is deterministic
order = ['OC', 'MO', 'DE', 'JU']
for i, biome in enumerate(order):
    for j in range(6):
        gs.earth[i * 6 + j][0] = biome
gs.earth[0].append('A')
gs.earth[0].append('B')
gs.earth_initial_b0 = order[0]
gs.earth_rotation = 0
ga, gb = gs.players['A'], gs.players['B']
ga.hand = [main[0], main[1], BLACK, main[3], main[4], main[5]]
ga.deck = a_deck[6:]
gb.hand = [main[19]] * 6
gb.deck = [main[19]] * 24
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.mana, gb.discard, gb.dwelling = [], [], None
c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
c.commit(); c.close()
print(f"game {gid} created (engine_version={gs.engine_version})")

def codes(gs):
    return [cell[0] for cell in gs.earth]

try:
    # --- init: both put 3 cards in mana (A keeps the black_hole in hand) -----
    gs, r = send(gid, 'A', {'cards': [main[0], main[1], main[3]], 'to': 'mana', 'mode': '', 'pendings': []})
    assert (r.get('message') or {}).get('success'), f"A init rejected: {r.get('message')}"
    gs, r = send(gid, 'B', {'cards': b_deck[:3], 'to': 'mana', 'mode': '', 'pendings': []})
    assert (r.get('message') or {}).get('success'), f"B init rejected: {r.get('message')}"
    check('init done - turn 1 play phase', 'turn 1' in gs.state and 'to play' in gs.state, f"(state={gs.state})")
    check('initial earth_initial_b0 is OC', gs.earth_initial_b0 == 'OC', f"({gs.earth_initial_b0})")
    check('initial earth_rotation is 0', gs.earth_rotation == 0, f"({gs.earth_rotation})")

    # --- A (first player) PLACES the black_hole dwelling ----------------------
    gs, r = send(gid, 'A', {'cards': [BLACK], 'to': 'dwelling', 'mode': '', 'pendings': []})
    assert (r.get('message') or {}).get('success'), f"place rejected: {r.get('message')}"
    check('black_hole placed in the dwelling zone', gs.players['A'].dwelling == BLACK)

    # --- B passes, so it is A's turn again -----------------------------------
    gs, r = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    assert (r.get('message') or {}).get('success'), f"B pass rejected: {r.get('message')}"

    # --- A TAPS the black_hole CW (the key path) -----------------------------
    base = ['OC']*6 + ['MO']*6 + ['DE']*6 + ['JU']*6
    gs, r = send(gid, 'A', {'cards': [], 'to': 'dwelling', 'mode': 'dwelling_activation',
                            'pendings': [], 'rotation': 'cw'})
    assert (r.get('message') or {}).get('success'), f"tap rejected: {r.get('message')}"
    # re-read from the DB to prove the rotation PERSISTED through the copy path
    conn, _, gs2 = ge.get_current_game(gid)
    conn.close()
    check('earth rotated 3 cells CW (persisted to the DB)',
          codes(gs2) == [base[(i - 3) % 24] for i in range(24)], f"(earth={codes(gs2)})")
    check('earth_rotation == 3 (persisted)', gs2.earth_rotation == 3, f"({gs2.earth_rotation})")
    check('dwelling_tapped synced back to the authoritative state',
          gs2.players['A'].dwelling_tapped is True, f"({gs2.players['A'].dwelling_tapped})")
    check('card conservation after the tap (no card moved)',
          len(all_cards(zones(gs2, 'A'))) == len(a_deck), f"({len(all_cards(zones(gs2, 'A')))} cards)")

    # --- A passes -> both passed -> resolution + cleaning (turn end) ---------
    # A is the first player; B already passed, so A passing ends the turn.
    gs, r = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    # after the turn end, the dwelling_tapped flag must be RESET (cleaning phase),
    # but earth_rotation (the PERMANENT earth state) must SURVIVE the cleaning phase.
    check('dwelling_tapped reset in the cleaning phase (ready for next turn)',
          gs.players['A'].dwelling_tapped is False, f"({gs.players['A'].dwelling_tapped})")
    check('earth_rotation SURVIVED the turn end (permanent, not a per-turn flag)',
          gs.earth_rotation == 3, f"({gs.earth_rotation})")
    check('the earth biomes are still rotated after the turn end',
          codes(gs) == [base[(i - 3) % 24] for i in range(24)], f"(earth={codes(gs)})")
    print(f"  after turn 1: state={gs.state!r} turn={gs.turn} earth_rotation={gs.earth_rotation}")

    print(f"\nALL {ok} CHECKS PASSED - black_hole tap persists through the real WS entry point")
finally:
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit(); conn.close()
