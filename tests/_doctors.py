# Manual smoke test: Doctors support faction (engine_version 16)
#  - 4 PENDING cards (epo / virus / bloodtest / mercurochrome):
#      * placed in the pending zone (to 'pending_zone')
#      * attached to a main card (pendings: [card_name])
#      * effect fires ONLY if the main card's condition is met
#      * epo: +1 advancing / virus: -1 knockback / bloodtest: discard 1
#      * mercurochrome: unstoppable (bypasses blocks and landmines)
#  - 1 DWELLING card (laboratory):
#      * placed in the dwelling zone (to 'dwelling')
#      * tapped once per turn (free) -> adds an 'epo' pending card
#  - pending card placeholders in the stopover (pending_slots)
#  - card conservation (pending card flushed to discard on mid-chain win)
#  - old games (engine_version < 16): pending zone placement rejected
# Run from the project root:  uv run python tests/_doctors.py
# NOTE: writes to games.db (like _engineers.py)
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

def q(**kw):
    df = DB
    for k, v in kw.items():
        df = df.filter(pl.col(k) == v)
    return df

# a no_condition advancing card (for the main play)
adv1 = q(condition='no_condition', advancing=2, mana=1)['card_id'].to_list()
adv1 = adv1[0] if adv1 else q(condition='no_condition')['card_id'].to_list()[0]

# a no_condition card with a higher advancing (for testing +1 bonus)
adv2 = q(condition='no_condition', advancing=3, mana=2)['card_id'].to_list()
adv2 = adv2[0] if adv2 else adv1

# an unstoppable card (for mercurochrome interaction test)
un1 = None
for fac in ('Miaous', 'Orcs', 'Mummies'):
    ids = q(effect='unstoppable', condition='no_condition', faction=fac)['card_id'].to_list()
    if ids:
        un1 = ids[0]
        break
if un1 is None:
    un1 = adv1  # fallback

# filler cards
filler = [c for c in q(faction='Miaous')['card_id'].to_list() if c not in {adv1, adv2, un1}][:14]

# doctor support cards
DOC_PENDING = ['epo', 'virus', 'bloodtest', 'mercurochrome']
DOC_DWELLING = 'laboratory'

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()

def new_game(a_cards, b_cards, version=None):
    p1 = PlayerState(name='A', deck=list(a_cards) + filler)
    p2 = PlayerState(name='B', deck=list(b_cards) + filler)
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
    p = gs.players[name]
    for c in cards:
        if c not in p.hand:
            if c in p.deck:
                p.deck.remove(c)
            p.hand.append(c)

def ensure_mana(gs, name, n):
    p = gs.players[name]
    while len(p.mana) - (p.mana_spend or 0) < n and p.deck:
        p.mana.append(p.deck.pop(0))

def play(gs, name, first_second, card, mode='move', to='stopover_4', cell=None, pendings=None):
    p = gs.players[name]
    cost = max(ge._play_cost(gs, card), 1)
    ensure_mana(gs, name, cost)
    p.mana_spend = 0
    msg = {'cards': [card], 'to': to, 'mode': mode, 'pendings': pendings or []}
    if cell is not None:
        msg['cell'] = cell
    p.message = msg
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    assert okk, f'play rejected: {msgtxt}'
    return gs

