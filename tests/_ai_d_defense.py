# Manual smoke test (section D): defense - choose_defend + reaction logic in ai_decide.
#   * #27 replace the 50% coin flip: react only to a CONCRETE declared threat on my next column,
#     block when its value >= DEFEND_THRESHOLD (or it would win them the game);
#   * #28 multi-card defends: _blocking_combo stacks up to 5 cards (cheapest shield sum that wins
#     the race), tie-breaks: mana < include-a-block-card (#30) < fewer cards < sacrifice slow movers;
#   * #29 defend-first: allowed before my first move this turn, with a self-denial guard - never
#     trade an affordable winning move for a non-winning threat (survival exception when they win);
#   * refusals: nothing declared / not a move / wrong column, pet_trap (instant), unstoppable with
#     not-provably-unmet condition, mercurochrome attach, unaffordable shield race.
# Unit scenarios run against synthetic rows + hand-crafted oppo_actions. E2E: through
# ge.handle_websocket_message - the opponent declares a big move first and the robot ANSWERS WITH A
# DEFEND (defend-first) that actually BLOCKS it at resolution, then moves normally afterwards.
# Games are written to games.db and deleted after. Run from the project root:
#   uv run python tests/_ai_d_defense.py
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState
from player_ai.playerai import (PlayerAI, DEFEND_THRESHOLD, validate_message)
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
           oppo_pos=None, oppo_actions=None, play_count=0):
    """Dwarves on an OC sea (home biome) by default; pass earth='DE' for a uniform off-home layout.
    `oppo_actions` are the opponent's declared plays THIS turn (public action_chain)."""
    ps = PlayerState(name='A', current_position=pos, faction=faction,
                     hand=list(hand or []), mana=['m'] * mana_n, mana_spend=0)
    ps.play_count = play_count
    ai.player_state = ps
    if rows is not None:
        ai.CARDS_DB = pl.DataFrame(rows)          # instance-level override (hand-crafted candidates)
    else:
        ai.CARDS_DB = POOL_DB                     # restore the full pool
    ai.MAIN_ROWS = {r['card_id']: r for r in ai.CARDS_DB.iter_rows(named=True)}   # keep the dict lookup in sync
    ai.oppo_position, ai.oppo_mana, ai.oppo_hand = oppo_pos, None, None
    ai.oppo_actions = list(oppo_actions or [])
    if earth is None:
        ai.earth_biomes = ['OC'] * 24
    elif isinstance(earth, str):
        ai.earth_biomes = [earth] * 24     # a single biome code repeated over all cells
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


def row_of(cid):
    return next(r for r in ai.CARDS_DB.iter_rows(named=True) if r['card_id'] == cid)


# =====================================================================================
# #27 - reaction only to a concrete declared threat on my next column
# =====================================================================================
print("\n--- Unit: #27 concrete-threat reaction (no coin flip) ---")
THREAT = row('BIG', 3, 6)                       # m3 adv6 no_condition -> potential 7 (Twigs home bonus on OC)
SHD = row('S', 2, 0, shield=4)                  # my affordable shield card

set_ai(rows=[THREAT, SHD], hand=['S'])
check("no declared plays at all -> None", ai.choose_defend() is None)
set_ai(rows=[THREAT, SHD], hand=['S'],
       oppo_actions=[{'mode': 'defend', 'to': 'stopover_4', 'cards': ['S']}])
check("their LATEST play is a defend (not a move) -> None", ai.choose_defend() is None)
set_ai(rows=[THREAT, SHD], hand=['S'],
       oppo_actions=[{'mode': 'move', 'to': 'stopover_3', 'cards': ['BIG']}])
check("their move sits on another column (faces an already-declared entry of mine) -> None",
      ai.choose_defend() is None)

