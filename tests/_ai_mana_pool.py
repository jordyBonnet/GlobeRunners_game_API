# Manual smoke test (I.1): mana pool management — the robot no longer grows its mana
# pool automatically in the mana phase. New rule (put_mana, in_turn only):
#   * available mana >= TOTAL cost of the whole hand -> PASS (the pool already covers
#     everything the robot owns; a sacrificed card is never played again);
#   * available mana <  total cost of the hand      -> place 1 card (keep-value sacrifice).
# Coverage:
#   * unit: _hand_total_cost (mains / support mix / empty) and every put_mana in_turn branch
#     (pool >, ==, < hand cost; mana_spend deducted; empty hand; init phase unaffected);
#   * E2E (2 real games through ge.handle_websocket_message + ai_decide): at the turn-2 mana
#     phase the robot PASSES when the pool covers the hand cost, and PUTS A CARD IN MANA
#     (sacrificing the cost-4 main over the m1s) when the pool is short.
# Run from the project root:  uv run python tests/_ai_mana_pool.py
# NOTE: writes to games.db (like the other smoke tests); all games are deleted after.
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState
from player_ai.playerai import PlayerAI, validate_message
from game_ui.ai_driver import ai_decide

DB = ge.CARDS_DB


def pick(f, n=1):
    return DB.filter(f)['card_id'].to_list()[:n]


ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

# deterministic card picks (verified against the pool)
M1 = 'Twi11_2ba5b3'    # Twigs m1 adv4 draw_oppo (used by the engineers test - exists)
M1B = 'Twi11_c6d14c'   # Twigs m1 adv3 advancing_oppo
M1C = 'Twi11_230497'   # Twigs m1 adv4 advancing_oppo
M4 = pick((pl.col('faction') == 'Twigs') & (pl.col('mana') == 4))[0]
assert DB.filter(pl.col('card_id').is_in([M1, M1B, M1C]))['mana'].to_list() == [1, 1, 1]
assert DB.filter(pl.col('card_id') == M4)['mana'][0] == 4

ai = PlayerAI(player_state=None)


def set_ai(hand=None, mana_n=3, mana_spend=0):
    ai.player_state = PlayerState(name='A', current_position=0, faction='Twigs',
                                  hand=list(hand or []), deck=[],
                                  mana=['m'] * mana_n, mana_spend=mana_spend)
    return ai


# =====================================================================================
# _hand_total_cost
# =====================================================================================
print("\n--- Unit: _hand_total_cost ---")
set_ai(hand=[M1, M1B, M1C])
check("three m1 mains -> 3", ai._hand_total_cost() == 3)
set_ai(hand=[M4, M1])
check("m4 + m1 -> 5", ai._hand_total_cost() == 5)
set_ai(hand=['boost', 'epo'])          # support cards count at their mana_cost (1 + 1)
check("two support m1 cards -> 2", ai._hand_total_cost() == 2)
set_ai(hand=['refinery', M1])          # support cost 3 + main 1
check("refinery(3) + m1 -> 4", ai._hand_total_cost() == 4)
set_ai(hand=[])
check("empty hand -> 0", ai._hand_total_cost() == 0)

# =====================================================================================
# put_mana (in_turn) - the mana-phase decision
# =====================================================================================
print("\n--- Unit: put_mana in_turn decision ---")
set_ai(hand=[M1, M1B, M1C], mana_n=3)
msg = ai.put_mana(1, in_turn=True)
check("pool == hand cost (3 == 3) -> PASS", msg == {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []}, f"(msg={msg})")
set_ai(hand=[M1, M1B, M1C], mana_n=5)
msg = ai.put_mana(1, in_turn=True)
check("pool > hand cost (5 > 3) -> PASS", msg.get('mode') == 'pass' and msg.get('cards') == [], f"(msg={msg})")
set_ai(hand=[M1, M1B, M1C], mana_n=2)
msg = ai.put_mana(1, in_turn=True)
check("pool < hand cost (2 < 3) -> place 1 card in mana", msg.get('to') == 'mana' and len(msg.get('cards', [])) == 1, f"(msg={msg})")
set_ai(hand=[M4], mana_n=3)
msg = ai.put_mana(1, in_turn=True)
check("pool 3 < lone m4 cost 4 -> place the m4", msg.get('to') == 'mana' and msg.get('cards') == [M4], f"(msg={msg})")
set_ai(hand=[M4], mana_n=4)
msg = ai.put_mana(1, in_turn=True)
check("pool 4 >= lone m4 cost 4 -> PASS", msg.get('mode') == 'pass', f"(msg={msg})")
set_ai(hand=[M4, M1], mana_n=4, mana_spend=1)   # available 3 < cost 5
msg = ai.put_mana(1, in_turn=True)
check("mana_spend deducted (4-1 < 5) -> place 1 card", msg.get('to') == 'mana' and len(msg.get('cards', [])) == 1, f"(msg={msg})")
set_ai(hand=[M4, M1], mana_n=6, mana_spend=1)   # available 5 >= cost 5
msg = ai.put_mana(1, in_turn=True)
check("mana_spend deducted (6-1 >= 5) -> PASS", msg.get('mode') == 'pass', f"(msg={msg})")
set_ai(hand=[], mana_n=3)
msg = ai.put_mana(1, in_turn=True)
check("empty hand -> PASS", msg.get('mode') == 'pass', f"(msg={msg})")
set_ai(hand=['refinery', M1, M1B], mana_n=4)    # cost 3+1+1 = 5 > 4
msg = ai.put_mana(1, in_turn=True)
check("support costs counted (pool 4 < 5) -> place 1 card", msg.get('to') == 'mana' and len(msg.get('cards', [])) == 1, f"(msg={msg})")

