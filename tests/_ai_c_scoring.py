# Manual smoke test (section C): card scoring / move selection in PlayerAI.play_card.
#   * #22 real effective-advance model - _home_bonus, _forward_if_met (advancing/backward/jump/
#     plain), _forward_if_unmet (mana-1 reduced value + bonus when positive, suppressed by locks);
#   * #23 win detection - pos+forward>=24 including drops on the path (_drop_path_bonus: boosts /
#     trampolines / gluetraps / landmine stop / pet_trap tokens, step vs jump landing) and the
#     attachable-epo completion (mirroring _choose_attachment's estimator);
#   * #24 tempo values - flat TEMPO_VALUES per effect + public situational adjustments (avalanche /
#     wrecking_ball);
#   * #25 block awareness on my own plays - declared opponent shields vs my cost, unstoppable and
#     mercurochrome exceptions;
#   * #26 pass discipline - play_card returns None when the best card has no positive expected value.
# Unit scenarios run against a monkey-patched CARDS_DB (hand-crafted rows). E2E: through
# ge.handle_websocket_message, a robot holding only net-negative junk cards PASSES its turn-1 play
# phase instead of burning a card + slot on them. Games are written to games.db and deleted after.
# Run from the project root:  uv run python tests/_ai_c_scoring.py
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState
from player_ai.playerai import (PlayerAI, FACTION_BIOMES, TEMPO_VALUES, validate_message)
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
POOL_DB = ai.CARDS_DB   # instance attribute - keep the full pool for restoring after overrides


def row(card_id, mana, advancing, condition='no_condition', effect='', en=0, shield=0):
    return {'card_id': card_id, 'name': f'n_{card_id}', 'faction': 'Dwarves', 'mana': int(mana),
            'advancing': int(advancing), 'shield': int(shield), 'condition': condition,
            'effect': effect, 'effect_number': int(en), 'rare': False}


def set_ai(rows=None, pos=0, faction='Dwarves', hand=None, mana_n=3, earth=None, temp=None,
           dn='day', pendings=None, oppo_pos=None, oppo_actions=None, board_drops=None,
           drop_tokens=None, nobodymoves=False, locked_by_mine=False):
    """Dwarves on an OC sea -> home biome (bonus +1) by default; pass earth='DE' for a uniform off-home
    layout or an explicit list of 24 biome codes. NOTE: 'OC'*24 would be ONE string - the helper
    treats a plain str as a single code repeated across all cells."""
    ps = PlayerState(name='A', current_position=pos, faction=faction,
                     hand=list(hand or []), mana=['m'] * mana_n, mana_spend=0)
    if pendings is not None:
        ps.pendings = list(pendings)
    ai.player_state = ps
    if rows is not None:
        ai.CARDS_DB = pl.DataFrame(rows)          # instance-level override (hand-crafted candidates)
    else:
        ai.CARDS_DB = POOL_DB                     # restore the full pool (it is an INSTANCE attribute)
    ai.oppo_position, ai.oppo_mana, ai.oppo_hand = oppo_pos, None, None
    ai.oppo_actions = list(oppo_actions or [])
    if earth is None:
        ai.earth_biomes = ['OC'] * 24
    elif isinstance(earth, str):
        ai.earth_biomes = [earth] * 24     # a single biome code repeated over all cells
    else:
        ai.earth_biomes = list(earth)
    ai.temperature, ai.day_night, ai.day_night_fixed = temp, dn, False
    ai.cataclysm_pile = ['OC', 'MO', 'DE', 'JU']
    ai.drops_on_board = bool(board_drops or drop_tokens)
    ai.board_drops = [dict(d) for d in (board_drops or [])]
    ai.drop_tokens = {int(k): int(v) for k, v in (drop_tokens or {}).items()}
    ai.nobodymoves_active = nobodymoves
    ps.landmine_blocked = locked_by_mine
    ai.rooted_on_board = []
    ai.occupied_cells = set()
    ai.oppo_dwelling = None
    ai.oppo_faction, ai.oppo_pending_slots = 'Twigs', []


def row_of(cid):
    return next(r for r in ai.CARDS_DB.iter_rows(named=True) if r['card_id'] == cid)


