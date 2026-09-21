# Manual smoke test (engine + WS + replay): wrecking_ball and the dwelling PLACEHOLDER
# (engine_version 28 — the designer rule).
#
# The bug (game 26_09_20_18_54_55_plQOZ, turn 2): the Robot's wrecking_ball removed
# the black_hole dwelling, and the engine cleared `dwelling_slot` along with `dwelling`
# — the placeholder VANISHED while the consumed trip-chain position stayed consumed
# (`play_count` untouched). The slot was left empty (a "hole" in the stopovers) and
# the UI's next-slot hint disagreed with the engine.
#
# Designer decision (engine_version 28): the placeholder must STAY IN PLACE — only
# the dwelling CARD goes to the discard. The placeholder keeps occupying the consumed
# trip-chain position (play_count untouched, in all versions) and visually fills the
# slot until the cleaning phase clears it (like every placeholder).
#
#   * v28: wreck -> dwelling=None, dwelling_slot UNCHANGED (placeholder stays)
#   * v27 and below (PIN): wreck -> dwelling=None, dwelling_slot CLEARED (old behavior)
#   * in both: the owner's next play lands on the position AFTER the placeholder (slot 3)
#
# Run from the project root:  uv run python tests/_wrecking_placeholder.py
# NOTE: writes a few games to games.db (like the other smoke tests); all are deleted after.
import sys, io, os, sqlite3, json as _json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
# plain-ish cards (effect 'advancing' is a safe movement-only effect; no_condition so they always resolve)
ADV2 = DB.filter(pl.col('advancing').eq(2) & pl.col('effect').eq('advancing') & pl.col('condition').eq('no_condition') & pl.col('mana').eq(2))['card_id'].to_list()[0]
ADV0 = DB.filter(pl.col('advancing').eq(0) & pl.col('effect').eq('advancing') & pl.col('condition').eq('no_condition') & pl.col('mana').eq(1))['card_id'].to_list()[0]
FILLER = DB.filter(pl.col('effect').eq('advancing') & pl.col('condition').eq('no_condition'))['card_id'].to_list()
FILLER = [c for c in FILLER if c not in (ADV2, ADV0)][:30]
WB = DB.filter(pl.col('effect').eq('wrecking_ball') & pl.col('condition').eq('no_condition') & pl.col('mana').eq(2))['card_id'].to_list()[0]
BLACK = ge.MAGE_BLACK_HOLE
REFINERY = 'refinery'

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

def new_game(a_ids, b_ids, version=28):
    """ create a game with deterministic hands/decks/mana and a given engine_version.
    layout: hand = ids[0:6], mana = ids[6:12], deck = ids[12:] """
    p1 = PlayerState(name='A', deck=list(a_ids))
    p2 = PlayerState(name='B', deck=list(b_ids))
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    ga, gb = gs.players['A'], gs.players['B']
    ga.hand = list(a_ids[0:6]); ga.deck = list(a_ids[12:]); ga.mana = list(a_ids[6:12])
    ga.discard = []; ga.dwelling = None; ga.dwelling_slot = None; ga.play_count = 0
    gb.hand = list(b_ids[0:6]); gb.deck = list(b_ids[12:]); gb.mana = list(b_ids[6:12])
    gb.discard = []; gb.dwelling = None; gb.dwelling_slot = None; gb.play_count = 0
    gs.turn_order = ['A', 'B']
    if version is not None:
        gs.engine_version = version
    conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    conn.commit(); conn.close()
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    return gs, gid

def _delete(gid):
    conn = sqlite3.connect(os.path.join(PROJECT_ROOT, 'games', 'games.db'))
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit(); conn.close()

def send(gid, name, message):
    """ drive the engine exactly like the WS layer / AI driver do (model_copy path) """
    conn, _, gs = ge.get_current_game(gid)
    player = gs.players[name].model_copy()
    player.message = message
    conn.close()
    ge.handle_websocket_message(gid, player)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    return gs

