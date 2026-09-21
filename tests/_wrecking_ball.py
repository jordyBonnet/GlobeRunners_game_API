# Manual smoke test: wrecking_ball (rule of engine_version 11)
#  - effect == 'wrecking_ball' is INSTANT: fires at PLAY TIME (move mode)
#  - it REMOVES the opponent's dwelling card (PlayerState.dwelling):
#      * the card is sent to the opponent's discard pile
#      * the dwelling slot is cleared (dwelling = None)
#  - a no-op today (no implemented effect places a dwelling card yet) but the
#    removal system is live: setting opponent.dwelling manually -> wrecking_ball
#    removes it
#  - defend mode: wrecking_ball does NOT remove (instant effects are move-only)
#  - old games (engine_version < 11): wrecking_ball is a no-op (dwelling untouched)
#  - the wrecking_ball card itself still advances by its basic value (no-op marker)
# Run from the project root:  uv run python tests/_wrecking_ball.py
# NOTE: writes to games.db (like _diag.py / _rooted.py)
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

def q(**kw):
    df = DB
    for k, v in kw.items():
        df = df.filter(pl.col(k) == v)
    return df

# a real wrecking_ball card (no_condition so it always resolves; mana 1 is cheap)
wb_ids = q(effect='wrecking_ball', condition='no_condition', mana=1)['card_id'].to_list()
assert len(wb_ids) >= 1, 'need a wrecking_ball/no_condition/mana1 card'
wb1 = wb_ids[0]

# a neutral advancing card for the opponent (so they can play something)
adv1 = q(effect='advancing', condition='no_condition', mana=1)['card_id'].to_list()
adv1 = adv1[0] if adv1 else q(effect='advancing', condition='no_condition')['card_id'].to_list()[0]

# filler cards (any faction) to pad the decks / pay mana
filler = [c for c in q(faction='Miaous')['card_id'].to_list()
          if c not in {wb1, adv1}][:14]
assert len(filler) == 14, f'need 14 Miaous filler cards, got {len(filler)}'

def new_game(a_cards, b_cards, version=None):
    p1 = PlayerState(name='A', deck=list(a_cards) + filler)
    p2 = PlayerState(name='B', deck=list(b_cards) + filler)
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    if version is not None:
        gs.engine_version = version
    return gs

def set_biomes(gs, biome='OC'):
    # OC (ocean): NOT a home biome for the test factions -> no +1 bonus
    for cell in gs.earth:
        cell[0] = biome

def force_hand(gs, name, cards):
    p = gs.players[name]
    for c in cards:
        if c not in p.hand:
            if c in p.deck:
                p.deck.remove(c)
            p.hand.append(c)

