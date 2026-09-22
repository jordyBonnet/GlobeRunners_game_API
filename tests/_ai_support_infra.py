# Manual smoke test (A.1): support-faction infrastructure for the Robot AI.
# Verifies that PlayerAI/ai_decide can now LEGALLY play every implemented support card:
#   * unit: message builders produce engine-valid shapes (cell / day_night / temp_change /
#     cataclysm_order / rotation / to:'dwelling' / to:'pending_zone'), validate_message
#     accepts them and rejects malformed ones, put_mana no longer burns playable support,
#     baseline choice functions behave deterministically.
#   * E2E (through the REAL ge.handle_websocket_message entry point, like ai_driver):
#     Game 1 - Engineers: dwelling PLACE + TAP (exactly once per turn) + drop with 'cell',
#               init sacrifice order (support stays in hand), no engine rejection anywhere.
#     Game 2 - Doctors: laboratory placement + tap (+epo pending) + pending-zone placement.
#     Game 3 - regression: a mains-only hand still plays normal move cards via play_card.
# Run from the project root:  uv run python tests/_ai_support_infra.py
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


def pick_main(cond, mana=None, n=1, adv_min=None):
    """Deterministically pick n main-pool card_ids with the given condition (and optional mana / min advancing)."""
    sub = DB.filter(pl.col('condition') == cond)
    if mana is not None:
        sub = sub.filter(pl.col('mana') == mana)
    if adv_min is not None:
        sub = sub.filter(pl.col('advancing') >= adv_min)
    return sub['card_id'].to_list()[:n]


def pad_mana(gid, name, n):
    """Test-only: top up the mana zone with REAL deck cards (the engine only counts len(mana),
    but card conservation must stay exact)."""
    conn, _, gs = ge.get_current_game(gid)
    p = gs.players[name]
    take = (p.deck or [])[:n]
    p.deck = list(p.deck[n:])
    p.mana = list(p.mana or []) + take
    set_state(gid, gs)
    conn.close()
    return take


ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")


def send(gid, name, message):
    """Drive the engine exactly like the WS layer / AI driver do (model_copy)."""
    conn, _, gs = ge.get_current_game(gid)
    player = gs.players[name].model_copy()
    player.message = message
    conn.close()
    resp = ge.handle_websocket_message(gid, player)
    if isinstance(resp, str):
        resp = json.loads(resp)
    info = (resp or {}).get('message', {}) if isinstance(resp, dict) else {}
    return info.get('success'), info.get('message')


def set_state(gid, gs):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
    conn.commit(); conn.close()


def all_cards(gs, name):
    """Every card of the player across ALL zones: hand/mana/deck/discard + pending zone
    + the dwelling zone (a placed dwelling like refinery/laboratory/black_hole lives there)."""
    p = gs.players[name]
    out = [c for z in (p.hand or [], p.mana or [], p.deck or [], p.discard or []) for c in z] \
          + list(p.pendings or [])
    if getattr(p, 'dwelling', None):
        out.append(p.dwelling)
    return out


gids = []

# =====================================================================================
# Game 1 - ENGINEERS: dwelling place + tap + drop('cell') through the real entry point
# =====================================================================================
print("\n--- Game 1: engineers (dwelling place/tap, engineer drop) ---")
m5a, m5b, m5c = pick_main('no_condition', mana=5, n=3)
m5d, m5e = pick_main('cataclysm', mana=5, n=2)   # expensive mains the robot can't afford in T1
rest_g1 = [x for x in pick_main('no_condition', n=20) if x not in {m5a, m5b, m5c}][:13]
a_deck = ['refinery', 'boost'] + [m5a, m5b, m5c, m5d, m5e] + rest_g1
assert len(a_deck) == 20 and len(set(a_deck)) == 20, (len(a_deck), len(set(a_deck)))
b_deck = pick_main('no_condition', mana=1, n=6) + pick_main('no_condition', n=14)

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
gids.append(gid)
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

conn, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
ga, gb = gs.players['A'], gs.players['B']
# deterministic hands: A holds the 2 support cards + expensive mains (unaffordable while T1 spend>0)
ga.hand = ['refinery', 'boost', m5a, m5b, m5c]
ga.deck = [x for x in a_deck if x not in ga.hand]
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.hand = b_deck[:6]; gb.deck = list(b_deck[6:]); gb.mana, gb.discard, gb.dwelling = [], [], None
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
conn.commit(); conn.close()
print(f"game {gid} created")