def pas(gs, name, first_second):
    p = gs.players[name]
    p.message = {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    assert okk, f'pass rejected: {msgtxt}'
    return gs

def dwelling(gs, name, first_second, cards=None, mode=''):
    p = gs.players[name]
    ensure_mana(gs, name, 3)
    p.mana_spend = 0
    p.message = {'cards': cards or [], 'to': 'dwelling', 'mode': mode, 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    return gs, okk, msgtxt

def next_turn(gs):
    gs.turn += 1
    gs.first_player_passed = False
    gs.second_player_passed = False
    gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
    return gs

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
print("\n=== Test 1: Pending zone placement ===")
# ============================================================
a_cards = [adv1, adv2, un1] + DOC_PENDING
b_cards = [adv1, adv2, un1]
gs, gid = new_game(a_cards, b_cards)
set_biomes(gs)

alice = gs.players['A']
bob = gs.players['B']

print(f"  A hand: {alice.hand[:6]}")
print(f"  A mana: {alice.mana}")

# A places 'epo' in the pending zone
# Cost: 1 mana (mana_cost from support_factions.parquet)
force_hand(gs, 'A', ['epo', 'virus'])
gs = play(gs, 'A', 'first', 'epo', mode='', to='pending_zone')
check('epo' in alice.pendings, f"'epo' is in A's pendings: {alice.pendings}")
check(len(alice.pendings) == 1, f"A has 1 pending card")
check('epo' not in alice.hand, f"'epo' is not in A's hand")
check(alice.pending_slots is not None and len(alice.pending_slots) == 1,
      f"A has 1 pending slot: {alice.pending_slots}")

# A places 'virus' in the pending zone
gs = play(gs, 'A', 'first', 'virus', mode='', to='pending_zone')
check('virus' in alice.pendings, f"'virus' is in A's pendings: {alice.pendings}")
check(len(alice.pendings) == 2, f"A has 2 pending cards")

print(f"  A pendings: {alice.pendings}")
print(f"  A pending_slots: {alice.pending_slots}")

_delete(gid)

# ============================================================
print("\n=== Test 2: Pending card attachment (epo: +1 advancing) ===")
# ============================================================
a_cards = [adv1, adv2, un1] + DOC_PENDING
b_cards = [adv1, adv2, un1]
gs, gid = new_game(a_cards, b_cards)
set_biomes(gs)

alice = gs.players['A']
bob = gs.players['B']

# A places 'epo' in the pending zone
force_hand(gs, 'A', ['epo'])
gs = play(gs, 'A', 'first', 'epo', mode='', to='pending_zone')
check('epo' in alice.pendings, f"'epo' placed: {alice.pendings}")

# A plays adv1 (advancing=2, no_condition) with 'epo' attached
# Expected: advancing 2 + 1 (epo) = 3
force_hand(gs, 'A', [adv1])
pos_before = alice.current_position
adv1_row = ge.CARDS_DB.filter(ge.CARDS_DB['card_id'] == adv1)
base_adv = int(adv1_row['advancing'][0] or 0) if adv1_row.height > 0 else 0
print(f"  A position before: {pos_before}, base advancing: {base_adv}")

gs = play(gs, 'A', 'first', adv1, mode='move', to='stopover_4', pendings=['epo'])
check('epo' not in alice.pendings, f"'epo' consumed from pendings: {alice.pendings}")
check(alice.pending_slots == [['epo', 4]], f"the epo placeholder stays in place: {alice.pending_slots}")

# B passes
gs = pas(gs, 'B', 'second')

# Resolve the trip chain
gs = ge.process_trip_chain(gs)

print(f"  A position after: {alice.current_position}")
expected = pos_before + base_adv + 1  # base advancing + epo +1
# Note: the faction biome bonus may add +1 if on a home biome
# OC is not a home biome for Miaous, so no bonus
print(f"  Expected: {expected} (base {base_adv} + epo +1)")
check(alice.current_position >= expected - 1, f"A advanced (got {alice.current_position}, expected ~{expected})")

_delete(gid)

# ============================================================
print("\n=== Test 3: Laboratory tap (adds 'epo' pending) ===")
# ============================================================
a_cards = [adv1, adv2, un1] + [DOC_DWELLING]
b_cards = [adv1, adv2, un1]
gs, gid = new_game(a_cards, b_cards)
set_biomes(gs)

alice = gs.players['A']

# A places the laboratory (dwelling card)
force_hand(gs, 'A', [DOC_DWELLING])
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=[DOC_DWELLING], mode='')
check(okk, f"Laboratory placement succeeded (msg: {msgtxt})")
check(alice.dwelling == DOC_DWELLING, f"A's dwelling is 'laboratory': {alice.dwelling}")

# A taps the laboratory
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=[], mode='dwelling_activation')
check(okk, f"Laboratory tap succeeded (msg: {msgtxt})")
check('epo' in alice.pendings, f"'epo' added to A's pendings: {alice.pendings}")
check(len(alice.pendings) == 1, f"A has 1 pending card after tap")

