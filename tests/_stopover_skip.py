# Manual smoke test: stopover placement rules — engine as the single source of truth.
#
# engine_version 15 (CURRENT) uses PER-PLAYER stopover positions (a player's plays
# go after the player's OWN rooted cards; the opponent's board never shifts your
# position). ge._player_stopover / ge._player_chain are the v15 source of truth and
# are covered by tests/_rooted_v15_multiturn.py + tests/_dwelling_playcount.py.
#
# This file tests the LEGACY v14 SHARED-skip rule (ge._next_free_stopover_v14) that
# is still kept in the engine so v14 stored games replay exactly as they were played:
#
#   1. helper (v14): no furniture -> stopover_4, stopover_3, ... (legacy order)
#   2. helper (v14): rooted card on stopover_4  -> 1st action goes to stopover_3 (SKIP)
#   3. helper (v14): dwelling placeholder col 4 -> 1st action goes to stopover_3 (SKIP)
#   4. _process_rooted_cards (engine_version 14): dwelling on col 4 -> rooted
#      survivors land on stopover_3, stopover_2 (NOT stopover_4)
#   5. _process_rooted_cards (engine_version 13, legacy): same setup -> the plain
#      convention is kept (stopover_4, stopover_3 — NO skip), so old stored games
#      (e.g. 26_09_15_20_11_18_AAMLM) keep verifying
#   6. dwelling PLACE (engine_version 15, CURRENT): per-player — Al's placeholder
#      lands on Al's OWN first position (col 4); the opponent's rooted card does NOT
#      shift it (the v14 shared-skip rule is superseded)
#
# Run from the project root:  uv run python tests/_stopover_skip.py
# NOTE: test 6 writes ONE game to games.db (like _diag.py) and deletes it.
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import GameState, PlayerState

DB = ge.CARDS_DB
ok = 0

# --- 1. helper (v14 shared-skip), no furniture: legacy order 4, 3, 2, 1, 0
g = GameState(id='t1', players={})
assert ge._next_free_stopover_v14(g, 0, 'A') == 'stopover_4', ge._next_free_stopover_v14(g, 0, 'A')
assert ge._next_free_stopover_v14(g, 1, 'A') == 'stopover_3'
assert ge._next_free_stopover_v14(g, 4, 'A') == 'stopover_0'
ok += 1; print('1) helper (v14), no furniture -> 4,3,2,1,0      OK')

# --- 2. helper (v14), rooted card on stopover_4 -> skip it
g2 = GameState(id='t2', players={})
g2.rooted_on_board = [{'card_id': 'R1', 'owner': 'A', 'stopover': 'stopover_4'}]
assert ge._next_free_stopover_v14(g2, 0, 'A') == 'stopover_3', ge._next_free_stopover_v14(g2, 0, 'A')
assert ge._next_free_stopover_v14(g2, 1, 'A') == 'stopover_2'
ok += 1; print('2) helper (v14), rooted on stopover_4 -> skip   OK')

# --- 3. helper (v14), dwelling placeholder on col 4 -> skip it
g3 = GameState(id='t3', players={'A': PlayerState(name='A', deck=[], dwelling='refinery', dwelling_slot=4)})
assert ge._next_free_stopover_v14(g3, 0, 'A') == 'stopover_3', ge._next_free_stopover_v14(g3, 0, 'A')
# both reserved (rooted col 4 + dwelling col 3) -> 1st action goes to stopover_2
g3.rooted_on_board = [{'card_id': 'R1', 'owner': 'A', 'stopover': 'stopover_4'}]
g3.players['A'].dwelling_slot = 3
assert ge._next_free_stopover_v14(g3, 0, 'A') == 'stopover_2', ge._next_free_stopover_v14(g3, 0, 'A')
ok += 1; print('3) helper (v14), dwelling reserved -> skip      OK')

# --- 4. _process_rooted_cards (engine_version 14) skips the dwelling's column AND
# each just-placed rooted card (ge._next_free_stopover_v14 re-reads rooted_on_board,
# which grows as the loop appends). So with a dwelling on col 4: RA -> col 3,
# RB -> col 1 (it skips the dwelling AND the just-placed RA). This is the v15
# engine's v14-legacy branch (faithful to the .pyc) and is what v14 stored games
# replay against.
g4 = GameState(id='t4', engine_version=14,
               players={'A': PlayerState(name='A', deck=[], hand=[], discard=['RA', 'RB'])})