ai = PlayerAI(player_state=None)
st: dict = {}
sent = []   # (player, message) - to audit what the robot actually submitted


def drive_robot(gid):
    """One ai_decide tick for A + submit (exactly like run_ai_loop). Returns the msg or None."""
    conn, _, gs = ge.get_current_game(gid); conn.close()
    if gs.state == "game over" or gs.winner:
        return None
    msg = ai_decide(gs, 'A', st, ai)
    if msg is None:
        return None
    okv, why = validate_message(msg, dwelling=gs.players['A'].dwelling)   # same gate as run_ai_loop
    check(f"local validation passes ({msg.get('mode') or msg.get('to')} / {msg.get('cards')})", okv, f"({why})")
    succ, why2 = send(gid, 'A', msg)
    sent.append(('A', dict(msg)))
    check("engine accepted the robot's action", succ is True, f"(resp={why2!r} msg={msg})")
    return msg


def drive_b(gid):
    """Scripted opponent: mandatory mana in init/mana phase, pass in the play phase."""
    conn, _, gs = ge.get_current_game(gid); conn.close()
    state = gs.state
    b = gs.players['B']
    if state.startswith("waiting for both players to put"):
        cards = (b.hand or [])[:3]
        succ, why2 = send(gid, 'B', {'cards': cards, 'to': 'mana', 'mode': '', 'pendings': []})
    elif state == "waiting for both players to mana or pass":
        if b.hand:
            succ, why2 = send(gid, 'B', {'cards': [b.hand[0]], 'to': 'mana', 'mode': '', 'pendings': []})
        else:
            succ, why2 = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    elif "to play" in state and "(B)" in state:
        succ, why2 = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    else:
        return   # not B's turn (or a pause) - nothing to do this tick
    sent.append(('B', None))
    check("engine accepted the opponent's action", succ is True, f"(resp={why2!r} state={state})")


# --- init: both put 3 in mana. A's sacrifice must be the 3 mains (support stays in hand) -
#     refinery(keep 50)/boost(25) beat every main (18-mana=13). The robot decides its own
#     sacrifice via ai_decide; B is scripted.
for _ in range(4):   # alternate until init is done
    conn, _, gs = ge.get_current_game(gid); state = gs.state; conn.close()
    if not state.startswith("waiting for both players to put"):
        break
    drive_robot(gid)
    drive_b(gid)

conn, _, gs = ge.get_current_game(gid); ga0 = gs.players['A']; conn.close()
check("init: A's 3 mains went to mana (support KEPT)",
      set(ga0.mana or []) == {m5a, m5b, m5c}, f"(mana={ga0.mana})")
check("init: support cards stayed in hand", ('refinery' in ga0.hand) and ('boost' in ga0.hand),
      f"(hand={ga0.hand})")

# --- turn 1 play phase (test-only: top up the mana pool so both the dwelling AND the drop fit) -
pad_mana(gid, 'A', 3)   # 6 available mana

taps_t1 = 0
for _ in range(40):   # bounded loop until turn 2 starts (resolution ran)
    conn, _, gs = ge.get_current_game(gid); state = gs.state; conn.close()
    if "turn 2" in state:
        break
    msg = drive_robot(gid)
    if msg is not None and msg.get('mode') == 'dwelling_activation':
        taps_t1 += 1
    drive_b(gid)

conn, _, gs = ge.get_current_game(gid)
ga, gb2 = gs.players['A'], gs.players['B']
check("turn 1 completed (resolution + cleaning ran)", "turn 2" in gs.state or gs.turn == 2, f"(state={gs.state})")
check("refinery PLACED on the board via ai_decide", ga.dwelling == 'refinery', f"(dwelling={ga.dwelling})")
check("exactly ONE dwelling tap in turn 1 (tapped_this_turn bookkeeping)", taps_t1 == 1, f"(taps={taps_t1})")
# the boost was PLACED with a valid cell (and may already have been CONSUMED by A's own
# advancing step passing over it during resolution - that is correct engine behavior)
boost_msgs = [d for who, d in sent if who == 'A' and d.get('cards') == ['boost']]
drops = gs.board_drops or []
check("boost drop PLACED with a valid 'cell' ahead of A",
      len(boost_msgs) == 1 and isinstance(boost_msgs[0].get('cell'), int)
      and 1 <= boost_msgs[0]['cell'] <= 6, f"(msg={boost_msgs}, board_drops_after_res={drops})")
