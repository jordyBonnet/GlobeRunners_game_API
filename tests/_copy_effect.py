# Manual smoke test: copy_effect (rule of engine_version 7)
# Run from the project root:  uv run python tests/_copy_effect.py
# NOTE: writes to games.db (like _diag.py)
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

def cid(df):
    return df['card_id'].to_list()

# card picks (real pool cards)
copy_nc = cid(DB.filter((DB['effect'] == 'copy_effect') & (DB['condition'] == 'no_condition')))
copy_notmet = cid(DB.filter((DB['effect'] == 'copy_effect') & (DB['condition'] == 'dist_ahead_sup_3')))
draw_nc = cid(DB.filter((DB['effect'] == 'draw') & (DB['condition'] == 'no_condition')))
draw_notmet = cid(DB.filter((DB['effect'] == 'draw') & (DB['condition'] == 'dist_ahead_sup_3')))
defend_high = cid(DB.filter((DB['shield'] == 6) & (DB['condition'] != 'block')))
assert copy_nc and copy_notmet and draw_nc and draw_notmet and defend_high, 'need real pool cards'

row = lambda c: DB.filter(pl.col('card_id') == c).row(0, named=True)
filler = [c for c in DB['card_id'].to_list() if c not in set(copy_nc + copy_notmet + draw_nc + draw_notmet + defend_high)][:13]

def new_game(ca, cb):
    p1 = PlayerState(name='A', deck=[ca] + filler)
    p2 = PlayerState(name='B', deck=[cb] + filler)
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    return gs

def run(gs, a_cards, b_cards):
    a, b = gs.players['A'], gs.players['B']
    a.action_chain = [{'cards': [c], 'to': 'stopover_4', 'mode': 'move', 'pendings': []} for c in a_cards]
    b.action_chain = [{'cards': [c], 'to': 'stopover_4', 'mode': 'move', 'pendings': []} for c in b_cards]
    h_a, h_b = len(a.hand), len(b.hand)
    gs2 = ge.process_trip_chain(gs)
    return gs2, len(a.hand) - h_a, len(b.hand) - h_b

draw_n = abs(int(row(draw_nc[0])['effect_number']))   # cards the facing 'draw' effect moves

ok = 0

# 1) BOTH effects fired -> the copier (A) draws the facing effect_number cards
gs = new_game(copy_nc[0], draw_nc[0])
_, da, db = run(gs, [copy_nc[0]], [draw_nc[0]])
print(f"1) copy fires: A {da:+d} (expected {1 - 1 + 0 + draw_n:+d})  B {db:+d} (expected {1 - 1 + 0 + draw_n:+d})")
assert da == draw_n - 0 and db == draw_n, (da, db)   # A: -? no: A plays 1 (removed BEFORE trip chain by player_play)... here hand delta: A drew draw_n (played card was NOT removed from hand in this harness!)
ok += 1

# note: in this harness the played cards stay in the hand (player_play does the removal),
# so the delta is exactly the copied/effect draws.

# 2) facing condition NOT met -> facing effect never fired -> no copy
gs = new_game(copy_nc[0], draw_notmet[0])
_, da, db = run(gs, [copy_nc[0]], [draw_notmet[0]])
print(f"2) facing not met: A {da:+d} (expected 0)  B {db:+d} (expected 0)")
assert da == 0 and db == 0, (da, db)
ok += 1

# 3) copier condition NOT met -> copier's effect never fired -> no copy (B still draws for itself)
gs = new_game(copy_notmet[0], draw_nc[0])
_, da, db = run(gs, [copy_notmet[0]], [draw_nc[0]])
print(f"3) copier not met: A {da:+d} (expected 0)  B {db:+d} (expected +{draw_n})")
assert da == 0 and db == draw_n, (da, db)
ok += 1

# 4) NO RECURSION: both copy_effect -> nothing copied
gs = new_game(copy_nc[0], copy_nc[0])
_, da, db = run(gs, [copy_nc[0]], [copy_nc[0]])
print(f"4) mutual copy (no recursion): A {da:+d} (expected 0)  B {db:+d} (expected 0)")
assert da == 0 and db == 0, (da, db)
ok += 1

# 6) facing card BLOCKED -> its effect never fired -> no copy
gs = new_game(copy_nc[0], draw_nc[0])
a, b = gs.players['A'], gs.players['B']
a.action_chain = [
    {'cards': [copy_nc[0]], 'to': 'stopover_4', 'mode': 'move', 'pendings': []},
    {'cards': [defend_high[0]], 'to': 'stopover_4', 'mode': 'defend', 'pendings': []},
]
b.action_chain = [{'cards': [draw_nc[0]], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}]
h_a, h_b = len(a.hand), len(b.hand)
ge.process_trip_chain(gs)
da, db = len(a.hand) - h_a, len(b.hand) - h_b
print(f"6) facing blocked: A {da:+d} (expected 0)  B {db:+d} (expected 0)")
assert da == 0 and db == 0, (da, db)
ok += 1

# 7) log entry carries the copy note on the copier's line
gs = new_game(copy_nc[0], draw_nc[0])
a, b = gs.players['A'], gs.players['B']
entry = ge.new_log_entry(a, {'cards': [copy_nc[0]], 'to': 'stopover_4', 'mode': 'move'}, 1)
b_entry = ge.new_log_entry(b, {'cards': [draw_nc[0]], 'to': 'stopover_4', 'mode': 'move'}, 2)
a.action_chain = [{'cards': [copy_nc[0]], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}]
b.action_chain = [{'cards': [draw_nc[0]], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}]
# call process_card for both, then the copy (mirrors the trip chain with log entries)
gs, g1, e1 = ge.process_card(a.action_chain[0], a, gs, log_entry=entry, oppo_entry=b_entry)
gs, g2, e2 = ge.process_card(b.action_chain[0], b, gs, log_entry=b_entry, oppo_entry=entry)
gs = ge.apply_copy_effect(gs, a, a.action_chain[0], b, b.action_chain[0], entry)
print(f"7) log note: {entry['notes']}")
assert any('copy_effect' in n and 'copied' in n for n in entry['notes']), entry['notes']
assert entry['notes'] and b_entry['notes'].count('copy_effect') == 0
ok += 1

print(f"\n{ok}/7 copy_effect tests passed")
