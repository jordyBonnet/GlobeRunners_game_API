# Manual smoke test (A.4): Engineers - refined drop targeting (#16) and refinery ordering (#17).
#   * unit: path-aware _choose_drop_cell for all 4 drops (self-buff on my predicted path with the
#     arrival-first check, denial on the opponent's declared/typical path, self-harm avoidance,
#     busy-cell skip, movement-lock handling), the _oppo_finish_threat landmine value model, and
#     the put_mana / choose_support_play ordering (refinery before drops, landmine kept over a
#     main while its threat is live).
#   * E2E (real game through ge.handle_websocket_message): the robot declares its move FIRST, then
#     places its boost on cell 19 - the first cell of its OWN declared path - and the engine fires
#     that self-placed drop during the same resolution step to push it across cell 24 (win).
# Run from the project root:  uv run python tests/_ai_a4_engineers.py
# NOTE: writes to games.db (like the other smoke tests); all games are deleted after.
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState
from player_ai.playerai import (PlayerAI, validate_message)
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

# deterministic card picks (verified against the pool)
ADV5_M2 = 'Twi22_ae27f4'     # Twigs m2 adv5 advancing_oppo  -> est 5
ADV6_M3 = pick((pl.col('faction') == 'Twigs') & (pl.col('condition') == 'no_condition')
               & (pl.col('advancing') >= 6) & (pl.col('mana') <= 3))[0]   # for opponent declared moves
assert ROWS[ADV5_M2]['advancing'] == 5 and ROWS[ADV6_M3]['advancing'] >= 6

ai = PlayerAI(player_state=None)


def set_ai(pos=5, faction='Twigs', hand=None, deck=None, mana_n=3, oppo_pos=None, oppo_faction='Dwarves',
           declared_mine=None, declared_oppo=None, earth=('DE' * 24), busy_cells=(), board_drops=None):
    ai.player_state = PlayerState(name='A', current_position=pos, faction=faction,
                                  hand=list(hand or []), deck=list(deck or []),
                                  mana=['m'] * mana_n, mana_spend=0)
    if declared_mine:
        ai.player_state.action_chain = [{'cards': [c], 'to': 'stopover_4', 'mode': 'move'} for c in declared_mine]
    ai.oppo_position, ai.oppo_faction, ai.oppo_mana, ai.oppo_hand = oppo_pos, oppo_faction, None, None
    ai.earth_biomes = list(earth)
    ai.cataclysm_pile = ['OC', 'MO', 'DE', 'JU']
    ai.oppo_actions = [{'cards': [c], 'to': 'stopover_4', 'mode': 'move'} for c in (declared_oppo or [])]
    ai.nobodymoves_active, ai.rooted_on_board, ai.board_drops = False, [], list(board_drops or [])
    ai.drops_on_board, ai.occupied_cells, ai.temperature, ai.day_night, ai.day_night_fixed = False, set(busy_cells), None, 'day', False
    return ai


# =====================================================================================
# #16 boost / trampoline - self-buff on my predicted path
# =====================================================================================
print("\n--- Unit: boost/trampoline targeting ---")
set_ai(pos=5, declared_mine=[ADV5_M2], oppo_pos=0)   # est 5 -> reach cells 6..10
check("boost lands on the FIRST cell of my declared path (est 5 from cell 5)",
      ai._choose_drop_cell('boost') == 6)
check("trampoline targets identically to boost", ai._choose_drop_cell('trampoline') == 6)

set_ai(pos=5, declared_mine=[ADV5_M2], oppo_pos=0, busy_cells=(6, 7))   # occupied/existing drops skipped
check("occupied cells and existing drop tokens are skipped (cell 8)", ai._choose_drop_cell('boost') == 8)

set_ai(pos=5, declared_mine=[ADV5_M2], oppo_pos=0, busy_cells=(6, 7), board_drops=[{'cell': 8, 'kind': 'gluetrap', 'owner': 'B'}])
check("a cell already carrying a drop token is skipped (cell 9)", ai._choose_drop_cell('boost') == 9)

set_ai(pos=5, declared_mine=[ADV5_M2], oppo_pos=8, busy_cells=(6, 7, 8))   # every remaining candidate is closer to B
check("arrival-first: when the opponent reaches EVERY candidate strictly sooner -> not placed",
      ai._choose_drop_cell('boost') is None)

set_ai(pos=5, declared_mine=[ADV5_M2], oppo_pos=9)   # cell 6 sits BEHIND the opponent's token - forward-only tokens can't consume it
check("arrival-first: a cell behind the opponent's token is always safe (cell 6)", ai._choose_drop_cell('boost') == 6)

set_ai(pos=5, declared_mine=[ADV5_M2], oppo_pos=None)   # no public info about the opponent -> fall back to pure ahead-range
check("no opponent position: falls back to my path / ahead range (cell 6)", ai._choose_drop_cell('boost') == 6)