check("card conservation (every card of A's deck exactly once)",
      sorted(all_cards(gs, 'A')) == sorted(a_deck), f"({len(all_cards(gs, 'A'))}/{len(a_deck)})")

# --- unit checks on the AI state derived from this real game --------------------------
conn, _, gs = ge.get_current_game(gid)
ai.update_player_state(gs.players['A'], gs.players['B'], gs)
check("update_player_state: earth biomes captured (24 cells)",
      len(ai.earth_biomes) == 24 and all(b in ('OC', 'MO', 'DE', 'JU') for b in ai.earth_biomes))

# --- unit: the other support cards' message builders (shapes + validate_message) ------
print("\n--- Unit: message builders for every support card type ---")
# the E2E game placed A's refinery - reset for the synthetic builder tests below
ai.player_state.dwelling = None
ai.player_state.dwelling_tapped = False

ai.player_state.hand = ['laboratory', 'epo', 'Celestial_reversal', 'thermic_flux', 'Apocalypticritual',
                        'nobodymoves', 'trampoline', 'gluetrap', 'landmine', 'black_hole']
ai.player_state.mana, ai.player_state.mana_spend = ['m'] * 6, 0

msg = ai.build_support_message('laboratory')
check("laboratory place: to='dwelling'", msg and msg['to'] == 'dwelling' and msg['cards'] == ['laboratory'])
check("validate: laboratory place ok", validate_message(msg)[0] is True)

ai.player_state.dwelling = 'laboratory'   # zone full -> the 2nd dwelling copy must not be built
check("2nd dwelling copy: builder returns None (zone full)", ai.build_support_message('black_hole') is None)
ai.player_state.dwelling = None

msg = ai.build_support_message('epo')
check("epo pending place: to='pending_zone'", msg and msg['to'] == 'pending_zone' and msg['mode'] == '')
check("validate: epo pending ok", validate_message(msg)[0] is True)

# Celestial_reversal: craft a deck that prefers day (2 day-cards, 0 night) -> must choose 'day'
ai.player_state.hand = ['Celestial_reversal', *pick_main('day', n=2)]
ai.player_state.deck = []
ai.day_night, ai.day_night_fixed = 'night', False
msg = ai.build_support_message('Celestial_reversal')
check("Celestial_reversal: chooses the preferred phase with day_night field",
      msg and msg.get('day_night') == 'day' and msg['mode'] == 'move', f"(msg={msg})")
check("validate: celestial ok", validate_message(msg)[0] is True)
ai.day_night, ai.day_night_fixed = 'day', True   # already fixed to the preference -> dead card
check("Celestial_reversal: not played when day/night already fixed to my preference",
      ai.build_support_message('Celestial_reversal') is None)

# thermic_flux: craft temp_* cards so that going UP helps (temp_sup_9 cards, current temp 6 -> 10 met)
ai.player_state.hand = ['thermic_flux', *pick_main('temp_sup_9', n=3)]
ai.player_state.deck = []
ai.temperature = 6
check("thermic_flux: picks the improving direction", ai._choose_temp_change() == 'up')
msg = ai.build_support_message('thermic_flux')
check("validate: thermic_flux ok (temp_change field)", msg and msg.get('temp_change') == 'up'
      and validate_message(msg)[0] is True)

# Apocalypticritual: needs a cataclysm card in hand; opponent biome first, mine last
ai.player_state.hand = ['Apocalypticritual', pick_main('cataclysm')[0]]
ai.earth_biomes = ['OC'] * 6 + ['MO'] * 6 + ['DE'] * 6 + ['JU'] * 6   # A on OC(0), B on MO(7)
ai.player_state.current_position, ai.oppo_position = 0, 7
ai.cataclysm_pile = ['OC', 'MO', 'DE', 'JU']
order = ai._choose_cataclysm_order()
# A sits at OC idx 0 (segment start): an OC strike costs it zero cells, so ONLY the
# opponent's deep MO slot is value-relevant - MO must strike first; the rest is neutral.
check("Apocalypticritual: oppo biome first", order and order[0] == 'MO'
      and sorted(order) == ['DE', 'JU', 'MO', 'OC'], f"(order={order})")
