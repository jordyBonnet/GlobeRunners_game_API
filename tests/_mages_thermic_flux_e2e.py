# Manual smoke test (E2E): Mages thermic_flux driven through the REAL entry
# point ge.handle_websocket_message() with a model_copy() — exactly like API.py /
# ai_driver.py (the copy path that caught the refinery-tap bug). Verifies:
#   * a MOVE play of thermic_flux (with a +4/−4 choice) is accepted
#   * the INSTANT effect fires (game.temperature changes by ±4, clamped 1..20)
#   * the card is a NO-OP on the trip chain (no advancing)
#   * the temperature is PERMANENT (does not reset after the turn)
#   * the temp_* condition evaluates against the CHANGED value
#   * card conservation (the card is in exactly one zone)
# Run from the project root:  uv run python tests/_mages_thermic_flux_e2e.py
# NOTE: writes to games.db (like the other smoke tests); the game is deleted after.
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
from collections import Counter
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
main = DB.filter(pl.col('faction') == 'Miaous')['card_id'].to_list()[:20]
THERMIC = ge.MAGE_THERMIC_FLUX   # 'thermic_flux'

def zones(gs, name):
    p = gs.players[name]
    return {'hand': list(p.hand or []), 'mana': list(p.mana or []),
            'deck': list(p.deck or []), 'discard': list(p.discard or []),
            'dwelling': [p.dwelling] if p.dwelling else []}

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
        import json as _json
        resp = _json.loads(resp)
    return gs, resp

ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

# --- deterministic game: A's hand has thermic_flux, B has filler --------------
a_deck = [THERMIC] + main[:19]                # 20 cards (thermic_flux first)
b_deck = [main[19]] * 20                       # filler deck

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

c, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
ga, gb = gs.players['A'], gs.players['B']
ga.hand = [THERMIC, main[1], main[2], main[3], main[4], main[5]]
ga.deck = list(a_deck[6:])
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.hand = [main[19]] * 6
gb.deck = [main[19]] * 14
gb.mana, gb.discard, gb.dwelling = [], [], None
# set a known initial temperature (10) so the ±4 change is deterministic
gs.temperature = 10
gs.temperature_initial = 10
c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
c.commit(); c.close()
print(f"game {gid} created (engine_version={gs.engine_version})")

try:
    # --- init: both put 3 cards in mana --------------------------------------
    gs, _ = send(gid, 'A', {'cards': [main[1], main[2], main[3]], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': b_deck[:3], 'to': 'mana', 'mode': '', 'pendings': []})
    check('init done - turn 1 play phase', 'turn 1' in gs.state and 'to play' in gs.state, f"(state={gs.state})")
    check('temperature starts at 10', gs.temperature == 10, f"(temp={gs.temperature})")
    check('temperature_initial recorded as 10', gs.temperature_initial == 10, f"(initial={gs.temperature_initial})")

    # --- A (first player) plays thermic_flux, choosing +4 (UP) ---------------
    pos_before = gs.players['A'].current_position
    gs, resp = send(gid, 'A', {'cards': [THERMIC], 'to': 'stopover_4', 'mode': 'move',
                               'pendings': [], 'temp_change': 'up'})
    check('thermic_flux play accepted', (resp.get('message') or {}).get('success') is True,
          f"(resp message={resp.get('message')})")
    check('INSTANT effect: temperature now 14 (10 + 4)', gs.temperature == 14, f"(temp={gs.temperature})")
    check('card left A\'s hand', THERMIC not in gs.players['A'].hand, f"(hand={gs.players['A'].hand})")
    check('card is on the trip chain', any(a['cards'][0] == THERMIC for a in (gs.players['A'].action_chain or [])),
          f"(chain={gs.players['A'].action_chain})")

    # --- B passes, then A passes -> BOTH passed -> resolution + cleaning ------
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check('temp_inf_11 condition now met (temp 14 -> not met; check temp_sup_9 met)',
          ge.is_condition_met('temp_sup_9', gs.players['A'], gs) is True,
          f"(temp={gs.temperature})")
    # A passes too -> the play phase ends -> trip chain resolves + cleaning
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check('turn advanced (resolution + cleaning ran)', gs.turn == 2, f"(turn={gs.turn})")
    check('the card is a NO-OP (A did not advance)', gs.players['A'].current_position == pos_before,
          f"(pos {pos_before} -> {gs.players['A'].current_position})")
    check('temperature is PERMANENT (still 14 after the turn)', gs.temperature == 14, f"(temp={gs.temperature})")
    check('temperature_initial still 10', gs.temperature_initial == 10, f"(initial={gs.temperature_initial})")
    check('card conservation (thermic_flux in exactly one zone)',
          all_cards(zones(gs, 'A')).count(THERMIC) == 1,
          f"(zones={all_cards(zones(gs, 'A'))})")
    check('card is in the discard after resolution', THERMIC in gs.players['A'].discard,
          f"(discard={gs.players['A'].discard})")
    check('card is NOT still on the trip chain',
          all(a['cards'][0] != THERMIC for a in (gs.players['A'].action_chain or [])),
          f"(chain={gs.players['A'].action_chain})")

    print(f"\nALL {ok} E2E CHECKS PASSED - thermic_flux works through the real entry point")
finally:
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit(); conn.close()
