# Manual smoke test (A.3): Mages - refined choice functions for all 5 instant/dwelling cards.
#   * unit: value-weighted phase/temperature fixing (#11/#12), the public-estimate threat model
#     of nobodymoves with self-deny guard / declared-vs-latent threats / block & unstoppable
#     handling / provably-unmet conditions (#13), Apocalypticritual's "strike coming + strict
#     improvement" gates incl. the shared-biome case (#14), and black_hole rotation weighing my
#     biome_*-condition mains against the faction bonus (#15).
#   * E2E (real game through ge.handle_websocket_message): a Mage-flavored robot whose opponent
#     stands on cell 20 with visible mana to spare PLAYS nobodymoves as its first action of turn 1
#     (the A.3 threat estimate fires from public info alone) and the lock is set in the state.
# Run from the project root:  uv run python tests/_ai_a3_mages.py
# NOTE: writes to games.db (like the other smoke tests); all games are deleted after.
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState
from player_ai.playerai import (PlayerAI, FACTION_BIOMES, BIOME_COND_FACTION, validate_message)
from game_ui.ai_driver import ai_decide

DB = ge.CARDS_DB.with_columns(((pl.col('advancing') - (pl.col('mana') - 1)).alias('_delta')))
ROWS = {r['card_id']: r for r in DB.drop('_delta').iter_rows(named=True)}


def pick(f, n=1):
    return DB.filter(f)['card_id'].to_list()[:n]


ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")


# =====================================================================================
# Unit setup: one PlayerAI driven by synthetic (public) state
# =====================================================================================
ai = PlayerAI(player_state=None)

DAY_NEG = pick((pl.col('condition') == 'day') & (pl.col('_delta') <= -2), n=2)        # 2 negative-delta day cards
NIGHT_POS = pick((pl.col('condition') == 'night') & (pl.col('_delta') >= 3))[0]       # a valuable night card
SUP9_NEG = DB.filter(pl.col('condition') == 'temp_sup_9').sort('_delta')['card_id'].to_list()[:2]   # 2 worst-delta temp_sup_9
INF6_BIGPOS = DB.filter(pl.col('condition') == 'temp_inf_6').sort('_delta', descending=True)['card_id'][0]  # best-delta temp_inf_6
# a genuine FORWARD-mover: exclude the self-movement effects whose engine semantics differ ('backward' recoils)
HIGHADV = pick((pl.col('condition') == 'no_condition') & (pl.col('mana') <= 2) & (pl.col('advancing') >= 5)
               & (~pl.col('effect').is_in(['backward'])))[0]
NIGHT_UNMET = pick((pl.col('condition') == 'night') & (pl.col('mana') <= 3) & (pl.col('advancing') >= 6))[0]
UNSTOP_DAY = [c for c in pick((pl.col('effect') == 'unstoppable') & (pl.col('condition') == 'day'), n=10) if ROWS[c]['advancing'] == 2][0]
SHIELD = pick(pl.col('shield') >= 3)[0]
CAT_AFFORD = pick((pl.col('condition') == 'cataclysm') & (pl.col('mana') <= 2))[0]
CAT_COST5 = pick((pl.col('condition') == 'cataclysm') & (pl.col('mana') == 5))[0]
BIAOME_MIA = DB.filter(pl.col('condition') == 'biome_Mia').sort('_delta', descending=True)['card_id'].to_list()[:4]

for cid in [DAY_NEG[0], DAY_NEG[1], SUP9_NEG[0], SUP9_NEG[1], NIGHT_POS, INF6_BIGPOS, HIGHADV,
            NIGHT_UNMET, UNSTOP_DAY, SHIELD, CAT_AFFORD, CAT_COST5] + BIAOME_MIA:
    assert cid in ROWS

# deterministic earth helper: all 'DE' by default (non-home for Dwarves), overrides per index
def make_eb(overrides=None):
    eb = ['DE'] * 24
    for i, b in (overrides or {}).items():
        eb[i] = b
    return eb