msg = ai.build_support_message('Apocalypticritual')
check("validate: ritual ok (cataclysm_order field)", msg and msg.get('cataclysm_order') == order
      and validate_message(msg)[0] is True)
ai.player_state.hand = ['Apocalypticritual']   # no cataclysm card -> not worth playing
check("Apocalypticritual: skipped without a cataclysm card in hand", ai.build_support_message('Apocalypticritual') is None)

# nobodymoves (A.3): public-estimate threat - the opponent can still ACT this turn and afford a winning card
ai.player_state.hand.append('nobodymoves')
ai.oppo_hand, ai.oppo_mana = 2, 3   # cards left in hand + visible mana to spend this turn (public counts)
ai.player_state.current_position, ai.oppo_position = 5, 21
check("nobodymoves: threat detected (oppo at 21 can still afford a winning card)", ai._nobodymoves_threat() is True)
msg = ai.build_support_message('nobodymoves')
check("validate: nobodymoves ok (no choice field needed)", msg and msg['mode'] == 'move'
      and validate_message(msg)[0] is True)
ai.player_state.current_position, ai.oppo_position = 21, 5
check("nobodymoves: no threat when I am ahead", ai._nobodymoves_threat() is False)

# black_hole tap: rotation field required + direction favors my faction biome bonus
ga_test = PlayerState(name='A', current_position=0, faction='Dwarves')   # Dwarves home: MO/OC
ai.player_state = ga_test
ai.oppo_faction, ai.oppo_position = None, None
# after CW the biome on cell 0 is old[(0-3)%24]=old[21]; put MO there (home) and JU at old[3] (ccw source)
eb = ['DE'] * 24; eb[21] = 'MO'; eb[3] = 'JU'
ai.earth_biomes = eb
check("black_hole rotation: picks the direction that puts my token on a home biome",
      ai._choose_rotation() == 'cw', f"(got {ai._choose_rotation()})")
ga_test.dwelling, ga_test.dwelling_tapped = 'black_hole', False
msg = ai.tap_dwelling()
check("black_hole tap: mode='dwelling_activation' + rotation field", msg and msg['mode'] == 'dwelling_activation'
      and msg.get('rotation') in ('cw', 'ccw') and validate_message(msg, dwelling='black_hole')[0] is True)
ga_test.dwelling = 'refinery'
msg = ai.tap_dwelling()
check("refinery tap: valid WITHOUT rotation", msg and 'rotation' not in msg
      and validate_message(msg, dwelling='refinery')[0] is True)

# --- unit: validate_message rejects malformed messages ---------------------------------
print("\n--- Unit: validate_message rejection cases ---")
base = {'cards': ['boost'], 'to': 'stopover_2', 'mode': 'move', 'pendings': []}
check("reject: engineer drop without cell", validate_message(dict(base))[0] is False)
check("accept: engineer drop with cell", validate_message({**base, 'cell': 7})[0] is True)
check("reject: celestial without day_night",
      validate_message({'cards': ['Celestial_reversal'], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})[0] is False)
check("reject: ritual with a non-permutation order",
      validate_message({'cards': ['Apocalypticritual'], 'to': 'stopover_4', 'mode': 'move', 'pendings': [],
                        'cataclysm_order': ['OC', 'OC', 'MO', 'DE']})[0] is False)
check("reject: black_hole tap without rotation",
      validate_message({'cards': [], 'to': 'dwelling', 'mode': 'dwelling_activation', 'pendings': []},
                       dwelling='black_hole')[0] is False)
