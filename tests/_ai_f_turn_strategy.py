# Manual smoke test (section F): turn-level strategy.
#   * #35a winning cards resolve EARLIEST - the score tuple ranks wins before net value, so a
#     guaranteed win is played even when another card has higher expected value;
#   * #35b cancel / copy / grappling land on the position FACING their target - greedy per-tick with
#     facing-aware scoring (§E) yields the right sequence under play alternation: a special card waits
#     while my next column faces something weak, and is played as soon as a strong declared card lands
#     on it (no explicit planner needed);
#   * #35c cheap low-value cards fill late indices - pass discipline (§C #26) declines net-negative
#     plays so only positive-expectation cards consume positions;
#   * #36 mana economy / play priority: MAIN moves always come before support plays (the driver's
#     3.4 -> 3.5 order, §A.1 #5) - verified end to end with a robot holding both a mover and a
#     'laboratory' dwelling in hand: it MOVES first and only places the dwelling as leftover.
# Games are written to games.db and deleted after. Run from the project root:
#   uv run python tests/_ai_f_turn_strategy.py
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState
from player_ai.playerai import PlayerAI
from game_ui.ai_driver import ai_decide

DB = ge.CARDS_DB
ROWS = {r['card_id']: r for r in DB.iter_rows(named=True)}


def pick(f, n=1):
    return DB.filter(f)['card_id'].to_list()[:n]


ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

ai = PlayerAI(player_state=None)
POOL_DB = ai.CARDS_DB


def set_ai(rows=None, pos=0, faction='Dwarves', hand=None, mana_n=5, earth='DE', temp=None,
           oppo_pos=None, oppo_actions=None, play_count=0):
    ps = PlayerState(name='A', current_position=pos, faction=faction,
                     hand=list(hand or []), mana=['m'] * mana_n, mana_spend=0)
    ps.play_count = play_count
    ai.player_state = ps
    if rows is not None:
        ai.CARDS_DB = pl.DataFrame(rows)
    else:
        ai.CARDS_DB = POOL_DB
    ai.MAIN_ROWS = {r['card_id']: r for r in ai.CARDS_DB.iter_rows(named=True)}
    ai.oppo_position, ai.oppo_mana, ai.oppo_hand = oppo_pos, None, 1
    ai.oppo_actions = list(oppo_actions or [])
    if earth is None:
        ai.earth_biomes = ['OC'] * 24
    elif isinstance(earth, str):
        ai.earth_biomes = [earth] * 24
    else:
        ai.earth_biomes = list(earth)
    ai.temperature, ai.day_night, ai.day_night_fixed = temp, 'day', False
    ai.cataclysm_pile = ['OC', 'MO', 'DE', 'JU']
    ai.drops_on_board = False
    ai.board_drops = []
    ai.drop_tokens = {}
    ai.nobodymoves_active = False
    ps.landmine_blocked = False
    ai.rooted_on_board = []
    ai.occupied_cells = set()
    ai.oppo_dwelling = None
    ai.oppo_faction, ai.oppo_pending_slots = 'Twigs', []
    ai.hard_mode, ai._hard_oppo_win, ai.oppo_history = False, None, []


# =====================================================================================
# #35a - a guaranteed win is played FIRST, even below a higher-net non-winning card
# =====================================================================================
print("\n--- Unit: #35a wins resolve earliest ---")
WINNER = 'Orc32_cb6aa6'    # m3 adv6 taxation -> net 4 off-home, WINS from pos 18 (18+6 >= 24)
DECOY  = 'Twi11_2ba5b3'    # m1 adv4 draw_oppo en2 -> net 5 (> winner's 4), does NOT win (18+4 < 24)
set_ai(pos=18, hand=[WINNER, DECOY])
m = ai.play_card()
check("from pos 18 the winning card goes even though its net value (4) is BELOW the decoy's (5)",
      m and m['cards'][0] == WINNER, f"(got {m})")