# the INIT phase is unaffected: still returns its 3 cards (the engine requires them)
set_ai(hand=[M1, M1B, M1C, M4], mana_n=0)
cards = ai.put_mana(3, in_turn=False)
check("init: returns exactly 3 cards even with an empty pool", isinstance(cards, list) and len(cards) == 3, f"(cards={cards})")

# keep-value ordering inside the sacrifice: the cost-4 main sinks below the m1s
set_ai(hand=[M1, M4, M1B], mana_n=2)
msg = ai.put_mana(1, in_turn=True)
check("sacrifice picks the lowest keep-value first (m4 over m1s)", msg.get('cards') == [M4], f"(msg={msg})")

# =====================================================================================
# E2E helper: create a game, drive it to the turn-2 mana phase, then force a scenario
# =====================================================================================
M1S = [M1, M1B, M1C]
a_deck = M1S + [M4] + pick((pl.col('faction') == 'Twigs') & pl.col('card_id').is_in(M1S + [M4]).not_(), n=16)
b_deck = pick((pl.col('faction') == 'Dwarves') & (pl.col('mana') >= 4), n=20)
assert len(a_deck) == 20 and len(set(a_deck)) == 20 and len(set(b_deck)) == 20

MANA_STATE = "waiting for both players to mana or pass"


def setup_game():
    p1 = PlayerState(name='A', deck=list(a_deck)); p2 = PlayerState(name='B', deck=list(b_deck))
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    gs.turn_order = ['A', 'B']
    ga, gb = gs.players['A'], gs.players['B']
    ga.hand = a_deck[:6]; ga.deck = list(a_deck[6:]); ga.mana, ga.discard, ga.dwelling, ga.pendings = [], [], None, []
    gb.hand = b_deck[:6]; gb.deck = list(b_deck[6:]); gb.mana, gb.discard, gb.dwelling = [], [], None
    ga.current_position, gb.current_position = 2, 2
    for i in range(24):
        gs.earth[i][0] = 'DE'   # not a Twigs home biome -> no bonus
    gs.board_drops, gs.rooted_on_board, gs.nobodymoves_active = [], [], False
    conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    conn.commit(); conn.close()
    return gid


def send(gid, name, message):
    conn, _, g = ge.get_current_game(gid)
    player = g.players[name].model_copy()
    player.message = message
    conn.close()
    resp = ge.handle_websocket_message(gid, player)
    if isinstance(resp, str):
        resp = json.loads(resp)
    info = (resp or {}).get('message', {}) if isinstance(resp, dict) else {}
    return info.get('success'), info.get('message')


def drive_b(gid):
    conn, _, g = ge.get_current_game(gid); state = g.state; b = g.players['B']; conn.close()
    if state.startswith("waiting for both players to put") and len(b.mana or []) < 3:
        succ, why2 = send(gid, 'B', {'cards': (b.hand or [])[:3], 'to': 'mana', 'mode': '', 'pendings': []})
    elif state == MANA_STATE:
        succ, why2 = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    elif "to play" in state and "(B)" in state:
        succ, why2 = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    else:
        return
    check("engine accepted the opponent's action", succ is True, f"(resp={why2!r} state={state})")