# =====================================================================================
# #22 real effective-advance model
# =====================================================================================
print("\n--- Unit: #22 effective advance ---")
set_ai()   # Dwarves on OC -> home biome
check("_home_bonus: +1 on a home biome", ai._home_bonus() == 1)
set_ai(earth='DE')
check("_home_bonus: 0 off the home biomes", ai._home_bonus() == 0)

R = [row('P5', 3, 5), row('A7', 3, 4, effect='advancing', en=2), row('B6', 3, 6, effect='backward', en=-2),
     row('J4', 2, 4, effect='jump'), row('M1', 1, 3)]
set_ai(rows=R)   # home
check("_forward_if_met plain: base + bonus", ai._forward_if_met(row_of('P5')) == 6)
check("_forward_if_met 'advancing' effect: base+en (+bonus on the total)", ai._forward_if_met(row_of('A7')) == 7)
check("_forward_if_met 'backward': base(+bonus) then recoil", ai._forward_if_met(row_of('B6')) == 5)   # 6+1-2
check("_forward_if_met 'jump': teleport of base (+bonus), landing only", ai._forward_if_met(row_of('J4')) == 5)
set_ai(rows=R, earth='DE')
check("off-home: plain 5 / advancing 6 / backward 4 / jump 4",
      [ai._forward_if_met(r) for r in R[:4]] == [5, 6, 4, 4], f"({[ai._forward_if_met(r) for r in R[:4]]})")

set_ai(rows=R)   # home again, synthetic rows still loaded
check("_forward_if_unmet: mana-1 reduced value GAINS the bonus when positive (m3 -> 2+1)", ai._forward_if_unmet(row_of('P5')) == 3)
check("_forward_if_unmet m2 card: mana-1 = 1 reduced value GAINS the bonus (home) -> 2", ai._forward_if_unmet(row_of('J4')) == 2)
check("_forward_if_unmet m1 card: reduced value 0 (no bonus - not positive)", ai._forward_if_unmet(row_of('M1')) == 0)
set_ai(rows=R, nobodymoves=True)
check("nobodymoves lock suppresses even the reduced advancing", ai._forward_if_unmet(row_of('P5')) == 0)

# play_card with effect-adjusted movement (the old raw-advancing score would pick B6 here: recoil makes it net 4 vs plain P5's 6... and A7 is best of all)
set_ai(rows=[row('A7', 3, 4, effect='advancing', en=2), row('B6', 3, 6, effect='backward', en=-2)], hand=['A7', 'B6'])
check("play_card: the 'advancing'-effect card (effective 7) beats a raw-6 recoil card (effective 5)", ai.play_card()['cards'][0] == 'A7')
set_ai(rows=[row('B6', 3, 6, effect='backward', en=-2), row('P4', 2, 4)], hand=['B6', 'P4'])   # home: B6 -> 5, P4 -> 5 ; cost breaks the tie
check("play_card: recoil net (5) ties a plain card (5) - the cheaper one is preferred", ai.play_card()['cards'][0] == 'P4')

# =====================================================================================
# #23 win detection + drops on the path
# =====================================================================================
print("\n--- Unit: #23 wins & drop paths ---")
set_ai()   # home (OC), pos 19, plain m2 adv4 -> fwd 5 -> 24 WIN
set_ai(rows=[row('W', 2, 4)], hand=['W'], pos=19)
check("_completes_win: pos + forward >= 24", ai._completes_win(row_of('W'), ai._forward_if_met(row_of('W')), False))

# drops on a stepped path (pos 5, fwd 4 -> cells 6,7,8,9)
set_ai(rows=[row('S', 3, 4)], pos=5, board_drops=[{'cell': 7, 'kind': 'boost', 'owner': 'A'}, {'cell': 9, 'kind': 'gluetrap', 'owner': 'B'}])
check("_drop_path_bonus step: boost +2 (cell 7) and gluetrap -1 (cell 9)", ai._drop_path_bonus(row_of('S'), 4) == 1)
set_ai(rows=[row('S', 3, 4)], pos=5, board_drops=[{'cell': 7, 'kind': 'boost', 'owner': 'A'}, {'cell': 8, 'kind': 'landmine', 'owner': 'B'}])
check("_drop_path_bonus step: a landmine cancels the remaining steps (only +2 from cell 7)", ai._drop_path_bonus(row_of('S'), 4) == 2)
set_ai(rows=[row('J', 3, 4, effect='jump')], pos=5, board_drops=[{'cell': 9, 'kind': 'trampoline', 'owner': 'A'}, {'cell': 7, 'kind': 'boost', 'owner': 'B'}])
check("_drop_path_bonus jump: only the LANDING cell fires (+2 trampoline at 9; the boost at 7 is skipped)", ai._drop_path_bonus(row_of('J'), 4) == 2)
set_ai(rows=[row('S', 3, 4)], pos=5, drop_tokens={8: 2})
check("_drop_path_bonus step: pet_trap tokens knock back per token (-2 at cell 8)", ai._drop_path_bonus(row_of('S'), 4) == -2)
set_ai(rows=[row('J', 3, 4, effect='jump')], pos=5, drop_tokens={9: 1})
check("_drop_path_bonus jump: a pet_trap token on the landing cell fires (-1)", ai._drop_path_bonus(row_of('J'), 4) == -1)

