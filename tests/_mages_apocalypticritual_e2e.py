# Manual smoke test (E2E): Mages Apocalypticritual driven through the REAL entry
# point ge.handle_websocket_message() with a model_copy() — exactly like API.py /
# ai_driver.py (the copy path that caught the refinery-tap bug). Verifies:
#   * a MOVE play of Apocalypticritual (with an order) is accepted
#   * the INSTANT effect fires (the cataclysm pile is SET to the chosen order)
#   * the card is a NO-OP on the trip chain (no advancing)
#   * the order is PERMANENT (does not reset after the turn)
#   * a cataclysm-condition card strikes the CHOSEn top biome
#   * card conservation (the card is in exactly one zone)
# Run from the project root:  uv run python tests/_mages_apocalypticritual_e2e.py
# NOTE: writes to games.db (like the other smoke tests); the game is deleted after.
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
main = DB.filter(pl.col('faction') == 'Miaous')['card_id'].to_list()[:20]
RITUAL = ge.MAGE_APOCALYPTICRITUAL   # 'Apocalypticritual'
CATACLYSM = 'Dem10_af058e'           # a clean cataclysm-condition card (adv 0, mana 1)
assert DB.filter(pl.col('card_id') == CATACLYSM)['condition'].to_list() == ['cataclysm']

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
        import json as _json
        resp = _json.loads(resp)
    return gs, resp

ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

# --- deterministic game: A's hand has Apocalypticritual + a cataclysm card -----
a_deck = [RITUAL, CATACLYSM] + main[:18]      # 20 cards
b_deck = [main[19]] * 20                       # filler deck

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

# all cells = OC so a cataclysm 'OC' strike knocks A back to cell 0 (observable)
c, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
for cell in gs.earth:
    cell[0] = 'OC'
# A starts on cell 5 (move the token, keep the earth list in sync)
for cell in gs.earth:
    if 'A' in cell:
        cell.remove('A')
ga = gs.players['A']
ga.current_position = 5
gs.earth[5].append('A')
ga.hand = [RITUAL, CATACLYSM, main[1], main[2], main[3], main[4]]
ga.deck = list(a_deck[6:])
ga.mana, ga.discard = [], []
gb = gs.players['B']
gb.hand = [main[19]] * 6
gb.deck = [main[19]] * 14
gb.mana, gb.discard = [], []
# fix the pile to a known order (OC last, so the ritual putting OC first is observable)
gs.cataclysm_pile = ['JU', 'MO', 'DE', 'OC']
c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
c.commit(); c.close()
print(f"game {gid} created (engine_version={gs.engine_version})")

try:
    # --- init: both put 3 cards in mana --------------------------------------
    gs, _ = send(gid, 'A', {'cards': [main[1], main[2], main[3]], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': b_deck[:3], 'to': 'mana', 'mode': '', 'pendings': []})
    check('init done - turn 1 play phase', 'turn 1' in gs.state and 'to play' in gs.state, f"(state={gs.state})")
    check('pile starts at the fixed order', gs.cataclysm_pile == ['JU', 'MO', 'DE', 'OC'],
          f"(pile={gs.cataclysm_pile})")

    # ============ TURN 1: A plays the ritual (sets the top to OC) ============
    chosen = ['OC', 'JU', 'MO', 'DE']
    gs, resp = send(gid, 'A', {'cards': [RITUAL], 'to': 'stopover_4', 'mode': 'move',
                               'pendings': [], 'cataclysm_order': chosen})
    check('Apocalypticritual play accepted', (resp.get('message') or {}).get('success') is True,
          f"(resp message={resp.get('message')})")
    check('INSTANT effect: pile SET to the chosen order', gs.cataclysm_pile == chosen,
          f"(pile={gs.cataclysm_pile})")
    check('OC is now on top of the pile', gs.cataclysm_pile[0] == 'OC', f"(pile={gs.cataclysm_pile})")
    check('card left A\'s hand', RITUAL not in gs.players['A'].hand, f"(hand={gs.players['A'].hand})")
    check('card is on the trip chain', any(RITUAL in (a.get('cards') or []) for a in (gs.players['A'].action_chain or [])),
          f"(chain={gs.players['A'].action_chain})")

    # B passes, A passes -> BOTH passed -> resolution + cleaning
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check('turn 1 resolved (turn advanced to 2)', gs.turn == 2, f"(turn={gs.turn})")
    check('the ritual card is a NO-OP (A did not advance)', gs.players['A'].current_position == 5,
          f"(pos = {gs.players['A'].current_position})")
    check('card conservation (Apocalypticritual in exactly one zone)',
          all_cards(zones(gs, 'A')).count(RITUAL) == 1,
          f"(zones={all_cards(zones(gs, 'A'))})")
    check('the ritual card is in the discard after resolution', RITUAL in gs.players['A'].discard,
          f"(discard={gs.players['A'].discard})")
    # the order is PERMANENT: no cataclysm triggered on turn 1, so OC is still on top
    check('the chosen order is PERMANENT (OC still on top after turn 1)',
          gs.cataclysm_pile == chosen, f"(pile={gs.cataclysm_pile})")

    # ============ TURN 2: A plays the cataclysm card (strikes OC) ============
    # NOTE: the turn order FLIPS each turn — after turn 1 (['A','B']) it is ['B','A'],
    # so B is FIRST and A is SECOND on turn 2.
    check('turn order flipped (B now first)', gs.turn_order == ['B', 'A'], f"(order={gs.turn_order})")
    # mana phase: both pass (A already has 3 mana from init — enough for the cataclysm)
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check('turn 2 play phase', 'turn 2' in gs.state and 'to play' in gs.state, f"(state={gs.state})")

    # B (first) passes, then A (second) plays the cataclysm card (adv 0) — strikes OC
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, resp = send(gid, 'A', {'cards': [CATACLYSM], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    check('cataclysm-card play accepted', (resp.get('message') or {}).get('success') is True,
          f"(resp message={resp.get('message')})")
    # A (second) passes -> BOTH passed -> resolution
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check('turn 2 resolved (turn advanced to 3)', gs.turn == 3, f"(turn={gs.turn})")
    # the OC strike knocked A back from cell 5 to cell 0 (first OC cell)
    check('the cataclysm struck OC: A knocked back to cell 0',
          gs.players['A'].current_position == 0,
          f"(pos = {gs.players['A'].current_position})")
    # the pile rotated after the OC strike (OC → bottom): JU on top now
    check('pile rotated after the strike (OC → bottom)', gs.cataclysm_pile == ['JU', 'MO', 'DE', 'OC'],
          f"(pile={gs.cataclysm_pile})")
    # the log note confirms OC struck (the CHOSEn top biome)
    note = None
    for turn in gs.log:
        for sv in turn.get('stopovers', []):
            for e in sv.get('entries', []):
                for n in e.get('notes', []):
                    if '⚡ cataclysm —' in n:
                        note = n
    check('the cataclysm struck the CHOSEn top biome OC', note is not None and 'OC strikes' in note,
          f"(note={note!r})")

    print(f"\nALL {ok} E2E CHECKS PASSED - Apocalypticritual works through the real entry point")
finally:
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit(); conn.close()
