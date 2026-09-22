# Manual smoke test (A.2): Doctors - pending-zone placement + ATTACHMENT policy.
#   * unit: _choose_attachment for every rule branch of AI_improvement_list.md #8/#9
#     (epo win-completion, lab/zone tempo attach, keep-one-for-pending-condition, mercurochrome
#     vs block / landmine path / nobodymoves-lock, no-attach when the effect cannot fire,
#     virus never auto-attached, bloodtest (an opponent-discard, a discard_oppo effect)
#     attaches when the effect can fire and the opponent has cards in hand)
#     + _my_next_stopover rooted-shift mirror.
#   * E2E (real game through ge.handle_websocket_message): a Doctor-flavored robot with 'epo' in
#     its pending zone plays the card that leaves it at cell 23 - and ATTACHES the epo, so the +1
#     crosses the finish line: winner == robot, epo flushed to the discard.
# Run from the project root:  uv run python tests/_ai_a2_doctors.py
# NOTE: writes to games.db (like the other smoke tests); all games are deleted after.
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState
from player_ai.playerai import PlayerAI, FACTION_BIOMES, validate_message
from game_ui.ai_driver import ai_decide

DB = ge.CARDS_DB


def pick(cond=None, mana=None, adv_min=None, effect=None, shield_min=None):
    sub = DB
    if cond is not None:
        sub = sub.filter(pl.col('condition') == cond)
    if mana is not None:
        sub = sub.filter(pl.col('mana') == mana)
    if adv_min is not None:
        sub = sub.filter(pl.col('advancing') >= adv_min)
    if effect is not None:
        sub = sub.filter(pl.col('effect') == effect)
    if shield_min is not None:
        sub = sub.filter(pl.col('shield') >= shield_min)
    return sub['card_id'].to_list()


ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")


def send(gid, name, message):
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


# =====================================================================================
# Unit: _choose_attachment - every rule branch of the A.2 #8/#9 policy
# =====================================================================================
print("\n--- Unit: pending attachment policy ---")
ai = PlayerAI(player_state=None)

# a plain advancing main card: condition met, moves me EXACTLY by 'advancing' cells
# (exclude the self-movement effects whose engine semantics differ from the estimate).
# Twigs on purpose: their pool is cost<=4 with plenty of fillers -> I can build a SINGLE-FACTION deck,
# which the engine assumes (_player_faction derives the effective faction by majority vote over all zones)
ADV4_ID = DB.filter(
    (pl.col('condition') == 'no_condition') & (pl.col('faction') == 'Twigs')
    & (pl.col('mana') <= 2) & (pl.col('advancing') >= 3)
    & (~pl.col('effect').is_in(['advancing', 'backward']))
)['card_id'].to_list()[0]
ADV_ROW = {r['card_id']: r for r in DB.filter(pl.col('card_id') == ADV4_ID).iter_rows(named=True)}[ADV4_ID]
SHIELD_CARD = pick(shield_min=2)[0]          # some real card with shield >= 2 (opponent defend)
CANCEL_CARD = [c for c in pick(effect='effect_canceled')]
CANCEL_ID = CANCEL_CARD[0] if CANCEL_CARD else None

# earth with NO home biome of A's faction anywhere on the path -> _move_estimate stays exact
A_FACTION = ADV_ROW['faction']
NON_HOME = next(b for b in ('OC', 'MO', 'DE', 'JU') if b not in FACTION_BIOMES[A_FACTION])
EARTH_NO_BONUS = [NON_HOME] * 24

ai.earth_biomes = EARTH_NO_BONUS
ai.rooted_on_board = []
ai.board_drops = []
ai.nobodymoves_active = False
ai.day_night, ai.temperature = 'day', None   # no_condition card: irrelevant


def fresh_ps(pendings, pos, dwelling=None):
    return PlayerState(name='A', current_position=pos, faction=A_FACTION,
                       hand=[ADV4_ID], mana=['m'] * 3, mana_spend=0,
                       pendings=list(pendings), play_count=0, dwelling=dwelling)


def attach(pendings, pos, oppo_actions=None, board_drops=None, dwelling=None, locked=False, row=None, oppo_hand=None):
    """Run _choose_attachment with a synthetic state (opponent public plays = oppo_actions)."""
    ai.player_state = fresh_ps(pendings, pos, dwelling)
    ai.oppo_actions = oppo_actions or []
    ai.board_drops = board_drops or []
    ai.nobodymoves_active = locked
    ai.oppo_hand = oppo_hand
    return ai._choose_attachment(row or ADV_ROW)