# a win THROUGH a self-placed boost (pos 21, fwd 2 -> cells 22,23; boost at 23 adds +2 -> reach 25)
set_ai(rows=[row('W', 2, 1)], pos=21, board_drops=[{'cell': 23, 'kind': 'boost', 'owner': 'A'}])
r = row_of('W')
fwd = ai._forward_if_met(r) + ai._drop_path_bonus(r, ai._forward_if_met(r))   # the same composition play_card uses
check("win through a self-placed boost on the path (21 + 2 + 2 >= 24)", ai._completes_win(r, fwd, False))

# attachable-epo completion: pos 19 off-home, est = 4 -> 23; epo in zone completes it
REAL_EPO_CARD = 'Orc21_de4294'   # m2 adv4 no_condition taxation (verified in pool)
set_ai(pos=19, earth='DE', pendings=['epo'], hand=[REAL_EPO_CARD])
r = ROWS[REAL_EPO_CARD]
check("_completes_win: pos + est == 23 with an attachable epo (the A.2 attach rule)", ai._completes_win(r, ai._forward_if_met(r), False))
set_ai(pos=19, earth='DE', hand=[REAL_EPO_CARD])   # no epo in zone
check("_completes_win: without the epo the same card does not win (19 + 4 = 23 < 24)", not ai._completes_win(r, ai._forward_if_met(r), False))

# =====================================================================================
# #24 tempo values
# =====================================================================================
print("\n--- Unit: #24 tempo ---")
check("TEMPO_VALUES sanity: ramp/rooted positive, advancing_oppo/taxation negative",
      TEMPO_VALUES['ramp'] > 0 and TEMPO_VALUES['rooted'] > 0 and TEMPO_VALUES['advancing_oppo'] < 0 and TEMPO_VALUES['taxation'] < 0)
set_ai(rows=[row('R1', 2, 3, effect='ramp'), row('P4', 2, 5)], hand=['R1', 'P4'])   # home: R1 fwd 4 tempo+3 = net 7 ; P4 fwd 6 net 6
check("play_card: a ramp card with slightly less advance wins on tempo (net 7 > 6)", ai.play_card()['cards'][0] == 'R1')
set_ai(rows=[row('X', 2, 5, effect='advancing_oppo'), row('P4', 2, 3)], hand=['X', 'P4'])   # X fwd 6 - tempo 3 = net 3 ; P4 net 4
check("play_card: advancing_oppo is penalized (net 3 < 4)", ai.play_card()['cards'][0] == 'P4')

set_ai(rows=[row('AV', 2, 1, effect='avalanche')], oppo_pos=7)   # earth OC*24 -> opponent NOT on MO
check("_situational_tempo avalanche: opponent off the mountain -> 0", ai._situational_tempo(row_of('AV')) == 0)
set_ai(rows=[row('AV', 2, 1, effect='avalanche')], oppo_pos=7, earth=['MO'] * 8 + ['OC'] * 16)   # I am on MO too (pos 0)
check("_situational_tempo avalanche: opponent ON the mountain -> +3 (even if I am too)", ai._situational_tempo(row_of('AV')) == 3)
set_ai(rows=[row('AV', 2, 1, effect='avalanche')], oppo_pos=7, earth=['OC'] * 8 + ['MO'] * 16)   # I (pos 0) and the opponent (cell 7) are both on OC
check("_situational_tempo avalanche: neither token on the mountain -> 0", ai._situational_tempo(row_of('AV')) == 0)
set_ai(rows=[row('WB', 2, 1, effect='wrecking_ball')])
ai.oppo_dwelling = 'refinery'
check("_situational_tempo wrecking_ball: +4 when the opponent has a dwelling", ai._situational_tempo(row_of('WB')) == 4)
ai.oppo_dwelling = None
check("_situational_tempo wrecking_ball: 0 with no dwelling to destroy", ai._situational_tempo(row_of('WB')) == 0)

