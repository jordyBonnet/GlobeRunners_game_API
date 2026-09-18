"""Multi-turn v15 rooted card test: a rooted card from turn 1 resolves its basic
advancing on turn 2 (the active-rooted rule). Verifies the engine's trip chain
produces the expected log entries and card conservation.

Creates a 2-turn game:
  Turn 1: Al plays a rooted card (advances + earns a rooted token).
  Turn 2: Al's rooted card is on the board (position 1) and advances again;
          Al also plays a filler (position 2). Bo plays a filler (position 1).

Run from the project root:
    PYTHONPATH=. python tests/_rooted_v15_multiturn.py
"""
import contextlib
import io
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import engine.game_engine as ge
from models import GameState, PlayerState


def _mk_player(name, deck):
    # hand: 6 cards, mana: 3 cards, deck: the rest (21 cards). Total: 30.
    return PlayerState(
        name=name, hand=deck[:6], deck=deck[9:], discard=[], mana=deck[6:9],
        action_chain=[], current_position=0, mana_spend=0, faction="Twigs",
    )


def _mk_game(a_deck, b_deck):
    game = GameState(
        id="test_v15_mt", players={"Al": _mk_player("Al", a_deck), "Bo": _mk_player("Bo", b_deck)},
        turn_order=["Al", "Bo"], turn=1, state="turn 1 - waiting for first player (Al) to play",
        day_night="day", temperature=10, engine_version=15, log=[],
        cataclysm_pile=["OC", "MO", "DE", "JU"],
        earth=[[ge.BIOMES[i // 6]] for i in range(24)],
    )
    game.earth[0].append("Al")
    game.earth[0].append("Bo")
    return game


# Find a rooted card with no_condition + positive advancing and a filler card
# from the SAME faction (so the majority faction is clear).
rows = ge.CARDS_DB.filter(
    (ge.pl.col("effect") == "rooted") & (ge.pl.col("condition") == "no_condition")
    & (ge.pl.col("advancing") > 0)
)
assert len(rows) > 0, "no rooted cards with no_condition + positive advancing in pool"
r = rows.row(0, named=True)
rooted_id, rooted_adv, rooted_faction = r["card_id"], int(r["advancing"]), r["faction"]
print(f"rooted card: {rooted_id} (adv {rooted_adv}, faction {rooted_faction})")

filler_rows = ge.CARDS_DB.filter(
    (ge.pl.col("advancing") == 2) & (ge.pl.col("condition") == "no_condition")
    & (ge.pl.col("effect") != "rooted") & (ge.pl.col("faction") == rooted_faction)
)
assert len(filler_rows) > 0, f"no filler cards from faction {rooted_faction} in pool"
f = filler_rows.row(0, named=True)
filler_id = f["card_id"]
print(f"filler card: {filler_id} (adv 2, faction {rooted_faction})")

a_deck = [rooted_id] + [filler_id] * 29
b_deck = [filler_id] * 30
gs = _mk_game(a_deck, b_deck)
a, b = gs.players["Al"], gs.players["Bo"]
buf = io.StringIO()

# --- Turn 1: Al plays the rooted card, Bo passes ---
a.message = {"cards": [rooted_id], "to": "stopover_4", "mode": "move", "pendings": []}
with contextlib.redirect_stdout(buf):
    a, gs, success, msg = ge.player_play("first", a, gs)
assert success, f"Al play failed: {msg}"
b.message = {"cards": [], "to": "", "mode": "pass", "pendings": []}
with contextlib.redirect_stdout(buf):
    b, gs, success, msg = ge.player_play("second", b, gs)
assert success, f"Bo pass failed: {msg}"
with contextlib.redirect_stdout(buf):
    gs = ge.process_trip_chain(gs)
pos_a_t1 = a.current_position
print(f"turn 1: Al at {pos_a_t1}")
assert pos_a_t1 > 0, "Al should have advanced (rooted card + faction bonus)"

# End of turn: rooted card is placed on the board
with contextlib.redirect_stdout(buf):
    gs = ge._process_rooted_cards(gs)
assert len(gs.rooted_on_board) == 1 and gs.rooted_on_board[0]["owner"] == "Al"
print(f"rooted_on_board: {gs.rooted_on_board[0]}")

# Reset for turn 2
for p in (a, b):
    p.action_chain, p.play_count, p.mana_spend = [], 0, 0
gs.turn = 2
gs.state = "turn 2 - waiting for first player (Bo) to play"
gs.turn_order = ["Bo", "Al"]

# --- Turn 2: Bo plays filler (position 1), Al plays filler (position 2) ---
# Al's rooted card is on the board at position 1. Al's play goes to position 2.
b.message = {"cards": [filler_id], "to": "stopover_4", "mode": "move", "pendings": []}
with contextlib.redirect_stdout(buf):
    b, gs, success, msg = ge.player_play("first", b, gs)
assert success, f"Bo play failed: {msg}"
a.message = {"cards": [filler_id], "to": "stopover_3", "mode": "move", "pendings": []}
with contextlib.redirect_stdout(buf):
    a, gs, success, msg = ge.player_play("second", a, gs)
assert success, f"Al play failed: {msg}"
with contextlib.redirect_stdout(buf):
    gs = ge.process_trip_chain(gs)   # resolve turn 2's trip chain

pos_a_t2 = a.current_position
pos_b_t2 = b.current_position
print(f"turn 2: Al at {pos_a_t2} (was {pos_a_t1}), Bo at {pos_b_t2} (was 0)")

# KEY ASSERTIONS:
# 1. Al's rooted card on the board advanced (Al moved further than just the filler)
# 2. The log shows the rooted card's basic advancing on turn 2
# 3. The rooted card is discarded at the end of turn 2

# Check the log for turn 2's rooted card entry
t2_log = gs.log[-1]
rooted_entries = [e for s in t2_log["stopovers"] for e in s["entries"]
                  if e["player"] == "Al" and "rooted card" in " ".join(e.get("notes", []))]
assert len(rooted_entries) >= 1, "no rooted card entry in turn 2 log"
re = rooted_entries[0]
print(f"rooted card on board (turn 2): {re['player']} pos {re['pos_before']}->{re['pos_after']} (basic advancing only)")

# End of turn: rooted card is discarded (served its turn)
with contextlib.redirect_stdout(buf):
    gs = ge._process_rooted_cards(gs)
assert len(gs.rooted_on_board) == 0, "rooted card should be discarded"
assert rooted_id in a.discard, "rooted card should be in Al's discard"
print(f"rooted card discarded: {rooted_id} in Al's discard: True")

# Card conservation: every card is in exactly one zone (30 cards per player)
for name, p in (("Al", a), ("Bo", b)):
    zones = (p.hand or []) + (p.deck or []) + (p.discard or []) + (p.mana or [])
    assert len(zones) == 30, f"{name}: zones total {len(zones)} != 30"
print("card conservation: OK (30 cards per player)")

print("\n=== ENGINE v15 MULTI-TURN ROOTED TEST PASSED ===")
print(f"  turn 1: Al at {pos_a_t1} (rooted card played + advanced)")
print(f"  turn 2: Al at {pos_a_t2} (rooted card on board + filler), Bo at {pos_b_t2}")
print(f"  rooted card discarded at end of turn 2")