set_ai(pos=18, declared_mine=[ADV5_M2], oppo_pos=3)   # near the finish: capped at cell 23
check("near the finish the candidate list is capped at cell 23 (first free = 19)", ai._choose_drop_cell('boost') == 19)

# =====================================================================================
# #16 gluetrap / landmine - denial on the opponent's path, never on mine
# =====================================================================================
print("\n--- Unit: gluetrap/landmine targeting ---")
set_ai(pos=2, hand=[], oppo_pos=10, declared_oppo=[ADV6_M3])   # their declared est >= 6 -> cells 11..15+
check("gluetrap lands on the EARLIEST cell of the opponent's declared path", ai._choose_drop_cell('gluetrap') == 11)

set_ai(pos=9, hand=[], oppo_pos=8, declared_mine=[ADV5_M2], declared_oppo=[ADV6_M3])   # my reach 10..14 overlaps their whole path
check("self-harm: a denial drop is NEVER placed on my own predicted path -> not placed", ai._choose_drop_cell('gluetrap') is None)

set_ai(pos=20, hand=[], oppo_pos=4)   # opponent has not declared anything -> typical range 1..4 ahead
check("no declared opponent move: falls back to the typical range (cell 5)", ai._choose_drop_cell('landmine') == 5)

set_ai(pos=2, hand=[], oppo_pos=None)
check("denial with no public opponent position -> not placed", ai._choose_drop_cell('gluetrap') is None)

# =====================================================================================
# #16 movement lock: nobodymoves suppresses both players' declared moves this turn
# =====================================================================================
print("\n--- Unit: movement-lock handling ---")
set_ai(pos=5, declared_mine=[ADV5_M2], oppo_pos=0)
ai.nobodymoves_active = True
check("locked: my predicted path is empty (no steps happen this turn)", ai._my_reach_cells() == [])
check("locked: boost falls back to the plain ahead range (cell 6)", ai._choose_drop_cell('boost') == 6)
set_ai(pos=5, hand=[], oppo_pos=10, declared_oppo=[ADV6_M3])
ai.nobodymoves_active = True
check("locked: the opponent's declared reach is ignored -> typical range (cell 11)", ai._choose_drop_cell('gluetrap') == 11)

# =====================================================================================
# #16 landmine value model + put_mana keep-value interplay
# =====================================================================================
print("\n--- Unit: landmine endgame value ---")
set_ai(pos=5, hand=['landmine'], oppo_pos=21, declared_oppo=[ADV6_M3])   # 21 + est(>=6) >= 24 -> declared winning move
check("declared winning opponent move -> _oppo_finish_threat True", ai._oppo_finish_threat() is True)
set_ai(pos=5, hand=['landmine'], oppo_pos=20)
ai.oppo_hand = 1
check("latent: cell >= 20 with cards in hand -> threat True", ai._oppo_finish_threat() is True)
ai.oppo_hand = 0
check("latent but an EMPTY hand -> no threat", ai._oppo_finish_threat() is False)
set_ai(pos=5, hand=['landmine'], oppo_pos=5)
check("opponent far from the finish -> no threat", ai._oppo_finish_threat() is False)

m4 = pick((pl.col('faction') == 'Twigs') & (pl.col('mana') == 4))[0]   # keep-value 14
set_ai(pos=5, hand=['landmine', m4], oppo_pos=21, declared_oppo=[ADV6_M3])
check("threat live: landmine value 45", ai._support_value('landmine') == 45)
sac = ai.put_mana(1, in_turn=True)['cards']
check("threat live: a cost-4 main is sacrificed to mana BEFORE the landmine", sac == [m4], f"(sac={sac})")
set_ai(pos=5, hand=['landmine', m4], oppo_pos=5)
check("no threat: landmine value drops to 18", ai._support_value('landmine') == 18)

# =====================================================================================
# #17 refinery ordering in choose_support_play
# =====================================================================================
print("\n--- Unit: refinery before drops (#17) ---")
set_ai(pos=5, hand=['refinery', 'boost'], oppo_pos=0, mana_n=3)
msg = ai.choose_support_play()
check("affordable dwelling is played BEFORE a drop (value 50 > ~26)", msg and msg['to'] == 'dwelling' and msg['cards'] == ['refinery'])
ai.player_state.dwelling = 'refinery'
msg = ai.choose_support_play()
check("with the zone already full it falls through to the boost", msg and msg['mode'] == 'move' and msg['cards'] == ['boost']
      and isinstance(msg.get('cell'), int))

