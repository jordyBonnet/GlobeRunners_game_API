# TEMP script: play a 2-turn WS-layer game where bloodtest fires (opponent
# discards), A wins on turn 2, then run the analysis replay on it.
# Expect: the replay completes with NO zone/identity warnings.
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

MAIN1 = 'Twi11_68cfac'   # no_condition m1, adv 0 + advancing 2
MAIN2 = 'Twi11_ac1b0e'   # no_condition m1, adv 1 + advancing 1 -> 2 cells

_db = ge.CARDS_DB
FILLER = [c for c in _db.filter((pl.col('faction') == 'Miaous') & (pl.col('mana') == 1))['card_id'].to_list()
          if c not in {MAIN1, MAIN2}]

# realistic split: 6 cards in hand / 24 in deck (like a real game)
A_HAND = [0, 1, 2]                       # + bloodtest + MAIN1 + MAIN2 = 6
A_DECK = list(range(3, 27))              # 24
B_HAND = list(range(30, 36))             # 6
B_DECK = list(range(36, 60))             # 24
def f(i): return FILLER[i]

def send(gid, name, message, verbose=True):
    conn, _, gs = ge.get_current_game(gid)
    player = gs.players[name].model_copy()
    player.message = message
    conn.close()
    resp = ge.handle_websocket_message(gid, player)
    if isinstance(resp, str):
        resp = json.loads(resp)
    info = (resp or {}).get('message') or {}
    ok = info.get('success') if isinstance(info, dict) else None
    if verbose and ok is False:
        print('REJECTED:', info.get('message'))
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    return gs, ok

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()

a_hand = ['bloodtest', MAIN1, MAIN2] + [f(i) for i in A_HAND]
a_deck = [f(i) for i in A_DECK]
b_hand = [f(i) for i in B_HAND]
b_deck = [f(i) for i in B_DECK]
assert len(FILLER) >= 60, f'need 60 filler cards, got {len(FILLER)}'
p1 = PlayerState(name='A', deck=a_hand + a_deck)
p2 = PlayerState(name='B', deck=b_hand + b_deck)
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

c, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
for cell in gs.earth:
    cell[0] = 'OC'
ga, gb = gs.players['A'], gs.players['B']
ga.hand, ga.mana, ga.deck, ga.discard = a_hand, [], a_deck, []
gb.hand, gb.mana, gb.deck, gb.discard = b_hand, [], b_deck, []
c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
c.commit()
c.close()
print('game', gid)

try:
    # init mana
    gs, ok = send(gid, 'A', {'cards': [f(0), f(1), f(2)], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, ok = send(gid, 'B', {'cards': [f(30), f(31), f(32)], 'to': 'mana', 'mode': '', 'pendings': []})
    print('init:', ok, gs.state)
    # turn 1: A places bloodtest (2), plays MAIN1 with bloodtest attached (1)
    gs, ok = send(gid, 'A', {'cards': ['bloodtest'], 'to': 'pending_zone', 'mode': '', 'pendings': []})
    print('place bloodtest:', ok, gs.state)
    gs, ok = send(gid, 'A', {'cards': [MAIN1], 'to': 'stopover_4', 'mode': 'move', 'pendings': ['bloodtest']})
    print('play MAIN1:', ok, gs.state)
    gs, ok = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, ok = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    print('after chain:', ok, gs.state)
    assert 'waiting for B to discard' in gs.state, gs.state
    # B chooses f(33)
    gs, ok = send(gid, 'B', {'cards': [f(33)], 'to': 'discard_pile', 'mode': '', 'pendings': []})
    print('after B discard:', ok, gs.state, 'winner=', gs.winner)
    assert gs.state.startswith('turn 2') or 'mana or pass' in gs.state, gs.state

    # set A to cell 22 so MAIN2 wins on turn 2
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    conn, _, gs = ge.get_current_game(gid)
    gs.players['A'].current_position = 22
    conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    conn.commit()
    conn.close()

    # turn 2 (order flipped: B first): B passes mana + play; A passes mana, plays MAIN2 -> win
    gs, ok = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gs, ok = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    print('turn2 mana:', ok, gs.state)
    gs, ok = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    print('turn2 B play pass:', ok, gs.state)
    conn, _, gsx = ge.get_current_game(gid)
    conn.close()
    ax = gsx.players['A']
    print('DEBUG A before MAIN2: hand', ax.hand, 'mana', ax.mana, 'spend', ax.mana_spend, 'pos', ax.current_position, 'state', gsx.state)
    gs, ok = send(gid, 'A', {'cards': [MAIN2], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    print('turn2 A MAIN2:', ok, gs.state, 'winner=', gs.winner)
    # B already passed -> control stays with A: A must pass to end the play phase
    gs, ok = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    print('turn2 A pass:', ok, gs.state, 'winner=', gs.winner)
    assert gs.state == 'game over' and gs.winner == 'A', (gs.state, gs.winner)

    # conservation
    for n in ('A', 'B'):
        p = gs.players[n]
        tot = len(p.hand or []) + len(p.deck or []) + len(p.discard or []) + len(p.mana or [])
        print(n, 'total cards:', tot)
    print('SAVED gid for replay:', gid)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '_bt_gid.txt'), 'w') as fh:
        fh.write(gid)
finally:
    pass

# ---- replay ----
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'games', 'analysis'))
import replay
st = replay.load_game(gid)
res = replay.analyze_game(st)
warns = res.get('warnings') or []
print(f"\nREPLAY WARNINGS ({len(warns)}):")
for w in warns:
    print(' -', w)
#_delete(gid)
print('\nDONE (game deleted after replay)')