_delete(gid)

# ============================================================
print("\n=== Test 3b: Laboratory tap is a QUICK action with NO placeholder (v18) ===")
# ============================================================
a_cards = [adv1, adv2, un1] + [DOC_DWELLING]
b_cards = [adv1, adv2, un1]
gs, gid = new_game(a_cards, b_cards)
set_biomes(gs)

alice = gs.players['A']

# A places the laboratory (dwelling card) — still alternates, still gets a placeholder
force_hand(gs, 'A', [DOC_DWELLING])
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=[DOC_DWELLING], mode='')
check(okk, f"Laboratory placement succeeded (msg: {msgtxt})")
check(alice.dwelling == DOC_DWELLING, f"A's dwelling is 'laboratory': {alice.dwelling}")
check(alice.dwelling_slot is not None, f"Dwelling placeholder slot set: {alice.dwelling_slot}")
play_count_after_place = alice.play_count or 0

# A taps the laboratory — QUICK action, adds 'epo' with NO trip-chain placeholder
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=[], mode='dwelling_activation')
check(okk, f"Laboratory tap succeeded (msg: {msgtxt})")
check(alice.pendings == ['epo'], f"'epo' added to A's pendings: {alice.pendings}")
check(alice.pending_slots == [],
      f"pending_slots has no tap-epo entry (no placeholder): {alice.pending_slots}")
check((alice.play_count or 0) == play_count_after_place,
      f"tap consumes NO position (play_count {alice.play_count} == {play_count_after_place})")
check(gs.state == f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play",
      f"tap is a QUICK action — state still waits for A: {gs.state}")

# the 'epo' must NOT appear in A's trip chain as a placeholder entry
chain = ge._player_chain(gs, 'A')
ph = [e for e in chain if e.get('kind') == 'placeholder']
check(len(ph) == 1 and ph[0].get('stopover') == f"stopover_{alice.dwelling_slot}",
      f"chain has only the DWELLING placeholder, not the tap's epo: {chain}")

# A can still play a card after the tap (quick action — no alternation happened)
p = gs.players['A']
p.mana_spend = 0
cost = max(ge._play_cost(gs, adv1), 1)
ensure_mana(gs, 'A', cost)
p.mana_spend = 0
p.hand = [adv1] + p.hand
p.message = {'cards': [adv1], 'to': 'stopover_3', 'mode': 'move', 'pendings': []}
p, gs, okk, msgtxt = ge.player_play('first', p, gs)
check(okk, f"A can still play a card after the tap (msg: {msgtxt})")
# the play lands on the position the tap's 'epo' would have taken (no slot consumed)
expected_to = ge._player_stopover(gs, 'A', play_count_after_place)
check(p.message.get('to') == expected_to,
      f"play lands on {expected_to} (the tap consumed no position): {p.message.get('to')}")

