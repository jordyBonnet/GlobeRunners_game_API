# Manual smoke test: swap_cards effect (engine_version 29)
#  - swap_cards is an INSTANT play-time effect: the card being played (move mode)
#    SWAPS its trip-chain POSITION with the entry chosen by the player (message
#    'swap_with': a position 1..5 of the player's OWN chain — a play (move/defend),
#    a board placeholder (doctor pending / dwelling) or a rooted card).
#  - The two entries exchange their stopover columns (the play's recorded 'to' and
#    the target's slot are rewritten at play time); the trip chain then resolves
#    everything by the recorded columns, so the swapped order is applied.
#  - 'swap_with' is OPTIONAL — without it the card simply advances as usual.
#  - A DEFEND play of swap_cards never swaps.
#  - Old games (engine_version < 29): swap_cards is a no-op (no swap).
# Run from the project root:  uv run python tests/_swap_cards.py
# NOTE: writes to games.db (like _engineers.py / _doctors.py); games deleted after.
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

SWAP = 'Mia11_0e90ab'   # adv -1, mana 1, no_condition, effect swap_cards
ADV  = 'Dwa23_79c784'   # adv 2, mana 2, no_condition, effect advancing (movement)
ROOT = 'Twi33_c0e6df'   # adv 1, mana 3, no_condition, effect rooted
EPO  = 'epo'            # doctors pending card (mana_cost 1)
REF  = 'refinery'       # engineers dwelling card (mana_cost 3)

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()

def new_game(a_cards, b_cards, version=None):
    p1 = PlayerState(name='A', deck=list(a_cards))
    p2 = PlayerState(name='B', deck=list(b_cards))
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    if version is not None:
        gs.engine_version = version
    return gs, gid

def set_biomes(gs, biome='OC'):
    for cell in gs.earth:
        cell[0] = biome

def force_hand(gs, name, cards):
    """ Move the given cards to the hand (idempotent — no duplication: a card
    already in the initial hand is left as-is, otherwise moved from the deck). """
    p = gs.players[name]
    for c in cards:
        if c in p.hand:
            continue
        if c in p.deck:
            p.deck.remove(c)
        p.hand.append(c)

def ensure_mana(gs, name, n):
    p = gs.players[name]
    while len(p.mana) - (p.mana_spend or 0) < n and p.deck:
        p.mana.append(p.deck.pop(0))

def play(gs, name, first_second, card, mode='move', to='stopover_4', **extra):
    p = gs.players[name]
    cost = max(ge._play_cost(gs, card), 1)
    ensure_mana(gs, name, cost)
    p.mana_spend = 0
    msg = {'cards': [card], 'to': to, 'mode': mode, 'pendings': []}
    msg.update(extra)
    p.message = msg
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    return gs, okk, msgtxt

