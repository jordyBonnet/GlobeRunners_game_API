# Manual smoke test (section E): reactive / instant main-pool effects, facing-aware scoring.
#   * #31 swap_cards: the robot now USES 'swap_with' - a decoy swap rescues an earlier move out of a
#     defended column (the swap card absorbs the block); scored including its best-swap gain;
#   * #32 effect_canceled placement: plays where it FACES a declared opponent card whose effect is
#     worth denying (TEMPO + situational + extra movement), never against plain/penalizing cards;
#   * #33 pet_trap: situational tempo when the opponent sits behind me and will step over my cell;
#   * #34 grappling_hook / copy_effect: score by what they FACE - copied advancing (public worst case)
#     or replayed effect value.
# Unit scenarios run against real pool cards + hand-crafted oppo_actions; one E2E drives the decoy
# swap through ge.handle_websocket_message and checks the engine's instant-swap log. Games are
# written to games.db and deleted after. Run from the project root:
#   uv run python tests/_ai_e_reactive.py
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState
from player_ai.playerai import (PlayerAI, SWAP_MIN_GAIN, validate_message)
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
POOL_DB = ai.CARDS_DB   # instance attribute - restore after overrides


def set_ai(rows=None, pos=0, faction='Dwarves', hand=None, mana_n=5, earth='DE', temp=None,
           oppo_pos=None, oppo_actions=None, play_count=0):
    """Dwarves on a uniform 'DE' sea = OFF-HOME for the default faction (no biome bonus) unless
    told otherwise. `oppo_actions` are the opponent's declared plays THIS turn (public chain)."""
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


def row_of(cid):
    return next(r for r in ai.CARDS_DB.iter_rows(named=True) if r['card_id'] == cid)


# real pool cards (see AI_improvement_list.md section E for the numbers used below)
CANCEL  = 'Dem21_6530e6'   # m2 adv0 sh1 no_condition effect_canceled
HOOK    = 'Dwa23_bb44be'   # m2 adv0 sh3 no_condition grappling_hook
COPY    = 'Mum21_6edd0d'   # m2 adv0 sh1 no_condition copy_effect
SWAPC   = 'Mia11_0e90ab'   # m1 adv-1 sh1 no_condition swap_cards
PET     = 'Mia11_7971c5'   # m1 adv-1 sh1 no_condition pet_trap
W5      = 'Orc21_6d4858'   # m2 adv5 sh1 no_condition taxation en2   (my strong mover)
PLAIN_A = 'Twi11_7da2a0'   # m1 adv3 sh1 no_condition draw_oppo       (net 4 off-home)
PLAIN_B = 'Mia11_87dba8'   # m1 adv2 sh1 no_condition draw_oppo       (net 3)
F_DRAW  = 'Dem43_60882a'   # m4 adv2 draw en1      -> denied value 2 (TEMPO draw)
F_WEAK  = 'Mia22_ccd78b'   # m2 adv3 taxation en1  -> denied value 0 (self-penalty, denying helps THEM)
F_RAMP  = 'Dwa23_714dd1'   # m2 adv0 ramp en1      -> copy gain 3 / denied 3
F_BIG   = 'Orc54_ca391f'   # m5 adv7 taxation en2  -> potential advance 7 (off-home)
F_ADV   = 'Dwa34_b99623'   # m3 adv3 advancing en1 -> movement copy: my estimator gives 4


def face(cid, to='stopover_4'):
    return {'mode': 'move', 'to': to, 'cards': [cid]}


# =====================================================================================
# #32 - effect_canceled placement (faces a strong declared card, waits otherwise)
# =====================================================================================
PLAIN_C = 'Orc21_de4294'   # m2 adv4 taxation -> net 2 off-home
print("\n--- Unit: #32 effect_canceled placement ---")
set_ai(hand=[CANCEL, PLAIN_C], oppo_actions=[face(F_DRAW)])
m = ai.play_card()
check("facing a 'draw' card -> the cancel is played (denies it) instead of the plain mover",
      m and m['cards'][0] == CANCEL, f"(got {m})")
