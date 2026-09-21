# Manual smoke test: nobodymoves + cataclysm interaction (engine_version 27)
#
#  - nobodymoves (v23) locks ALL PLAYERS' MOVEMENT for the rest of the turn.
#  - The CATACLYSM TRIGGER (condition 'cataclysm') knocks back every token on the
#    struck biome — that knockback is a MOVEMENT, so since engine_version 27 it is
#    SUPPRESSED while nobodymoves is active: the trigger still FIRES (the pile
#    rotates and the biome is announced) but NO TOKEN MOVES.
#  - Games 23-26 keep the OLD behavior (the knockback fires even during a
#    nobodymoves turn) — the observed case: game 26_09_19_17_30_27_KEVVF turn 6.
#
# Run from the project root:  uv run python tests/_mages_nobodymoves_cataclysm.py
# NOTE: writes to games.db (deletes its own games)
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
NOBODY = ge.MAGE_NOBODYMOVES          # 'nobodymoves' (Mages support card)
CAT = 'Dwa23_cedf98'                  # cataclysm-condition card (Karga Norik, adv 2, mana 2)

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()

def new_game():
    filler = [c for c in DB.filter(pl.col('faction') == 'Miaous')['card_id'].to_list()
              if c != CAT][:18]
    a_deck = [NOBODY] + filler
    b_deck = [CAT] + [c for c in filler if c not in (CAT,)]
    p1 = ge.PlayerState(name='A', deck=list(a_deck))
    p2 = ge.PlayerState(name='B', deck=list(b_deck))
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    # make the whole board MO so both tokens (placed on MO cells) are on the struck biome
    for cell in gs.earth:
        cell[0] = 'MO'
    # top of the cataclysm pile = MO (so the trigger strikes MO)
    gs.cataclysm_pile = ['MO', 'OC', 'DE', 'JU']
    # place both players on MO cells (A at 5, B at 3) — the knockback would send them to cell 0
    a = gs.players['A']; b = gs.players['B']
    for p, pos in ((a, 5), (b, 3)):
        for cell in gs.earth:
            if p.name in cell:
                cell.remove(p.name)
        p.current_position = pos
        gs.earth[pos].append(p.name)
    return gs, gid

def ensure_mana(gs, name, n):
    p = gs.players[name]
    while len(p.mana) - (p.mana_spend or 0) < n and p.deck:
        p.mana.append(p.deck.pop(0))

def force_hand(gs, name, cards):
    p = gs.players[name]
    for c in cards:
        if c not in p.hand:
            if c in p.deck:
                p.deck.remove(c)
            p.hand.append(c)

def play(gs, name, first_second, card, to):
    p = gs.players[name]
    ensure_mana(gs, name, 3)
    p.mana_spend = 0
    p.message = {'cards': [card], 'to': to, 'mode': 'move', 'pendings': []}
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

def notes_of(gs):
    out = []
    for turn in gs.log:
        for sv in turn.get('stopovers', []):
            for e in sv.get('entries', []):
                out.extend(e.get('notes', []))
    return out

passed = 0
failed = 0
def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1; print(f"  \u2713 {msg}")
    else:
        failed += 1; print(f"  \u2717 {msg}")

# ============================================================
print("\n=== Test 1 (v27): cataclysm knockback SUPPRESSED during nobodymoves; pile still rotates ===")
# ============================================================
gs, gid = new_game()
a = gs.players['A']; b = gs.players['B']
force_hand(gs, 'A', [NOBODY])
force_hand(gs, 'B', [CAT])
gs, okk, m = play(gs, 'A', 'first', NOBODY, 'stopover_4')
check(okk, f"play nobodymoves accepted (msg: {m})")
check(gs.nobodymoves_active is True, f"lock active: {gs.nobodymoves_active}")
gs, okk, m = play(gs, 'B', 'second', CAT, 'stopover_3')
check(okk, f"play cataclysm card accepted (msg: {m})")
gs = pas(gs, 'A', 'first')
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
check(a.current_position == 5, f"A (victim) NOT knocked back (stays on cell 5): {a.current_position}")
check(gs.cataclysm_pile == ['OC', 'DE', 'JU', 'MO'], f"pile ROTATED (MO -> bottom): {gs.cataclysm_pile}")
nt = notes_of(gs)
check(any('cataclysm' in n and 'strikes' in n for n in nt), f"log: biome announced ({[n for n in nt if 'cataclysm' in n]})")
check(any('nobodymoves' in n and 'knockback suppressed' in n for n in nt), f"log: knockback-suppressed note present")
_delete(gid)

# ============================================================
print("\n=== Test 2 (v27 control): WITHOUT nobodymoves the cataclysm knockback FIRES ===")
# ============================================================
gs, gid = new_game()
a = gs.players['A']; b = gs.players['B']
force_hand(gs, 'B', [CAT])
gs, okk, m = play(gs, 'B', 'second', CAT, 'stopover_4')
check(okk, f"play cataclysm card accepted (msg: {m})")
check(gs.nobodymoves_active is False, f"no lock (nobodymoves not played): {gs.nobodymoves_active}")
gs = pas(gs, 'A', 'first')
gs = pas(gs, 'B', 'second')
gs = ge.process_trip_chain(gs)
check(a.current_position == 0, f"A (victim) knocked back 5 -> 0 (first MO cell): {a.current_position}")
check(gs.cataclysm_pile == ['OC', 'DE', 'JU', 'MO'], f"pile rotated: {gs.cataclysm_pile}")
_delete(gid)

print(f"\n{'='*50}")
print(f"RESULTS: {passed} passed, {failed} failed")
print(f"{'='*50}")
if failed == 0:
    print("All tests passed!")
else:
    print(f"{failed} test(s) failed!")
    sys.exit(1)
