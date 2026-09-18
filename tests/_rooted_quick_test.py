# Manual smoke helper: create a game in a turn-1 play-phase state so a HUMAN can
# test the ROOTED effect (engine_version 10+) in the web UI quickly.
#
#   - You join as "Al" (first player). Your hand contains a rooted card
#     (no_condition -> always met, so the rooted token always fires).
#   - The opponent ("Bo") has ALREADY passed, so you only need to:
#         1) select the rooted card in your hand -> "Play the card"
#         2) click "Pass"
#     and the trip chain resolves: the card resolves, earns its rooted token, and
#     _process_rooted_cards() places it on the board (stopover 4) -> you see it
#     sitting there with the green rooted-token chip (the "solo" rooted state).
#   - Your hand also holds a second, cheap card, so on the NEXT turn you could play
#     it onto the same stopover to see the "layered" rooted state (rooted card
#     behind). (The next turn's mana phase needs both players, so the layered state
#     is easier to inspect in a dedicated game - see _rooted_ui_game.py.)
#
# Run from the project root:  uv run python tests/_rooted_quick_test.py
# NOTE: writes to games.db (like _diag.py). Prints the GAME ID at the end.
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

# --- pick a rooted card: no_condition (always fires), cheap, zero advancing (clean)
ROOTED = 'Twi22_38f79b'   # mana 2, advancing 0, effect rooted, condition no_condition
assert DB.filter(pl.col('card_id') == ROOTED).select('effect').item(row=0, column=0) == 'rooted'

# cheap filler cards for both players (anything playable, low mana)
# filler: simple 'advancing' cards only (no side effects, no other rooted, no
# instant/board effects) so the test stays unambiguous
filler_pool = (
    DB.filter(pl.col('mana') <= 2, pl.col('condition') == 'no_condition',
             pl.col('effect') == 'advancing', pl.col('card_id') != ROOTED)['card_id'].to_list()
)
AL_FILLER  = filler_pool[:6]
BO_FILLER  = filler_pool[6:12]

NAME_A, NAME_B = 'Al', 'Bo'   # 2 chars: passes the UI's name-length check
p1 = PlayerState(name=NAME_A, deck=[ROOTED] + AL_FILLER)
p2 = PlayerState(name=NAME_B, deck=list(BO_FILLER))
gid = ge.create_new_game(p1.model_dump())
ge.p2_connect_to_game(p2.model_dump(), gid)
conn, _, gs = ge.get_current_game(gid)
A, B = gs.players[NAME_A], gs.players[NAME_B]

# --- redistribute ALL of a player's cards into the exact zones we want.
#     p2_connect_to_game dealt 6 to hand, so collect hand+deck+mana and reassign:
#       mana  = 3 cheap cards (turn-1 mana is covered by these, per the rules)
#       hand  = the cards we want face-up for the test
#       deck  = everything else
def set_zones(p, want_hand, want_mana=3):
    all_cards = list(p.hand or []) + list(p.deck or []) + list(p.mana or [])
    hand = list(want_hand)
    mana = [c for c in all_cards if c not in hand][:want_mana]
    rest = [c for c in all_cards if c not in hand and c not in mana]
    p.hand, p.mana, p.deck = hand, mana, rest

set_zones(A, [ROOTED, AL_FILLER[0]])   # rooted card + 1 cheap card (for a 2nd-turn layered try)
set_zones(B, [BO_FILLER[0]])

# --- turn 1, Al first, PLAY phase. Bo has ALREADY passed -> Al only needs to
#     play + pass for the trip chain to resolve.
gs.turn = 1
gs.turn_order = [NAME_A, NAME_B]
gs.first_player_passed = False
gs.second_player_passed = True                      # Bo already passed (pre-acted)
for p in (A, B):
    p.mana_spend = 0
    p.action_chain = []
gs.rooted_on_board = []
gs.rooted_this_turn = []
gs.state = f"turn {gs.turn} - waiting for first player ({NAME_A}) to play"

conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gs.id))
conn.commit()
conn.close()

print(f"GAME ID: {gs.id}")
print(f"  you join as: {NAME_A}  (rooted card {ROOTED} is in your hand)")
print(f"  to test: select the rooted card -> 'Play the card' -> then 'Pass'")
print(f"  -> the card resolves, earns its rooted token, and stays on the board (stopover 4)")
