# Manual smoke test (E2E): Mages nobodymoves driven through the REAL entry point
# ge.handle_websocket_message() with a model_copy() — exactly like API.py /
# ai_driver.py (the copy path that caught the refinery-tap bug). Verifies:
#   * a MOVE play of nobodymoves (no choice needed) is accepted
#   * the INSTANT effect fires (game.nobodymoves_active = True, movement lock for both players)
#   * the opponent's MOVE card's movement is LOCKED (no advancing; its non-movement
#     effects still fire)
#   * the card is a NO-OP on the trip chain (no advancing of its own)
#   * the lock is CLEARED in the cleaning phase (nobodymoves_active back to False)
#   * card conservation (the card is in exactly one zone)
# Run from the project root:  uv run python tests/_mages_nobodymoves_e2e.py
# NOTE: writes to games.db (like the other smoke tests); the game is deleted after.
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
main = DB.filter(pl.col('faction') == 'Miaous')['card_id'].to_list()[:20]
NOBODY = ge.MAGE_NOBODYMOVES   # 'nobodymoves'
ADV = 'Dwa23_79c784'          # a no_condition advancing card

def zones(gs, name):
    p = gs.players[name]
    return {'hand': list(p.hand or []), 'mana': list(p.mana or []),
            'deck': list(p.deck or []), 'discard': list(p.discard or [])}

def all_cards(z):
    return [c for lst in z.values() for c in lst]

def send(gid, name, message):
    """ drive the engine exactly like the WS layer / AI driver do (model_copy) """
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

# --- deterministic game: A has nobodymoves, B has adv --------------------------
a_deck = [NOBODY] + main[:19]   # 20 cards
b_deck = [ADV] + main[:19]      # 20 cards

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

c, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
ga, gb = gs.players['A'], gs.players['B']
ga.hand = [NOBODY, main[1], main[2], main[3], main[4], main[5]]
ga.deck = list(a_deck[6:])
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.hand = [ADV, main[6], main[7], main[8], main[9], main[10]]
gb.deck = list(b_deck[6:])
gb.mana, gb.discard, gb.dwelling = [], [], None
c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
c.commit(); c.close()
print(f"game {gid} created (engine_version={gs.engine_version})")

try:
    # --- init: both put 3 cards in mana --------------------------------------
    gs, _ = send(gid, 'A', {'cards': [main[1], main[2], main[3]], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [main[6], main[7], main[8]], 'to': 'mana', 'mode': '', 'pendings': []})
    check('init done - turn 1 play phase', 'turn 1' in gs.state and 'to play' in gs.state, f"(state={gs.state})")
    check('block starts False', gs.nobodymoves_active is False, f"(nobodymoves_active={gs.nobodymoves_active})")

    pos_a0 = gs.players['A'].current_position
    pos_b0 = gs.players['B'].current_position

    # --- A plays nobodymoves (block set), B plays adv -------------------------
    gs, resp = send(gid, 'A', {'cards': [NOBODY], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    check('A plays nobodymoves accepted (no choice needed)', (resp.get('message') or {}).get('success') is True,
          f"(resp={resp.get('message')})")
    check('INSTANT effect: block active', gs.nobodymoves_active is True, f"(nobodymoves_active={gs.nobodymoves_active})")
    check('card left A\'s hand', NOBODY not in gs.players['A'].hand, f"(hand={gs.players['A'].hand})")
    gs, resp = send(gid, 'B', {'cards': [ADV], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    check('B plays adv accepted', (resp.get('message') or {}).get('success') is True, f"(resp={resp.get('message')})")

    # --- A passes, B passes -> BOTH passed -> resolution + cleaning -----------
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check('turn advanced (resolution + cleaning ran)', gs.turn == 2, f"(turn={gs.turn})")
    check('B\'s adv movement was LOCKED (no advancing)', gs.players['B'].current_position == pos_b0,
          f"(pos {pos_b0} -> {gs.players['B'].current_position})")
    check('A\'s nobodymoves is a NO-OP (no advancing)', gs.players['A'].current_position == pos_a0,
          f"(pos {pos_a0} -> {gs.players['A'].current_position})")
    check('lock CLEARED after the turn (cleaning phase)', gs.nobodymoves_active is False,
          f"(nobodymoves_active={gs.nobodymoves_active})")
    check('card conservation (nobodymoves in exactly one zone)',
          all_cards(zones(gs, 'A')).count(NOBODY) == 1, f"(zones={all_cards(zones(gs, 'A'))})")
    check('card is in the discard after resolution', NOBODY in gs.players['A'].discard,
          f"(discard={gs.players['A'].discard})")

    print(f"\nALL {ok} E2E CHECKS PASSED - nobodymoves works through the real entry point")
finally:
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit(); conn.close()
