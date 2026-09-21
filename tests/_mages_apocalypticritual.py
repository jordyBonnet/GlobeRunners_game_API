# Manual smoke test: Mages support faction, FOURTH card — Apocalypticritual (engine_version 25)
#  - Apocalypticritual (mana_cost 3): an INSTANT play-time effect — the player CHOOSES
#    THE ORDER OF ALL 4 CATACLYSM CARDS (message 'cataclysm_order': a permutation of
#    the 4 biomes, index 0 = strikes next) and the cataclysm pile is SET to that
#    order, PERMANENTLY (until the next ritual reorders it).
#  - The new order is read by trigger_cataclysm at resolution time (a 'cataclysm'-
#    condition card strikes the top of the pile, then rotates it).
#  - The card is played in MOVE mode on the trip chain and resolves as a NO-OP
#    (occupies a stopover position, costs its mana_cost, no advancing / no effect).
#  - A MOVE play WITHOUT an order choice is REJECTED.
#  - Old games (engine_version < 25): Apocalypticritual is a no-op, the pile is never
#    reordered.
# Run from the project root:  uv run python tests/_mages_apocalypticritual.py
# NOTE: writes to games.db (like _mages_thermic_flux.py)
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

# a no_condition advancing card (for a normal move play / filler)
adv1 = q(condition='no_condition', advancing=2, mana=1)['card_id'].to_list()
adv1 = adv1[0] if adv1 else q(condition='no_condition')['card_id'].to_list()[0]

# a clean CATACLYSM-condition card: advancing 0, mana 1, effect 'draw' — the only
# position change on it is the cataclysm knockback (no basic advancing to muddy it).
CATACLYSM = 'Dem10_af058e'
assert q(card_id=CATACLYSM)['condition'].to_list() == ['cataclysm']

# filler cards
filler = [c for c in q(faction='Miaous')['card_id'].to_list() if c != adv1][:16]

RITUAL = ge.MAGE_APOCALYPTICRITUAL   # 'Apocalypticritual'
BIOMES = ge.BIOMES                   # ['OC', 'MO', 'DE', 'JU']

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
    # the pile is shuffled at creation; fix it to a known order for deterministic tests
    gs.cataclysm_pile = ['OC', 'MO', 'DE', 'JU']
    return gs, gid

def set_biomes(gs, biome='OC'):
    for cell in gs.earth:
        cell[0] = biome

def move_player_to(gs, name, pos):
    """Move a player's token to a cell, keeping the earth list in sync."""
    p = gs.players[name]
    for cell in gs.earth:
        if name in cell:
            cell.remove(name)
    p.current_position = pos
    gs.earth[pos].append(name)

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

def play(gs, name, first_second, card, mode='move', to='stopover_4', order=None):
    p = gs.players[name]
    cost = max(ge._play_cost(gs, card), 1)
    ensure_mana(gs, name, cost)
    p.mana_spend = 0
    msg = {'cards': [card], 'to': to, 'mode': mode, 'pendings': []}
    if order is not None:
        msg['cataclysm_order'] = order
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

def last_strike_note(gs):
    """Return the biome of the most recent '⚡ cataclysm — X strikes' log note."""
    found = None
    for turn in gs.log:
        for sv in turn.get('stopovers', []):
            for e in sv.get('entries', []):
                for note in e.get('notes', []):
                    if '⚡ cataclysm —' in note:
                        found = note
    return found

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
print("\n=== Test 1: Apocalypticritual SETS the pile to the chosen order (v25) ===")
# ============================================================
gs, gid = new_game([RITUAL, adv1], [adv1], version=25)
a = gs.players['A']
check(gs.cataclysm_pile == ['OC', 'MO', 'DE', 'JU'],
      f"pile starts at the fixed order: {gs.cataclysm_pile}")

chosen = ['JU', 'OC', 'MO', 'DE']
force_hand(gs, 'A', [RITUAL])
pos_before = a.current_position
gs, okk, msgtxt = play(gs, 'A', 'first', RITUAL, mode='move', to='stopover_4', order=chosen)
check(okk, f"play Apocalypticritual with a valid order accepted (msg: {msgtxt})")
check(gs.cataclysm_pile == chosen, f"pile SET to the chosen order {chosen}: {gs.cataclysm_pile}")
check(RITUAL not in a.hand, f"card left A's hand: {a.hand}")
check(len(a.action_chain) == 1, f"card is on the trip chain (1 action): {a.action_chain}")

# resolve the chain — the card is a NO-OP (no advancing)
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
check(a.current_position == pos_before,
      f"the card is a NO-OP on the chain (position unchanged {a.current_position} == {pos_before})")