# A: BLACK + ADV2 in hand, 6 mana (black_hole costs 3, ADV2 costs 2). All ids unique.
A_DECK  = [BLACK, ADV2, ADV0, FILLER[0], FILLER[1], FILLER[2], FILLER[3], FILLER[4], FILLER[5], FILLER[6], FILLER[7], FILLER[8], FILLER[9]]
B_DECK  = [WB, ADV0, FILLER[10], FILLER[11], FILLER[12], FILLER[13], FILLER[14], FILLER[15], FILLER[16], FILLER[17], FILLER[18], FILLER[19], FILLER[20]]

def play_wreck(gid, gs):
    """ B (second player) plays the wrecking_ball move-card — returns the mutated game.
    NOTE: player_play does NOT persist to the DB (handle_websocket_message does) — so
    the tests chain the returned in-memory game object (no DB re-read between actions). """
    gs.players['B'].message = {'cards': [WB], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}
    p, gs, r, msg = ge.player_play('second', gs.players['B'], gs)
    assert r, f"B wrecking_ball rejected: {msg}"
    return gs

# ============================================================
print("=== Test 1: v28 — wreck AFTER placement -> the placeholder STAYS, the card goes to discard ===")
# ============================================================
gs, gid = new_game(A_DECK, B_DECK, version=28)
gs.players['A'].message = {'cards': [BLACK], 'to': 'dwelling', 'mode': '', 'pendings': []}
p, gs, r, msg = ge.player_play('first', gs.players['A'], gs)
check(r, f"A places the black_hole dwelling: {msg}")
check(gs.players['A'].dwelling == BLACK and gs.players['A'].dwelling_slot == 4,
      f"dwelling placed on stopover_4 (position 1) [dwelling={gs.players['A'].dwelling}, slot={gs.players['A'].dwelling_slot}]")

gs = play_wreck(gid, gs)
a = gs.players['A']
check(a.dwelling is None, "wreck: A's dwelling card removed")
check(a.dwelling_slot == 4, f"v28: the placeholder STAYS in place [dwelling_slot={a.dwelling_slot}]")
check((a.play_count or 0) == 1, f"v28: the consumed position stays consumed (play_count=1) [play_count={a.play_count}]")
check(BLACK in (a.discard or []), f"conservation: black_hole in the OWNER's (A's) discard [A discard={a.discard}]")

# A's next play must land on position 2 (stopover_3) — AFTER the placeholder
gs.players['A'].message = {'cards': [ADV2], 'to': 'stopover_3', 'mode': 'move', 'pendings': []}
p, gs, r, msg = ge.player_play('first', gs.players['A'], gs)
check(r, f"A plays a card after the wreck: {msg}")
last_a = (gs.players['A'].action_chain or [{}])[-1]
check(last_a.get('to') == 'stopover_3', f"A's next play lands on stopover_3 (position 2 — after the placeholder) [got {last_a.get('to')}]")
check(gs.players['A'].dwelling_slot == 4, "the placeholder is still in place after A's play [slot=%s]" % gs.players['A'].dwelling_slot)
_delete(gid)

# ============================================================
print("=== Test 2: v27 pinning — the OLD behavior (placeholder VANISHES with the card) ===")
# ============================================================
gs, gid = new_game(A_DECK, B_DECK, version=27)
gs.players['A'].message = {'cards': [BLACK], 'to': 'dwelling', 'mode': '', 'pendings': []}
p, gs, r, msg = ge.player_play('first', gs.players['A'], gs)
check(gs.players['A'].dwelling == BLACK, "v27: A places the black_hole")
gs = play_wreck(gid, gs)
check(gs.players['A'].dwelling is None, "v27: the wreck removes the card")
check(gs.players['A'].dwelling_slot is None, f"v27 pin: the placeholder VANISHES with the card [slot={gs.players['A'].dwelling_slot}]")
gs.players['A'].message = {'cards': [ADV2], 'to': 'stopover_3', 'mode': 'move', 'pendings': []}
p, gs, r, msg = ge.player_play('first', gs.players['A'], gs)
last_a = (gs.players['A'].action_chain or [{}])[-1]
check(last_a.get('to') == 'stopover_3', f"v27: the next play also lands on stopover_3 (same placement as v28 — only the DISPLAY differs) [got {last_a.get('to')}]")
_delete(gid)