# placeholder state so _move_estimate has a player_state to read from (attach() re-sets it per scenario)
ai.player_state = PlayerState(name='A', current_position=0, faction=A_FACTION,
                              hand=[ADV4_ID], mana=['m'] * 3, play_count=0)
est4 = ai._move_estimate(ADV_ROW)   # == advancing (no home biome anywhere on the earth)
check("_move_estimate: no biome bonus off-home, exact base value", est4 == int(ADV_ROW['advancing']), f"(est={est4})")

# 1. epo COMPLETES the win: pos + est == 23 -> attach (pos+est >= 24 already wins without it)
check("epo attaches when it completes the win (pos+est==23)", attach(['epo'], 23 - est4) == 'epo')
# 2. ...but NOT when the base card already wins (keep the zone for the `pending` condition)
check("no epo attach when the base card already wins (pos+est>=24)", attach(['epo'], 23 - est4 + 1) is None)
# 3. tempo attach WITH a laboratory dwelling (it refills one epo per turn)
check("epo tempo attach with laboratory dwelling", attach(['epo'], 5, dwelling='laboratory') == 'epo')
# 4. tempo attach with >=2 pendings in zone (at least one stays for the `pending` condition)
check("epo tempo attach keeps one pending when zone has >=2", attach(['epo', 'virus'], 5) == 'epo')
# 5. NO tempo attach without lab and a single pending (keep it - unlocks `pending`-condition mains, #9)
check("no epo tempo attach: no lab + single pending stays in zone", attach(['epo'], 5) is None)

MY_COL = 'stopover_4'   # play_count=0, no rooted -> position 1 -> column 4
# 6. BLOCKED + mercurochrome in zone -> unstoppable buys back the whole play (prefer over epo)
check("mercurochrome attaches when the card would be blocked",
      attach(['epo', 'mercurochrome'], 5, oppo_actions=[{'mode': 'defend', 'to': MY_COL, 'cards': [SHIELD_CARD]}]) == 'mercurochrome')
# 7. BLOCKED without mercurochrome -> attach NOTHING (the pending would be consumed and wasted)
check("no attach at all when blocked and no mercurochrome",
      attach(['epo'], 5, oppo_actions=[{'mode': 'defend', 'to': MY_COL, 'cards': [SHIELD_CARD]}]) is None)
# shields below the cost -> block fails, plain tempo rules apply again (single pending, no lab)
ZERO_SHIELD = DB.filter(pl.col('shield') == 0)['card_id'].to_list()[0]
check("shields < cost: block fails, single-pending keep rule applies",
      attach(['epo'], 5, oppo_actions=[{'mode': 'defend', 'to': MY_COL, 'cards': [ZERO_SHIELD]}]) is None)

# 8. landmine on the path + mercurochrome -> unstoppable dodges it
check("mercurochrome attaches over a landmine on its path",
      attach(['epo', 'mercurochrome'], 5, board_drops=[{'cell': 7, 'kind': 'landmine', 'owner': 'B'}]) == 'mercurochrome')
# 9. ...same mine, no mercurochrome -> nothing attached (no way to save the play)
check("no attach over a landmine path without mercurochrome",
      attach(['epo'], 5, board_drops=[{'cell': 7, 'kind': 'landmine', 'owner': 'B'}]) is None)

# 10. movement LOCKED this turn (nobodymoves) + mercurochrome -> unstoppable+met still moves
check("mercurochrome attaches when my movement is locked (nobodymoves)", attach(['mercurochrome'], 5, locked=True) == 'mercurochrome')
# 11. ...locked without mercurochrome: epo's +1 would be suppressed - do NOT waste it
check("no epo attach while movement-locked (+1 would be suppressed)", attach(['epo'], 23 - est4, locked=True) is None)