def pas(gs, name, first_second):
    p = gs.players[name]
    p.message = {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    assert okk, f'pass rejected: {msgtxt}'
    return gs

def all_cards(p):
    out = []
    for z in ('hand', 'mana', 'deck', 'discard'):
        out.extend(getattr(p, z) or [])
    out.extend(p.pendings or [])
    if p.dwelling:
        out.append(p.dwelling)
    return out

passed = 0
failed = 0

def check(condition, msg):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {msg}")
    else:
        failed += 1
        print(f"  ✗ {msg}")

# ============================================================
print("\n=== Test 1: play <-> play swap (v29) — the two plays exchange positions ===")
# ============================================================
filler = [c for c in DB.filter(pl.col('faction') == 'Miaous')['card_id'].to_list() if c not in (SWAP, ADV, ROOT)][:18]
gs, gid = new_game([SWAP, ADV] + filler, [ADV] + filler[:16])
set_biomes(gs)
a = gs.players['A']
check(gs.engine_version == 29, f"new games are engine_version 29: {gs.engine_version}")

force_hand(gs, 'A', [ADV, SWAP])
n_a_start = len(all_cards(gs.players['A']))   # snapshot BEFORE any play (conservation reference)
gs, okk, msgtxt = play(gs, 'A', 'first', ADV, mode='move', to='stopover_4')   # position 1
check(okk, f"play ADV accepted (msg: {msgtxt})")
gs, okk, msgtxt = play(gs, 'A', 'first', SWAP, mode='move', to='stopover_3', swap_with=1)  # position 2, swap with 1
check(okk, f"play SWAP (swap_with=1) accepted (msg: {msgtxt})")

chain = a.action_chain
check(len(chain) == 2, f"two plays in the action chain: {len(chain)}")
adv_act = [x for x in chain if ADV in (x.get('cards') or [])]
swp_act = [x for x in chain if SWAP in (x.get('cards') or [])]
check(adv_act and adv_act[0]['to'] == 'stopover_3',
      f"ADV moved to the SWAP's original position (stopover_3): {adv_act and adv_act[0]['to']}")
check(swp_act and swp_act[0]['to'] == 'stopover_4',
      f"SWAP took ADV's position (stopover_4): {swp_act and swp_act[0]['to']}")
if swp_act:
    s = swp_act[0]
    check(s.get('swapped_with') == ADV, f"annotation swapped_with == ADV: {s.get('swapped_with')}")
    check(s.get('swapped_with_kind') == 'play', f"annotation swapped_with_kind == 'play': {s.get('swapped_with_kind')}")
    check(s.get('swapped_from') == 'stopover_3', f"annotation swapped_from == 'stopover_3': {s.get('swapped_from')}")
inst = [ln for t in (gs.log or []) for ln in (t.get('instant') or [])]
inst_txt = [(ln or {}).get('what') if isinstance(ln, dict) else str(ln) for ln in inst]
check(any('swap_cards' in (s or '') for s in inst_txt), f"turn log has the swap entry: {inst_txt}")

# resolve: B passes, then the trip chain
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
a2 = gs.players['A']
check(SWAP not in a2.hand and ADV not in a2.hand, f"both cards left A's hand: {a2.hand}")
check(len(all_cards(a2)) == n_a_start, f"card conservation (A has {len(all_cards(a2))}/{n_a_start})")
_delete(gid)

# ============================================================
print("\n=== Test 2: play <-> pending placeholder swap (v29) ===")
# ============================================================
gs, gid = new_game([SWAP, EPO] + filler, [ADV] + filler[:16])
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [EPO, SWAP])
gs, okk, msgtxt = play(gs, 'A', 'first', EPO, mode='', to='pending_zone')    # position 1
check(okk, f"pending placement accepted (msg: {msgtxt})")
check(a.pending_slots == [[EPO, 4]], f"pending placeholder at position 1 (slot 4): {a.pending_slots}")
gs, okk, msgtxt = play(gs, 'A', 'first', SWAP, mode='move', to='stopover_3', swap_with=1)  # position 2
check(okk, f"play SWAP (swap_with=1) accepted (msg: {msgtxt})")
a = gs.players['A']
check(a.pending_slots == [[EPO, 3]],
      f"the pending placeholder MOVED to the SWAP's position (slot 3): {a.pending_slots}")
chain = a.action_chain
swp_act = [x for x in chain if SWAP in (x.get('cards') or [])]
check(swp_act and swp_act[0]['to'] == 'stopover_4',
      f"SWAP took the placeholder's position (stopover_4): {swp_act and swp_act[0]['to']}")
if swp_act:
    check(swp_act[0].get('swapped_with') == EPO and swp_act[0].get('swapped_with_kind') == 'pending',
          f"annotations: swapped_with={swp_act[0].get('swapped_with')} kind={swp_act[0].get('swapped_with_kind')}")
# resolve
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
a2 = gs.players['A']
check(SWAP not in a2.hand, f"SWAP resolved out of the hand: {a2.hand}")
check(EPO in (a2.pendings or []), f"the pending card STAYS in the pending zone (not consumed by the swap): {a2.pendings}")
_delete(gid)

# ============================================================
print("\n=== Test 3: play <-> dwelling placeholder swap (v29) ===")
# ============================================================
gs, gid = new_game([SWAP, REF] + filler, [ADV] + filler[:16])
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [REF, SWAP])
gs, okk, msgtxt = play(gs, 'A', 'first', REF, mode='', to='dwelling')        # position 1
check(okk, f"dwelling placement accepted (msg: {msgtxt})")
a = gs.players['A']
check(a.dwelling == REF and a.dwelling_slot == 4,
      f"refinery dwelling at position 1 (slot 4): dwelling={a.dwelling} slot={a.dwelling_slot}")
gs, okk, msgtxt = play(gs, 'A', 'first', SWAP, mode='move', to='stopover_3', swap_with=1)  # position 2
check(okk, f"play SWAP (swap_with=1) accepted (msg: {msgtxt})")
a = gs.players['A']
check(a.dwelling_slot == 3, f"the dwelling placeholder MOVED to the SWAP's position (slot 3): {a.dwelling_slot}")
check(a.dwelling == REF, f"the dwelling card is UNCHANGED (only the placeholder moved): {a.dwelling}")
chain = a.action_chain
swp_act = [x for x in chain if SWAP in (x.get('cards') or [])]
check(swp_act and swp_act[0]['to'] == 'stopover_4',
      f"SWAP took the placeholder's position (stopover_4): {swp_act and swp_act[0]['to']}")