set_ai(hand=[CANCEL, PLAIN_C], oppo_actions=[face(F_WEAK)])
m = ai.play_card()
check("facing a self-penalizing 'taxation' card -> cancel gains nothing, plain mover goes",
      m and m['cards'][0] == PLAIN_C, f"(got {m})")
set_ai(hand=[CANCEL, PLAIN_C])
m = ai.play_card()
check("nothing declared yet -> the special card WAITS (plain mover first)",
      m and m['cards'][0] == PLAIN_C, f"(got {m})")

# =====================================================================================
# #34 - grappling_hook / copy_effect score by what they FACE
# =====================================================================================
print("\n--- Unit: #34 grappling_hook / copy_effect facing value ---")
set_ai(hand=[HOOK, PLAIN_A], oppo_pos=10, oppo_actions=[face(F_BIG)])
m = ai.play_card()
check("hook faces an adv7 card -> copies it (net 9) and beats the plain mover (net 4)",
      m and m['cards'][0] == HOOK, f"(got {m})")
set_ai(hand=[HOOK, PLAIN_A], oppo_pos=10)
m = ai.play_card()
check("hook with nothing to copy -> plain mover first", m and m['cards'][0] == PLAIN_A, f"(got {m})")

set_ai(hand=[COPY, PLAIN_B], oppo_actions=[face(F_RAMP)])
m = ai.play_card()
check("copy faces a 'ramp' card -> replays it (net 4) and beats the plain mover (net 3)",
      m and m['cards'][0] == COPY, f"(got {m})")
set_ai(hand=[COPY, PLAIN_B])
m = ai.play_card()
check("copy with nothing to copy -> plain mover first", m and m['cards'][0] == PLAIN_B, f"(got {m})")

set_ai(hand=[COPY, PLAIN_B], oppo_actions=[face(F_ADV)])
m = ai.play_card()
check("copy faces an 'advancing' card -> the copied MOVEMENT counts (net 4+1 > net 3)",
      m and m['cards'][0] == COPY, f"(got {m})")

# nobodymoves lock: the copied movement is suppressed, a cancel still denies zone effects
set_ai(hand=[HOOK, CANCEL], oppo_pos=10, oppo_actions=[face(F_DRAW)])
ai.nobodymoves_active = True
cf_h, ct_h = ai._facing_gain(row_of(HOOK), 'stopover_4')
check("under a nobodymoves lock the hook's copied movement is suppressed", (cf_h, ct_h) == (0, 0))
cf_c, ct_c = ai._facing_gain(row_of(CANCEL), 'stopover_4')
check("under a nobodymoves lock a cancel still denies the facing effect", ct_c == 2, f"(got {ct_c})")

# =====================================================================================
# #31 - swap_cards: the decoy swap rescues my strong move out of a defended column
# =====================================================================================
print("\n--- Unit: #31 swap_cards decoy ---")
D1, D2 = 'Twi11_e4d118', 'Mia11_c63a55'   # two m1 sh1 cards -> declared shield 2 on my column
def defend_on(col):
    return {'mode': 'defend', 'to': col, 'cards': [D1, D2]}

set_ai(hand=[SWAPC], play_count=1,
       oppo_actions=[defend_on('stopover_4')])
ai.player_state.action_chain = [{'mode': 'move', 'to': 'stopover_4', 'cards': [W5]}]
opt = ai._choose_swap_target(row_of(SWAPC))
check("a defend (shields 2 >= W.mana 2) sits on my earlier move's column -> swap target found",
      opt is not None and opt[0] == 1, f"(got {opt})")
m = ai.play_card()
check("the play carries 'swap_with': the decoy takes the defended slot, W escapes",
      m and m['cards'][0] == SWAPC and m.get('swap_with') == 1, f"(got {m})")
ok_v, why_v = validate_message(m)
check("the swap message passes local pre-validation", ok_v, f"({why_v})")

set_ai(hand=[SWAPC], play_count=1)
ai.player_state.action_chain = [{'mode': 'move', 'to': 'stopover_4', 'cards': [W5]}]
opt2 = ai._choose_swap_target(row_of(SWAPC))
m2 = ai.play_card()
check("no defend declared -> no gain, no swap (and the negative-net card is not played at all)",
      opt2 is None and m2 is None, f"(opt={opt2} msg={m2})")