check("accept: pass message", validate_message({'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})[0] is True)
check("reject: pending zone with 2 cards",
      validate_message({'cards': ['epo', 'virus'], 'to': 'pending_zone', 'mode': '', 'pendings': []})[0] is False)

# =====================================================================================
# Game 2 - DOCTORS: laboratory place + tap (+epo pending) + pending-zone placement
# =====================================================================================
print("\n--- Game 2: doctors (laboratory, pending zone) ---")
# pool has 28 cost-5 no_condition cards; the first three are m5a-c (Game 1), so take a wider slice
m5f = [x for x in pick_main('no_condition', mana=5, n=26) if x not in {m5a, m5b, m5c}]
a_deck = ['laboratory', 'epo'] + m5f[:18]   # 2 support + 18 cost-5 mains (unaffordable while T1 spend>0)
b_deck = pick_main('no_condition', mana=1, n=6) + pick_main('day', n=14)   # disjoint conditions -> no dup ids
assert len(a_deck) == 20 and len(set(a_deck)) == 20

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid2 = ge.create_new_game(player=p1.model_dump())
gids.append(gid2)
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid2)

conn, _, gs = ge.get_current_game(gid2)
gs.turn_order = ['A', 'B']
ga, gb = gs.players['A'], gs.players['B']
# A holds the 2 support cards + 3 cost-5 mains (init filler); B gets a normal-ish hand
ga.hand = ['laboratory', 'epo', a_deck[2], a_deck[3], a_deck[4]]
ga.deck = [x for x in a_deck if x not in ga.hand]
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.hand = b_deck[:6]; gb.deck = list(b_deck[6:]); gb.mana, gb.discard, gb.dwelling = [], [], None
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid2))
conn.commit(); conn.close()
print(f"game {gid2} created")

ai2 = PlayerAI(player_state=None)
st2: dict = {}
sent2 = []


def drive_robot2(gid):
    conn, _, gs = ge.get_current_game(gid); conn.close()
    if gs.state == "game over" or gs.winner:
        return None
    msg = ai_decide(gs, 'A', st2, ai2)
    if msg is None:
        return None
    okv, why = validate_message(msg, dwelling=gs.players['A'].dwelling)
    check(f"G2 local validation ({msg.get('mode') or msg.get('to')} / {msg.get('cards')})", okv, f"({why})")
    succ, why2 = send(gid, 'A', msg)
    sent2.append(dict(msg))
    check("G2 engine accepted the robot's action", succ is True, f"(resp={why2!r} msg={msg})")
    return msg


def drive_b2(gid):
    conn, _, gs = ge.get_current_game(gid); conn.close()
    state = gs.state
    b = gs.players['B']
    if state.startswith("waiting for both players to put"):
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
    check("G2 engine accepted the opponent's action", succ is True, f"(resp={why2!r} state={state})")


# --- init MANUALLY (both players): A sacrifices its 3 cost-5 mains, B the first 3 of its hand -
#     the robot's put_mana sacrifice is already covered by Game 1; here we want a deterministic
#     play-phase start with exactly [laboratory, epo] in A's hand.
succ, why2 = send(gid2, 'A', {'cards': [a_deck[2], a_deck[3], a_deck[4]], 'to': 'mana', 'mode': '', 'pendings': []})
check("G2 init: A put its 3 mains in mana", succ is True, f"(resp={why2!r})")
succ, why2 = send(gid2, 'B', {'cards': b_deck[:3], 'to': 'mana', 'mode': '', 'pendings': []})
check("G2 init: B put 3 cards in mana", succ is True, f"(resp={why2!r})")
pad_mana(gid2, 'A', 3)   # test-only: 6 available mana (lab=3 + epo=1 fit comfortably)

for _ in range(40):   # until turn 2 starts
    conn, _, gs = ge.get_current_game(gid2); state = gs.state; conn.close()
    if "turn 2" in state:
        break
    drive_robot2(gid2)
    drive_b2(gid2)

conn, _, gs = ge.get_current_game(gid2)
ga = gs.players['A']
check("G2 laboratory PLACED via ai_decide", ga.dwelling == 'laboratory', f"(dwelling={ga.dwelling})")
pendings = ga.pendings or []
# the lab TAP adds an 'epo' pending (no slot) and/or the robot placed its hand 'epo' in the zone -
# at least one must be there, and a tap happened exactly once:
taps_g2 = sum(1 for m in sent2 if (m.get('mode') == 'dwelling_activation'))
check("G2 laboratory tapped (adds an epo pending) - exactly once", taps_g2 == 1, f"(taps={taps_g2})")
check("G2 pending zone non-empty after the turn", len(pendings) >= 1 and all(p in ('epo', 'virus', 'bloodtest', 'mercurochrome') for p in pendings),
      f"(pendings={pendings})")
placed_epo = [m for m in sent2 if m.get('to') == 'pending_zone']
check("G2 a pending card was placed via to:'pending_zone'", len(placed_epo) >= 1, f"(sent={[(m['mode'], m['to'], m['cards']) for m in sent2]})")
# NOTE: each laboratory tap GENERATES one extra 'epo' into the pending zone (engine design -
# a produced resource, not a deck card), so the expected total is a_deck + 1 epo per tap.
check("G2 card conservation (deck cards exactly once + 1 generated epo per lab tap)",
      sorted(all_cards(gs, 'A')) == sorted(a_deck + ['epo'] * taps_g2),
      f"({len(all_cards(gs, 'A'))}/{len(a_deck) + taps_g2})")