g4.players['A'].dwelling = 'refinery'
g4.players['A'].dwelling_slot = 4
g4.rooted_this_turn = [{'card_id': 'RA', 'owner': 'A'}, {'card_id': 'RB', 'owner': 'A'}]
ge._process_rooted_cards(g4)
stops = [e['stopover'] for e in g4.rooted_on_board]
assert stops == ['stopover_3', 'stopover_1'], stops
# the survivors were PULLED OUT of the discard (they are on the board now)
assert 'RA' not in g4.players['A'].discard and 'RB' not in g4.players['A'].discard
ok += 1; print('4) rooted placement skips dwelling + placed (v14) OK', stops)

# --- 5. _process_rooted_cards (engine_version 13) keeps the legacy plain convention
g5 = GameState(id='t5', engine_version=13,
               players={'A': PlayerState(name='A', deck=[], hand=[], discard=['RA', 'RB'])})
g5.players['A'].dwelling = 'refinery'
g5.players['A'].dwelling_slot = 4
g5.rooted_this_turn = [{'card_id': 'RA', 'owner': 'A'}, {'card_id': 'RB', 'owner': 'A'}]
ge._process_rooted_cards(g5)
stops5 = [e['stopover'] for e in g5.rooted_on_board]
assert stops5 == ['stopover_4', 'stopover_3'], stops5   # legacy: NO skip
ok += 1; print('5) rooted placement legacy plain (v13)          OK', stops5)

# --- 6. dwelling PLACE with a rooted card on stopover_4 -> dwelling_slot == 3
filler = DB.filter(pl.col('mana') <= 2, pl.col('condition') == 'no_condition',
                   pl.col('effect') == 'advancing')['card_id'].to_list()[:8]
p1 = PlayerState(name='Al', deck=['refinery'] + filler)
p2 = PlayerState(name='Bo', deck=list(filler))
gid = ge.create_new_game(p1.model_dump())
ge.p2_connect_to_game(p2.model_dump(), gid)
conn, _, gs = ge.get_current_game(gid)
A = gs.players['Al']
# hand = [refinery], mana = 3 (refinery costs 3)
allc = list(A.hand or []) + list(A.deck or [])
A.hand = ['refinery']
A.mana = [c for c in allc if c != 'refinery'][:3]
A.deck = [c for c in allc if c != 'refinery'][3:]
gs.state = f"turn {gs.turn} - waiting for first player ({gs.turn_order[0]}) to play"
# v15 is PER-PLAYER: Al's placeholder lands on Al's OWN first position (col 4).
# The opponent's (Bo's) rooted card on col 4 does NOT shift Al (unlike v14 shared-skip).
gs.rooted_on_board = [{'card_id': 'FAKE_ROOTED', 'owner': gs.turn_order[1], 'stopover': 'stopover_4'}]
A.message = {'cards': ['refinery'], 'to': 'dwelling', 'mode': '', 'pendings': []}
player, gs, success, msg = ge.player_play('first', A, gs)
assert success, msg
assert A.dwelling == 'refinery'
# Al has NO rooted cards of his own -> his first position is col 4 (the opponent's
# rooted card does not shift Al — that is the v15 per-player rule).
assert A.dwelling_slot == 4, f"dwelling_slot={A.dwelling_slot} (expected 4 — Al's own position 1, per-player)"
# and with Al's OWN rooted card on col 4, his placeholder would skip to col 3:
gs.rooted_on_board = [{'card_id': 'AL_ROOTED', 'owner': gs.turn_order[0], 'stopover': 'stopover_4'}]
slot2 = ge._player_stopover(gs, gs.turn_order[0], 0)
assert int(slot2.rsplit('_', 1)[-1]) == 3, f"slot2={slot2} (expected stopover_3 — Al skips his own rooted card)"
conn.execute("DELETE FROM games WHERE game_id = ?", (gs.id,))   # clean up the test game
conn.commit(); conn.close()
assert (gs.engine_version or 0) >= 15, gs.engine_version   # new games are v15
ok += 1; print('6) dwelling PLACE per-player (v15)              OK  (slot =', A.dwelling_slot, ', engine_version =', gs.engine_version, ')')

print(f'\nALL {ok} STOPOVER-SKIP TESTS PASSED')