# 12. my condition NOT met -> nothing attaches (mercurochrome needs the met condition too)
# ai.day_night is 'day', so a NIGHT-condition card is not met for A
night_row = {r['card_id']: r for r in DB.filter(pl.col('condition') == 'night').iter_rows(named=True)}[pick(cond='night')[0]]
check("no attach when MY condition is not met (even blocked + mercurochrome)",
      attach(['mercurochrome', 'epo'], 5, oppo_actions=[{'mode': 'defend', 'to': MY_COL, 'cards': [SHIELD_CARD]}], row=night_row) is None)

# 13. a facing move-mode effect_canceled card -> conservative skip even in the win case (#8 waste guard)
if CANCEL_ID is not None:
    check("no attach when an effect_canceled card faces me (conservative, even on a winning play)",
          attach(['epo'], 23 - est4, oppo_actions=[{'mode': 'move', 'to': MY_COL, 'cards': [CANCEL_ID]}]) is None)

# 14. virus is self-penalizing (-1 knockback to me): never auto-attached, even on a
#     winning play. bloodtest is the OPPONENT who discards (a discard_oppo effect):
#     attach when the effect can fire and the opponent has cards in hand (public count),
#     keeping the pending-condition guard (single pending + `pending`-condition mains in
#     hand+deck -> keep the zone non-empty).
PEND_ID = pick(cond='pending')[0]   # a `pending`-condition main (for the keep guard)
check("virus never auto-attached (even win-completing)", attach(['virus'], 23 - est4) is None)
check("bloodtest attaches when the effect can fire and the opponent has cards in hand",
      attach(['bloodtest'], 23 - est4, oppo_hand=3) == 'bloodtest')
check("bloodtest NOT attached when the opponent's hand is empty (public count)",
      attach(['bloodtest'], 23 - est4, oppo_hand=0) is None)
check("bloodtest NOT attached when the opponent's hand count is unknown",
      attach(['bloodtest'], 23 - est4, oppo_hand=None) is None)
check("bloodtest NOT attached when it would be blocked (the pending would be consumed and wasted)",
      attach(['bloodtest'], 5, oppo_actions=[{'mode': 'defend', 'to': MY_COL, 'cards': [SHIELD_CARD]}], oppo_hand=3) is None)
ai.player_state = fresh_ps(['bloodtest'], 5)
ai.player_state.hand = [ADV4_ID, PEND_ID]   # I hold a `pending`-condition main
ai.oppo_actions = []
ai.oppo_hand = 3
check("bloodtest single-pending kept in zone while I hold a `pending`-condition main",
      ai._choose_attachment(ADV_ROW) is None)

# 15. _my_next_stopover: my rooted cards shift MY column; the opponent's do not
ai.player_state = fresh_ps([], 0)
ai.rooted_on_board = [{'card_id': 'x', 'owner': 'A'}, {'card_id': 'y', 'owner': 'B'}]
check("_my_next_stopover: own rooted cards shift my column (oppo's do not)",
      ai._my_next_stopover() == 'stopover_3')

# 16. _support_value: the first pending into an empty zone is worth more with `pending`-condition mains in deck
PEND_MAIN = DB.filter(pl.col('condition') == 'pending')['card_id'].to_list()[0]
ai.player_state = fresh_ps([], 0)
check("_support_value: no bonus without pending-condition mains in hand+deck", ai._support_value('epo') == 30,
      f"(value={ai._support_value('epo')})")
ai.player_state.hand.append(PEND_MAIN)   # a `pending`-condition main exists to unlock
check("_support_value: empty zone + pending-condition mains -> placement value boosted",
      ai._support_value('epo') == 36, f"(value={ai._support_value('epo')})")

# =====================================================================================
# E2E: the robot ATTACHES its epo to complete the win (real engine flow)
# =====================================================================================
print("\n--- E2E: epo attachment completes the win through the real entry point ---")
# single-faction decks (the engine derives the effective faction by majority vote over all zones):
# A = Twigs (cost-4 fillers are unaffordable with 3 init mana; the filler condition is irrelevant -
# they never get played), B = Dwarves (never plays)
a_deck = ['epo'] + [ADV4_ID] + DB.filter(
    (pl.col('mana') == 4) & (pl.col('faction') == 'Twigs') & (pl.col('card_id') != ADV4_ID)
)['card_id'].to_list()[:18]
assert len(a_deck) == 20 and len(set(a_deck)) == 20, (len(a_deck), len(set(a_deck)))
b_deck = DB.filter((pl.col('faction') == 'Dwarves') & (pl.col('mana') >= 4))['card_id'].to_list()[:20]

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
gids = [gid]
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

