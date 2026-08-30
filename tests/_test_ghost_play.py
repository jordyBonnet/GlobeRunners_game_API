"""Test: a play rejected for insufficient mana must NOT be recorded in
messages_history (it would otherwise show up as a real play in the analysis app).

In-process engine test (no HTTP/WS)."""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.chdir(os.path.join(os.path.dirname(__file__), '..'))

from models import PlayerState
import engine.game_engine as ge
import polars as pl

pool = ge.get_cardpool()
costs = dict(zip(pool['card_id'], pool['mana']))
exp = pool.filter(pl.col('faction').str.starts_with('Dwa') & (pl.col('mana') == 5))['card_id'].to_list()[:6]
cheap = pool.filter(pl.col('faction').str.starts_with('Dwa') & (pl.col('mana') <= 2))['card_id'].to_list()[:9]
orc = pool.filter(pl.col('faction').str.starts_with('Orc') & (pl.col('mana') <= 3))['card_id'].to_list()[:15]
deck1 = exp + cheap   # G1: 6 expensive (cost 5) + 9 cheap


def view(gid, name):
    conn, _, g = ge.get_current_game(gid)
    v = json.loads(ge.current_game_json(name, g))
    conn.close()
    return v


def act(gid, name, cards, to, mode):
    conn, _, g = ge.get_current_game(gid)
    plr = g.players[name].model_copy()
    plr.message = {'cards': cards, 'to': to, 'mode': mode, 'pendings': []}
    conn.close()
    out = json.loads(ge.handle_websocket_message(gid, plr))
    m = out['message']
    return m['success'], m['message']


gid = None
for trial in range(50):
    p1 = PlayerState(name='G1', deck=list(deck1))
    gid = ge.create_new_game(p1.model_dump())
    p2 = PlayerState(name='G2', deck=list(orc))
    ge.p2_connect_to_game(p2.model_dump(), gid)
    v1, v2 = view(gid, 'G1'), view(gid, 'G2')
    first = v1['turn_order'][0]
    other = 'G2' if first == 'G1' else 'G1'
    if first != 'G2':
        continue  # need G1 to be the second player (so it plays after G2)
    h_g1 = v1['players']['G1']['hand']
    h_g2 = v2['players']['G2']['hand']
    exp_in_g1 = [c for c in h_g1 if costs[c] > 3]
    if not exp_in_g1:
        continue  # need an unaffordable card in G1's hand
    break
assert gid, 'could not build a suitable game in 50 trials'

h_g1 = v1['players']['G1']['hand']
h_g2 = v2['players']['G2']['hand']
target = exp_in_g1[0]
print(f'trial setup OK: G2 first, G1 second, G1 holds {target} (cost {costs[target]})')

# --- initial mana phase: 3 cards each ---
for cid in h_g2[:3]:
    s, m = act(gid, 'G2', [cid], 'mana', ''); assert s, m
for cid in h_g1[:3]:
    s, m = act(gid, 'G1', [cid], 'mana', ''); assert s, m
v = view(gid, 'G1')
assert 'turn 1' in v['state'], v['state']
print('state:', v['state'])

# --- play phase ---
cheap_g2 = next(c for c in h_g2 if costs[c] <= 3 and c not in h_g2[:3])
s, m = act(gid, 'G2', [cheap_g2], 'stopover_4', 'move')
print(f'G2 cheap play      -> {s} | {m[:60]}')
assert s is True

# G1 tries the expensive card: cost 5 > 3 available mana -> MUST be rejected
s, m = act(gid, 'G1', [target], 'stopover_4', 'move')
print(f'G1 EXPENSIVE play  -> {s} | {m[:80]}')
assert s is False, f'expected rejection but got success! ({m})'
assert 'not enough mana' in m.lower(), m

# G1 then plays a cheap card -> accepted
cheap_g1 = next(c for c in h_g1 if costs[c] <= 3 and c not in h_g1[:3])
s, m = act(gid, 'G1', [cheap_g1], 'stopover_4', 'move')
print(f'G1 cheap play      -> {s} | {m[:60]}')
assert s is True

# --- history assertions ---
v = view(gid, 'G1')
h = v['players']['G1']['messages_history']
plays = [m for m in h if m['to'] != 'mana']
print()
print('G1 history:')
for m in h:
    print('  ', m['cards'], '->', m['to'], m['mode'])
assert target not in [m['cards'][0] for m in h if m['to'] != 'mana'], \
    f'BUG: rejected play {target} was recorded in history!'
assert cheap_g1 in [m['cards'][0] for m in h if m['to'] != 'mana'], 'accepted play missing from history!'
print()
print('OK: rejected play (insufficient mana) is NOT recorded; accepted plays ARE')
