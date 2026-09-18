# Manual smoke helper: create a game in a mid-turn state that exercises the
# FRONTEND rooted-on-board rendering in BOTH situations:
#   (a) Al's rooted card (from "last turn") sits on Al's stopover 1 (col 4),
#   (b) Al has ALREADY played this turn's first card on that same stopover 1
#       (action_chain, unresolved) -> the "layered" look (rooted card behind).
# The game is left on turn 2 "waiting for second player (Bo) to play" — so the
# UI shows Al's hand, the Play/Pass buttons, and the collision slot.
# Run from the project root:  uv run python tests/_rooted_ui_game.py
# NOTE: writes to games.db (like _diag.py). Prints the game id at the end.
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
rooted_ids = DB.filter(pl.col('effect') == 'rooted', pl.col('condition') == 'no_condition')['card_id'].to_list()
rooted1 = rooted_ids[0]
adv_list = DB.filter(pl.col('effect') == 'advancing', pl.col('condition') == 'no_condition', pl.col('mana') <= 3)['card_id'].to_list()
adv_list = [c for c in adv_list if c not in rooted_ids]
adv1, adv2 = adv_list[0], adv_list[1]
filler = [c for c in DB.filter(pl.col('faction') == 'Miaous')['card_id'].to_list() if c not in {rooted1, adv1, adv2}][:12]

NAME_A, NAME_B = 'Al', 'Bo'   # 2 chars: passes the UI's name-length check too
p1 = PlayerState(name=NAME_A, deck=[rooted1, adv2] + filler)
p2 = PlayerState(name=NAME_B, deck=[adv1] + filler)
gid = ge.create_new_game(p1.model_dump())
ge.p2_connect_to_game(p2.model_dump(), gid)
conn, _, gs = ge.get_current_game(gid)

# OC (ocean): NOT a home biome for Miaous -> the faction +1 bonus never applies
for cell in gs.earth:
    cell[0] = 'OC'
gs.turn_order = [NAME_A, NAME_B]
A, B = gs.players[NAME_A], gs.players[NAME_B]

def force_hand(p, cards):
    for c in p.hand:
        if c not in cards:
            p.deck.insert(0, c)
    p.hand = list(cards)

# reserve adv2 at the BACK of Al's deck (or keep it in hand) so the init mana
# (front 3) doesn't take it
if adv2 in A.deck:
    A.deck.remove(adv2)
    A.deck.append(adv2)
# both players put 3 cards in mana (init done)
for p in (A, B):
    p.mana = p.deck[:3]
    p.deck = p.deck[3:]

# ---------------- turn 1: Al plays the rooted card, Bo plays an advancing card
force_hand(A, [rooted1])
force_hand(B, [adv1])
A.message = {'cards': [rooted1], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}
gs.state = f"turn {gs.turn} - waiting for first player ({NAME_A}) to play"
A, gs, ok, msg = ge.player_play('first', A, gs)
assert ok, f'{NAME_A} play rejected: {msg}'
B.message = {'cards': [adv1], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}
gs.state = f"turn {gs.turn} - waiting for second player ({NAME_B}) to play"
B, gs, ok, msg = ge.player_play('second', B, gs)
assert ok, f'{NAME_B} play rejected: {msg}'
gs = ge.process_trip_chain(gs)
gs = ge._process_rooted_cards(gs)
assert any(e['card_id'] == rooted1 for e in gs.rooted_on_board), gs.rooted_on_board
rooted_stopover = [e for e in gs.rooted_on_board if e['card_id'] == rooted1][0]['stopover']
print(f'  [turn 1] rooted card {rooted1} -> {rooted_stopover}')

# ---------------- turn 2: Al plays adv2 on the SAME stopover (the collision)
gs.turn += 1
gs.turn_order = gs.turn_order[::-1]          # now Bo first? keep Al first for the UI
gs.turn_order = [NAME_A, NAME_B]
gs.day_night = 'night' if gs.day_night == 'day' else 'day'
for p in gs.players.values():
    p.mana_spend = 0
    p.action_chain = []

# Al draws adv2 from the deck into hand (simulating the cleaning-phase draw);
# if it was in the initial hand it's already there (nothing to do)
if adv2 not in A.hand:
    assert adv2 in A.deck, f'adv2 not in Al hand/deck: hand={A.hand} deck={A.deck}'
    A.deck.remove(adv2)
    A.hand.append(adv2)
# B just needs a non-empty hand (can pass): pull 3 filler cards from the deck
for f in filler[:3]:
    if f in B.deck:
        B.deck.remove(f)
        B.hand.append(f)
A.message = {'cards': [adv2], 'to': rooted_stopover, 'mode': 'move', 'pendings': []}
gs.state = f"turn {gs.turn} - waiting for first player ({NAME_A}) to play"
A, gs, ok, msg = ge.player_play('first', A, gs)
assert ok, f'{NAME_A} turn-2 play rejected: {msg}'
gs.state = f"turn {gs.turn} - waiting for second player ({NAME_B}) to play"

conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gs.id))
conn.commit()
conn.close()
print(f"GAME ID: {gs.id}")
print(f"  [turn 2] Al played {adv2} on {rooted_stopover} (rooted card also on {rooted_stopover}) -> layered slot")