# ============================================================
print("=== Test 3: v28 — re-place a dwelling after the wreck -> the new placeholder takes the NEXT position ===")
# ============================================================
A_DECK3 = [BLACK, REFINERY, ADV2, FILLER[21], FILLER[22], FILLER[23], FILLER[24], FILLER[25], FILLER[26], FILLER[27], FILLER[28], FILLER[29], FILLER[0]]
gs, gid = new_game(A_DECK3, B_DECK, version=28)
gs.players['A'].message = {'cards': [BLACK], 'to': 'dwelling', 'mode': '', 'pendings': []}
p, gs, r, msg = ge.player_play('first', gs.players['A'], gs)
check(gs.players['A'].dwelling == BLACK and gs.players['A'].dwelling_slot == 4, "A places the black_hole (position 1)")
gs = play_wreck(gid, gs)
check(gs.players['A'].dwelling is None and gs.players['A'].dwelling_slot == 4, "wreck: card gone, placeholder stays (ghost slot 4)")
gs.players['A'].message = {'cards': [REFINERY], 'to': 'dwelling', 'mode': '', 'pendings': []}
p, gs, r, msg = ge.player_play('first', gs.players['A'], gs)
check(r, f"A re-places a dwelling (the refinery): {msg}")
check(gs.players['A'].dwelling == REFINERY, f"refinery placed [dwelling={gs.players['A'].dwelling}]")
check(gs.players['A'].dwelling_slot == 3, f"the NEW placeholder takes the NEXT position (stopover_3 — the ghost slot 4 stays consumed) [slot={gs.players['A'].dwelling_slot}]")
_delete(gid)

# ============================================================
print("=== Test 4: v28 — a pending card placed after the wreck lands AFTER the ghost placeholder ===")
# ============================================================
A_DECK4 = [BLACK, 'epo', ADV2, FILLER[21], FILLER[22], FILLER[23], FILLER[24], FILLER[25], FILLER[26], FILLER[27], FILLER[28], FILLER[29], FILLER[1]]
gs, gid = new_game(A_DECK4, B_DECK, version=28)
gs.players['A'].message = {'cards': [BLACK], 'to': 'dwelling', 'mode': '', 'pendings': []}
p, gs, r, msg = ge.player_play('first', gs.players['A'], gs)
gs = play_wreck(gid, gs)
check(gs.players['A'].dwelling is None and gs.players['A'].dwelling_slot == 4, "wreck: card gone, placeholder stays (slot 4)")
gs.players['A'].message = {'cards': ['epo'], 'to': 'pending_zone', 'mode': '', 'pendings': []}
p, gs, r, msg = ge.player_play('first', gs.players['A'], gs)
check(r, f"A places the 'epo' pending card: {msg}")
a = gs.players['A']
check('epo' in (a.pendings or []), "epo is in the pending zone")
epo_slot = None
for e in (a.pending_slots or []):
    col = e[1] if isinstance(e, (list, tuple)) and len(e) == 2 else e
    if col is not None:
        epo_slot = col
check(epo_slot == 3, f"the 'epo' placeholder lands on position 2 (stopover_3 — after the ghost placeholder) [got {epo_slot}]")
gs.players['A'].message = {'cards': [ADV2], 'to': 'stopover_2', 'mode': 'move', 'pendings': []}
p, gs, r, msg = ge.player_play('first', gs.players['A'], gs)
last_a = (a.action_chain or [{}])[-1]
check(last_a.get('to') == 'stopover_2', f"A's play lands on position 3 (stopover_2) [got {last_a.get('to')}]")
check(a.dwelling_slot == 4, "the ghost placeholder is still in place (slot 4)")
_delete(gid)

