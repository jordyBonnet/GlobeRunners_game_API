# Manual smoke test (section B): condition evaluation - PlayerAI._condition_met mirror of
# game_engine.is_condition_met.
#   * #19 (P0 fix): biome_* conditions are now evaluated from public board info (exact engine
#     mirror) instead of hard-coded False - the full 6-condition x home/non-home matrix, plus the
#     permissive default for unknown names and the no-board-info fallback. The mirror constants
#     (FACTION_BIOMES / BIOME_COND_FACTION) are asserted to stay in sync with the engine's.
#   * regression: every other branch of _condition_met (day/night, temp_* boundaries, mana_*,
#     cards_in_hand_*, dist_*, pending, drop_on_board, block marker, cataclysm trigger, unknown).
#   * #20: sequence awareness - update_player_state re-reads temperature/day_night from the FRESH
#     game object each tick (so an INSTANT Celestial/flux played earlier this turn is already in
#     effect when later cards are scored) and drop_on_board picks up drops placed this turn.
#   * #19 decision level: play_card now PREFERS a met biome_X card over an always-met no_condition
#     one on a home biome, and flips the preference off-biome - verified as unit AND through a real
#     game via ge.handle_websocket_message (the robot's first move is the biome card only because of the fix).
# Run from the project root:  uv run python tests/_ai_b_conditions.py
# NOTE: writes to games.db (like the other smoke tests); all games are deleted after.
import sys, io, os, sqlite3, json
from types import SimpleNamespace as NS
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState
from player_ai.playerai import (PlayerAI, FACTION_BIOMES, BIOME_COND_FACTION, validate_message)
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

# =====================================================================================
# Mirror-constants sync guard (project rule: the AI re-implements engine constants)
# =====================================================================================
print("\n--- Mirror sync ---")
check("FACTION_BIOMES matches the engine", FACTION_BIOMES == ge.FACTION_BIOMES, f"({FACTION_BIOMES} vs {ge.FACTION_BIOMES})")
check("BIOME_COND_FACTION matches the engine's BIOME_CONDITION_FACTION", BIOME_COND_FACTION == ge.BIOME_CONDITION_FACTION)

ai = PlayerAI(player_state=None)


def set_ai(pos=0, faction='Orcs', hand=None, mana_n=3, earth=None, temp=None, dn='day',
           pendings=None, oppo_pos=None, oppo_mana=None, oppo_hand=None):
    # earth: None -> uniform OC sea (a safe default), a plain str -> that code on all 24 cells,
    # a list/tuple -> the explicit per-cell codes. ('MO'*24) would be ONE string - never pass it.
    ai.player_state = PlayerState(name='A', current_position=pos, faction=faction,
                                  hand=list(hand or []), mana=['m'] * mana_n, mana_spend=0)
    if pendings is not None:
        ai.player_state.pendings = list(pendings)
    ai.oppo_position, ai.oppo_mana, ai.oppo_hand = oppo_pos, oppo_mana, oppo_hand
    if earth is None:
        ai.earth_biomes = ['OC'] * 24
    elif isinstance(earth, str):
        ai.earth_biomes = [earth] * 24     # a single biome code repeated over all cells
    else:
        ai.earth_biomes = list(earth)
    ai.temperature, ai.day_night, ai.day_night_fixed = temp, dn, False
    ai.cataclysm_pile = ['OC', 'MO', 'DE', 'JU']
    ai.drops_on_board, ai.board_drops, ai.nobodymoves_active, ai.rooted_on_board, ai.occupied_cells = False, [], False, [], set()
    ai.oppo_actions, ai.oppo_faction, ai.oppo_pending_slots = [], None, []
    return ai


# =====================================================================================
# #19 biome_* - the P0 fix (exact engine mirror on public board info)
# =====================================================================================
print("\n--- Unit: biome_* (#19) ---")
for cond, fac in BIOME_COND_FACTION.items():
    home = FACTION_BIOMES[fac]
    for b in home:
        set_ai(pos=0, earth=[b] + ['DE'] * 23)
        check(f"{cond}: met standing on {b} (a {fac} home biome)", ai._condition_met(cond) is True)
    non_home = [x for x in ('OC', 'MO', 'DE', 'JU') if x not in home]
    set_ai(pos=0, earth=[non_home[0]] + ['DE'] * 23)
    check(f"{cond}: NOT met standing on {non_home[0]} (not a {fac} biome)", ai._condition_met(cond) is False)

set_ai(pos=0)
check("unknown biome_* name -> permissive default MET (engine catch-all)", ai._condition_met('biome_Xx') is True)
set_ai(pos=0)
ai.earth_biomes = None      # no board info at all (synthetic state only - production always has it)
check("no board info -> conservatively not met", ai._condition_met('biome_Orc') is False)

