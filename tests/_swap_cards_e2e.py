# Manual smoke test (E2E): swap_cards (engine_version 29) driven through the REAL
# entry point ge.handle_websocket_message() with a model_copy() — exactly like
# API.py / ai_driver.py (the copy path that caught the refinery-tap bug).
# Verifies the SYNC-BACK of the swap rewrites through the copy:
#   Game 1 (play <-> pending placeholder): the pending pair's slot rewrite and the
#           play's 'to' rewrite persist to the DB (p.pending_slots sync-back).
#   Game 2 (play <-> dwelling placeholder): the dwelling_slot rewrite persists to
#           the DB (p.dwelling_slot sync-back in the main play branch) — the exact
#           copy path that the refinery-tap bug lived in.
# Both games also verify: the swap annotations (swapped_with / swapped_with_kind /
# swapped_from), the swapped order on the trip chain, card conservation, and the
# turn advancing (the swap did not break the chain).
# Setup: mana/hand are pre-set and the state is set directly to the turn-1 play
# phase (like tests/_doctors.py) — the init phase is not under test here.
# Run from the project root:  uv run python tests/_swap_cards_e2e.py
# NOTE: writes to games.db (like the other smoke tests); the games are deleted after.
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
main = DB.filter(pl.col('faction') == 'Miaous')['card_id'].to_list()[:20]
SWAP = 'Mia11_0e90ab'          # mana 1, adv -1, no_condition, effect swap_cards
ADV = 'Dwa23_79c784'          # no_condition, mana 2, adv 2, effect advancing (+1)
ADV2 = 'Dwa23_5cd6c5'         # no_condition, mana 2, adv 1, effect advancing (+2)
EPO = 'epo'                   # doctor pending, mana_cost 1
REF = 'refinery'              # engineer dwelling, mana_cost 3

def new_game(a_hand, a_mana, b_hand, b_mana):
    # fillers: distinct main cards NOT already in this player's hand/mana
    a_fill = [c for c in main if c not in a_hand + a_mana][:10]
    b_fill = [c for c in main if c not in b_hand + b_mana][:10]
    a_deck = a_hand + a_mana + a_fill         # 17-18 cards
    b_deck = b_hand + b_mana + b_fill         # 17-18 cards
    p1 = PlayerState(name='A', deck=list(a_deck))
    p2 = PlayerState(name='B', deck=list(b_deck))
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    c, _, gs = ge.get_current_game(gid)
    gs.turn_order = ['A', 'B']
    ga, gb = gs.players['A'], gs.players['B']
    ga.hand, ga.deck, ga.mana, ga.discard, ga.dwelling = list(a_hand), list(a_fill), list(a_mana), [], None
    gb.hand, gb.deck, gb.mana, gb.discard, gb.dwelling = list(b_hand), list(b_fill), list(b_mana), [], None
    gs.state = 'turn 1 - waiting for first player (A) to play'
    c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    c.commit(); c.close()
    return gid, gs.engine_version

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