if swp_act:
    check(swp_act[0].get('swapped_with') == REF and swp_act[0].get('swapped_with_kind') == 'dwelling',
          f"annotations: swapped_with={swp_act[0].get('swapped_with')} kind={swp_act[0].get('swapped_with_kind')}")
# resolve
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
a2 = gs.players['A']
check(SWAP not in a2.hand, f"SWAP resolved out of the hand: {a2.hand}")
check(a2.dwelling == REF, f"the dwelling card survives the turn end: {a2.dwelling}")
_delete(gid)

# ============================================================
print("\n=== Test 4: play <-> rooted card swap (v29) — resolution order follows the swap ===")
# ============================================================
gs, gid = new_game([SWAP] + filler, [ROOT] + filler[:16])
set_biomes(gs)
a = gs.players['A']
# A has a rooted card on the board at position 1 (from a previous turn)
gs.rooted_on_board = [{'card_id': ROOT, 'owner': 'A', 'stopover': 'stopover_4'}]
force_hand(gs, 'A', [SWAP])
gs, okk, msgtxt = play(gs, 'A', 'first', SWAP, mode='move', to='stopover_3', swap_with=1)  # position 2 (rooted 1)
check(okk, f"play SWAP (swap_with=1, rooted target) accepted (msg: {msgtxt})")
a = gs.players['A']
r0 = [r for r in gs.rooted_on_board if r.get('owner') == 'A']
check(r0 and r0[0].get('stopover') == 'stopover_3',
      f"the rooted card MOVED to the SWAP's position (stopover_3): {r0}")
chain = a.action_chain
swp_act = [x for x in chain if SWAP in (x.get('cards') or [])]
check(swp_act and swp_act[0]['to'] == 'stopover_4',
      f"SWAP took the rooted card's position (stopover_4): {swp_act and swp_act[0]['to']}")
if swp_act:
    check(swp_act[0].get('swapped_with') == ROOT and swp_act[0].get('swapped_with_kind') == 'rooted',
          f"annotations: swapped_with={swp_act[0].get('swapped_with')} kind={swp_act[0].get('swapped_with_kind')}")
# resolve: SWAP is now position 1 (resolves FIRST: adv -1 from cell 0 -> stays 0),
# the rooted card is position 2 (adv +1 -> A moves to 1). The swapped ORDER is the
# observable: without the swap the rooted card would move first (0->1) and the SWAP
# card would knock A back (1->0).
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
a2 = gs.players['A']
check(a2.current_position == 1,
      f"the swapped RESOLUTION ORDER applied (SWAP first: 0->0; rooted second: 0->1): pos={a2.current_position}")
_delete(gid)

# ============================================================
print("\n=== Test 5: no swap_with -> NO swap (the card advances as usual) ===")
# ============================================================
gs, gid = new_game([SWAP, ADV] + filler, [ADV] + filler[:16])
set_biomes(gs)
force_hand(gs, 'A', [ADV, SWAP])
gs, okk, msgtxt = play(gs, 'A', 'first', ADV, mode='move', to='stopover_4')   # position 1
gs, okk, msgtxt = play(gs, 'A', 'first', SWAP, mode='move', to='stopover_3')  # position 2, NO swap
check(okk, f"play SWAP without swap_with accepted (msg: {msgtxt})")
chain = gs.players['A'].action_chain
adv_act = [x for x in chain if ADV in (x.get('cards') or [])]
swp_act = [x for x in chain if SWAP in (x.get('cards') or [])]
check(adv_act and adv_act[0]['to'] == 'stopover_4', f"ADV keeps its position: {adv_act and adv_act[0]['to']}")
check(swp_act and swp_act[0]['to'] == 'stopover_3', f"SWAP keeps its position: {swp_act and swp_act[0]['to']}")
check(swp_act and 'swapped_with' not in swp_act[0], f"no swap annotations: {swp_act and list(swp_act[0].keys())}")
_delete(gid)

