# Manual smoke helper: reproduce the "play lands on an occupied stopover" bug so a
# HUMAN can verify the SKIP rule in the web UI.
#
#   - A ROOTED card is already on the board (rooted_on_board) in stopover column 4
#     (the right-most slot, stopover "1") — it survived from a previous turn.
#   - It is now turn 2, Al's play phase. Al has a normal card in hand.
#   - OLD (buggy) behavior: Al's 1st play -> column 4 -> LANDS ON the rooted card.
#   - NEW (fixed) behavior:  Al's 1st play -> column 3 -> SKIPS the rooted card.
#
#   In the UI you should see:
#     * the rooted card (green frame + chip) sitting in the right-most slot (col 4),
#     * the "next" slot highlight on the slot to its LEFT (col 3), not col 4,
#     * when you play your card it goes to col 3 (stopover "2"), NOT on the rooted card.
#
# Run from the project root:  uv run python tests/_rooted_skip_test.py
# NOTE: writes to games.db (like _diag.py). Prints the GAME ID at the end.
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

# a rooted card to sit on the board (column 4) — no_condition, cheap
ROOTED = 'Twi22_38f79b'   # rooted, no_condition
# normal (advancing, no_condition) cards for both players' hands/decks
NORMALS = DB.filter(pl.col('mana') <= 2, pl.col('condition') == 'no_condition',
                    pl.col('effect') == 'advancing', pl.col('card_id') != ROOTED)['card_id'].to_list()[:8]
NORMAL = NORMALS[0]

NAME_A, NAME_B = 'Al', 'Bo'
p1 = PlayerState(name=NAME_A, deck=list(NORMALS))
p2 = PlayerState(name=NAME_B, deck=list(NORMALS[4:8] + NORMALS[:4]))
gid = ge.create_new_game(p1.model_dump())
ge.p2_connect_to_game(p2.model_dump(), gid)
conn, _, gs = ge.get_current_game(gid)
A, B = gs.players[NAME_A], gs.players[NAME_B]

def set_zones(p, want_hand, want_mana=3):
    all_cards = list(p.hand or []) + list(p.deck or []) + list(p.mana or [])
    hand = list(want_hand)
    mana = [c for c in all_cards if c not in hand][:want_mana]
    rest = [c for c in all_cards if c not in hand and c not in mana]
    p.hand, p.mana, p.deck = hand, mana, rest

set_zones(A, [NORMAL])
set_zones(B, [NORMAL])

# --- a rooted card is ALREADY on the board (column 4 = stopover "1"), owned by Al.
#     This is the state at the start of turn 2 (the card survived turn 1's cleaning).
gs.rooted_on_board = [{'card_id': ROOTED, 'owner': NAME_A, 'stopover': 'stopover_4'}]
gs.rooted_this_turn = []

# turn 2, Al first, PLAY phase. Bo already passed -> Al only needs to play + pass.
gs.turn = 2
gs.turn_order = [NAME_A, NAME_B]
gs.first_player_passed = False
gs.second_player_passed = True
for p in (A, B):
    p.mana_spend = 0
    p.action_chain = []
gs.state = f"turn {gs.turn} - waiting for first player ({NAME_A}) to play"

conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gs.id))
conn.commit()
conn.close()

print(f"GAME ID: {gs.id}")
print(f"  you join as: {NAME_A}")
print(f"  a rooted card ({ROOTED}) is ALREADY on the board in the right-most slot (col 4).")
print(f"  your card ({NORMAL}) should play to the slot to its LEFT (col 3), NOT on top of it.")
print(f"  -> select your card -> 'Play the card' -> watch which slot it lands in")