# =====================================================================================
# #25 block awareness on my own plays
# =====================================================================================
print("\n--- Unit: #25 blocks ---")
SHD = pick((pl.col('shield').ge(5)), n=1)[0]   # a real pool card with shield >= 5 (in MAIN_ROWS for the sum)
SHD_VAL = ROWS[SHD]['shield']
set_ai(rows=[row('BIG', SHD_VAL, 8)], pos=0)   # my next stopover is stopover_4 (no rooted, play_count 0)
check("facing position: with no rooted cards and no plays yet it is stopover_4", ai._my_next_stopover() == 'stopover_4')
set_ai(rows=[row('BIG', SHD_VAL, 8), row('SMALL', SHD_VAL + 1, 3)], pos=0,
       oppo_actions=[{'mode': 'defend', 'to': 'stopover_4', 'cards': [SHD]}])   # their shields (>=5) sit on my position
check(f"_will_be_blocked: declared shields ({SHD_VAL}) block a card costing {SHD_VAL}", ai._will_be_blocked(row_of('BIG')) is True)
check(f"...but NOT the more expensive one (cost {SHD_VAL + 1} > shields {SHD_VAL})",
      ai._will_be_blocked(row_of('SMALL')) is False)
check("_will_be_blocked: unstoppable with met condition buys through",
      ai._will_be_blocked({'card_id': 'U', 'mana': SHD_VAL, 'advancing': 8, 'condition': 'no_condition', 'effect': 'unstoppable', 'effect_number': 0, 'shield': 0}) is False)
check("_will_be_blocked: unstoppable with UNMET condition does not",
      ai._will_be_blocked({'card_id': 'U2', 'mana': SHD_VAL, 'advancing': 8, 'condition': 'temp_sup_15', 'effect': 'unstoppable', 'effect_number': 0, 'shield': 0}) is True)
set_ai(rows=[row('BIG', SHD_VAL, 8)], pos=0, pendings=['mercurochrome'],
       oppo_actions=[{'mode': 'defend', 'to': 'stopover_4', 'cards': [SHD]}])
check("_will_be_blocked: a mercurochrome in my zone (which I would attach) buys through", ai._will_be_blocked(row_of('BIG')) is False)

# play_card prefers the card that SLIPS THROUGH: shields SHD_VAL -> m3 always blocked, cost SHD_VAL+1 fine
set_ai(rows=[row('MED', 3, 9), row('HI', SHD_VAL + 1, 4)], pos=0, hand=['MED', 'HI'], mana_n=SHD_VAL + 2,
       oppo_actions=[{'mode': 'defend', 'to': 'stopover_4', 'cards': [SHD]}])   # MED (m3) blocked -> fwd 0; HI (cost > shields) not
check(f"play_card under fire: the cost-{SHD_VAL + 1} card beats a raw-9 m3 that would be canceled", ai.play_card()['cards'][0] == 'HI')

# =====================================================================================
# #26 pass discipline + attachment preserved
# =====================================================================================
print("\n--- Unit: #26 pass discipline ---")
JUNK = [row(f'J{i}', 1, -1) for i in range(3)]   # m1 no_condition adv -1 -> net <= 0 always
set_ai(rows=JUNK, hand=['J0', 'J1', 'J2'])
check("play_card: only net-negative junk in hand -> None (pass; unused mana carries over)", ai.play_card() is None)
set_ai(rows=[row('T', 1, 0, effect='taxation')], hand=['T'])   # met: fwd 0(+bonus? adv 0 -> no bonus), tempo -2 -> net -2
check("play_card: a zero-advance self-taxation card is not worth the slot", ai.play_card() is None)
set_ai(rows=[row('T', 1, 3, effect='taxation')], hand=['T'])   # home: fwd 4, tempo -2 -> net +2 > 0
check("play_card: ...but a positive-advance taxation card still plays (net +2)", ai.play_card() is not None)