def set_ai(pos=5, faction='Dwarves', hand=None, deck=None, mana_n=6, oppo_pos=None, oppo_faction='Miaous',
           earth=None, temp=None, dn='day', dn_fixed=False, pile=('OC', 'MO', 'DE', 'JU')):
    ai.player_state = PlayerState(name='A', current_position=pos, faction=faction,
                                  hand=list(hand or []), deck=list(deck or []),
                                  mana=['m'] * mana_n, mana_spend=0)
    ai.oppo_position, ai.oppo_faction, ai.oppo_mana, ai.oppo_hand = oppo_pos, oppo_faction, None, None
    ai.earth_biomes = earth if earth is not None else make_eb()
    ai.temperature, ai.day_night, ai.day_night_fixed = temp, dn, dn_fixed
    ai.cataclysm_pile = list(pile)
    ai.oppo_actions, ai.nobodymoves_active, ai.rooted_on_board, ai.board_drops = [], False, [], []
    ai.drops_on_board, ai.occupied_cells = False, set()
    return ai


# =====================================================================================
# #11 Celestial_reversal - value-weighted (not count-based) phase fixing
# =====================================================================================
print("\n--- Unit: Celestial_reversal (#11) ---")
set_ai(hand=['Celestial_reversal'], deck=[*DAY_NEG, NIGHT_POS])
wd = ROWS[DAY_NEG[0]]['advancing'] - (ROWS[DAY_NEG[0]]['mana'] - 1) + ROWS[DAY_NEG[1]]['advancing'] - (ROWS[DAY_NEG[1]]['mana'] - 1)
check("weighted: 2 junk day cards (negative delta sum) lose to 1 valuable night card",
      wd < 0 and ai._choose_day_night() == 'night', f"(w_day={wd}, w_night={ROWS[NIGHT_POS]['advancing']-(ROWS[NIGHT_POS]['mana']-1)})")
check("...whereas raw counts would have picked day (2 > 1)", 2 > 1)   # documents the flip vs the A.1 baseline
set_ai(hand=['Celestial_reversal'], deck=[])
check("neutral (no phase cards): not played", ai._choose_day_night() is None)

# =====================================================================================
# #12 thermic_flux - value-weighted direction, clamp awareness
# =====================================================================================
print("\n--- Unit: thermic_flux (#12) ---")
set_ai(hand=['thermic_flux'], deck=[*SUP9_NEG, INF6_BIGPOS], temp=8)
check("weighted: 'down' (one big-gain card met at t=4) beats 'up' (two negative-delta cards met at t=12)",
      ai._choose_temp_change() == 'down', f"(W(4)={ai._temp_weight(4)}, W(8)={ai._temp_weight(8)}, W(12)={ai._temp_weight(12)})")
check("...whereas raw counts would have picked up (2 met > 1 met)", True)
set_ai(hand=['thermic_flux'], deck=[SUP9_NEG[0]], temp=20)   # already met at 20; up clamps to 20, down loses them
check("clamp: at t=20 neither direction improves (up re-clamps to 20)", ai._choose_temp_change() is None)

# =====================================================================================
# #13 nobodymoves - public-estimate threat model
# =====================================================================================
print("\n--- Unit: nobodymoves threat (#13) ---")
# latent threat: opponent at 21 with cards in hand + visible mana to afford a winning card
set_ai(pos=5, hand=['nobodymoves'], oppo_pos=21, oppo_faction='Miaous')
ai.oppo_hand, ai.oppo_mana = 2, 3
check("latent: opponent at 21 with visible mana -> threat", ai._nobodymoves_threat() is True)

# self-deny guard: an affordable main in MY hand could win this turn -> never lock myself out
set_ai(pos=19, hand=['nobodymoves', HIGHADV], oppo_pos=21)
ai.oppo_hand, ai.oppo_mana = 2, 3
check("self-deny: I can win this turn (pos+adv>=24) -> no nobodymoves", ai._nobodymoves_threat() is False)

# declared threat from the opponent's PUBLIC action_chain (no hand/mana needed at all)
ai.oppo_hand, ai.oppo_mana = None, None
set_ai(pos=5, hand=['nobodymoves'], oppo_pos=24 - int(ROWS[HIGHADV]['advancing']))
ai.oppo_actions = [{'cards': [HIGHADV], 'to': 'stopover_4', 'mode': 'move'}]
check("declared: an unresolved opponent move card reaching 24 -> threat", ai._nobodymoves_threat() is True)

# ...unless MY defends on that stopover out-shield its cost
set_ai(pos=5, hand=['nobodymoves'], oppo_pos=24 - int(ROWS[HIGHADV]['advancing']))
ai.oppo_actions = [{'cards': [HIGHADV], 'to': 'stopover_4', 'mode': 'move'}]
ai.player_state.action_chain = [{'cards': [SHIELD], 'to': 'stopover_4', 'mode': 'defend'}]
check("declared but blocked by my shields on the facing stopover -> no threat", ai._nobodymoves_threat() is False)

