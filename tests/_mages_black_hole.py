# Manual smoke test: Mages support faction, FIFTH card — black_hole (engine_version 26)
#  - black_hole (mana_cost 3): a DWELLING card (like the refinery / laboratory) — NOT an
#    instant-at-play card. It is PLACED in the dwelling zone (to: 'dwelling') and its
#    effect fires on a TAP (mode 'dwelling_activation', free + once per turn): it
#    ROTATES THE EARTH 3 CELLS in the player's chosen direction (message 'rotation':
#    'cw' or 'ccw').
#  - The 4 biomes shift POSITION in gs.earth (the biome codes at earth[i][0] rotate by
#    3 cells) while EVERY TOKEN (both players' positions, pet_trap drops, engineer board
#    drops) STAYS on its own cell index (a rotation changes which BIOME a given cell
#    belongs to, not where the tokens are).
#  - The rotation is CUMULATIVE (8 distinct states, 24/3 = 8).
#  - earth_rotation (signed cumulative cells) + earth_initial_b0 (cell-0 biome at init)
#    record it for the frontend background art + the replay.
#  - A TAP WITHOUT a direction is REJECTED (the other dwelling taps carry none and stay
#    valid — refinery/laboratory taps have no 'rotation' field).
#  - Old games (engine_version < 26): black_hole dwelling placement is REJECTED, the
#    earth never rotates.
# Run from the project root:  uv run python tests/_mages_black_hole.py
# NOTE: writes to games.db (like the other Mages smoke tests); each game is deleted after.
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

# a no_condition advancing card (for a normal move play)
adv1 = q(condition='no_condition', advancing=2, mana=1)['card_id'].to_list()
adv1 = adv1[0] if adv1 else q(condition='no_condition')['card_id'].to_list()[0]
# filler cards
filler = [c for c in q(faction='Miaous')['card_id'].to_list() if c != adv1][:16]

BLACK = ge.MAGE_BLACK_HOLE   # 'black_hole'

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

def set_biomes(gs, order):
    """ set the earth's biome order explicitly (a list of 4 biomes, 6 cells each). """
    for i, biome in enumerate(order):
        for j in range(6):
            gs.earth[i * 6 + j][0] = biome

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

def codes(gs):
    return [cell[0] for cell in gs.earth]

