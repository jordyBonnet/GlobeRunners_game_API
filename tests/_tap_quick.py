# quick check: dwelling TAP is a QUICK action — the floor stays with the tapper,
# who can still play a card or pass afterwards (2026-09). Deletes its own game.
# Run from the project root:  uv run python tests/_tap_quick.py
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

adv1 = q(effect='advancing', condition='no_condition', mana=1)['card_id'].to_list()[0]
filler = [c for c in q(faction='Miaous')['card_id'].to_list() if c != adv1][:14]

p1 = PlayerState(name='A', deck=['refinery'] * 10 + filler)
p2 = PlayerState(name='B', deck=[adv1] * 10 + filler)
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
conn2, _, gs = ge.get_current_game(gid)
conn2.close()

gs.state = f"turn {gs.turn} - waiting for first player (A) to play"   # play phase
a = gs.players['A']
a.dwelling = 'refinery'          # pretend the refinery was already placed
a.dwelling_tapped = False

# --- TAP (quick action): floor must STAY with A ---
before = gs.state
a.message = {'cards': [], 'to': 'dwelling', 'mode': 'dwelling_activation', 'pendings': []}
p, gs, okk, txt = ge.player_play('first', a, gs)
assert okk, f"tap rejected: {txt}"
assert a.dwelling_tapped is True
print("state after tap:", gs.state)
assert gs.state == before, f"FLOOR LOST after tap: {gs.state}"

# --- after the tap, A can STILL play a card ---
a.mana += ['m'] * 5   # top up (test helper)
if filler[0] not in a.hand: a.hand.append(filler[0])
a.message = {'cards': [filler[0]], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}
a.mana_spend = 0
p, gs, okk, txt = ge.player_play('first', a, gs)
assert okk, f"post-tap play rejected: {txt}"
assert any(ac.get('cards') == [filler[0]] for ac in a.action_chain), "post-tap card not in action_chain"
print("state after post-tap play:", gs.state)

# --- PLACE still alternates (normal play) ---
gs.state = f"turn {gs.turn} - waiting for second player (B) to play"
b = gs.players['B']
b.dwelling = None
b.message = {'cards': ['refinery'], 'to': 'dwelling', 'mode': '', 'pendings': []}
b.hand = ['refinery'] + b.hand
b.mana_spend = 0
b.mana += ['x'] * 5
p, gs, okk, txt = ge.player_play('second', b, gs)
assert okk, f"place rejected: {txt}"
assert b.dwelling == 'refinery'
print("state after place:", gs.state)
assert 'waiting for first player (A)' in gs.state, f"place should alternate: {gs.state}"

# cleanup
conn = sqlite3.connect(ge.DB_PATH)
conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
conn.commit(); conn.close()
print("\nOK — TAP = quick action (floor kept, can still play/pass); PLACE still alternates")
