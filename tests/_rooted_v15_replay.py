"""Multi-turn v15 rooted card test (ENGINE + REPLAY).

Builds a 2-turn game through the REAL engine flow (create_new_game + init +
player_play via handle_websocket_message, exactly like the WS layer / AI driver)
so the messages_history is genuine, then verifies:
  1. ENGINE: the rooted card from turn 1 resolves its basic advancing on turn 2
     (the active-rooted rule) — Al advances by it in BOTH turns.
  2. REPLAY: the v15 branch of _replay_trip_chain reproduces the engine's final
     state (analyze_game -> verified True) for a game with a rooted card on the
     board at turn 2.

Creates:
  Turn 1: Al plays a rooted card (advances + earns a rooted token). Bo plays a filler.
  Turn 2: Al's rooted card is on the board (Al's position 1) and advances again;
          Al also plays a filler (Al's position 2). Bo plays a filler (Bo's position 1).

Run from the project root:
    PYTHONPATH=. python tests/_rooted_v15_replay.py
NOTE: writes to games.db (like the other smoke tests); the game is deleted at the end.
"""
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

# a rooted card (no_condition so the effect always fires, positive advancing so it
# moves on the board, mana <= 3 so Al can pay it in turn 1 with a 3-card mana pool)
rooted_rows = DB.filter(
    (pl.col('effect') == 'rooted') & (pl.col('condition') == 'no_condition')
    & (pl.col('advancing') > 0) & (pl.col('mana') <= 3)
)
assert len(rooted_rows) >= 1, "no rooted/no_condition/adv>0/mana<=3 card in pool"
ROOTED = rooted_rows.row(0, named=True)['card_id']
print(f"rooted card: {ROOTED} (adv {rooted_rows.row(0, named=True)['advancing']}, "
      f"mana {rooted_rows.row(0, named=True)['mana']}, faction {rooted_rows.row(0, named=True)['faction']})")

# filler pool: distinct Miaous mana-1 cards (the rooted card is Twigs so the deck
# majority is Miaous -> OC is not a home biome -> no faction +1 bonus; any condition
# is fine - the replay mirrors the engine, so the test still verifies)
FILLER = [c for c in DB.filter((pl.col('faction') == 'Miaous') & (pl.col('mana') == 1))['card_id'].to_list()
          if c != ROOTED]
assert len(FILLER) >= 59, f"need >= 59 distinct Miaous mana-1 fillers, got {len(FILLER)}"

# Al: 1 rooted + 29 fillers (30). Bo: 30 fillers (disjoint from Al's).
a_full = [ROOTED] + FILLER[:29]
b_full = FILLER[29:59]
assert len(set(a_full)) == 30 and len(set(b_full)) == 30 and not (set(a_full) & set(b_full))

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

gid = ge.create_new_game(player=PlayerState(name='Al', deck=list(a_full)).model_dump())
ge.p2_connect_to_game(player=PlayerState(name='Bo', deck=list(b_full)).model_dump(), game_id=gid)

# deterministic setup: Al first, all biomes OC (not a Miaous home biome -> no +1 bonus)
conn, _, gs = ge.get_current_game(gid)
gs.turn_order = ['Al', 'Bo']
for cell in gs.earth:
    cell[0] = 'OC'
ga, gb = gs.players['Al'], gs.players['Bo']
ga.hand, ga.mana, ga.deck, ga.discard = a_full[:6], [], a_full[6:], []
gb.hand, gb.mana, gb.deck, gb.discard = b_full[:6], [], b_full[6:], []
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
conn.commit()
conn.close()
print(f"created game {gid} (v{ge.create_new_game.__module__})")

try:
    # --- init: both players put 3 cards in mana -> turn 1 play phase ---
    gs, _ = send(gid, 'Al', {'cards': a_full[1:4], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'Bo', {'cards': b_full[1:4], 'to': 'mana', 'mode': '', 'pendings': []})
    assert 'turn 1' in gs.state and 'to play' in gs.state, f"init failed: {gs.state}"
    # force the rooted card back into Al's hand (so Al can play it in turn 1)
    conn, _, gs = ge.get_current_game(gid)
    if ROOTED in gs.players['Al'].mana:
        gs.players['Al'].mana.remove(ROOTED)
        gs.players['Al'].hand.append(ROOTED)
    conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    conn.commit()
    conn.close()

    # --- turn 1: Al plays the rooted card, Bo plays a filler ---
    gs, _ = send(gid, 'Al', {'cards': [ROOTED], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'Bo', {'cards': [b_full[4]], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'Al', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'Bo', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    # the trip chain resolved (both passed); end-of-turn ran (rooted card placed on board)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    rooted_on_board_t1 = list(gs.rooted_on_board or [])
    print(f"turn 1: Al at {gs.players['Al'].current_position}, Bo at {gs.players['Bo'].current_position}")
    print(f"rooted_on_board after turn 1: {rooted_on_board_t1}")
    assert any(e['card_id'] == ROOTED for e in rooted_on_board_t1), \
        f"rooted card {ROOTED} should be on the board after turn 1"

    # --- mana phase of turn 2: Al places 1 card in mana (creates the turn boundary in
    # the history); Bo passes. Both must act for the mana phase to complete. ---
    gs, _ = send(gid, 'Al', {'cards': [a_full[5]], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'Bo', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    # --- turn 2: Al's rooted card is on the board (Al pos 1); Al plays a filler (pos 2) ---
    # (a_full[4] is in Al's hand after the init; a_full[6] would be in the deck)
    gs, _ = send(gid, 'Bo', {'cards': [b_full[5]], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'Al', {'cards': [a_full[4]], 'to': 'stopover_3', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'Bo', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'Al', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    pos_al_t2 = gs.players['Al'].current_position
    pos_bo_t2 = gs.players['Bo'].current_position
    print(f"turn 2: Al at {pos_al_t2} (rooted on board + filler), Bo at {pos_bo_t2} (filler)")
    # the rooted card on the board should have advanced (Al moved further than just the filler)
    assert any('rooted card' in ' '.join(e.get('notes', []))
               for t in gs.log for s in t['stopovers'] for e in s['entries']
               if 'basic advancing' in ' '.join(e.get('notes', []))), \
        "engine log should show the rooted card's basic advancing on turn 2"
    # the rooted card is discarded after serving its board turn
    assert not any(e['card_id'] == ROOTED for e in (gs.rooted_on_board or [])), \
        "rooted card should be off the board after turn 2"

    # --- REPLAY: the v15 branch must reproduce the engine's final state ---
    from games.analysis.replay import analyze_game
    st = gs.model_dump()
    res = analyze_game(st)
    print(f"replay: verified={res['verified']} turns={len(res['turns'])} "
          f"warnings={res['warnings'][:3]}")
    if not res['verified']:
        for t in res['turns']:
            print(f"  turn {t['turn']}: pos_before={t['positions_before']} pos_after={t['positions_after']}")
    assert res['verified'], f"REPLAY FAILED to verify the v15 multi-turn rooted game: {res['warnings']}"

    print("\n=== ENGINE + REPLAY v15 MULTI-TURN ROOTED TEST PASSED ===")
    print(f"  turn 1: Al at {gs.players['Al'].current_position} (rooted card played + advanced + token)")
    print(f"  turn 2: Al at {pos_al_t2} (rooted on board + filler), Bo at {pos_bo_t2}")
    print(f"  replay: verified=True (v15 branch reproduces the engine)")
finally:
    _delete(gid)
    print(f"(deleted game {gid})")