# ============================================================
print("\n=== Test 6: invalid swap_with -> the play is REJECTED (no state change) ===")
# ============================================================
gs, gid = new_game([SWAP] + filler, [ADV] + filler[:16])
set_biomes(gs)
a = gs.players['A']
hand_before = list(a.hand)
chain_before = list(a.action_chain or [])
gs, okk, msgtxt = play(gs, 'A', 'first', SWAP, mode='move', to='stopover_4', swap_with=3)  # position 3 is empty
check(not okk, f"play SWAP (swap_with=3, empty position) REJECTED: {msgtxt}")
a = gs.players['A']
check(list(a.hand) == hand_before, f"hand unchanged: {a.hand}")
check(list(a.action_chain or []) == chain_before, f"action chain unchanged: {a.action_chain}")

gs, gid2 = new_game([SWAP] + filler, [ADV] + filler[:16])
set_biomes(gs)
gs, okk, msgtxt = play(gs, 'A', 'first', SWAP, mode='move', to='stopover_4', swap_with=1)  # its own position (empty before the play)
check(not okk, f"self-swap (swap_with=1 on the first play) REJECTED: {msgtxt}")
_delete(gid); _delete(gid2)

# ============================================================
print("\n=== Test 7: old game (v28) — swap_with is IGNORED (no swap) ===")
# ============================================================
gs, gid = new_game([SWAP, ADV] + filler, [ADV] + filler[:16], version=28)
set_biomes(gs)
force_hand(gs, 'A', [ADV, SWAP])
gs, okk, msgtxt = play(gs, 'A', 'first', ADV, mode='move', to='stopover_4')   # position 1
gs, okk, msgtxt = play(gs, 'A', 'first', SWAP, mode='move', to='stopover_3', swap_with=1)  # position 2
check(okk, f"play SWAP (swap_with=1) accepted in a v28 game (the field is inert): {msgtxt}")
chain = gs.players['A'].action_chain
adv_act = [x for x in chain if ADV in (x.get('cards') or [])]
swp_act = [x for x in chain if SWAP in (x.get('cards') or [])]
check(adv_act and adv_act[0]['to'] == 'stopover_4', f"v28: ADV keeps its position (no swap): {adv_act and adv_act[0]['to']}")
check(swp_act and swp_act[0]['to'] == 'stopover_3', f"v28: SWAP keeps its position (no swap): {swp_act and swp_act[0]['to']}")
check(swp_act and 'swapped_with' not in swp_act[0], f"v28: no swap annotations")
_delete(gid)

# ============================================================
print("\n=== Test 8: DEFEND mode -> NO swap ===")
# ============================================================
gs, gid = new_game([SWAP, ADV] + filler, [ADV] + filler[:16])
set_biomes(gs)
force_hand(gs, 'A', [ADV, SWAP])
gs, okk, msgtxt = play(gs, 'A', 'first', ADV, mode='move', to='stopover_4')   # position 1
gs, okk, msgtxt = play(gs, 'A', 'first', SWAP, mode='defend', to='stopover_3', swap_with=1)  # position 2
check(okk, f"defend SWAP (swap_with=1) accepted: {msgtxt}")
chain = gs.players['A'].action_chain
adv_act = [x for x in chain if ADV in (x.get('cards') or [])]
swp_act = [x for x in chain if SWAP in (x.get('cards') or [])]
check(adv_act and adv_act[0]['to'] == 'stopover_4', f"defend: ADV keeps its position: {adv_act and adv_act[0]['to']}")
check(swp_act and swp_act[0]['to'] == 'stopover_3', f"defend: SWAP keeps its position: {swp_act and swp_act[0]['to']}")
check(swp_act and 'swapped_with' not in swp_act[0], f"defend: no swap annotations")
_delete(gid)

# ============================================================
print("\n=== Test 9: message_check validates swap_with ===")
# ============================================================
base = {'cards': [SWAP], 'to': 'stopover_3', 'mode': 'move', 'pendings': []}
ok, msg = ge.message_check({**base, 'swap_with': 2});   check(ok, f"swap_with=2 accepted: {msg}")
ok, msg = ge.message_check({**base, 'swap_with': 9});   check(not ok, f"swap_with=9 rejected: {msg}")
ok, msg = ge.message_check({**base, 'swap_with': 0});   check(not ok, f"swap_with=0 rejected: {msg}")
ok, msg = ge.message_check({**base, 'swap_with': True}); check(not ok, f"swap_with=True (bool) rejected: {msg}")
ok, msg = ge.message_check({**base, 'swap_with': '2'});  check(not ok, f"swap_with='2' (str) rejected: {msg}")
ok, msg = ge.message_check(base);                       check(ok, f"no swap_with accepted: {msg}")

# ============================================================
print(f"\n{'=' * 60}\n{passed} passed, {failed} failed\n{'=' * 60}")
sys.exit(1 if failed else 0)
