"""Tests of the DEFENSE mode (condition "block").

Scenarios (2 players via the engine, no server):
  A. Opponent's card NOT blocked on another stopover + UNSTOPPABLE card
     (condition met) that ignores the block on the same stopover as the defense card.
     Since no block occurred, the block card's effect (draw 1) must NOT fire.
  B. Blocked card: cumulative shields (>= card's mana) -> no effect, no advancing.
     The "block" condition defense card plays ITS effect (draw 1) at block time.
  C. Block failure: shields < mana -> the card plays normally.
  D. Message validation (message_check) in defend mode.

Run (from the project root):
    python tests/_block_mode.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl  # noqa: E402
import engine.game_engine as ge  # noqa: E402
from models import PlayerState  # noqa: E402

POOL = ge.get_cardpool()
FAILURES = []
EXTRA_DECK = ["EXTRA1", "EXTRA2", "EXTRA3", "EXTRA4", "EXTRA5", "EXTRA6"]   # out of pool: reserve (draws + initial deck)
MANA_POOL = ["M1", "M2", "M3", "M4", "M5"]     # fake mana (cost = mana of the played cards, not of the mana cards)


def check(label, cond, detail=""):
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def find_card(faction_prefix="Dwa", **kw):
    df = POOL.filter(pl.col("faction").str.starts_with(faction_prefix))
    for k, v in kw.items():
        df = df.filter(pl.col(k) == v)
    if df.is_empty():
        raise RuntimeError(f"no card for {faction_prefix} {kw}")
    return df.row(0, named=True)


def setup_game(alice_hand, bob_hand):
    """creates a 2-player game, returns game_id with Alice first and forced hands"""
    alice = PlayerState(name="Alice", deck=list(alice_hand) + list(EXTRA_DECK))
    gid = ge.create_new_game(alice.model_dump())
    bob = PlayerState(name="Bob", deck=list(bob_hand) + list(EXTRA_DECK))
    ge.p2_connect_to_game(bob.model_dump(), gid)

    # deterministic state: Alice plays first, forced hands/mana, whole board in MO biome
    conn, _, game = ge.get_current_game(gid)
    c = conn.cursor()
    game.turn_order = ["Alice", "Bob"]
    game.state = f"turn {game.turn} - waiting for first player (Alice) to play"
    a, b = game.players["Alice"], game.players["Bob"]
    a.hand, a.mana = list(alice_hand), list(MANA_POOL)
    b.hand, b.mana = list(bob_hand), list(MANA_POOL)
    a.mana_spend = b.mana_spend = 0
    a.deck = list(EXTRA_DECK); b.deck = list(EXTRA_DECK)
    a.discard, b.discard = [], []
    # force the whole board into MO biome (biome_Dwa = MO/OC) so that
    # biome-type conditions are deterministic (the player starts at position 0)
    for i in range(len(game.earth)):
        cell = game.earth[i]
        if cell and cell[0] in ge.BIOMES:
            cell[0] = "MO"
        else:
            cell.insert(0, "MO")
    c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (game.to_json(), gid))
    conn.commit()
    conn.close()
    return gid


def act(gid, name, cards, to, mode):
    conn, _, game = ge.get_current_game(gid)
    p = game.players[name].model_copy()
    p.message = {"cards": cards, "to": to, "mode": mode, "pendings": []}
    conn.close()
    resp = ge.handle_websocket_message(gid, p)
    info = json.loads(resp)
    assert info["message"]["success"], f"{name} {mode} {cards}->{to} refused: {info['message']}"
    return info


def expected_advance(row):
    """total advancing of a card without condition (no_condition is always met)"""
    adv = int(row["advancing"])
    if row["effect"] in ("advancing", "backward", "jump"):
        adv += int(row["effect_number"])
    return adv


def scenario_a_unstoppable_ignores_block():
    print("\nA. unstoppable (condition met) ignores the block + other card not blocked")
    move = find_card("Dwa", mana=2, effect="advancing")                    # basic + bonus advancing
    unstop = find_card("Dwa", effect="unstoppable", condition="biome_Dwa") # ignores the block
    block = find_card("Dwa", condition="block", effect="draw")             # draw 1 + shields
    defend = find_card("Dwa", condition="no_condition", effect="draw")     # plain shields

    gid = setup_game([move["card_id"], unstop["card_id"]], [block["card_id"], defend["card_id"]])

    conn, _, g = ge.get_current_game(gid); bob_hand0 = len(g.players["Bob"].hand); conn.close()

    act(gid, "Alice", [move["card_id"]], "stopover_3", "move")                    # stopover 3: nobody defends
    act(gid, "Bob", [block["card_id"], defend["card_id"]], "stopover_4", "defend")  # cumulative shields on stopover 4
    act(gid, "Alice", [unstop["card_id"]], "stopover_4", "move")                  # stopover 4: block present, unstoppable
    act(gid, "Alice", [], "", "pass")
    act(gid, "Bob", [], "", "pass")

    conn, _, g = ge.get_current_game(gid); conn.close()
    alice, bob = g.players["Alice"], g.players["Bob"]
    # NB: the engine draws 3 cards into hand at the end of the turn (start of the next turn).
    # NB: setup forces the whole board into the MO biome and Alice is a Dwarven
    # (home biome MO/OC) -> EVERY forward movement gets the +1 faction biome bonus.
    exp1 = expected_advance(move) + 1                    # +1 biome bonus (token on MO)
    exp2 = exp1 + expected_advance(unstop) + 1           # +1 biome bonus (still on MO)
    check("final position accumulates both cards (+ biome bonus on each move)",
          alice.current_position == exp2, f"pos={alice.current_position}, exp. {exp2}")
    # bob: initial hand - 2 defense cards played + 0 (no draw: NO block occurred, the
    # unstoppable card slipped through) + 3 (turn draw). The block card's effect must NOT fire.
    check("bob: the effect of his block card (draw 1) did NOT fire (no block happened)",
          len(bob.hand) == bob_hand0 - 2 + ge.turn_n_draw_cards,
          f"hand bob {bob_hand0} -> {len(bob.hand)} (exp. {bob_hand0 - 2 + ge.turn_n_draw_cards})")


def scenario_b_card_blocked():
    print("\nB. blocked card: shields >= mana -> no effect, no advancing")
    move = find_card("Dwa", mana=2, effect="advancing")
    block = find_card("Dwa", condition="block", effect="draw")
    assert block["shield"] >= move["mana"], "setup: the block's shield must be >= the move's mana"

    gid = setup_game([move["card_id"]], [block["card_id"]])

    conn, _, g = ge.get_current_game(gid); bob_hand0 = len(g.players["Bob"].hand); conn.close()

    act(gid, "Alice", [move["card_id"]], "stopover_4", "move")      # same stopover as the defense
    act(gid, "Bob", [block["card_id"]], "stopover_4", "defend")
    act(gid, "Alice", [], "", "pass")
    act(gid, "Bob", [], "", "pass")

    conn, _, g = ge.get_current_game(gid); conn.close()
    alice, bob = g.players["Alice"], g.players["Bob"]
    check("Alice stays at position 0 (blocked)", alice.current_position == 0, f"pos={alice.current_position}")
    # bob: initial hand - 1 defense card played + 1 (draw from the block card, fired at
    # block time) + 3 (turn draw)
    check("bob: the effect of his block card (draw 1) fired at block time",
          len(bob.hand) == bob_hand0 - 1 + 1 + ge.turn_n_draw_cards,
          f"hand bob {bob_hand0} -> {len(bob.hand)} (exp. {bob_hand0 - 1 + 1 + ge.turn_n_draw_cards})")
    check("the blocked card went to the discard pile", move["card_id"] in (alice.discard or []),
          f"discard={alice.discard}")


def scenario_c_block_fails():
    print("\nC. block failure: shields < mana -> the card plays normally")
    move = find_card("Dwa", mana=2, effect="advancing")
    weak = POOL.filter((pl.col("shield") < move["mana"]) & (pl.col("condition") != "block"))
    if weak.is_empty():
        weak = POOL.filter((pl.col("shield") < 3) & (pl.col("condition") != "block"))
    weak = weak.row(0, named=True)

    gid = setup_game([move["card_id"]], [weak["card_id"]])

    act(gid, "Alice", [move["card_id"]], "stopover_4", "move")
    act(gid, "Bob", [weak["card_id"]], "stopover_4", "defend")
    act(gid, "Alice", [], "", "pass")
    act(gid, "Bob", [], "", "pass")

    conn, _, g = ge.get_current_game(gid); conn.close()
    alice = g.players["Alice"]
    check(f"Alice advances normally (shields {weak['shield']} < mana {move['mana']})",
          alice.current_position > 0, f"pos={alice.current_position}")


def scenario_d_message_validation():
    print("\nD. defend message validation")
    base = {"cards": ["X"], "to": "stopover_4", "mode": "defend", "pendings": []}
    ok, msg = ge.message_check(dict(base))
    check("defend 1 card -> valid", ok, msg)
    ok, msg = ge.message_check({**base, "cards": []})
    check("defend 0 cards -> invalid", not ok, msg)
    ok, msg = ge.message_check({**base, "to": "mana"})
    check("defend to=mana -> invalid", not ok, msg)
    ok, msg = ge.message_check({"cards": ["A", "B", "C", "D", "E", "F"], "to": "stopover_4", "mode": "defend", "pendings": []})
    check("defend 6 cards -> invalid (max 5)", not ok, msg)
    ok, msg = ge.message_check({"cards": [], "to": "stopover_4", "mode": "move", "pendings": []})
    check("move 0 cards -> invalid", not ok, msg)


if __name__ == "__main__":
    for fn in (scenario_a_unstoppable_ignores_block, scenario_b_card_blocked,
               scenario_c_block_fails, scenario_d_message_validation):
        try:
            fn()
        except Exception as e:
            FAILURES.append(f"{fn.__name__} crashed: {e}")
            print(f"  [FAIL] {fn.__name__} crashed: {e}")

    print()
    if FAILURES:
        print(f"BLOCK MODE: {len(FAILURES)} failure(s):")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("BLOCK MODE: all tests OK")