# the A.2 attachment rule still rides on top of the new score (epo completing 19+4=23 off-home)
set_ai(pos=19, earth='DE', pendings=['epo'], hand=[REAL_EPO_CARD])
msg = ai.play_card()
check("play_card + A.2: the winning-distance card still carries its epo attach", msg is not None and msg['pendings'] == ['epo'])

# =====================================================================================
# E2E: a junk-only robot PASSES turn 1 instead of burning cards (through the engine)
# =====================================================================================
print("\n--- E2E: pass discipline in a real game ---")
junk = pick((pl.col('faction') == 'Miaous') & (pl.col('mana') == 1) & (pl.col('condition') == 'no_condition')
            & (pl.col('advancing').lt(0)), n=6)
fillers = pick((pl.col('faction') == 'Miaous') & (pl.col('mana') == 4), n=14)
a_deck = junk + fillers
b_deck = pick((pl.col('faction') == 'Twigs') & (pl.col('mana').le(3)), n=20)
assert len(set(a_deck)) == 20 and len(set(b_deck)) == 20

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
gs.board_drops, gs.rooted_on_board, gs.nobodymoves_active = [], [], False
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
conn.commit(); conn.close()
print(f"game {gid} created (A holds only net-negative m1 junk cards)")


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
        # B plays its best affordable card or passes (scripted human)
        conn_, _, g2 = ge.get_current_game(gid); b2 = g2.players['B']; conn_.close()
        avail = len(b2.mana or []) - (b2.mana_spend or 0)
        playables = [c for c in (b2.hand or []) if ROWS[c]['mana'] <= avail]
        if playables:
            best = max(playables, key=lambda c: (1 if True else 0, ROWS[c]['advancing']))
            succ, why2 = send(gid, 'B', {'cards': [best], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
        else:
            succ, why2 = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    else:
        return
    check("engine accepted the opponent's action", succ is True, f"(resp={why2!r} state={state})")


ai2 = PlayerAI(player_state=None)
st: dict = {}
sent_a, sent_b_moves = [], 0

for _ in range(6):   # init
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
    sent_a.append(dict(msg))
    check("engine accepted the robot's init action", succ is True, f"(resp={why2!r} msg={msg})")

turn1_moves_by_A = []
for _ in range(10):   # turn 1 - stop once it ends
    conn, _, g = ge.get_current_game(gid); state = g.state; conn.close()
    if g.winner is not None or g.turn > 1:
        break
    if "to play" in state and "(A)" in state:
        msg = ai_decide(g, 'A', st, ai2)
        conn.close()
        if msg is not None:
            okv, why = validate_message(msg)
            check(f"local validation passes ({msg.get('mode') or msg.get('to')} / {msg.get('cards')})", okv, f"({why})")
            succ, why2 = send(gid, 'A', msg)
            sent_a.append(dict(msg))
            if msg.get('mode') == 'move' and turn == 1:
                turn1_moves_by_A.append(msg['cards'])
            check("engine accepted the robot's action", succ is True, f"(resp={why2!r} msg={msg})")
        else:   # defensive - ai_decide should always answer with a pass at worst
            conn.close()
            break
    elif "to play" in state and "(B)" in state:
        conn.close()
        drive_b()
    else:
        conn.close()

a_moves_t1 = [m for m in sent_a if m.get('mode') == 'move' and m.get('cards')]
check("PASS DISCIPLINE (#26): the robot played NO move card on turn 1 (all candidates net <= 0)", not a_moves_t1, f"(sent={[(m['mode'], m.get('to'), m['cards']) for m in sent_a]})")
a_passes = [m for m in sent_a if m.get('mode') == 'pass']
check("...and it did pass its play phase", len(a_passes) >= 1, f"(sent={[(m['mode'], m.get('to')) for m in sent_a]})")

conn, _, g = ge.get_current_game(gid); conn.close()
print(f"game ends at turn {g.turn}, state: {g.state!r}")

# --- cleanup ---------------------------------------------------------------------------
conn = sqlite3.connect(ge.DB_PATH)
for g_ in gids:
    conn.execute("DELETE FROM games WHERE game_id = ?", (g_,))
conn.commit(); conn.close()

print(f"\nALL {ok} CHECKS PASSED - section C scoring verified")