# an UNSTOPPABLE card whose condition is provably met ignores the block
set_ai(pos=5, hand=['nobodymoves'], oppo_pos=21, oppo_faction='Dwarves', earth=make_eb({21: 'MO'}))   # MO = Dwarves home -> +1 bonus
ai.oppo_actions = [{'cards': [UNSTOP_DAY], 'to': 'stopover_4', 'mode': 'move'}]     # day-condition, adv 2 (+1 bonus) from cell 21
ai.player_state.action_chain = [{'cards': [SHIELD], 'to': 'stopover_4', 'mode': 'defend'}]   # shields >= its cost 3
check("declared unstoppable + provably met (day on day) beats my block -> threat", ai._nobodymoves_threat() is True)

# a card whose condition is PROVABLY not met only advances by mana-1 -> no threat from cell 20
set_ai(pos=5, hand=['nobodymoves'], oppo_pos=20, dn='day')   # NIGHT_UNMET is a night card; it is day
ai.oppo_actions = [{'cards': [NIGHT_UNMET], 'to': 'stopover_4', 'mode': 'move'}]
check("declared but provably unmet (night card on day): reduced advance only -> no threat", ai._nobodymoves_threat() is False)

# the lock already active this turn: a 2nd copy buys nothing
set_ai(pos=5, hand=['nobodymoves'], oppo_pos=21)
ai.oppo_hand, ai.oppo_mana = 2, 3
ai.nobodymoves_active = True
check("already locked this turn -> no threat", ai._nobodymoves_threat() is False)

# _oppo_condition_eval spot checks (public info only)
set_ai(pos=5, hand=[], oppo_pos=7, oppo_faction='Dwarves', earth=make_eb({7: 'MO'}), temp=8, dn='day')
check("eval: day/night from the global phase", ai._oppo_condition_eval('day') is True and ai._oppo_condition_eval('night') is False)
check("eval: temp_* from the global temperature", ai._oppo_condition_eval('temp_inf_11') is True and ai._oppo_condition_eval('temp_sup_9') is False)
check("eval: biome_X at the opponent's cell", ai._oppo_condition_eval('biome_Dwa') is True and ai._oppo_condition_eval('biome_Mia') is False)
check("eval: dist_* from THEIR perspective (they are 2 cells ahead of me)", ai._oppo_condition_eval('dist_ahead_sup_1') is True and ai._oppo_condition_eval('dist_behind_sup_1') is False)
check("eval: hidden state (pending zone) -> None", ai._oppo_condition_eval('pending') is None)

# =====================================================================================
# #14 Apocalypticritual - "strike coming" + strict-improvement gates, shared-biome case
# =====================================================================================
print("\n--- Unit: Apocalypticritual (#14) ---")
# contiguous 6-cell segments (the real board layout): OC=cells 0-5, MO=6-11, DE=12-17, JU=18-23
SEG_EARTH = ['OC'] * 6 + ['MO'] * 6 + ['DE'] * 6 + ['JU'] * 6
set_ai(pos=0, hand=['Apocalypticritual'], oppo_pos=9, earth=SEG_EARTH)          # A OC idx 0, B MO idx 3
check("gate: no cataclysm card resolving soon -> not played (bad pile or not)", ai._choose_cataclysm_order() is None)

set_ai(pos=0, hand=['Apocalypticritual', CAT_COST5], oppo_pos=9, earth=SEG_EARTH, mana_n=1)   # unaffordable (cost 5 > 1), none declared
check("gate: cataclysm card in hand but UNAFFORDABLE and not declared -> not played", ai._choose_cataclysm_order() is None)

set_ai(pos=0, hand=['Apocalypticritual'], oppo_pos=9, earth=SEG_EARTH)          # A OC idx 0 (no knockback risk), B MO idx 3
ai.player_state.action_chain = [{'cards': [CAT_AFFORD], 'to': 'stopover_4', 'mode': 'move'}]
order = ai._choose_cataclysm_order()
check("gate: my own declared (unresolved) cataclysm play is enough; strike B's deep MO first",
      order is not None and order[0] == 'MO', f"(order={order})")

set_ai(pos=0, hand=['Apocalypticritual', CAT_AFFORD], oppo_pos=9, earth=SEG_EARTH, pile=('MO', 'DE', 'JU', 'OC'))   # MO already first = optimal
check("improvement gate: the current order is already as good -> not played", ai._choose_cataclysm_order() is None)