# =====================================================================================
# #33 - pet_trap situational tempo
# =====================================================================================
print("\n--- Unit: #33 pet_trap ---")
set_ai(pos=10, hand=[PET], oppo_pos=6)
check("opponent 4 cells BEHIND me -> situational tempo +2", ai._situational_tempo(row_of(PET)) == 2)
set_ai(pos=10, hand=[PET], oppo_pos=30 % 24 if False else 14)
check("opponent AHEAD of me -> no bonus (my cell is not on their forward path)",
      ai._situational_tempo(row_of(PET)) == 0)
BWD = 'Mia11_c63a55'   # m1 adv2 backward en-1 -> net 1 off-home
set_ai(pos=10, hand=[PET, BWD], oppo_pos=6)
m = ai.play_card()
check("behind-opponent: the trap (net 2) beats the plain mover (net 1)", m and m['cards'][0] == PET, f"(got {m})")
set_ai(pos=10, hand=[PET, BWD], oppo_pos=None)
m = ai.play_card()
check("no opponent behind: the trap is net-zero -> plain mover goes", m and m['cards'][0] == BWD, f"(got {m})")

# =====================================================================================
# E2E - decoy swap through the real engine (instant swap + block materializes)
# =====================================================================================
print("\n--- E2E: decoy swap accepted by the engine ---")
# A (Orcs deck) holds W5 + the swap decoy SWAPC; B (Twigs deck) opens with a 2-card DEFEND on
# stopover_4 - exactly where A's first move sits. The robot must answer with the DECOY SWAP so
# that its strong mover escapes to an undefended column while S absorbs the block.
_f = pl.col('faction').eq('Orcs')
fillers_a = pick(_f & (pl.col('mana').le(3)) & (~pl.col('card_id').is_in([W5, SWAPC])), n=18)
a_deck = [W5, SWAPC] + list(dict.fromkeys(fillers_a))[:18]
assert len(set(a_deck)) == 20, f"deck collision A: {len(set(a_deck))}"

_f = pl.col('faction').eq('Twigs')
fillers_b = pick(_f & (pl.col('mana').le(3)) & (~pl.col('card_id').is_in([D1, D2])), n=18)
b_deck = [D1, D2] + list(dict.fromkeys(fillers_b))[:18]
assert len(set(b_deck)) == 20, f"deck collision B: {len(set(b_deck))}"

p1 = PlayerState(name='A', deck=list(a_deck)); p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
gids = [gid]

conn, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']                       # A plays first -> its move sits on stopover_4
gs.earth = [['OC'] for _ in range(24)]           # off-home for Orcs (W5's faction) - deterministic advance
ga, gb = gs.players['A'], gs.players['B']
ga.hand = a_deck[:6]; ga.deck = list(a_deck[6:]); ga.mana, ga.discard, ga.dwelling, ga.pendings = [], [], None, []
# fillers FIRST in B's hand so its init mana placement burns them - D1/D2 stay available for the defend
gb.hand = list(b_deck[2:8]) + [b_deck[0], b_deck[1]]
gb.deck = list(dict.fromkeys([d for d in (b_deck[:6] + b_deck[8:]) if d not in set(gb.hand)]))
gb.mana, gb.discard, gb.dwelling, gb.pendings = [], [], None, []
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
conn.commit(); conn.close()
print(f"game {gid} created (A holds mover {W5} + decoy {SWAPC}, B opens with a 2-shield defend)")


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
        # B's scripted play phase: ONE 2-card defend on stopover_4 per turn (while it holds both), then pass
        if not b_defended[0] and D1 in (b.hand or []) and D2 in (b.hand or []):
            msg = {'cards': [D1, D2], 'to': 'stopover_4', 'mode': 'defend', 'pendings': []}
        else:
            msg = {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []}
        succ, why2 = send(gid, 'B', msg)
    else:
        return
    check("engine accepted the opponent's action", succ is True, f"(resp={why2!r} state={state})")


ai2 = PlayerAI(player_state=None)
st: dict = {}
sent_a = []
b_defended = [False]