# =====================================================================================
# E2E: declare the move, place the boost on my own path, win on the self-triggered drop
# =====================================================================================
print("\n--- E2E: self-placed boost completes the win ---")
MAIN = 'Twi11_2ba5b3'   # Twigs m1 adv4 draw_oppo (verified in pool)
assert ROWS[MAIN]['advancing'] == 4 and ROWS[MAIN]['mana'] == 1
# NOTE: the Twigs pool has NO cost-5 cards - cost-4 fillers keep the deck single-faction
a_deck = ['boost', MAIN] + pick((pl.col('faction') == 'Twigs') & (pl.col('mana') == 4), n=18)   # single-faction Twigs deck
b_deck = pick((pl.col('faction') == 'Dwarves') & (pl.col('mana') >= 4), n=20)
assert len(a_deck) == 20 and len(set(a_deck)) == 20

p1 = PlayerState(name='A', deck=list(a_deck)); p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
gids = [gid]
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

conn, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
ga, gb = gs.players['A'], gs.players['B']
ga.hand = a_deck[:6]; ga.deck = list(a_deck[6:]); ga.mana, ga.discard, ga.dwelling, ga.pendings = [], [], None, []
gb.hand = b_deck[:6]; gb.deck = list(b_deck[6:]); gb.mana, gb.discard, gb.dwelling = [], [], None
ga.current_position, gb.current_position = 18, 3      # A is 6 cells from the finish (4 + boost 2)
for i in range(24):                                    # DE everywhere: not a Twigs home biome -> no bonus
    gs.earth[i][0] = 'DE'
gs.board_drops, gs.rooted_on_board, gs.nobodymoves_active = [], [], False
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
conn.commit(); conn.close()
print(f"game {gid} created (A at 18 with boost+MAIN in hand, B at 3)")


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
        if b.hand:
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
sent = []

# --- init: B places first (its visible mana is irrelevant here), then the robot sacrifices its 3 fillers
for _ in range(4):
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
    okv, why = validate_message(msg)
    check(f"local validation passes ({msg.get('to')})", okv, f"({why})")
    succ, why2 = send(gid, 'A', msg)
    sent.append(dict(msg))
    check("engine accepted the robot's init action", succ is True, f"(resp={why2!r} msg={msg})")

conn, _, g = ge.get_current_game(gid); ga = g.players['A']; conn.close()
check("init: boost + MAIN kept in hand, fillers sacrificed to the zone",
      'boost' in ga.hand and MAIN in ga.hand and len(ga.mana) == 3, f"(hand={ga.hand}, mana={ga.mana})")

# --- turn 1: move first (cell 18 -> path 19..22), then the boost must land on cell 19
for _ in range(10):
    conn, _, g = ge.get_current_game(gid); state = g.state; conn.close()
    if g.winner is not None or g.turn > 1:
        break
    msg = ai_decide(g, 'A', st, ai2)
    if msg is not None:
        okv, why = validate_message(msg, dwelling=g.players['A'].dwelling, pendings_zone=g.players['A'].pendings)
        check(f"local validation passes ({msg.get('mode') or msg.get('to')} / {msg.get('cards')})", okv, f"({why})")
        succ, why2 = send(gid, 'A', msg)
        sent.append(dict(msg))
        check("engine accepted the robot's action", succ is True, f"(resp={why2!r} msg={msg})")
    drive_b()
    conn, _, g = ge.get_current_game(gid); done = g.turn > 1 or g.winner is not None; conn.close()
    if done:
        break

main_msgs = [m for m in sent if m.get('cards') == [MAIN]]
boost_msgs = [m for m in sent if m.get('cards') == ['boost']]
check("the robot declared its move card", len(main_msgs) == 1 and main_msgs[0]['mode'] == 'move')
check("then placed the boost as a support fallback", len(boost_msgs) == 1 and boost_msgs[0]['mode'] == 'move', f"(sent={[(m['mode'], m.get('to'), m['cards']) for m in sent]})")
check("the boost landed on cell 19 - the FIRST cell of its own declared path (est 4 from cell 18)",
      len(boost_msgs) == 1 and boost_msgs[0].get('cell') == 19, f"(got {boost_msgs and boost_msgs[0].get('cell')})")

conn, _, g = ge.get_current_game(gid); ga = g.players['A']; conn.close()
check("the self-placed drop fired during resolution and pushed A across cell 24 -> WINNER A",
      g.winner == 'A', f"(winner={g.winner}, state={g.state})")
check("the consumed boost left no token on the board", (g.board_drops or []) == [], f"(drops={g.board_drops})")

# card conservation: every card of A's deck exactly once across all zones
all_a = ga.hand + ga.deck + ga.discard + ga.mana
check("A card conservation (20 cards, MAIN+boost in discard)",
      len(all_a) == 20 and sorted(all_a) == sorted(a_deck),
      f"(got {len(all_a)}; hand={ga.hand}, discard={ga.discard})")

# --- cleanup ---------------------------------------------------------------------------
conn = sqlite3.connect(ge.DB_PATH)
for g_ in gids:
    conn.execute("DELETE FROM games WHERE game_id = ?", (g_,))
conn.commit(); conn.close()

print(f"\nALL {ok} CHECKS PASSED - A.4 Engineers drop targeting + refinery ordering verified")