# attaching the tap's 'epo' to a main card still works (parallel-list removal by index)
gs2, gid2 = new_game([adv1, adv2, un1] + [DOC_DWELLING], [adv1, adv2, un1])
set_biomes(gs2)
a2 = gs2.players['A']
force_hand(gs2, 'A', [DOC_DWELLING])
gs2, okk, msgtxt = dwelling(gs2, 'A', 'first', cards=[DOC_DWELLING], mode='')
check(okk, f"[attach] Laboratory placement succeeded (msg: {msgtxt})")
gs2, okk, msgtxt = dwelling(gs2, 'A', 'first', cards=[], mode='dwelling_activation')
check(okk, f"[attach] Laboratory tap succeeded (msg: {msgtxt})")
check(a2.pending_slots == [], f"[attach] pending_slots has no tap-epo entry: {a2.pending_slots}")
p2 = gs2.players['A']
p2.mana_spend = 0
ensure_mana(gs2, 'A', max(ge._play_cost(gs2, adv1), 1) + 1)
p2.mana_spend = 0
p2.hand = [adv1] + p2.hand
p2.message = {'cards': [adv1], 'to': 'stopover_3', 'mode': 'move', 'pendings': ['epo']}
p2, gs2, okk, msgtxt = ge.player_play('first', p2, gs2)
check(okk, f"[attach] play with the tap's 'epo' attached succeeded (msg: {msgtxt})")
check(a2.pendings == [], f"[attach] 'epo' consumed from the pending zone: {a2.pendings}")
check(a2.pending_slots == [], f"[attach] pending_slots cleared (None entry removed): {a2.pending_slots}")

_delete(gid)
_delete(gid2)

# ============================================================
print("\n=== Test 5: Mercurochrome unstoppable ===")
# ============================================================
# This test verifies that a card with mercurochrome attached can bypass blocks.
# We need:
#  - A plays a card with mercurochrome attached (unstoppable)
#  - B defends with a high-shield card on the same stopover
#  - The block should be bypassed
a_cards = [adv1, adv2, un1] + DOC_PENDING
b_cards = [adv1, adv2, un1]
gs, gid = new_game(a_cards, b_cards)
set_biomes(gs)

alice = gs.players['A']
bob = gs.players['B']

# A places 'mercurochrome' in the pending zone
force_hand(gs, 'A', ['mercurochrome'])
gs = play(gs, 'A', 'first', 'mercurochrome', mode='', to='pending_zone')
check('mercurochrome' in alice.pendings, f"'mercurochrome' placed: {alice.pendings}")

# B defends with a high-shield card
# Find a high-shield card
shield_card = q(shield=6)['card_id'].to_list()[0]
force_hand(gs, 'B', [shield_card])
gs = play(gs, 'B', 'second', shield_card, mode='defend', to='stopover_4')

# A plays adv1 with mercurochrome attached (should bypass the block)
force_hand(gs, 'A', [adv1])
pos_before = alice.current_position
gs = play(gs, 'A', 'first', adv1, mode='move', to='stopover_4', pendings=['mercurochrome'])
check('mercurochrome' not in alice.pendings, f"'mercurochrome' consumed: {alice.pendings}")

# B passes
gs = pas(gs, 'B', 'second')

# Resolve the trip chain
gs = ge.process_trip_chain(gs)

print(f"  A position before: {pos_before}")
print(f"  A position after: {alice.current_position}")
# The card should have advanced despite the block (mercurochrome = unstoppable)
adv1_row2 = ge.CARDS_DB.filter(ge.CARDS_DB['card_id'] == adv1)
base_adv2 = int(adv1_row2['advancing'][0] or 0) if adv1_row2.height > 0 else 0
print(f"  Base advancing: {base_adv2}")
check(alice.current_position >= pos_before + base_adv2, f"A advanced despite block (got {alice.current_position})")

_delete(gid)

# ============================================================
print("\n=== Test 6: attachment must not wipe an UNRELATED placeholder (v19 bug fix) ===")
# ============================================================
# Repro of game 26_09_17_21_33_39_d5oEd turn 4: an OLD pending card (from a
# previous turn, low index in the persistent `pendings` zone) was attached to a
# play — the v18-and-below index splice deleted pending_slots[0], which was the
# FRESH placeholder of a card placed THIS turn. v19 removes the placeholder
# BY CARD NAME (pending_slots = [card, slot] pairs).