def cleanup(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit(); conn.close()

# ==========================================================================
# GAME 1 — play <-> pending placeholder swap (p.pending_slots sync-back)
# ==========================================================================
print("\n=== GAME 1: play<->pending swap through the WS copy path ===")
ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

gid, ev = new_game(
    a_hand=[EPO, SWAP, main[0], main[1]], a_mana=[main[4], main[5], main[6]],
    b_hand=[ADV, main[0], main[1]], b_mana=[main[4], main[5], main[6]])
print(f"game {gid} created (engine_version={ev})")

try:
    # --- A places 'epo' in the pending zone (position 1) ----------------------
    gs, _ = send(gid, 'A', {'cards': [EPO], 'to': 'pending_zone', 'mode': '', 'pendings': []})
    ga = gs.players['A']
    check('epo in A pendings', EPO in (ga.pendings or []), ga.pendings)
    check('pending placeholder at col 4', (ga.pending_slots or []) == [[EPO, 4]], ga.pending_slots)

    # --- A plays SWAP with swap_with=1 (swap with the pending placeholder) ----
    gs, _ = send(gid, 'A', {'cards': [SWAP], 'to': 'stopover_3', 'mode': 'move', 'pendings': [], 'swap_with': 1})
    ga = gs.players['A']
    acts = ga.action_chain or []
    swap_act = [a for a in acts if a['cards'] == [SWAP]]
    check('SWAP in action_chain', len(swap_act) == 1)
    check("SWAP 'to' rewritten to stopover_4 (position 1)", swap_act and swap_act[0]['to'] == 'stopover_4', swap_act and swap_act[0]['to'])
    check('pending pair slot rewritten 4->3', (ga.pending_slots or []) == [[EPO, 3]], ga.pending_slots)
    check('swapped_with_kind = pending', swap_act and swap_act[0].get('swapped_with_kind') == 'pending', swap_act)
    check('swapped_from = stopover_3', swap_act and swap_act[0].get('swapped_from') == 'stopover_3', swap_act)

    # --- A passes, B plays ADV (position 1), B passes -> resolution -----------
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [ADV], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check('turn resolved (turn-2 mana phase)', gs.state == 'waiting for both players to mana or pass', gs.state)
    ga, gb = gs.players['A'], gs.players['B']
    check('A on cell 0 (adv -1 clamped)', ga.current_position == 0, ga.current_position)
    check('B advanced (>=2)', gb.current_position >= 2, gb.current_position)
    check('SWAP in A discard (conserved)', SWAP in (ga.discard or []), ga.discard)
    check('epo still in A pendings (conserved)', EPO in (ga.pendings or []), ga.pendings)
    check('ADV in B discard (conserved)', ADV in (gb.discard or []), gb.discard)

    # --- the turn log carries the swap note ------------------------------------
    t1 = (gs.log or [{}])[0]
    inst = t1.get('instant') or []
    check('log has the swap instant note', any('swap_cards' in (e.get('what') or '') for e in inst), inst)
    print(f"GAME 1: {ok} checks ok")
finally:
    cleanup(gid)

# ==========================================================================
# GAME 2 — play <-> dwelling placeholder swap (p.dwelling_slot sync-back)
# ==========================================================================
print("\n=== GAME 2: play<->dwelling swap through the WS copy path ===")
ok2 = 0
def check2(label, cond, detail=''):
    global ok2
    assert cond, f"FAIL: {label} {detail}"
    ok2 += 1
    print(f"  ok - {label}")

# A needs 4 mana (REF cost 3 + SWAP cost 1): 4 mana cards pre-set.
gid2, ev2 = new_game(
    a_hand=[REF, SWAP, main[0], main[1]], a_mana=[main[4], main[5], main[6], main[7]],
    b_hand=[ADV, main[0], main[1]], b_mana=[main[4], main[5], main[6]])
print(f"game {gid2} created (engine_version={ev2})")

try:
    # --- A places the REFINERY (dwelling, position 1, cost 3) -----------------
    gs, _ = send(gid2, 'A', {'cards': [REF], 'to': 'dwelling', 'mode': '', 'pendings': []})
    ga = gs.players['A']
    check2('REF in A dwelling', ga.dwelling == REF, ga.dwelling)
    check2('dwelling placeholder at col 4', ga.dwelling_slot == 4, ga.dwelling_slot)

    # --- A plays SWAP with swap_with=1 (swap with the dwelling placeholder) ---
    # A's chain: REF dwelling placeholder (col 4, position 1). SWAP lands on
    # position 2 (col 3), then swaps with position 1: SWAP -> col 4, the
    # dwelling placeholder -> col 3.
    gs, _ = send(gid2, 'A', {'cards': [SWAP], 'to': 'stopover_3', 'mode': 'move', 'pendings': [], 'swap_with': 1})
    ga = gs.players['A']
    acts = ga.action_chain or []
    swap_act = [a for a in acts if a['cards'] == [SWAP]]
    check2("SWAP 'to' rewritten to stopover_4 (position 1)", swap_act and swap_act[0]['to'] == 'stopover_4', swap_act and swap_act[0]['to'])
    check2('dwelling_slot rewritten 4->3 (sync-back)', ga.dwelling_slot == 3, ga.dwelling_slot)
    check2('REF still in A dwelling (unchanged)', ga.dwelling == REF, ga.dwelling)
    check2('swapped_with_kind = dwelling', swap_act and swap_act[0].get('swapped_with_kind') == 'dwelling', swap_act)
    check2('swapped_from = stopover_3', swap_act and swap_act[0].get('swapped_from') == 'stopover_3', swap_act)

    # --- A passes, B plays ADV (position 1), B passes -> resolution -----------
    gs, _ = send(gid2, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, _ = send(gid2, 'B', {'cards': [ADV], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    gs, _ = send(gid2, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check2('turn resolved (turn-2 mana phase)', gs.state == 'waiting for both players to mana or pass', gs.state)
    ga, gb = gs.players['A'], gs.players['B']
    check2('A on cell 0 (adv -1 clamped)', ga.current_position == 0, ga.current_position)
    check2('SWAP in A discard (conserved)', SWAP in (ga.discard or []), ga.discard)
    check2('REF still in A dwelling (conserved)', ga.dwelling == REF, ga.dwelling)
    check2('ADV in B discard (conserved)', ADV in (gb.discard or []), gb.discard)
    check2('dwelling_slot cleared in cleaning', ga.dwelling_slot is None, ga.dwelling_slot)

    print(f"GAME 2: {ok2} checks ok")
finally:
    cleanup(gid2)

print(f"\nALL E2E CHECKS PASSED ({ok} + {ok2})")