# --- init phase: both place 3 mana cards (the robot does it itself via ai_decide)
for _ in range(6):
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

# --- guarantee the scenario regardless of what put_mana kept: A holds exactly W5 + SWAPC (3 mana budget)
conn, _, g = ge.get_current_game(gid)
ga = g.players['A']
ga.hand = [W5, SWAPC]
ga.mana = list(a_deck[2:5])                      # three filler tokens - the turn-1 budget
ga.deck = list(a_deck[5:])
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (g.to_json(), gid))
conn.commit(); conn.close()

# --- play phase: A is first. Robot plays W5, B declares its defend, robot answers with the decoy swap.
for _ in range(14):
    conn, _, g = ge.get_current_game(gid); state = g.state; winner = g.winner; conn.close()
    if state == 'game over' or winner:
        break
    drive_b()   # a no-op when it is not B's expected action (state must name (B))
    conn, _, g = ge.get_current_game(gid); state = g.state; conn.close()
    if "to play" in state and "(A)" in state:
        msg = ai_decide(g, 'A', st, ai2)
        conn.close()
        sent_a.append(dict(msg))
        succ, why2 = send(gid, 'A', msg)
        label = f"{msg.get('mode')}/{[c for c in (msg.get('cards') or [])]}" + \
                (f" swap_with={msg.get('swap_with')}" if msg.get('swap_with') else "")
        check(f"engine accepted the robot's action #{len(sent_a)} ({label})", succ is True, f"(resp={why2!r})")
    elif "to play" in state and "(B)" in state:
        drive_b()   # second chance after B acted (e.g. A passed -> back to B)

a_moves = [m for m in sent_a if m.get('mode') == 'move']
check("the robot played the strong mover first", a_moves and a_moves[0]['cards'][0] == W5, f"(got {sent_a})")
swap_plays = [m for m in sent_a if m.get('swap_with')]
check("then it played the swap decoy WITH 'swap_with' (decoy swap, E #31)",
      len(swap_plays) == 1 and swap_plays[0]['cards'][0] == SWAPC and swap_plays[0]['swap_with'] == 1,
      f"(got {sent_a})")

conn4, _, g4 = ge.get_current_game(gid)
log_txt = json.dumps(g4.log, ensure_ascii=False)
check("the engine logged the INSTANT swap at play time", 'swaps places with' in log_txt, f"(log={g4.log!r})")


def find_log_entry(obj, player, cards):
    if isinstance(obj, dict):
        if obj.get('player') == player and obj.get('mode') == 'move' and obj.get('cards') == list(cards):
            return obj
        for v in obj.values():
            r = find_log_entry(v, player, cards)
            if r is not None:
                return r
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            r = find_log_entry(v, player, cards)
            if r is not None:
                return r
    return None


e_w = find_log_entry(g4.log, 'A', [W5])
e_s = find_log_entry(g4.log, 'A', [SWAPC])
check("the turn log records both of A's plays", e_w is not None and e_s is not None)
if e_s is not None:
    check("the decoy S absorbed the block (shields 2 >= cost 1)",
          any('blocked' in str(n).lower() for n in (e_s.get('negatives') or [])), f"({e_s.get('negatives')!r})")
if e_w is not None:
    dpos = (e_w.get('pos_after') or 0) - (e_w.get('pos_before') or 0)
    fac_a = g4.players['A'].faction
    from player_ai.playerai import FACTION_BIOMES
    home = FACTION_BIOMES.get(fac_a) if fac_a else None
    expect_adv = ROWS[W5]['advancing'] + (1 if (home and g4.earth[e_w['pos_before']][0] in home) else 0)
    check("the rescued mover advanced unblocked at its new position", dpos == expect_adv,
          f"(delta {dpos} vs expected {expect_adv}, faction={fac_a})")

# cleanup
_c = sqlite3.connect(os.path.join(PROJECT_ROOT, 'games', 'games.db'))
for _gid in gids:
    _c.execute("DELETE FROM games WHERE game_id=?", (_gid,))
_c.commit(); _c.close()

print(f"\nALL {ok} CHECKS PASSED - section E reactive effects verified")