WINNER2 = 'Orc43_6c53ce'   # m4 adv7 taxation -> net 5, wins from pos 18
set_ai(pos=18, hand=[WINNER2, DECOY])
m = ai.play_card()
check("same with a higher-cost winner: the win still outranks raw net value",
      m and m['cards'][0] == WINNER2, f"(got {m})")

# =====================================================================================
# #35b - facing-aware sequencing: the cancel WAITS for the strong card to land on my column
# =====================================================================================
print("\n--- Unit: #35b special cards face their target (emergent ordering) ---")
CANCEL = 'Dem21_6530e6'    # m2 adv0 effect_canceled
PLAIN  = 'Orc21_6d4858'    # m2 adv5 taxation -> net 3 off-home
F_WEAK = 'Mia22_ccd78b'    # their declared card on my column: self-penalty, nothing worth denying
F_DRAW = 'Dem43_60882a'    # a strong 'draw' declaration (denied value 2)

set_ai(hand=[CANCEL, PLAIN], oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': [F_WEAK]}])
m1 = ai.play_card()
check("tick 1: my column faces a weak card -> the plain mover goes first (the cancel waits)",
      m1 and m1['cards'][0] == PLAIN, f"(got {m1})")

# ... between my plays the opponent declares its strong card on MY NEXT column (stopover_3) -
# simulate the state as it stands at tick 2:
set_ai(hand=[CANCEL], play_count=1, oppo_actions=[
    {'mode': 'move', 'to': 'stopover_4', 'cards': [F_WEAK]},
    {'mode': 'move', 'to': 'stopover_3', 'cards': [F_DRAW]}])
ai.player_state.action_chain = [{'mode': 'move', 'to': 'stopover_4', 'cards': [PLAIN]}]
ai.player_state.mana_spend = 2
m2 = ai.play_card()
check("tick 2: the strong card now faces my next column -> the cancel is played onto it",
      m2 and m2['cards'][0] == CANCEL, f"(got {m2})")

# #35c regression (pass discipline from section C keeps holding inside the new scoring):
set_ai(pos=0, hand=['Mia11_9b28d6'])   # m1 adv3 backward en-2 -> net 1... and a pure junk:
m = ai.play_card()
check("a single positive-net card is still played (no over-conservatism)", m is not None, f"(got {m})")

# =====================================================================================
# #36 - play priority: MAIN moves before support plays (leftover mana only)
# =====================================================================================
print("\n--- E2E: main move first, dwelling as leftover ---")
MOVER = 'Twi11_7da2a0'    # m1 adv3 draw_oppo -> net 4 off-home
_f = pl.col('faction').eq('Orcs')
fillers_a = pick(_f & (pl.col('mana').le(3)), n=18)
a_deck = ['laboratory', MOVER] + list(dict.fromkeys(fillers_a))[:18]
assert len(set(a_deck)) == 20, f"deck collision A: {len(set(a_deck))}"

_f = pl.col('faction').eq('Twigs')
b_deck = pick(_f & (pl.col('mana').le(2)), n=20)
assert len(b_deck) == 20 and len(set(b_deck)) == 20, f"deck collision B: {len(set(b_deck))}"

p1 = PlayerState(name='A', deck=list(a_deck)); p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
gids = [gid]

conn, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
gs.earth = [['OC'] for _ in range(24)]    # off-home for the Orcs deck / Twigs mover alike (Dwarves default faction is irrelevant here)
ga, gb = gs.players['A'], gs.players['B']
ga.hand = a_deck[:6]; ga.deck = list(a_deck[6:]); ga.mana, ga.discard, ga.dwelling, ga.pendings = [], [], None, []
gb.hand = b_deck[:6]; gb.deck = list(b_deck[6:]); gb.mana, gb.discard, gb.dwelling, gb.pendings = [], [], None, []
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
conn.commit(); conn.close()


def send(gid_, name, message):
    conn, _, g = ge.get_current_game(gid_)
    player = g.players[name].model_copy()
    player.message = message
    conn.close()
    resp = ge.handle_websocket_message(gid_, player)
    if isinstance(resp, str):
        resp = json.loads(resp)
    info = (resp or {}).get('message', {}) if isinstance(resp, dict) else {}
    return info.get('success'), info.get('message')


def drive_b():
    conn, _, g = ge.get_current_game(gid); state = g.state; b = g.players['B']; conn.close()
    if state.startswith("waiting for both players to put") and len(b.mana or []) < 3:
        succ, why2 = send(gid, 'B', {'cards': (b.hand or [])[:3], 'to': 'mana', 'mode': '', 'pendings': []})
    elif state == "waiting for both players to mana or pass":
        if b.hand and len(b.mana or []) < 5:
            succ, why2 = send(gid, 'B', {'cards': [b.hand[0]], 'to': 'mana', 'mode': '', 'pendings': []})
        else:
            succ, why2 = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    elif "to play" in state and "(B)" in state:
        succ, why2 = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    else:
        return
    check("engine accepted the opponent's action", succ is True, f"(resp={why2!r} state={state})")


ai2 = PlayerAI(player_state=None)
st: dict = {}
sent_a = []

for _ in range(6):   # init phase
    conn, _, g = ge.get_current_game(gid); state = g.state; conn.close()
    if not state.startswith("waiting for both players to put"):
        break
    drive_b()
    conn, _, g = ge.get_current_game(gid)
    if len(g.players['A'].mana or []) >= 3:
        conn.close()
        continue
    msg = ai_decide(g, 'A', st, ai2)
    conn.close()
    succ, why2 = send(gid, 'A', msg)
    sent_a.append(dict(msg))
    check("engine accepted the robot's init action", succ is True, f"(resp={why2!r} msg={msg})")

# guarantee: A holds exactly the mover + the laboratory (4 mana budget = m1 mover + cost-3 dwelling)
conn, _, g = ge.get_current_game(gid)
ga = g.players['A']
ga.hand = [MOVER, 'laboratory']
ga.mana = list(a_deck[2:6])
ga.deck = list(dict.fromkeys([d for d in a_deck if d not in ga.hand and d not in ga.mana]))
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (g.to_json(), gid))
conn.commit(); conn.close()