# ============================================================
print("=== Test 5 (E2E): v28 through the REAL handle_websocket_message entry point (model_copy path) ===")
# (exactly like API.py / ai_driver.py drive the engine — the placeholder state must
#  persist through the copy path into the saved DB state)
# ============================================================
def setup_v28_game():
    """ create a v28 game with deterministic zones, persisted to the DB, state = play phase """
    gs, gid = new_game(A_DECK, B_DECK, version=28)
    conn, _, gs = ge.get_current_game(gid)
    gs.state = f"turn {gs.turn} - waiting for first player (A) to play"
    conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    conn.commit(); conn.close()
    return gid

gid = setup_v28_game()
gs = send(gid, 'A', {'cards': [BLACK], 'to': 'dwelling', 'mode': '', 'pendings': []})
check(gs.players['A'].dwelling == BLACK and gs.players['A'].dwelling_slot == 4, "e2e: A places the black_hole (WS path)")
gs = send(gid, 'B', {'cards': [WB], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
check(gs.players['A'].dwelling is None, "e2e: B plays the wrecking_ball (WS path)")
# read back from the DB (fresh read — the placeholder state must have persisted through the copy path)
conn, _, gs = ge.get_current_game(gid); conn.close()
check(gs.players['A'].dwelling_slot == 4, f"e2e: the placeholder persists in the DB [slot={gs.players['A'].dwelling_slot}]")
check((gs.players['A'].play_count or 0) == 1, f"e2e: play_count stays 1 (the position stays consumed) [play_count={gs.players['A'].play_count}]")
gs = send(gid, 'A', {'cards': [ADV2], 'to': 'stopover_3', 'mode': 'move', 'pendings': []})
last_a = (gs.players['A'].action_chain or [{}])[-1]
check(last_a.get('to') == 'stopover_3', f"e2e: A's next play lands on stopover_3 (after the ghost placeholder) [got {last_a.get('to')}]")
_delete(gid)

# ============================================================
print("=== Test 6 (replay): a full v28 wreck turn replays verified ===")
# (the wreck's instant effect is mirrored in the play loop; the placeholder stays in
#  the replay's local state — the trip chain must resolve identically)
# ============================================================
gid = setup_v28_game()
gs = send(gid, 'A', {'cards': [BLACK], 'to': 'dwelling', 'mode': '', 'pendings': []})
gs = send(gid, 'B', {'cards': [WB], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
gs = send(gid, 'A', {'cards': [ADV2], 'to': 'stopover_3', 'mode': 'move', 'pendings': []})
# state is now "waiting for second player (B)" -> B passes, then A -> both passed -> the trip chain resolves
gs = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
gs = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
conn, _, gs = ge.get_current_game(gid); conn.close()
check('waiting for both players to mana or pass' in gs.state or 'turn 2' in gs.state or gs.state == 'game over',
      f"replay: the turn resolved (state moved past the play phase) [state={gs.state}]")

import games.analysis.replay as R
res = R.analyze_game(_json.loads(gs.to_json()))
print(f"  replay: verified={res['verified']} turns={len(res['turns'])} warn={len(res['warnings'])}")
for w in res['warnings']:
    print(f"    ! {w}")
check(res['verified'], f"replay: verified clean (warnings={res['warnings']})")
check(not any('dwelling' in w for w in res['warnings']), f"replay: no dwelling divergence (warnings={res['warnings']})")
check(not any('unknown' in w for w in res['warnings']), f"replay: no unknown-card warnings (warnings={res['warnings']})")
_delete(gid)

# ============================================================
print(f"\n{'='*60}\nRESULT: {passed} passed, {failed} failed\n{'='*60}")
if failed:
    sys.exit(1)