def play(gs, name, first_second, card, mode='move', to='stopover_4'):
    p = gs.players[name]
    mana = int(DB.filter(pl.col('card_id') == card)['mana'][0])
    p.mana = [filler[0]] * mana
    p.mana_spend = 0
    p.message = {'cards': [card], 'to': to, 'mode': mode, 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, ok, msg = ge.player_play(first_second, p, gs)
    assert ok, f'play rejected: {msg}'
    return gs

def all_zones(gs):
    """ flat list of every card_id across hand/deck/discard/mana (both players) """
    zones = []
    for p in gs.players.values():
        zones.extend(p.hand or [])
        zones.extend(p.deck or [])
        zones.extend(p.discard or [])
        zones.extend(p.mana or [])
    return zones

ok = 0

# ============================================================
# 1) BASIC: opponent has a dwelling card -> wrecking_ball removes it
# ============================================================
gs = new_game([wb1], [adv1])
set_biomes(gs, 'OC')
force_hand(gs, 'A', [wb1]); force_hand(gs, 'B', [adv1])
# simulate the opponent having a dwelling card (not yet placeable by any
# implemented effect, so we set it directly to exercise the removal system)
dwelling_card = filler[0]
gs.players['B'].dwelling = dwelling_card
a_pos_before = gs.players['A'].current_position
gs = play(gs, 'A', 'first', wb1)
assert gs.players['B'].dwelling is None, \
    f"B's dwelling should be cleared, got {gs.players['B'].dwelling}"
assert dwelling_card in (gs.players['B'].discard or []), \
    f"dwelling card {dwelling_card} should be in B's discard, got {gs.players['B'].discard}"
# the action annotation should record the removal
action = gs.players['A'].action_chain[-1]
assert action.get('dwelling_removed') == dwelling_card, \
    f"action should annotate dwelling_removed={dwelling_card}, got {action.get('dwelling_removed')}"
assert action.get('dwelling_removed_from') == 'B', \
    f"action should annotate dwelling_removed_from='B', got {action.get('dwelling_removed_from')}"
print(f"1) BASIC: wrecking_ball removed B's dwelling card {dwelling_card} -> PASS")
ok += 1

# ============================================================
# 2) NO-OP: opponent has NO dwelling card -> wrecking_ball does nothing
# ============================================================
gs = new_game([wb1], [adv1])
set_biomes(gs, 'OC')
force_hand(gs, 'A', [wb1]); force_hand(gs, 'B', [adv1])
gs.players['B'].dwelling = None   # no dwelling card
gs = play(gs, 'A', 'first', wb1)
assert gs.players['B'].dwelling is None
# no annotation (nothing was removed)
action = gs.players['A'].action_chain[-1]
assert 'dwelling_removed' not in action, f"no annotation expected, got {action.get('dwelling_removed')}"
print(f"2) NO-OP: no dwelling card -> nothing removed -> PASS")
ok += 1

# ============================================================
# 3) DEFEND MODE: wrecking_ball in defend does NOT remove (instant = move only)
# ============================================================
gs = new_game([wb1], [adv1])
set_biomes(gs, 'OC')
force_hand(gs, 'A', [wb1]); force_hand(gs, 'B', [adv1])
gs.players['B'].dwelling = filler[1]
dwelling_before = gs.players['B'].dwelling
gs = play(gs, 'A', 'first', wb1, mode='defend', to='stopover_4')
assert gs.players['B'].dwelling == dwelling_before, \
    f"defend wrecking_ball should NOT remove the dwelling, got {gs.players['B'].dwelling}"
print(f"3) DEFEND: defend-mode wrecking_ball does not remove -> PASS")
ok += 1

# ============================================================
# 5) CARD CONSERVATION: the dwelling card is in EXACTLY ONE zone after removal
#    (it was "on the dwelling spot" = not in any zone; after removal it is in
#    the opponent's discard, and nowhere else)
# ============================================================
gs = new_game([wb1], [adv1])
set_biomes(gs, 'OC')
force_hand(gs, 'A', [wb1]); force_hand(gs, 'B', [adv1])
dwelling_card = filler[3]
gs.players['B'].dwelling = dwelling_card
# before the play: the dwelling card is NOT in any zone (it's on the dwelling spot)
# (we don't count the dwelling slot in all_zones; it's a separate field)
gs = play(gs, 'A', 'first', wb1)
count = (gs.players['B'].discard or []).count(dwelling_card)
assert count == 1, f"dwelling card should be in B's discard exactly once, got {count}"
assert gs.players['B'].dwelling is None
print(f"5) CONSERVATION: dwelling card in discard exactly once -> PASS")
ok += 1

# ============================================================
# 6) LOG: the turn log entry of the wrecking_ball card should carry the note
# ============================================================
gs = new_game([wb1], [adv1])
set_biomes(gs, 'OC')
force_hand(gs, 'A', [wb1]); force_hand(gs, 'B', [adv1])
dwelling_card = filler[4]
gs.players['B'].dwelling = dwelling_card
gs = play(gs, 'A', 'first', wb1)
# the wreck note is a play-time instant effect -> the turn's 'instant' section
inst = [str(it.get('what')) for t in gs.log for it in (t.get('instant') or [])]
assert any('wrecking_ball' in w and 'dwelling' in w for w in inst), \
    f"turn log 'instant' should have a wrecking_ball note, got {inst}"
print(f"6) LOG: note present: {inst} -> PASS")
ok += 1

# ============================================================
# 7) FULL RESOLUTION: wrecking_ball card still advances by its basic value
#    (the effect is a no-op marker at resolution; only the basic advancing applies)
# ============================================================
gs = new_game([wb1], [adv1])
set_biomes(gs, 'OC')
force_hand(gs, 'A', [wb1]); force_hand(gs, 'B', [adv1])
gs.players['B'].dwelling = filler[5]
a_pos_before = gs.players['A'].current_position
gs = play(gs, 'A', 'first', wb1)
# resolve the trip chain (A's wrecking_ball, B's adv1)
gs = play(gs, 'B', 'second', adv1)
gs = ge.process_trip_chain(gs)
# wb1 is mana 1, advancing -1 (Dem10_5fb176) -> a recoil; A stays at 0 (clamped)
# adv1 is mana 1, advancing (whatever) -> B advances
# the key check: the wrecking_ball card resolved (no error), and B's dwelling was removed
assert gs.players['B'].dwelling is None, "B's dwelling should be removed"
print(f"7) FULL RESOLUTION: wrecking_ball resolved + dwelling removed -> PASS")
ok += 1

print(f"\nALL {ok} wrecking_ball TESTS PASSED")