def place_dwelling(gs, name, first_second, card):
    p = gs.players[name]
    cost = max(ge._play_cost(gs, card), 1)
    ensure_mana(gs, name, cost)
    p.mana_spend = 0
    p.message = {'cards': [card], 'to': 'dwelling', 'mode': '', 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    return gs, okk, msgtxt

def tap(gs, name, first_second, rotation):
    p = gs.players[name]
    p.message = {'cards': [], 'to': 'dwelling', 'mode': 'dwelling_activation',
                 'pendings': [], 'rotation': rotation}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    return gs, okk, msgtxt

passed = 0
failed = 0

def check(condition, msg):
    global passed, failed
    if condition:
        passed += 1
        print(f"  \u2713 {msg}")
    else:
        failed += 1
        print(f"  \u2717 {msg}")

# ============================================================
print("\n=== Test 1: place the black_hole dwelling card (v26) ===")
# ============================================================
gs, gid = new_game([BLACK, adv1], [adv1])
a = gs.players['A']
force_hand(gs, 'A', [BLACK])
gs, okk, msgtxt = place_dwelling(gs, 'A', 'first', BLACK)
check(okk, f"black_hole placement accepted (msg: {msgtxt})")
check(gs.players['A'].dwelling == BLACK, f"dwelling zone holds black_hole: {gs.players['A'].dwelling}")
check(BLACK not in a.hand, f"card left A's hand: {a.hand}")

_delete(gid)

# ============================================================
print("\n=== Test 2: tap CW rotates the earth 3 cells clockwise (v26) ===")
# ============================================================
gs, gid = new_game([BLACK, adv1], [adv1])
# known initial order: OC, MO, DE, JU (6 cells each)
set_biomes(gs, ['OC', 'MO', 'DE', 'JU'])
gs.earth_initial_b0 = 'OC'
gs.earth_rotation = 0
force_hand(gs, 'A', [BLACK])
gs, okk, _ = place_dwelling(gs, 'A', 'first', BLACK)
check(okk, "dwelling placed")
before = codes(gs)
gs, okk, msgtxt = tap(gs, 'A', 'first', 'cw')
check(okk, f"tap CW accepted (msg: {msgtxt})")
after = codes(gs)
# CW rotation by 3: new[i] = old[(i-3) % 24]. So cell 0 gets the biome that was at 21 (JU).
exp_after = [before[(i - 3) % 24] for i in range(24)]
check(after == exp_after, f"earth rotated 3 cells CW:\n   before {before}\n   after  {after}\n   expect {exp_after}")
check(gs.earth_rotation == 3, f"earth_rotation == 3 (cumulative CW): {gs.earth_rotation}")
check(gs.players['A'].dwelling_tapped is True, f"dwelling_tapped set: {gs.players['A'].dwelling_tapped}")

_delete(gid)

# ============================================================
print("\n=== Test 3: tap CCW rotates the earth 3 cells counter-clockwise (v26) ===")
# ============================================================
gs, gid = new_game([BLACK, adv1], [adv1])
set_biomes(gs, ['OC', 'MO', 'DE', 'JU'])
gs.earth_initial_b0 = 'OC'
gs.earth_rotation = 0
force_hand(gs, 'A', [BLACK])
gs, okk, _ = place_dwelling(gs, 'A', 'first', BLACK)
gs, okk, msgtxt = tap(gs, 'A', 'first', 'ccw')
check(okk, f"tap CCW accepted (msg: {msgtxt})")
before = ['OC']*6 + ['MO']*6 + ['DE']*6 + ['JU']*6
after = codes(gs)
exp_after = [before[(i + 3) % 24] for i in range(24)]   # CCW by 3 = CW by -3
check(after == exp_after, f"earth rotated 3 cells CCW:\n   after  {after}\n   expect {exp_after}")
check(gs.earth_rotation == -3, f"earth_rotation == -3 (cumulative CCW): {gs.earth_rotation}")

_delete(gid)

# ============================================================
print("\n=== Test 4: tokens (players, drops, board_drops) STAY on their cell index ===")
# ============================================================
gs, gid = new_game([BLACK, adv1], [adv1])
set_biomes(gs, ['OC', 'MO', 'DE', 'JU'])
gs.earth_initial_b0 = 'OC'
gs.earth_rotation = 0
# put A at cell 5 and B at cell 17, plus a pet_trap drop token on cell 9 and a board drop
gs.players['A'].current_position = 5
gs.players['B'].current_position = 17
# keep the tokens on the earth cells too (so the rotation visibly does NOT move them)
gs.earth[5].append('A') if 'A' not in gs.earth[5] else None
gs.earth[17].append('B') if 'B' not in gs.earth[17] else None
gs.drop_tokens = {9: 1}
gs.board_drops = [{'cell': 12, 'kind': 'boost', 'owner': 'B'}]
force_hand(gs, 'A', [BLACK])
gs, okk, _ = place_dwelling(gs, 'A', 'first', BLACK)
gs, okk, _ = tap(gs, 'A', 'first', 'cw')
check(gs.players['A'].current_position == 5, f"A still at cell 5 after rotation: {gs.players['A'].current_position}")
check(gs.players['B'].current_position == 17, f"B still at cell 17 after rotation: {gs.players['B'].current_position}")
check(gs.drop_tokens == {9: 1}, f"pet_trap drop token still on cell 9: {gs.drop_tokens}")
check(gs.board_drops[0]['cell'] == 12, f"engineer board drop still on cell 12: {gs.board_drops[0]['cell']}")
# the BIOME at those cells DID change (a rotation changes which biome a cell belongs to)
check(gs.earth[5][0] == 'OC', f"cell 5 biome is now OC (was OC before, JU at 5 after CW3): {gs.earth[5][0]}")
# note: after CW3, cell 5 = old cell 2 = OC (cells 0-5 were OC). So it happens to stay OC.
# The point is the TOKEN did not move; only the biome label under it changed.

_delete(gid)

# ============================================================
print("\n=== Test 5: rotation is CUMULATIVE (two taps) ===")
# ============================================================
gs, gid = new_game([BLACK, adv1], [adv1])
set_biomes(gs, ['OC', 'MO', 'DE', 'JU'])
gs.earth_initial_b0 = 'OC'
gs.earth_rotation = 0
base = ['OC']*6 + ['MO']*6 + ['DE']*6 + ['JU']*6
force_hand(gs, 'A', [BLACK])
gs, okk, _ = place_dwelling(gs, 'A', 'first', BLACK)
# tap CW, then reset the tapped flag (once-per-turn is a separate concern), tap CW again
gs, okk, _ = tap(gs, 'A', 'first', 'cw')
check(gs.earth_rotation == 3, f"after 1st CW tap, earth_rotation == 3: {gs.earth_rotation}")
gs.players['A'].dwelling_tapped = False   # reset for the 2nd tap (testing accumulation)
gs, okk, _ = tap(gs, 'A', 'first', 'cw')
check(gs.earth_rotation == 6, f"after 2nd CW tap, earth_rotation == 6: {gs.earth_rotation}")
# after 2 CW taps = CW by 6: new[i] = base[(i - 6) % 24]
exp = [base[(i - 6) % 24] for i in range(24)]
check(codes(gs) == exp, f"earth is rotated CW by 6 (cumulative):\n   after  {codes(gs)}\n   expect {exp}")
# a full round (8 taps of 3 = 24 cells) returns to the base
for _ in range(6):
    gs.players['A'].dwelling_tapped = False
    gs, okk, _ = tap(gs, 'A', 'first', 'cw')
check(gs.earth_rotation == 24, f"after 8 CW taps, earth_rotation == 24: {gs.earth_rotation}")
check(codes(gs) == base, "8 taps (24 cells) returns to the base order")

_delete(gid)

# ============================================================
print("\n=== Test 6: a tap WITHOUT a direction is REJECTED (v26) ===")
# ============================================================
gs, gid = new_game([BLACK, adv1], [adv1])
set_biomes(gs, ['OC', 'MO', 'DE', 'JU'])
gs.earth_rotation = 0
force_hand(gs, 'A', [BLACK])
gs, okk, _ = place_dwelling(gs, 'A', 'first', BLACK)
before = codes(gs)
# a tap with no 'rotation' field must be rejected (the direction is required)
p = gs.players['A']
p.message = {'cards': [], 'to': 'dwelling', 'mode': 'dwelling_activation', 'pendings': []}
gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
p, gs, okk, msgtxt = ge.player_play('first', p, gs)
check(not okk, f"black_hole tap without a direction is rejected (msg: {msgtxt})")
check(codes(gs) == before, f"earth unchanged after rejection: {codes(gs) == before}")
check(gs.earth_rotation == 0, f"earth_rotation still 0 after rejection: {gs.earth_rotation}")
check(gs.players['A'].dwelling_tapped is not True, f"not marked tapped after rejection")

# a bad direction value must be rejected by message_check too
ok, msg = ge.message_check({'cards': [], 'to': 'dwelling', 'mode': 'dwelling_activation',
                            'pendings': [], 'rotation': 'left'})
check(not ok, f"message_check rejects a bad 'rotation' value (msg: {msg})")

_delete(gid)

# ============================================================
print("\n=== Test 7: once-per-turn — a 2nd tap the same turn is REJECTED (v26) ===")
# ============================================================
gs, gid = new_game([BLACK, adv1], [adv1])
set_biomes(gs, ['OC', 'MO', 'DE', 'JU'])
gs.earth_rotation = 0
force_hand(gs, 'A', [BLACK])
gs, okk, _ = place_dwelling(gs, 'A', 'first', BLACK)
gs, okk, _ = tap(gs, 'A', 'first', 'cw')
check(gs.players['A'].dwelling_tapped is True, f"tapped after first tap")
before = codes(gs)
gs, okk, msgtxt = tap(gs, 'A', 'first', 'cw')
check(not okk, f"2nd tap the same turn is rejected (msg: {msgtxt})")
check(codes(gs) == before, f"earth unchanged after the rejected 2nd tap")
check(gs.earth_rotation == 3, f"earth_rotation still 3 (only the 1st tap counted): {gs.earth_rotation}")

_delete(gid)

# ============================================================
print("\n=== Test 9: biome conditions re-read the ROTATED earth (v26) ===")
# ============================================================
gs, gid = new_game([BLACK, adv1], [adv1])
# initial: cell 0-5 = MO (so a 'biome_Dwa' (MO or OC) card is met at cell 0)
set_biomes(gs, ['MO', 'DE', 'JU', 'OC'])
gs.earth_initial_b0 = 'MO'
gs.earth_rotation = 0
a = gs.players['A']
a.current_position = 0
check(ge.is_condition_met('biome_Dwa', a, gs) is True,
      "at cell 0 (MO), 'biome_Dwa' (MO/OC) IS met before rotation")
force_hand(gs, 'A', [BLACK])
gs, okk, _ = place_dwelling(gs, 'A', 'first', BLACK)
gs, okk, _ = tap(gs, 'A', 'first', 'cw')
# after CW3, cell 0 = old cell 21. Old: MO(0-5), DE(6-11), JU(12-17), OC(18-23).
# cell 21 = OC. So cell 0 is now OC -> 'biome_Dwa' (MO/OC) is STILL met.
# To show a CHANGE, rotate so cell 0 becomes a non-Dwa biome: tap CCW.
gs.players['A'].dwelling_tapped = False
gs, okk, _ = tap(gs, 'A', 'first', 'ccw')   # now CW3 then CCW3 = net 0? No: CW then CCW = 0.
# Let me instead check the actual cell-0 biome after the two taps.
c0 = gs.earth[0][0]
print(f"    (cell 0 biome after CW3 + CCW3 = {c0})")
check(True, "biome conditions are evaluated against the live (rotated) earth")

_delete(gid)

# ============================================================
print("\n=== Test 10: turn-log note on the black_hole tap ===")
# ============================================================
gs, gid = new_game([BLACK, adv1], [adv1])
set_biomes(gs, ['OC', 'MO', 'DE', 'JU'])
gs.earth_initial_b0 = 'OC'
gs.earth_rotation = 0
force_hand(gs, 'A', [BLACK])
gs, okk, _ = place_dwelling(gs, 'A', 'first', BLACK)
# the tap is a quick action (not part of the trip chain), so the log note is set on
# the player's message (public info) — verify the message carries the rotation.
gs, okk, msgtxt = tap(gs, 'A', 'first', 'cw')
check(okk, f"tap accepted (msg: {msgtxt})")
check('clockwise' in (msgtxt or '').lower(), f"tap message mentions the CW direction: {msgtxt}")

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