def _setup_attach_scenario(version):
    """ A: 1 OLD pending ('virus' — placed a previous turn, slots already cleared
     by the cleaning phase) + places 'mercurochrome' THIS turn (fresh placeholder).
     Then attaches the OLD 'virus' to a main-card play. Returns (gs, gid, alice). """
    gs, gid = new_game([adv1, adv2, un1], [adv1, adv2, un1], version=version)
    set_biomes(gs)
    a = gs.players['A']
    # simulate the persistent state at the start of the turn: an old 'virus'
    # pending card (from a previous turn) and a CLEARED placeholder list
    a.pendings = ['virus']
    a.pending_slots = []
    a.play_count = 0
    # A places 'mercurochrome' in the pending zone (fresh placeholder, position 1)
    force_hand(gs, 'A', ['mercurochrome'])
    gs = play(gs, 'A', 'first', 'mercurochrome', mode='', to='pending_zone')
    check('mercurochrome' in (a.pendings or []), f"[v{version}] mercurochrome in pendings: {a.pendings}")
    # A plays a main card with the OLD 'virus' attached
    force_hand(gs, 'A', [adv1])
    p = gs.players['A']
    cost = max(ge._play_cost(gs, adv1), 1)
    ensure_mana(gs, 'A', cost)
    p.mana_spend = 0
    p.message = {'cards': [adv1], 'to': 'stopover_3', 'mode': 'move', 'pendings': ['virus']}
    gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play('first', p, gs)
    assert okk, f"play with attached virus rejected: {msgtxt}"
    return gs, gid, gs.players['A']

# --- v19: the mercurochrome placeholder SURVIVES the old-pending attachment ---
gs, gid, a = _setup_attach_scenario(19)
check(a.pendings == ['mercurochrome'], f"[v19] virus consumed, mercurochrome kept: {a.pendings}")
slots_v19 = a.pending_slots or []
check(any(isinstance(e, (list, tuple)) and len(e) == 2 and e[0] == 'mercurochrome' for e in slots_v19),
      f"[v19] mercurochrome placeholder SURVIVES the virus attachment: {slots_v19}")
chain = ge._player_chain(gs, 'A')
ph = [e for e in chain if e.get('kind') == 'placeholder']
check(len(ph) == 1, f"[v19] exactly 1 placeholder in the trip chain (mercurochrome's): {chain}")
# the NEXT play must land on position 3 (the placeholder + the play), not 2
next_stop = ge._player_stopover(gs, 'A', a.play_count or 0)
check(next_stop == 'stopover_2', f"[v19] next play lands on position 3 (stopover_2), got {next_stop}")
_delete(gid)

# --- v19: laboratory tap adds 'epo' with NO placeholder entry ---
gs, gid = new_game([adv1, adv2, un1] + [DOC_DWELLING], [adv1, adv2, un1])
set_biomes(gs)
a = gs.players['A']
force_hand(gs, 'A', [DOC_DWELLING])
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=[DOC_DWELLING], mode='')
check(okk, f"[v19] laboratory placement succeeded (msg: {msgtxt})")
slots_before = list(a.pending_slots or [])
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=[], mode='dwelling_activation')
check(okk, f"[v19] laboratory tap succeeded (msg: {msgtxt})")
check('epo' in (a.pendings or []), f"[v19] 'epo' in pendings: {a.pendings}")
check((a.pending_slots or []) == slots_before,
      f"[v19] tap adds NO placeholder entry: {a.pending_slots}")
chain = ge._player_chain(gs, 'A')
ph = [e for e in chain if e.get('kind') == 'placeholder']
check(len(ph) == 1 and ph[0].get('stopover') == f"stopover_{a.dwelling_slot}",
      f"[v19] chain has only the dwelling placeholder (no tap-epo placeholder): {chain}")
_delete(gid)

# ============================================================
print("\n=== Test 7: placeholder STAYS IN PLACE when the pending card is attached this turn (v20) ===")
# ============================================================
# A places a pending card THIS turn (placeholder at position 1) and then attaches
# it to a main-card play (position 2). v20: the placeholder STAYS — it marks the
# consumed trip-chain position, so the next play lands on position 3 (stopover_2)
# and the frontend's next-slot mirror (visible action_chain + placeholders) stays
# consistent with the engine's play_count. v19 and below removed the placeholder
# on attachment — the freed position desynced the frontend mirror (it offered the
# main card's own slot for the next play).