def drive_to_turn2_mana(gid, ai_, st):
    """Init + turn 1 (robot acts, B passes) -> stop at the turn-2 mana phase."""
    for _ in range(40):
        conn, _, g = ge.get_current_game(gid); state = g.state; turn = g.turn; conn.close()
        if state == MANA_STATE and turn == 2:
            return
        if g.winner or 'game over' in state:
            raise AssertionError(f"game ended before the turn-2 mana phase (state={state}, winner={g.winner})")
        if state.startswith("waiting for both players to put"):
            drive_b(gid)
            conn, _, g = ge.get_current_game(gid)
            if len(g.players['A'].mana or []) < 3:
                msg = ai_decide(g, 'A', st, ai_)
                conn.close()
                check("robot init action valid locally", validate_message(msg)[0], f"({msg})")
                succ, why2 = send(gid, 'A', msg)
                check("engine accepted the robot's init action", succ is True, f"(resp={why2!r} msg={msg})")
            continue
        if state == MANA_STATE:   # turn >= 2 - only reached after the target; safety
            drive_b(gid)
            continue
        if "to play" in state and "(A)" in state:
            conn, _, g = ge.get_current_game(gid)
            msg = ai_decide(g, 'A', st, ai_)
            conn.close()
            if msg is not None:
                check("robot action valid locally", validate_message(msg)[0], f"({msg})")
                succ, why2 = send(gid, 'A', msg)
                check("engine accepted the robot's action", succ is True, f"(resp={why2!r} msg={msg})")
            else:
                succ, why2 = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
                check("engine accepted the robot's pass", succ is True, f"(resp={why2!r})")
        elif "to play" in state and "(B)" in state:
            drive_b(gid)
        else:
            raise AssertionError(f"unexpected state {state!r}")
    raise AssertionError("could not reach the turn-2 mana phase")


def force_hand(gid, hand, mana_n):
    conn, _, g = ge.get_current_game(gid)
    a = g.players['A']
    a.hand = list(hand); a.mana = ['m'] * mana_n; a.mana_spend = 0
    # keep the deck consistent: everything of the hand out of the deck, rest untouched
    a.deck = [c for c in (a.deck or []) if c not in hand]
    conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (g.to_json(), gid))
    conn.commit(); conn.close()


# =====================================================================================
# E2E game 1: pool COVERS the hand cost (3 >= 3) -> the robot PASSES the mana phase
# =====================================================================================
print("\n--- E2E 1: pool covers the hand -> PASS (no automatic mana) ---")
gid1 = setup_game()
gids = [gid1]
ai1 = PlayerAI(player_state=None)
st1: dict = {}
drive_to_turn2_mana(gid1, ai1, st1)
force_hand(gid1, [M1, M1B, M1C], mana_n=3)   # cost 3 == pool 3

conn, _, g = ge.get_current_game(gid1); pool0 = len(g.players['A'].mana); conn.close()
conn, _, g = ge.get_current_game(gid1)
msg = ai_decide(g, 'A', st1, ai1)
conn.close()
check("decision: PASS (pool 3 covers the 3-cost hand)", msg is not None and msg.get('mode') == 'pass' and msg.get('cards') == [], f"(msg={msg})")
succ, why2 = send(gid1, 'A', msg)
check("engine accepted the mana-phase pass", succ is True, f"(resp={why2!r})")
drive_b(gid1)   # B passes too -> play phase

conn, _, g = ge.get_current_game(gid1); a = g.players['A']; conn.close()
check("the pool was NOT grown (still 3 tokens)", len(a.mana) == 3 and len(a.mana) == pool0, f"(pool={len(a.mana)})")
check("the turn moved on to the play phase", g.turn == 2 and "to play" in g.state, f"(state={g.state})")
check("the hand is untouched (nothing was sacrificed)", sorted(a.hand) == sorted([M1, M1B, M1C]), f"(hand={a.hand})")

# =====================================================================================
# E2E game 2: pool SHORT of the hand cost (3 < 6) -> the robot PUTS A CARD IN MANA
# =====================================================================================
print("\n--- E2E 2: pool short of the hand -> place 1 card (sacrifice the m4) ---")
gid2 = setup_game()
gids.append(gid2)
ai2 = PlayerAI(player_state=None)
st2: dict = {}
drive_to_turn2_mana(gid2, ai2, st2)
force_hand(gid2, [M4, M1, M1B], mana_n=3)   # cost 6 > pool 3

conn, _, g = ge.get_current_game(gid2)
msg = ai_decide(g, 'A', st2, ai2)
conn.close()
check("decision: place 1 card in mana (pool 3 < 6-cost hand)",
      msg is not None and msg.get('to') == 'mana' and msg.get('cards') == [M4], f"(msg={msg})")
succ, why2 = send(gid2, 'A', msg)
check("engine accepted the mana placement", succ is True, f"(resp={why2!r})")
drive_b(gid2)

conn, _, g = ge.get_current_game(gid2); a = g.players['A']; conn.close()
check("the pool grew to 4 tokens", len(a.mana) == 4, f"(pool={len(a.mana)})")
check("the sacrificed card is the cost-4 main (keep-value order), hand = the two m1s",
      sorted(a.hand) == sorted([M1, M1B]), f"(hand={a.hand})")
check("the turn moved on to the play phase", g.turn == 2 and "to play" in g.state, f"(state={g.state})")

# --- cleanup ---------------------------------------------------------------------------
conn = sqlite3.connect(ge.DB_PATH)
for g_ in gids:
    conn.execute("DELETE FROM games WHERE game_id = ?", (g_,))
conn.commit(); conn.close()

print(f"\nALL {ok} CHECKS PASSED - I.1 mana pool management (pass when the pool covers the hand, sacrifice only while short)")