conn, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
ga, gb = gs.players['A'], gs.players['B']
ga.hand = ['epo', ADV4_ID, a_deck[2], a_deck[3], a_deck[4]]
ga.deck = [x for x in a_deck if x not in ga.hand]
ga.mana, ga.discard, ga.dwelling, ga.pendings = [], [], None, []
gb.hand = b_deck[:6]; gb.deck = list(b_deck[6:]); gb.mana, gb.discard, gb.dwelling = [], [], None
# positions: A stands exactly `est4` cells short of the finish (23 - est4 -> 23 without the epo)
POS_A = 23 - est4
ga.current_position, gb.current_position = POS_A, 0
# earth: no home biome of A's faction anywhere on its path (keeps _move_estimate exact), tokens kept intact
for i in range(POS_A, min(POS_A + est4 + 1, 24)):
    gs.earth[i][0] = NON_HOME
gs.board_drops, gs.rooted_on_board, gs.nobodymoves_active = [], [], False
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
conn.commit(); conn.close()
print(f"game {gid} created (A at cell {POS_A}, est={est4})")

ai2 = PlayerAI(player_state=None)
st: dict = {}
sent = []


def drive_robot():
    conn, _, gs = ge.get_current_game(gid); conn.close()
    if gs.state == "game over" or gs.winner:
        return None
    msg = ai_decide(gs, 'A', st, ai2)
    if msg is None:
        return None
    okv, why = validate_message(msg, dwelling=gs.players['A'].dwelling, pendings_zone=gs.players['A'].pendings)
    check(f"local validation passes ({msg.get('mode') or msg.get('to')} / {msg.get('cards')} pend={msg.get('pendings')})", okv, f"({why})")
    succ, why2 = send(gid, 'A', msg)
    sent.append(dict(msg))
    check("engine accepted the robot's action", succ is True, f"(resp={why2!r} msg={msg})")
    return msg


def drive_b():
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
    check("engine accepted the opponent's action", succ is True, f"(resp={why2!r} state={state})")


# --- init via ai_decide (put_mana sacrifice): the 3 cost-5 mains go to mana, 'epo' + ADV4 stay in hand ---
for _ in range(4):
    conn, _, gs = ge.get_current_game(gid); state = gs.state; conn.close()
    if not state.startswith("waiting for both players to put"):
        break
    drive_robot()
    drive_b()

# test-only: move the 'epo' from hand into the pending zone (as a lab tap / earlier placement would)
conn, _, gs = ge.get_current_game(gid)
ga = gs.players['A']
assert 'epo' in ga.hand and ADV4_ID in ga.hand, f"(hand={ga.hand})"
check("init: support kept in hand by the new put_mana (epo + main survive)", True)
ga.hand.remove('epo')
ga.pendings = ['epo']
set_state(gid, gs)

for _ in range(20):   # until the game ends or turn 2 starts
    conn, _, gs = ge.get_current_game(gid); state = gs.state; conn.close()
    if gs.winner is not None:
        break
    drive_robot()
    drive_b()

conn, _, gs = ge.get_current_game(gid)
adv_msgs = [m for m in sent if m.get('cards') == [ADV4_ID]]
check("the robot played the advancing card WITH the epo attached", len(adv_msgs) == 1 and adv_msgs[0].get('pendings') == ['epo'],
      f"(sent={[(m['mode'], m['to'], m['cards'], m.get('pendings')) for m in sent]})")
check("the robot WON - the attached epo's +1 crossed the finish line", gs.winner == 'A' and gs.state == "game over",
      f"(winner={gs.winner}, state={gs.state})")
ga = gs.players['A']
check("the consumed epo was flushed to A's discard (card conservation)",
      ga.discard.count('epo') == 1 and 'epo' not in (ga.pendings or []) and 'epo' not in (ga.hand or []),
      f"(discard={ga.discard}, pendings={ga.pendings})")

# --- cleanup ---------------------------------------------------------------------------
conn = sqlite3.connect(ge.DB_PATH)
for g in gids:
    conn.execute("DELETE FROM games WHERE game_id = ?", (g,))
conn.commit(); conn.close()

print(f"\nALL {ok} CHECKS PASSED - A.2 Doctors attachment policy works end to end")