# =====================================================================================
# Regression: every other branch of _condition_met
# =====================================================================================
print("\n--- Unit: remaining branches (regression) ---")
set_ai(pos=0, hand=['x'], temp=8, dn='day')
check("no_condition / cataclysm / block are always met",
      ai._condition_met('no_condition') is True and ai._condition_met('cataclysm') is True and ai._condition_met('block') is True)
check("day/night from the global phase", ai._condition_met('day') is True and ai._condition_met('night') is False)
set_ai(temp=8, dn='night')
check("night on night", ai._condition_met('night') is True)

set_ai(temp=6)
check("temp_inf_6 boundary: t == 6 -> NOT met (strict <)", ai._condition_met('temp_inf_6') is False)
set_ai(temp=5)
check("temp_inf_6: t = 5 -> met", ai._condition_met('temp_inf_6') is True)
set_ai(temp=9)
check("temp_sup_9 boundary: t == 9 -> NOT met (strict >)", ai._condition_met('temp_sup_9') is False)
set_ai(temp=10)
check("temp_sup_9: t = 10 -> met", ai._condition_met('temp_sup_9') is True)
set_ai(temp=None)
check("temperature unknown (not rolled yet) -> not met, like the engine", ai._condition_met('temp_inf_6') is False)

set_ai(mana_n=5)
check("own mana zone: 5 cards -> inf_6 met / sup_5 not", ai._condition_met('mana_inf_6') is True and ai._condition_met('mana_sup_5') is False)
set_ai(mana_n=7)
check("own mana zone: 7 cards -> inf_6 not / sup_5 met", ai._condition_met('mana_inf_6') is False and ai._condition_met('mana_sup_5') is True)
set_ai(oppo_mana=None)
check("opponent mana unknown (None) -> both oppo conditions not met",
      ai._condition_met('mana_inf_6_oppo') is False and ai._condition_met('mana_sup_5_oppo') is False)
set_ai(oppo_mana=2)
check("opponent mana zone: 2 cards -> inf_6 met / sup_5 not", ai._condition_met('mana_inf_6_oppo') is True and ai._condition_met('mana_sup_5_oppo') is False)

set_ai(hand=list('abc'))
check("own hand: 3 cards -> inf_4 met / sup_3 not", ai._condition_met('cards_in_hand_inf_4') is True and ai._condition_met('cards_in_hand_sup_3') is False)
set_ai(hand=list('abcde'))
check("own hand: 5 cards -> inf_4 not / sup_3 met", ai._condition_met('cards_in_hand_inf_4') is False and ai._condition_met('cards_in_hand_sup_3') is True)
set_ai(oppo_hand=None)
check("opponent hand unknown (None) -> both oppo conditions not met",
      ai._condition_met('cards_in_hand_inf_4_oppo') is False and ai._condition_met('cards_in_hand_sup_3_oppo') is False)

set_ai(pos=10, oppo_pos=3)
check("dist: I am 7 ahead -> dist_ahead_sup_3 met / dist_behind not",
      ai._condition_met('dist_ahead_sup_3') is True and ai._condition_met('dist_behind_sup_3') is False)
set_ai(pos=5, oppo_pos=20)
check("dist: opponent 15 ahead -> dist_behind_sup_9 met / dist_ahead not",
      ai._condition_met('dist_behind_sup_9') is True and ai._condition_met('dist_ahead_sup_9') is False)
set_ai(pos=5, oppo_pos=None)
check("opponent position unknown -> dist_* not met", ai._condition_met('dist_ahead_sup_1') is False)

set_ai(pendings=[])
check("pending: empty zone -> not met", ai._condition_met('pending') is False)
set_ai(pendings=['epo'])
check("pending: one card in my own zone -> met", ai._condition_met('pending') is True)

ai.drops_on_board = False
check("drop_on_board: no drops on the earth -> not met", ai._condition_met('drop_on_board') is False)
ai.drops_on_board = True
check("drop_on_board: a drop token exists -> met", ai._condition_met('drop_on_board') is True)

set_ai(pos=0)
check("unknown condition (legacy face_point_*) -> permissive default MET, like the engine",
      ai._condition_met('face_point_left') is True)

# =====================================================================================
# #20 sequence awareness: fresh re-reads each tick reflect INSTANT effects + new drops
# =====================================================================================
print("\n--- Unit: #20 tick re-reads ---")


def fake_game(**kw):
    base = dict(temperature=6, day_night='day', day_night_fixed=False, cataclysm_pile=['OC', 'MO', 'DE', 'JU'],
                earth=[['MO']] * 24, board_drops=[], drop_tokens={}, rooted_on_board=[], nobodymoves_active=False)
    base.update(kw)
    return NS(**base)