_delete(gid)

# ============================================================
print("\n=== Test 2: INSTANT — a cataclysm card strikes the CHOSEn top biome ===")
# ============================================================
gs, gid = new_game([RITUAL, CATACLYSM, adv1], [adv1], version=25)
# all cells = OC (player A is Miaous → no biome bonus on OC); A on cell 5
set_biomes(gs, 'OC')
a = gs.players['A']
move_player_to(gs, 'A', 5)
check(ge.is_condition_met('biome_Mia', a, gs) is False,
      "A (Miaous) is NOT on a home biome (OC) → no faction bonus")

# ritual: put OC FIRST (so the next cataclysm strikes OC)
chosen = ['OC', 'JU', 'MO', 'DE']
force_hand(gs, 'A', [RITUAL])
gs, okk, msgtxt = play(gs, 'A', 'first', RITUAL, mode='move', to='stopover_4', order=chosen)
check(okk, f"ritual play accepted (msg: {msgtxt})")
check(gs.cataclysm_pile[0] == 'OC', f"ritual put OC on top of the pile: {gs.cataclysm_pile}")

# now play the cataclysm card (adv 0) — it must strike OC (the top A chose)
force_hand(gs, 'A', [CATACLYSM])
gs, okk, msgtxt = play(gs, 'A', 'first', CATACLYSM, mode='move', to='stopover_3')
check(okk, f"cataclysm-card play accepted (msg: {msgtxt})")
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)

note = last_strike_note(gs)
check(note is not None and 'OC strikes' in note, f"the cataclysm struck the CHOSEn top biome OC: {note!r}")
check(a.current_position == 0,
      f"knockback to the first OC cell (cell 0) from cell 5: {a.current_position}")
# the pile rotated (OC → bottom): now JU on top
check(gs.cataclysm_pile == ['JU', 'MO', 'DE', 'OC'],
      f"pile rotated after the strike (OC → bottom): {gs.cataclysm_pile}")

_delete(gid)

# ============================================================
print("\n=== Test 3: the chosen order is PERMANENT (persists across turn-ends) ===")
# ============================================================
gs, gid = new_game([RITUAL, adv1], [adv1], version=25)
a = gs.players['A']
chosen = ['DE', 'JU', 'OC', 'MO']
force_hand(gs, 'A', [RITUAL])
gs, okk, msgtxt = play(gs, 'A', 'first', RITUAL, mode='move', to='stopover_4', order=chosen)
check(okk, f"play accepted (msg: {msgtxt})")
check(gs.cataclysm_pile == chosen, f"pile set to {chosen}: {gs.cataclysm_pile}")

# simulate several turn-ends — the pile must stay the chosen order (no reset/rotation)
stayed = True
for _ in range(4):
    gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
    gs, _s, _m = ge._end_turn(gs, "ok")
    if gs.cataclysm_pile != chosen:
        stayed = False
        break
check(stayed, f"pile stayed {chosen} across 4 turn-ends (final: {gs.cataclysm_pile})")

_delete(gid)

# ============================================================
print("\n=== Test 4: a MOVE play WITHOUT an order choice is REJECTED (v25) ===")
# ============================================================
gs, gid = new_game([RITUAL, adv1], [adv1], version=25)
a = gs.players['A']
start_pile = list(gs.cataclysm_pile)
force_hand(gs, 'A', [RITUAL])
# no 'cataclysm_order' field -> must be rejected
gs, okk, msgtxt = play(gs, 'A', 'first', RITUAL, mode='move', to='stopover_4', order=None)
check(not okk, f"play without an order choice is rejected (msg: {msgtxt})")
check(RITUAL in a.hand, f"card still in hand after rejection: {a.hand}")
check(len(a.action_chain) == 0, f"nothing added to the trip chain: {a.action_chain}")
check(gs.cataclysm_pile == start_pile, f"pile unchanged after rejection: {gs.cataclysm_pile}")

# a bad value must be rejected by message_check too
ok, msg = ge.message_check({'cards': [RITUAL], 'to': 'stopover_4', 'mode': 'move',
                            'pendings': [], 'cataclysm_order': ['OC', 'OC', 'DE', 'JU']})
check(not ok, f"message_check rejects a duplicate-biome order (msg: {msg})")
ok, msg = ge.message_check({'cards': [RITUAL], 'to': 'stopover_4', 'mode': 'move',
                            'pendings': [], 'cataclysm_order': ['OC', 'DE']})