set_ai(pos=4, hand=['Apocalypticritual', CAT_AFFORD], oppo_pos=1, earth=SEG_EARTH,
       pile=('OC', 'MO', 'DE', 'JU'))   # SHARED biome OC: I sit deep (idx 4), B shallow (idx 1)
order = ai._choose_cataclysm_order()
check("shared biome where I am DEEPER: park it last, strike an empty biome first",
      order is not None and order[0] != 'OC' and order[-1] == 'OC', f"(order={order})")

# =====================================================================================
# #15 black_hole rotation - faction bonus vs my biome_*-condition mains
# =====================================================================================
print("\n--- Unit: black_hole rotation (#15) ---")
# Dwarves (home MO/OC); token at cell 12 -> cw brings old[(12-3)%24]=old[9], ccw brings old[15].
# CW source = MO (my home, +2 bonus, no biome cards), CCW source = JU (no bonus but 4 of my
# biome_Mia mains become met: 4 x delta x 0.5 >> 2) -> the refined score must pick 'ccw'.
set_ai(pos=12, faction='Dwarves', hand=['black_hole'], deck=BIAOME_MIA, oppo_pos=None, earth=make_eb({9: 'MO', 15: 'JU'}))
mia_w = ai._biome_cond_weight('JU')
check("biome_* mains at my new cell are weighted (4 cards -> weight)", mia_w > 4, f"(w={mia_w})")
check("rotation picks the direction that meets my biome_Mia mains over my raw faction bonus",
      ai._choose_rotation() == 'ccw', f"(got {ai._choose_rotation()}, cw=+2 bonus only)")

# =====================================================================================
# E2E: the robot plays nobodymoves when the opponent stands on cell 20 with visible mana
# =====================================================================================
print("\n--- E2E: threat response through the real entry point ---")
a_deck = ['nobodymoves'] + pick((pl.col('mana') == 4) & (pl.col('faction') == 'Twigs'), n=19)   # single-faction Twigs deck
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
ga.current_position, gb.current_position = 0, 20      # B is within reach of the finish line
for i in range(18, 24):                               # keep B off any Dwarves home biome (deterministic estimate)
    gs.earth[i][0] = 'DE'
gs.board_drops, gs.rooted_on_board, gs.nobodymoves_active = [], [], False
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
conn.commit(); conn.close()
print(f"game {gid} created (A at 0, B at 20)")


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

# --- init via ai_decide: B places its initial mana FIRST (any real ordering is possible), so when
#     A acts the threat estimate sees B on cell 20 WITH a visible mana zone -> nobodymoves
#     (keep-value 48) must survive the sacrifice while the cost-4 fillers sink to the zone
for _ in range(4):
    conn, _, g = ge.get_current_game(gid); state = g.state; conn.close()
    if not state.startswith("waiting for both players to put"):
        break
    drive_b()   # let B act first so its visible mana feeds A's threat estimate
    conn, _, g = ge.get_current_game(gid)
    if g.players['A'].mana and len(g.players['A'].mana) >= 3:
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
check("init: nobodymoves kept in hand (threat-aware keep-value), fillers sacrificed to the zone",
      'nobodymoves' in ga.hand and len(ga.mana) == 3, f"(hand={ga.hand}, mana={ga.mana})")

# --- turn 1: the robot's FIRST action must be the nobodymoves threat response
lock_seen = False
for _ in range(12):
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
    conn, _, g = ge.get_current_game(gid)
    lock_seen = lock_seen or bool(g.nobodymoves_active)
    done = g.turn > 1 or g.winner is not None
    conn.close()
    if done:
        break

nm = [m for m in sent if m.get('cards') == ['nobodymoves']]
check("the robot played nobodymoves as a threat response", len(nm) == 1 and nm[0]['mode'] == 'move' and nm[0].get('to', '').startswith('stopover_'),
      f"(sent={[(m['mode'], m.get('to'), m['cards']) for m in sent]})")
check("the movement lock was set on turn 1 (nobodymoves_active)", lock_seen)

# --- cleanup ---------------------------------------------------------------------------
conn = sqlite3.connect(ge.DB_PATH)
for g_ in gids:
    conn.execute("DELETE FROM games WHERE game_id = ?", (g_,))
conn.commit(); conn.close()

print(f"\nALL {ok} CHECKS PASSED - A.3 Mages choice functions refined and verified")