ps = PlayerState(name='A', current_position=0, faction='Orcs', hand=['x'], mana=['m'] * 3)
ai.update_player_state(ps, None, fake_game(temperature=6))
check("update_player_state reads the temperature from the game object", ai.temperature == 6 and ai._condition_met('temp_inf_11') is True)
ai.update_player_state(ps, None, fake_game(temperature=12))   # e.g. after MY OWN thermic_flux played earlier this turn (INSTANT at play time)
check("a FRESH tick re-reads the changed temperature -> later scoring sees it (#20)", ai.temperature == 12 and ai._condition_met('temp_sup_9') is True)
ai.update_player_state(ps, None, fake_game(temperature=12, day_night='night', day_night_fixed=True))   # e.g. after a Celestial_reversal
check("a FRESH tick re-reads the fixed phase -> later scoring sees it (#20)", ai.day_night == 'night' and ai._condition_met('night') is True)
ai.update_player_state(ps, None, fake_game(board_drops=[{'cell': 5, 'kind': 'boost', 'owner': 'A'}]))   # a drop I placed THIS turn
check("a drop placed this turn makes drop_on_board met at resolution (#20)", ai._condition_met('drop_on_board') is True)

# =====================================================================================
# #19 decision level: the scoring flip in play_card (on home biome vs off it)
# =====================================================================================
print("\n--- Unit: #19 decision-level flip ---")
BIOME_CARD = 'Orc21_4170a9'   # biome_Orc m2 adv6 taxation  (verified in pool)
NOCOND = 'Orc21_de4294'       # no_condition m2 adv4 taxation (always met, lower advance)
assert ROWS[BIOME_CARD]['condition'] == 'biome_Orc' and ROWS[BIOME_CARD]['advancing'] == 6
assert ROWS[NOCOND]['condition'] == 'no_condition' and ROWS[NOCOND]['advancing'] == 4

set_ai(pos=0, hand=[BIOME_CARD, NOCOND], earth=['MO'] * 24)   # MO = Orcs home biome -> biome_Orc MET
_msg = ai.play_card()
check("ON a home biome: play_card prefers the met biome card (adv 6 beats adv 4)",
      _msg is not None and _msg['cards'][0] == BIOME_CARD, f"(got {_msg})")
set_ai(pos=0, hand=[BIOME_CARD, NOCOND], earth=['DE'] * 24)   # DE is not an Orcs biome -> NOT met (would be 0-adv before the fix... scored unmet)
check("OFF a home biome: play_card falls back to the always-met no_condition card", ai.play_card()['cards'][0] == NOCOND, f"(got {ai.play_card()['cards']})")

# =====================================================================================
# E2E: through the real entry point - the robot's FIRST move is the met biome card
# =====================================================================================
print("\n--- E2E: the fix changes a real game decision ---")
a_deck = [BIOME_CARD, NOCOND] + pick((pl.col('faction') == 'Orcs') & (pl.col('mana') == 4), n=18)   # single-faction Orcs deck
b_deck = pick((pl.col('faction') == 'Twigs') & (pl.col('mana') >= 4), n=20)
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
ga.current_position, gb.current_position = 0, 9
for i in range(24):                                    # MO everywhere: an Orcs HOME biome (biome_Orc met)
    gs.earth[i][0] = 'MO'
gs.board_drops, gs.rooted_on_board, gs.nobodymoves_active = [], [], False
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
conn.commit(); conn.close()
print(f"game {gid} created (A at 0 on a home biome with the biome card in hand)")


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

for _ in range(4):   # init (B first, then the robot sacrifices its 3 fillers - both mains keep-value 16 > filler 14)
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
check("init: both mains kept in hand (biome card not sacrificed)",
      BIOME_CARD in ga.hand and NOCOND in ga.hand and len(ga.mana) == 3, f"(hand={ga.hand})")

for _ in range(8):   # turn 1 - stop once the robot has declared a move
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

moves = [m for m in sent if m.get('mode') == 'move']
check("the robot's first move of turn 1 is the MET biome_Orc card (only possible because of the #19 fix)",
      len(moves) >= 1 and moves[0]['cards'][0] == BIOME_CARD, f"(sent={[(m['mode'], m.get('to'), m['cards']) for m in sent]})")

conn, _, g = ge.get_current_game(gid); ga = g.players['A']; conn.close()
check("the biome card resolved with its condition met (full advance + home-biome bonus: 6 + 1 from cell 0)",
      ga.current_position == 7, f"(pos={ga.current_position})")

# --- cleanup ---------------------------------------------------------------------------
conn = sqlite3.connect(ge.DB_PATH)
for g_ in gids:
    conn.execute("DELETE FROM games WHERE game_id = ?", (g_,))
conn.commit(); conn.close()

print(f"\nALL {ok} CHECKS PASSED - section B condition evaluation verified")