set_ai(rows=[THREAT, SHD], hand=['S'], oppo_pos=5,
       oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': ['BIG']}])
msg = ai.choose_defend()
check("concrete threat on my next column -> DEFEND", msg is not None and msg['mode'] == 'defend' and msg['to'] == 'stopover_4')
check("...and the declared shields win the race (4 >= cost 3)", sum(row_of(c)['shield'] for c in msg['cards']) >= 3)

# value threshold: a small threat is not worth burning cards + a position on
set_ai(rows=[row('TINY', 1, 2), SHD], hand=['S'], oppo_pos=5,
       oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': ['TINY']}])   # potential 3 < DEFEND_THRESHOLD(4)
check("small threat (value 3 < threshold 4) -> None", ai.choose_defend() is None)

# a declared threat that would WIN them the game is always blocked, whatever its value
set_ai(rows=[row('KILL', 1, 4), SHD], hand=['S'], oppo_pos=20, earth='DE',
       oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': ['KILL']}])   # off-home: potential 4 >= 24-20
msg = ai.choose_defend()
check("game-winning threat -> blocked regardless of threshold", msg is not None and msg['mode'] == 'defend')

# refusing what cannot be blocked anyway
set_ai(rows=[row('PT', 1, 8, effect='pet_trap'), SHD], hand=['S'], oppo_pos=5,
       oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': ['PT']}])
check("pet_trap is INSTANT at play time - a block cannot stop it -> None", ai.choose_defend() is None)
set_ai(rows=[row('UNS', 3, 8, effect='unstoppable'), SHD], hand=['S'], oppo_pos=5,
       oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': ['UNS']}])
check("unstoppable with met condition buys through any block -> None", ai.choose_defend() is None)
# an unstoppable whose condition is PROVABLY unmet loses its buy-through: the block race applies and
# the card only advances mana-1 - here m5 -> 4 cells (>= threshold), so it IS worth blocking (cost 5 needs stacked shields)
set_ai(rows=[row('UNS2', 5, 8, effect='unstoppable', condition='night'),
             row('S1', 2, 0, shield=3), row('S2', 2, 0, shield=3)], hand=['S1', 'S2'], mana_n=4,
       oppo_pos=5, oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': ['UNS2']}])   # it is DAY -> provably unmet
msg = ai.choose_defend()
check("unstoppable with PROVABLY-UNMET condition (night while day) can still be blocked",
      msg is not None and sorted(msg['cards']) == ['S1', 'S2'], f"(got {msg})")
set_ai(rows=[THREAT, SHD], hand=['S'], oppo_pos=5,
       oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': ['BIG'], 'pendings': ['mercurochrome']}])
check("they attached a mercurochrome -> the block fails by rule -> None", ai.choose_defend() is None)

# my next column follows play_count (an already-declared entry shifts it left)
set_ai(rows=[THREAT, SHD], hand=['S'], oppo_pos=5, play_count=1,
       oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': ['BIG']}])
check("after one of my plays the threat on stopover_4 faces my OLD entry -> None", ai.choose_defend() is None)
set_ai(rows=[THREAT, SHD], hand=['S'], oppo_pos=5, play_count=1,
       oppo_actions=[{'mode': 'move', 'to': 'stopover_3', 'cards': ['BIG']}])
check("...the same threat on my NEW next column (stopover_3) is reacted to", ai.choose_defend() is not None)

# =====================================================================================
# #28 - multi-card defends: cheapest shield combo that wins the race
# =====================================================================================
print("\n--- Unit: #28/#30 _blocking_combo ---")
A = row('A', 2, 1, shield=3)
B = row('B', 2, 9, shield=3)                    # same shields+cost as A but a FAST mover
set_ai(rows=[row('M5', 5, 7), A, B], hand=['A', 'B'], mana_n=4)
combo = ai._blocking_combo(5, 4)                # no single card (3) reaches 5 -> must stack both
check("two m2 cards (sh3+sh3=6 >= 5, cost 4) beat an unblockable m5 threat", sorted(combo or []) == ['A', 'B'])

set_ai(rows=[row('M4', 4, 6), A], hand=['A'], mana_n=6)
combo = ai._blocking_combo(4, 6)                # sh3 < 4 -> impossible even with the whole hand
check("cannot reach the shield target affordably -> None", combo is None)

C = row('C', 1, 9, shield=4)                    # cheap but FAST
D = row('D', 1, 1, shield=4, condition='block') # same cost+shields, SLOW and a block card
set_ai(rows=[row('M4b', 4, 6), C, D], hand=['C', 'D'], mana_n=5)
combo = ai._blocking_combo(4, 5)
check("#30: at equal cost the BLOCK-condition card is preferred (it fires - we ARE blocking)", combo == ['D'])

E = row('E', 1, 1, shield=4)                    # plain cheap
F = row('F', 2, 9, shield=4, condition='block') # block card but costs MORE
set_ai(rows=[row('M4c', 4, 6), E, F], hand=['E', 'F'], mana_n=5)
combo = ai._blocking_combo(4, 5)
check("cheaper total mana beats the block-card preference", combo == ['E'])

G = row('G', 2, 9, shield=5)                    # one expensive card vs two cheap ones at same total mana
H1 = row('H1', 1, 0, shield=3)
H2 = row('H2', 1, 0, shield=3)
set_ai(rows=[row('M4d', 4, 6), G, H1, H2], hand=['G', 'H1', 'H2'], mana_n=5)
combo = ai._blocking_combo(4, 5)
check("at equal total mana fewer cards are preferred (G m2 sh5 vs H1+H2 m1+m1 sh6)", combo == ['G'])

# unaffordable single best card -> None even though shields would suffice
set_ai(rows=[row('M5b', 5, 7), row('RICH', 5, 0, shield=6)], hand=['RICH'], mana_n=4)
check("the only covering card costs more than my available mana -> None", ai._blocking_combo(5, 4) is None)

# =====================================================================================
# #29 - defend-first + the self-denial guard (never trade a winning move for a defend)
# =====================================================================================
print("\n--- Unit: #29 defend-first / self-denial ---")
WIN = row('WIN', 3, 5)                           # home bonus -> fwd 6; from pos 19 that is a win (25 >= 24)
set_ai(rows=[THREAT, WIN, SHD], hand=['WIN', 'S'], pos=19, oppo_pos=0,
       oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': ['BIG']}])   # threat value 7, non-winning (pos 0)
check("I hold an affordable winning move -> NO defend (take my win)", ai.choose_defend() is None)

set_ai(rows=[row('KILL2', 3, 5), SHD], hand=['S'], pos=0, oppo_pos=18, earth='DE',
       oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': ['KILL2']}])  # off-home: potential 5 < need 6 -> not a win
check("threat below the win threshold but value >= 4 (and no winner in hand) -> still defended", ai.choose_defend() is not None)

set_ai(rows=[row('KILL3', 3, 5), WIN, SHD], hand=['WIN', 'S'], pos=19, oppo_pos=18, earth='OC',
       oppo_actions=[{'mode': 'move', 'to': 'stopover_4', 'cards': ['KILL3']}])  # Twigs home on OC: potential 6 >= 24-18 -> their win
check("THEIR game-winning threat + I am SECOND (resolve after them) -> block anyway (survival beats my win)",
      ai.choose_defend(i_am_first=False) is not None)
check("...the same position with ME first: my winning entry resolves before theirs -> take my win",
      ai.choose_defend(i_am_first=True) is None)

# defend-first is simply the reaction without a prior move - covered by every test above (play_count=0);
# explicit sanity: no moved_this_turn flag exists anymore, the column match alone gates it.

# =====================================================================================
# _threat_value - effect-denial adjustments
# =====================================================================================
print("\n--- Unit: #27 threat value (effect denial) ---")
AO = row('AO', 2, 4, effect='advancing_oppo', en=3)   # it pushes ME forward - never worth blocking here
set_ai(rows=[AO, SHD], hand=['S'], oppo_pos=5)         # Twigs on OC: potential 5; v = 5-3 = 2 < 4
check("advancing_oppo (en=3): the cells it gifts me outweigh its own advance -> None", ai.choose_defend() is None)

AV = row('AV', 2, 3, effect='avalanche')
set_ai(rows=[AV, SHD], hand=['S'], pos=6, oppo_pos=1, earth=['OC'] * 6 + ['MO'] * 18)   # I stand on MO alone
v_me = ai._threat_value(row_of('AV'))                    # potential (3, Twigs not home at cell 1? OC -> +1 => 4) + 3
check("avalanche with only ME on the mountain is worth denying (+3)", v_me >= 7, f"(got {v_me})")
set_ai(rows=[AV, SHD], hand=['S'], pos=0, oppo_pos=6, earth=['OC'] * 6 + ['MO'] * 18)   # only they are on MO
v_them = ai._threat_value(row_of('AV'))                  # potential 3 (no Twigs bonus on MO) - 3 = 0
check("avalanche with only the OPPONENT on the mountain is worth letting fire (-3)", v_them <= 1, f"(got {v_them})")

# wrecking_ball fires INSTANTLY at their play time - a block cannot stop the dwelling destruction,
# so ONLY its advance is deniable. adv2 off-home-ish (potential 3) stays below the threshold:
RW = row('RW', 1, 2, effect='wrecking_ball')
set_ai(rows=[RW, SHD], hand=['S'], pos=0, oppo_pos=6, earth=['OC'] * 6 + ['DE'] * 18)   # Twigs off-home at cell 6
check("wrecking_ball threat is valued by its advance ONLY (no dwelling bonus - it already fired)", ai.choose_defend() is None)

# =====================================================================================
# E2E - defend-first reaction through the engine: B declares a big move, A defends and BLOCKS it
# =====================================================================================
print("\n--- E2E: defend-first reaction blocks a declared threat ---")
# pool fact: shields are almost uniformly cost-1 (m1->sh1, m2->sh2). So a COST-1 threat is blocked
# by ANY single m1 card within turn-1's 3-mana budget: A plays Miaous (plenty of cheap positive movers),
# B opens with one of the four Twigs m1 adv4 cards (potential = 4 >= DEFEND_THRESHOLD off-home).
_f = (pl.col('faction') == 'Miaous')
_m = pl.col('mana').eq(1)
_sh = pl.col('shield').eq(1) & (pl.col('condition') == 'no_condition')
mia_m1 = DB.filter(_f & _m & _sh).sort('advancing', descending=True)['card_id'].to_list()[:6]
F = mia_m1[0]                                                 # the cheap mover (adv 3)
S = next(c for c in mia_m1 if c != F)                         # another m1 sh1 - the shield
others_a = pick((pl.col('faction') == 'Miaous') & (pl.col('mana').le(2))
               & (~pl.col('card_id').is_in([S, F])), n=14)
a_deck = [S, F] + others_a
assert ROWS[F]['advancing'] >= 3 and ROWS[S]['shield'] >= 1

strong_b = pick((pl.col('faction') == 'Twigs') & (pl.col('mana').eq(1)) & (pl.col('advancing').ge(4)
              & (pl.col('condition') == 'no_condition')), n=4)   # exactly four exist - all cost 1
fillers_b = pick((pl.col('faction') == 'Twigs') & (pl.col('mana').le(3))
               & (~pl.col('card_id').is_in(strong_b)), n=16)
b_deck = list(dict.fromkeys(strong_b + fillers_b))[:20]
assert len(set(a_deck)) == 16 and len(b_deck) == 20, f"deck collision: {a_deck} / {b_deck}"

p1 = PlayerState(name='A', deck=list(a_deck)); p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
gids = [gid]
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

conn, _, gs = ge.get_current_game(gid)
gs.turn_order = ['B', 'A']                       # B declares first -> A can react to a REAL threat
gs.earth = [['DE'] for _ in range(24)]           # uniform: off-home for the Twigs threat (no bonus); earth cells are [biome, tokens...] lists
ga, gb = gs.players['A'], gs.players['B']
ga.hand = a_deck[:6]; ga.deck = list(a_deck[6:]); ga.mana, ga.discard, ga.dwelling, ga.pendings = [], [], None, []
gb.hand = b_deck[:6]; gb.deck = list(b_deck[6:]); gb.mana, gb.discard, gb.dwelling = [], [], None
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
conn.commit(); conn.close()
print(f"game {gid} created (A holds shield S={S} + mover F={F}, B opens with a Twigs adv>=4 card)")


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
        # B plays its best affordable card ONCE per turn (the threat), then passes
        conn_, _, g2 = ge.get_current_game(gid); b2 = g2.players['B']; conn_.close()
        acted_moves = sum(1 for a in (b2.action_chain or []) if a and a.get('mode') == 'move' and a.get('cards'))
        avail = len(b2.mana or []) - (b2.mana_spend or 0)
        playables = [c for c in (b2.hand or []) if ROWS[c]['mana'] <= avail]
        if acted_moves == 0 and playables:
            best = max(playables, key=lambda c: ROWS[c]['advancing'])
            b_moves_declared.append(best)
            succ, why2 = send(gid, 'B', {'cards': [best], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
        else:
            succ, why2 = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    else:
        return
    check("engine accepted the opponent's action", succ is True, f"(resp={why2!r} state={state})")


ai2 = PlayerAI(player_state=None)
st: dict = {}
sent_a, b_moves_declared = [], []

for _ in range(6):   # init phase (both place 3 mana cards)
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

# guarantee the scenario regardless of which cards put_mana kept: A holds exactly S + F, 3 mana tokens
conn, _, g = ge.get_current_game(gid)
ga = g.players['A']
ga.hand = [S, F]
ga.mana = list(others_a[0:3])                    # exactly the turn-1 budget of 3 tokens
ga.deck = list(others_a[3:])
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (g.to_json(), gid))
conn.commit(); conn.close()

turn1_done = False
for _ in range(20):   # turn 1 play phase - until it resolves
    conn, _, g = ge.get_current_game(gid); state = g.state; turn = g.turn; conn.close()
    if g.winner is not None or turn > 1:
        break
    if "to play" in state and "(A)" in state:
        msg = ai_decide(g, 'A', st, ai2)
        conn.close()
        if msg is None:
            continue
        okv, why = validate_message(msg)
        check(f"local validation passes ({msg.get('mode') or msg.get('to')} / {msg.get('cards')})", okv, f"({why})")
        succ, why2 = send(gid, 'A', msg)
        sent_a.append(dict(msg))
        check("engine accepted the robot's action", succ is True, f"(resp={why2!r} msg={msg})")
    elif "to play" in state and "(B)" in state:
        conn.close()
        drive_b()
    else:
        conn.close()

a_defends_t1 = [m for m in sent_a if m.get('mode') == 'defend' and m.get('cards')]
a_moves_t1 = [m for m in sent_a if m.get('mode') == 'move' and m.get('cards')]
play_actions = [m for m in sent_a if m.get('mode') in ('move', 'defend')]
check("DEFEND-FIRST (#29): the robot's FIRST play-phase action was a defend (it had not moved yet)",
      bool(play_actions) and play_actions[0]['mode'] == 'defend',
      f"(sent={[(m['mode'], m.get('to'), m['cards']) for m in sent_a]})")

# the action_chain is CLEARED after resolution - use what drive_b recorded + the engine's own turn log
check("the threat was a real adv>=4 card (value >= DEFEND_THRESHOLD off-home)",
      bool(b_moves_declared) and ROWS[b_moves_declared[0]]['advancing'] >= 4,
      f"(got {b_moves_declared})")

def find_log_entry(obj, player, cards):
    """recursively locate the turn-log entry of `player`'s action with exactly `cards`."""
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

entry_b = find_log_entry(g.log, 'B', [b_moves_declared[0]])
check("the engine's turn log recorded B's declared threat", entry_b is not None, f"(log={g.log!r})")
if entry_b is not None:
    check("BLOCK materialized at resolution: the log marks it blocked (no advancing)",
          any('blocked' in str(n).lower() for n in (entry_b.get('negatives') or [])),
          f"(negatives={entry_b.get('negatives')!r} pos {entry_b.get('pos_before')}->{entry_b.get('pos_after')})")
check("no deadlock: after defending, the robot still MOVED this turn (game progresses)", len(a_moves_t1) >= 1,
      f"(sent={[(m['mode'], m.get('to'), m['cards']) for m in sent_a]})")

print(f"turn-1 actions by A: {[(m['mode'], m.get('to'), m['cards']) for m in sent_a]}")
conn, _, g = ge.get_current_game(gid); conn.close()
print(f"game at turn {g.turn}, state: {g.state!r}")

# --- cleanup ---------------------------------------------------------------------------
conn = sqlite3.connect(ge.DB_PATH)
for g_ in gids:
    conn.execute("DELETE FROM games WHERE game_id = ?", (g_,))
conn.commit(); conn.close()

print(f"\nALL {ok} CHECKS PASSED - section D defense verified")