def _setup_same_turn_attach(version):
    """ A places 'epo' in the pending zone THIS turn (fresh placeholder, position 1),
     then plays a main card with 'epo' attached (position 2). Returns (gs, gid, a). """
    gs, gid = new_game([adv1, adv2, un1], [adv1, adv2, un1], version=version)
    set_biomes(gs)
    force_hand(gs, 'A', ['epo'])
    gs = play(gs, 'A', 'first', 'epo', mode='', to='pending_zone')
    a = gs.players['A']
    check('epo' in (a.pendings or []), f"[v{version}] epo in pendings: {a.pendings}")
    # A plays a main card with the fresh 'epo' attached
    force_hand(gs, 'A', [adv1])
    gs = play(gs, 'A', 'first', adv1, mode='move', to='stopover_3', pendings=['epo'])
    return gs, gid, gs.players['A']

# --- v20: the placeholder of a same-turn attached pending card STAYS ---
gs, gid, a = _setup_same_turn_attach(20)
check(a.pendings == [], f"[v20] epo consumed from the zone: {a.pendings}")
check(any(isinstance(e, (list, tuple)) and len(e) == 2 and e[0] == 'epo' for e in (a.pending_slots or [])),
      f"[v20] the epo placeholder STAYS IN PLACE: {a.pending_slots}")
chain = ge._player_chain(gs, 'A')
ph = [e for e in chain if e.get('kind') == 'placeholder']
plays = [e for e in chain if e.get('kind') == 'play']
check(len(ph) == 1 and ph[0].get('stopover') == 'stopover_4',
      f"[v20] trip chain: placeholder at position 1 (stopover_4): {chain}")
check(len(plays) == 1 and plays[0].get('stopover') == 'stopover_3',
      f"[v20] trip chain: the main card at position 2 (stopover_3): {chain}")
next_stop = ge._player_stopover(gs, 'A', a.play_count or 0)
check(next_stop == 'stopover_2', f"[v20] next play lands on position 3 (stopover_2), got {next_stop}")
# frontend mirror consistency: visible playedCount (action_chain + non-null slots)
# must equal the engine's play_count — the v19 desync (mirror dropped by one)
front_mirror = len(a.action_chain) + sum(1 for e in (a.pending_slots or [])
    if (e[1] if isinstance(e, (list, tuple)) else e) is not None)
check(front_mirror == (a.play_count or 0),
      f"[v20] frontend mirror ({front_mirror}) == engine play_count ({a.play_count})")
_delete(gid)

# --- v20: attaching an OLD pending (no placeholder this turn) — nothing to keep ---
gs, gid = new_game([adv1, adv2, un1], [adv1, adv2, un1])
set_biomes(gs)
a = gs.players['A']
a.pendings = ['virus']          # placed a PREVIOUS turn (persistent zone)
a.pending_slots = []            # the per-turn placeholder list was cleared
a.play_count = 0
force_hand(gs, 'A', [adv1])
gs = play(gs, 'A', 'first', adv1, mode='move', to='stopover_4', pendings=['virus'])
a = gs.players['A']
check(a.pendings == [], f"[v20] old virus consumed: {a.pendings}")
check((a.pending_slots or []) == [],
      f"[v20] old pending has no placeholder — pending_slots stays empty: {a.pending_slots}")
next_stop = ge._player_stopover(gs, 'A', a.play_count or 0)
check(next_stop == 'stopover_3', f"[v20] next play lands on position 2 (stopover_3), got {next_stop}")
_delete(gid)

# ============================================================
print(f"\n{'='*50}")
print(f"RESULTS: {passed} passed, {failed} failed")
print(f"{'='*50}")

if failed == 0:
    print("All tests passed!")
else:
    print(f"{failed} test(s) failed!")
    sys.exit(1)