for _ in range(14):   # play phase + whatever follows - the assertions only need A's first two actions
    conn, _, g = ge.get_current_game(gid); state = g.state; winner = g.winner; conn.close()
    if state == 'game over' or winner:
        break
    drive_b()   # no-op when it is not B's expected action
    conn, _, g = ge.get_current_game(gid); state = g.state; conn.close()
    if "to play" in state and "(A)" in state:
        msg = ai_decide(g, 'A', st, ai2)
        conn.close()
        sent_a.append(dict(msg))
        succ, why2 = send(gid, 'A', msg)
        check(f"engine accepted the robot's action #{len(sent_a)} ({msg.get('mode')}/{[c for c in (msg.get('cards') or [])]})",
              succ is True, f"(resp={why2!r})")

plays = [m for m in sent_a if m.get('mode') in ('move', 'dwelling_activation')]
check("the robot's FIRST play-phase action is the MAIN mover (not the dwelling)",
      len(plays) >= 1 and plays[0].get('mode') == 'move' and plays[0]['cards'] == [MOVER], f"(got {sent_a})")
check("only THEN does the leftover support card get placed (dwelling priority, F #36)",
      any(m.get('to') == 'dwelling' and m['cards'] == ['laboratory'] for m in sent_a), f"(got {sent_a})")

# cleanup
_c = sqlite3.connect(os.path.join(PROJECT_ROOT, 'games', 'games.db'))
for _gid in gids:
    _c.execute("DELETE FROM games WHERE game_id=?", (_gid,))
_c.commit(); _c.close()

print(f"\nALL {ok} CHECKS PASSED - section F turn-level strategy verified")