# =====================================================================================
# Game 3 - REGRESSION: mains-only hand still plays normal move cards
# =====================================================================================
print("\n--- Game 3: regression (mains-only hand -> play_card path) ---")
adv1 = pick_main('no_condition', mana=2, n=1, adv_min=1)[0]
adv2 = pick_main('no_condition', mana=3, n=1, adv_min=1)[0]
# dedupe the fillers: adv1/adv2 are themselves no_condition cards and may appear in the pool slice
a_deck = [adv1, adv2] + [x for x in pick_main('no_condition', n=24, adv_min=1) if x not in {adv1, adv2}][:18]
assert len(a_deck) == 20 and len(set(a_deck)) == 20
b_deck = pick_main('no_condition', mana=1, n=6) + pick_main('night', n=14)

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid3 = ge.create_new_game(player=p1.model_dump())
gids.append(gid3)
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid3)

conn, _, gs = ge.get_current_game(gid3)
gs.turn_order = ['A', 'B']
ga, gb = gs.players['A'], gs.players['B']
ga.hand = [adv1, adv2, a_deck[2], a_deck[3], a_deck[4]]
ga.deck = [x for x in a_deck if x not in ga.hand]
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.hand = b_deck[:6]; gb.deck = list(b_deck[6:]); gb.mana, gb.discard, gb.dwelling = [], [], None
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid3))
conn.commit(); conn.close()
print(f"game {gid3} created")

ai3 = PlayerAI(player_state=None)
st3: dict = {}
sent3 = []


def drive_robot3(gid):
    conn, _, gs = ge.get_current_game(gid); conn.close()
    if gs.state == "game over" or gs.winner:
        return None
    msg = ai_decide(gs, 'A', st3, ai3)
    if msg is None:
        return None
    okv, why = validate_message(msg, dwelling=gs.players['A'].dwelling)
    check(f"G3 local validation ({msg.get('mode') or msg.get('to')} / {msg.get('cards')})", okv, f"({why})")
    succ, why2 = send(gid, 'A', msg)
    sent3.append(dict(msg))
    check("G3 engine accepted the robot's action", succ is True, f"(resp={why2!r} msg={msg})")
    return msg


def drive_b3(gid):
    conn, _, gs = ge.get_current_game(gid); conn.close()
    state = gs.state
    b = gs.players['B']
    if state.startswith("waiting for both players to put"):
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
    check("G3 engine accepted the opponent's action", succ is True, f"(resp={why2!r} state={state})")


for _ in range(4):
    conn, _, gs = ge.get_current_game(gid3); state = gs.state; conn.close()
    if not state.startswith("waiting for both players to put"):
        break
    drive_robot3(gid3)
    drive_b3(gid3)

pad_mana(gid3, 'A', 2)   # test-only: total mana zone = 5 (affords the move cards)

for _ in range(40):
    conn, _, gs = ge.get_current_game(gid3); state = gs.state; conn.close()
    if "turn 2" in state:
        break
    drive_robot3(gid3)
    drive_b3(gid3)

conn, _, gs = ge.get_current_game(gid3)
moves_g3 = [m for m in sent3 if m.get('mode') == 'move' and m.get('cards')]
check("G3 the robot played MAIN move cards (play_card path intact)", len(moves_g3) >= 1, f"(sent={[(m['mode'], m['to'], m['cards']) for m in sent3]})")
check("G3 A advanced during turn 1", gs.players['A'].current_position > 0, f"(pos={gs.players['A'].current_position})")
check("G3 card conservation (every card of A's deck exactly once)",
      sorted(all_cards(gs, 'A')) == sorted(a_deck), f"({len(all_cards(gs, 'A'))}/{len(a_deck)})")

# --- cleanup ---------------------------------------------------------------------------
conn = sqlite3.connect(ge.DB_PATH)
for g in gids:
    conn.execute("DELETE FROM games WHERE game_id = ?", (g,))
conn.commit(); conn.close()

print(f"\nALL {ok} CHECKS PASSED - A.1 support-faction infrastructure works end to end")
