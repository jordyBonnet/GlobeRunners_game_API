# Manual smoke test: dwelling PLACE must consume a stopover position (2026-09-17).
#
# Bug: the WS layer (API.py) and the AI driver pass `player` as a model_copy()
# of the stored PlayerState. The dwelling-PLACE branch incremented the COPY's
# play_count (the refinery placeholder consumes one position, engine_version 15),
# but the sync-back loop copied hand/mana/dwelling/dwelling_slot/dwelling_tapped
# to the authoritative state and NOT `play_count` -> the NEXT play re-read
# play_count=0 and the engine assigned it to position 1 (stopover_4), the SAME
# slot as the dwelling placeholder. The frontend (actions.mjs playedCount)
# correctly highlighted position 2, then the card visually snapped onto
# position 1 after the play.
#
# Same class as the 2026-09-04 refinery-TAP bug (tests/_tap_dup.py): a field
# mutated on the copy that the sync-back forgot.
#
# This test drives every action through ge.handle_websocket_message() with a
# model_copy() - exactly like API.py / ai_driver.py.
#
# Run from the project root:  uv run python tests/_dwelling_playcount.py
# NOTE: writes to games.db (like the other smoke tests); the game is deleted after.
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
main_all = DB.filter(pl.col('faction') == 'Miaous')['card_id'].to_list()
main = main_all[:20]
FILLER = main_all[20] if len(main_all) > 20 else main_all[0]   # any Miaous card for filler zones
M1 = DB.filter((pl.col('faction') == 'Miaous') & (pl.col('mana') == 1))['card_id'].to_list()
CHEAP = M1[0]   # a mana-1 Miaous card for A's 2nd play (affordable after the refinery)

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

ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

# --- build a deterministic game ---------------------------------------------
# A's deck (30 cards): refinery + fillers; B: cheap filler deck (3 mana only).
a_deck = ['refinery'] + [main_all[i % len(main_all)] for i in range(29)]
b_deck = [CHEAP] * 30

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

# deterministic zones (create_new_game random-samples the hand)
c, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
ga, gb = gs.players['A'], gs.players['B']
ga.hand = ['refinery', CHEAP, main[1], main[2], main[3], main[4]]
ga.deck = [main_all[i % len(main_all)] for i in range(5, 30)]
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.hand = [CHEAP] * 6
gb.deck = [CHEAP] * 24
gb.mana, gb.discard, gb.dwelling = [], [], None
c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
c.commit()
c.close()
print(f"game {gid} created (engine_version={gs.engine_version})")

try:
    assert gs.engine_version == "1.0", f"expected engine_version '1.0', got {gs.engine_version}"

    # --- init: both put 3 cards in mana ------------------------------------
    # (A's hand = [refinery, CHEAP, main[1], main[2], main[3], main[4]])
    gs, _ = send(gid, 'A', {'cards': [main[1], main[2], main[3]], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [CHEAP] * 3, 'to': 'mana', 'mode': '', 'pendings': []})
    check('init done - turn 1 play phase', 'turn 1' in gs.state and 'to play' in gs.state, f"(state={gs.state})")

    # A needs 4 mana to afford the refinery (cost 3) + a filler (cost 1) in the
    # same turn - in the user's live game this was turn 4 with accumulated mana.
    # Simulate a mana-phase placement (a legal state): move one hand card to mana.
    c, _, gs = ge.get_current_game(gid)
    ga = gs.players['A']
    moved = ga.hand.pop()
    ga.mana.append(moved)
    c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    c.commit()
    c.close()

    # --- A (first player) places the refinery -------------------------------
    gs, resp = send(gid, 'A', {'cards': ['refinery'], 'to': 'dwelling', 'mode': '', 'pendings': []})
    check('refinery placed in the dwelling zone', gs.players['A'].dwelling == 'refinery')
    check('placeholder at position 1 (stopover_4)', gs.players['A'].dwelling_slot == 4,
          f"(dwelling_slot={gs.players['A'].dwelling_slot})")
    check('AUTHORITATIVE play_count synced to 1 (the fix)', gs.players['A'].play_count == 1,
          f"(play_count={gs.players['A'].play_count})")

    # --- B plays a card (their own position 1, independent per-player) ------
    gs, _ = send(gid, 'B', {'cards': [CHEAP], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    check('B play on B position 1 (stopover_4)',
          gs.players['B'].action_chain and gs.players['B'].action_chain[0]['to'] == 'stopover_4',
          f"(B chain={[a.get('to') for a in gs.players['B'].action_chain]})")

    # --- A plays a 2nd card: MUST land on position 2 (stopover_3), NOT the
    # placeholder's slot (position 1 / stopover_4) ----------------------------
    gs, _ = send(gid, 'A', {'cards': [CHEAP], 'to': 'stopover_3', 'mode': 'move', 'pendings': []})
    a_tos = [a.get('to') for a in gs.players['A'].action_chain]
    check("BUG GONE: A's next play lands on position 2 (stopover_3)", a_tos == ['stopover_3'], f"(A chain={a_tos})")
    check('A play != dwelling placeholder slot', a_tos and a_tos[0] != 'stopover_4', f"(A chain={a_tos})")
    check('B play still on stopover_4 (per-player positions)',
          [a.get('to') for a in gs.players['B'].action_chain] == ['stopover_4'],
          f"(B chain={[a.get('to') for a in gs.players['B'].action_chain]})")

    # --- both pass -> resolution (positions are irrelevant to the fix) ------
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check('turn advanced', gs.turn == 2, f"(turn={gs.turn})")

    print(f"\nALL {ok} CHECKS PASSED - dwelling PLACE consumes a stopover position (WS copy path)")
finally:
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()
    print(f"(deleted game {gid})")