check(not ok, f"message_check rejects a 2-biome order (msg: {msg})")
ok, msg = ge.message_check({'cards': [RITUAL], 'to': 'stopover_4', 'mode': 'move',
                            'pendings': [], 'cataclysm_order': ['OC', 'XX', 'DE', 'JU']})
check(not ok, f"message_check rejects a non-biome entry (msg: {msg})")
ok, msg = ge.message_check({'cards': [RITUAL], 'to': 'stopover_4', 'mode': 'move',
                            'pendings': [], 'cataclysm_order': ['OC', 'MO', 'DE', 'JU']})
check(ok, f"message_check accepts a valid permutation (msg: {msg})")

_delete(gid)

# ============================================================
print("\n=== Test 5: a DEFEND play is a plain no-op (no order required, pile unchanged) ===")
# ============================================================
gs, gid = new_game([RITUAL, adv1], [adv1], version=25)
a = gs.players['A']
start_pile = list(gs.cataclysm_pile)
force_hand(gs, 'A', [RITUAL])
gs, okk, msgtxt = play(gs, 'A', 'first', RITUAL, mode='defend', to='stopover_4')
check(okk, f"defend play accepted (no order needed) (msg: {msgtxt})")
check(gs.cataclysm_pile == start_pile, f"pile NOT changed by a defend play: {gs.cataclysm_pile}")

_delete(gid)

# ============================================================
print("\n=== Test 6: old-game pinning (v24) — Apocalypticritual is a no-op, pile unchanged ===")
# ============================================================
gs, gid = new_game([RITUAL, adv1], [adv1], version=24)   # pre-Apocalypticritual
a = gs.players['A']
start_pile = list(gs.cataclysm_pile)
force_hand(gs, 'A', [RITUAL])
# v24: the card is a no-op support card; the 'cataclysm_order' field is ignored
gs, okk, msgtxt = play(gs, 'A', 'first', RITUAL, mode='move', to='stopover_4',
                       order=['JU', 'OC', 'MO', 'DE'])
check(okk, f"v24 play accepted (the card is a no-op support card) (msg: {msgtxt})")
check(gs.cataclysm_pile == start_pile, f"v24: pile NOT changed (still {start_pile}): {gs.cataclysm_pile}")

_delete(gid)

# ============================================================
print("\n=== Test 7: turn-log note records the chosen order ===")
# ============================================================
gs, gid = new_game([RITUAL, adv1], [adv1], version=25)
a = gs.players['A']
chosen = ['JU', 'OC', 'MO', 'DE']
force_hand(gs, 'A', [RITUAL])
gs, okk, msgtxt = play(gs, 'A', 'first', RITUAL, mode='move', to='stopover_4', order=chosen)
check(okk, f"play accepted (msg: {msgtxt})")
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
# find the log note for the ritual (turn 'instant' section) and check it carries
# the chosen order
found_note = None
for turn in gs.log:
    for it in (turn.get('instant') or []):
        note = str(it.get('what'))
        if 'Apocalypticritual' in note and 'cataclysm order set' in note:
            found_note = note
check(found_note is not None, f"turn log has an Apocalypticritual order note: {found_note!r}")
if found_note:
    check(all(b in found_note for b in chosen), f"the note lists the 4 chosen biomes: {found_note!r}")

_delete(gid)

# ============================================================
print("\n=== Test 8: card conservation — the ritual card ends in the discard ===")
# ============================================================
gs, gid = new_game([RITUAL, adv1], [adv1], version=25)
a = gs.players['A']
chosen = ['MO', 'DE', 'JU', 'OC']
force_hand(gs, 'A', [RITUAL])
gs, okk, msgtxt = play(gs, 'A', 'first', RITUAL, mode='move', to='stopover_4', order=chosen)
check(okk, f"play accepted (msg: {msgtxt})")
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
# NOTE: action_chain is the per-turn action record (cleared in _end_turn) — the card
# is CONSERVED by being in the discard (the hand is empty of it). The chain still
# lists this turn's play, which is expected before the cleaning phase.
in_discard = RITUAL in a.discard
in_hand = RITUAL in a.hand
check(in_discard, f"the ritual card is in A's discard after resolution: {a.discard}")
check(not in_hand, f"the ritual card left A's hand: {a.hand}")

# end the turn — the per-turn action_chain is cleared (card is fully in the discard)
gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
gs, _s, _m = ge._end_turn(gs, "ok")
in_chain = any(RITUAL in (act.get('cards') or []) for act in (a.action_chain or []))
check(not in_chain, f"after turn-end the action_chain is cleared: {a.action_chain}")
check(RITUAL in a.discard, f"the card is still conserved in the discard: {a.discard}")

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
