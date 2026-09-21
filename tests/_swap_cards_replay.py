# Manual smoke test (REPLAY): swap_cards (engine_version 29) — the replay must
# mirror the engine's INSTANT position swap:
#   * the SWAP card's recorded 'to' is already post-swap in messages_history
#     (the engine rewrote the action in place) -> the replay's chain is correct
#     for the play and for a play target automatically
#   * a PENDING placeholder target must be mirrored: the replay's [card, slot]
#     pair is rewritten to the play's original column (swapped_from)
#   * the trip chain resolves the SWAPPED order (SWAP card at position 1, the
#     placeholder at position 2) — the same positions the engine used
#   * no "unknown card" warnings, the swap event is recorded, the final state
#     matches (verified=True)
# Run from the project root:  uv run python tests/_swap_cards_replay.py
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
assert len(CARDS) >= 24, f"need 24 distinct low-advancing cards, got {len(CARDS)}"
SWAP = 'Mia11_0e90ab'          # mana 1, adv -1, no_condition, effect swap_cards
ADV = 'Dwa23_79c784'          # no_condition, mana 2, adv 2, effect advancing (+1)
ADV2 = 'Dwa23_5cd6c5'         # no_condition, mana 2, adv 1, effect advancing (+2)
EPO = 'epo'                   # doctor pending, mana_cost 1

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

# --- build the game (distinct cards, no duplicates):
#   A hand: EPO, SWAP, C0, C1, C2, C3, C8      | A deck: C9..C13
#   B hand: ADV, ADV2, C4, C5, C6, C7, C14     | B deck: C15..C19
A_HAND = [EPO, SWAP, CARDS[0], CARDS[1], CARDS[2], CARDS[3], CARDS[8]]
A_DECK = CARDS[9:14]
B_HAND = [ADV, ADV2, CARDS[4], CARDS[5], CARDS[6], CARDS[7], CARDS[14]]
B_DECK = CARDS[15:20]

p1 = PlayerState(name='A', deck=A_HAND + A_DECK)
p2 = PlayerState(name='B', deck=B_HAND + B_DECK)
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

c, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
ga, gb = gs.players['A'], gs.players['B']
ga.hand = list(A_HAND); ga.deck = list(A_DECK)
gb.hand = list(B_HAND); gb.deck = list(B_DECK)
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.mana, gb.discard, gb.dwelling = [], [], None
c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
c.commit(); c.close()
print(f"game {gid} created (engine_version={gs.engine_version})")

def mana_put(gs):
    """ both players put 1 card in mana (to='mana') -> creates the replay turn
    boundary (a mana-PASS with to='' would lose the boundary and lump the
    turns' moves together — a pre-existing replay edge case we avoid here) """
    # A: C8 (hand [C3] left) -> turn-2 play = C3
    # B: C14 (hand [ADV2, C7] left) -> turn-2 play = ADV2
    gs, _ = send(gid, 'A', {'cards': [CARDS[8]], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [CARDS[14]], 'to': 'mana', 'mode': '', 'pendings': []})
    return gs

try:
    # --- init: both put 3 cards in mana (required before turn 1) -------------
    # init: A puts C0, C1, C2 in mana -> hand [EPO, SWAP, C3, C8]
    #       B puts C4, C5, C6 in mana -> hand [ADV, ADV2, C7, C14]
    gs, r = send(gid, 'A', {'cards': [CARDS[0], CARDS[1], CARDS[2]], 'to': 'mana', 'mode': '', 'pendings': []})
    assert (r.get('message') or {}).get('success'), f"A init rejected: {r.get('message')}"
    gs, r = send(gid, 'B', {'cards': [CARDS[4], CARDS[5], CARDS[6]], 'to': 'mana', 'mode': '', 'pendings': []})
    assert (r.get('message') or {}).get('success'), f"B init rejected: {r.get('message')}"
    check('init done - turn 1 play phase', 'turn 1' in gs.state and 'to play' in gs.state, f"(state={gs.state})")

    # --- turn 1 (A first): A places EPO (position 1) then SWAP (swap_with=1) --
    #     B plays ADV. The swap moves the SWAP card to position 1 and the EPO
    #     placeholder to position 2.
    gs, r = send(gid, 'A', {'cards': [EPO], 'to': 'pending_zone', 'mode': '', 'pendings': []})
    assert (r.get('message') or {}).get('success'), f"EPO placement rejected: {r.get('message')}"
    gs, r = send(gid, 'A', {'cards': [SWAP], 'to': 'stopover_3', 'mode': 'move', 'pendings': [], 'swap_with': 1})
    assert (r.get('message') or {}).get('success'), f"SWAP play rejected: {r.get('message')}"
    ga = gs.players['A']
    acts = ga.action_chain or []
    swap_act = [a for a in acts if a['cards'] == [SWAP]]
    check('engine: SWAP at position 1 (to=stopover_4)', swap_act and swap_act[0]['to'] == 'stopover_4', swap_act)
    check('engine: EPO placeholder at position 2 (slot 3)', (ga.pending_slots or []) == [[EPO, 3]], ga.pending_slots)
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [ADV], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check('turn 1 resolved', 'turn 2' in gs.state or 'mana' in gs.state, f"(state={gs.state})")

    # --- turn 2 (B first): B plays ADV2, A plays a filler ---------------------
    gs = mana_put(gs)
    gs, _ = send(gid, 'B', {'cards': [ADV2], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'A', {'cards': [CARDS[3]], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check('turn 2 resolved', 'turn 3' in gs.state or 'mana' in gs.state, f"(state={gs.state})")

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
    check('replay VERIFIED (the swap is mirrored)', res['verified'] is True,
          f"(verified={res['verified']}, warnings={res['warnings']})")
    check('no "unknown card" warnings for swap_cards',
          not any('unknown' in w for w in res['warnings']),
          f"(warnings={res['warnings']})")
    # the replay should have recorded the swap event
    evs = [e for t in res['turns'] for e in t.get('events', []) if e.get('type') == 'swap_cards']
    check('replay recorded the swap_cards event', len(evs) == 1, f"(events={evs})")
    check('the event carries the pending target', evs and evs[0].get('kind') == 'pending', f"(events={evs})")

    # the turn-1 result: the SWAP card resolved at POSITION 1 (its swapped position,
    # adv -1 from cell 0 -> clamped at 0), and B's ADV resolved at position 1 too
    # (adv 2 + effect 1 = 3). Both chains saw the SWAPPED order.
    t1 = res['turns'][0]
    pa = t1.get('positions_after') or {}
    check('replay: A at cell 0 after turn 1 (SWAP adv -1, clamped)', pa.get('A') == 0, f"(pa={pa})")
    check('replay: B at cell 3 or 4 after turn 1 (ADV adv 2 + 1, maybe +1 biome bonus)',
          pa.get('B') in (3, 4), f"(pa={pa})")

    print(f"\nALL {ok} REPLAY CHECKS PASSED - the replay mirrors the swap_cards swap")
finally:
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit(); conn.close()
