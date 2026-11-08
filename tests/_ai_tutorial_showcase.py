# Manual smoke test: the TUTORIAL SHOWCASE rule in ai_decide (game_ui/ai_driver.py 3.0)
#
# When the setup page's "I want to activate the quick ingame tutorial" box is checked,
# the frontend sends `tutorial: true` to /create_game_ai, which starts the robot with
# run_ai_loop(..., tutorial=True). The tutorial then replays turn 1's trip chain card by
# card from the log — so the turn NEEDS a real opponent card on that chain.
#
# Rule under test: while the robot has no MAIN-faction card on its own trip chain, it
# plays one before anything else (a support card / a dwelling tap / a pass are not a
# main card), and PlayerAI's pass discipline (C #26: no positive expected value -> pass)
# is lifted for that single play via play_card(force=True).
#
# Checks:
#   1. a WEAK card exists (a single card the robot refuses to play: play_card() -> None)
#   2. the same hand with force=True -> a main-faction MOVE play (the showcase escape)
#   3. ai_decide(tutorial=False) on that hand -> pass; ai_decide(tutorial=True) -> the play
#   4. once a main card is on the chain, the showcase is over -> normal behaviour again
#   5. weak main card + a support card in hand -> the MAIN card is played, not the support
#   6. turn 2+ is not forced (the tutorial replays turn 1's resolution only)
#
# Run from the project root:  uv run python tests/_ai_tutorial_showcase.py
# (deletes its own games; every state below is an in-memory copy — nothing is saved)
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "game_ui"))

import polars as pl
import engine.game_engine as ge
from models import PlayerState
import ai_driver                                   # noqa: E402
from player_ai.playerai import PlayerAI            # noqa: E402

DB = ge.CARDS_DB
NAME_AI, NAME_HU = "Robot", "Human"

passed = failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label} {extra}")
    else:
        failed += 1
        print(f"  FAIL {label} {extra}")


# ---------------------------------------------------------------- the synthetic game
deck_ai = DB.filter(pl.col('faction') == 'Dwarves')['card_id'].to_list()[:20]
deck_hu = DB.filter(pl.col('faction') == 'Demons')['card_id'].to_list()[:20]
gid = ge.create_new_game(PlayerState(name=NAME_AI, deck=list(deck_ai)).model_dump())
ge.p2_connect_to_game(PlayerState(name=NAME_HU, deck=list(deck_hu)).model_dump(), gid)


def scenario(hand, chain=None, mana=6, pos=0, turn=1):
    """A PLAY-phase turn where the robot is expected to act, with a synthetic hand.
    Everything happens on the in-memory GameState returned by get_current_game: the DB
    row is never rewritten, so the game stays exactly as created."""
    conn, _, game = ge.get_current_game(gid)
    conn.close()
    game.turn = turn
    game.turn_order = [NAME_AI, NAME_HU]
    game.state = f"turn {turn} - waiting for first player ({NAME_AI}) to play"
    me = game.players[NAME_AI]
    me.hand, me.mana, me.mana_spend = list(hand), ["mana"] * mana, 0
    me.current_position, me.action_chain, me.play_count = pos, list(chain or []), len(chain or [])
    me.dwelling, me.dwelling_tapped, me.dwelling_slot = None, False, None
    me.pendings, me.pending_slots = [], []
    me.message = None
    oppo = game.players[NAME_HU]
    oppo.hand, oppo.mana, oppo.deck = ["x"] * 5, ["m"] * 3, ["y"] * 10
    oppo.action_chain = []
    return game


def probe_ai(game):
    """A PlayerAI looking at the scenario state (for the direct play_card checks)."""
    ai = PlayerAI(player_state=None)
    ai.update_player_state(game.players[NAME_AI], game.players[NAME_HU], game)
    return ai


# 1. a WEAK card: scored <= 0 expected value, so the robot refuses to play it
weak = None
for cid in deck_ai:
    g = scenario([cid])
    ai = probe_ai(g)
    row = DB.filter(pl.col('card_id') == cid).row(0, named=True)
    if ai.play_card() is None and not ai._will_be_blocked(row):
        weak = cid
        break
check("1 a weak card exists (pass discipline refuses it)", weak is not None, f"-> {weak}")
if weak is None:
    print("no weak card found; nothing to test")
    sys.exit(1)
w = DB.filter(pl.col('card_id') == weak).row(0, named=True)
print(f"     weak card {weak}: adv={w['advancing']} mana={w['mana']} cond={w['condition']} eff={w['effect']}")

# 2. force=True escapes the pass discipline, with a MAIN card in move mode
g = scenario([weak])
ai = probe_ai(g)
forced = ai.play_card(force=True)
check("2 force=True plays a main move", bool(forced) and forced.get('mode') == 'move'
      and forced['cards'][0] in ai.MAIN_ROWS, f"-> {forced and forced['cards']}")

# 3. ai_decide: the tutorial flag is the only difference between passing and playing
g = scenario([weak])
plain = ai_driver.ai_decide(g, NAME_AI, {}, PlayerAI(player_state=None), tutorial=False)
check("3a tutorial OFF -> pass", bool(plain) and plain.get('mode') == 'pass', f"-> {plain and plain.get('mode')}")
g = scenario([weak])
on = ai_driver.ai_decide(g, NAME_AI, {}, PlayerAI(player_state=None), tutorial=True)
check("3b tutorial ON -> main move on the chain",
      bool(on) and on.get('mode') == 'move' and on['cards'] and on['cards'][0] in ai.MAIN_ROWS,
      f"-> {on and (on['cards'], on.get('mode'))}")
check("3c the play carries the robot's next stopover", bool(on) and str(on.get('to', '')).startswith('stopover_'),
      f"-> {on and on.get('to')}")

# 4. the showcase is satisfied by ONE main card -> normal behaviour resumes (no second forced card)
already = [{'player': NAME_AI, 'order': 1, 'mode': 'move', 'cards': [weak], 'to': 'stopover_4'}]
g = scenario([weak], chain=already)
again = ai_driver.ai_decide(g, NAME_AI, {}, PlayerAI(player_state=None), tutorial=True)
check("4 main card already on the chain -> not forced again", again and again.get('mode') == 'pass',
      f"-> {again and again.get('mode')}")

# 5. a support card in hand does NOT satisfy the showcase: the MAIN card wins
SUPPORT = 'black_hole'
g = scenario([weak, SUPPORT])
sup = probe_ai(g).play_card()                                  # still refuses the weak main card
g = scenario([weak, SUPPORT])
sup_on = ai_driver.ai_decide(g, NAME_AI, {}, PlayerAI(player_state=None), tutorial=True)
check("5 support card does not count as the showcase play",
      sup is None and bool(sup_on) and sup_on.get('mode') == 'move' and sup_on['cards'] == [weak],
      f"-> refused={sup is None}, played={sup_on and sup_on['cards']}")

# 6. turn 2+ is never forced (the tutorial replays turn 1's resolution only)
g = scenario([weak], turn=2)
late = ai_driver.ai_decide(g, NAME_AI, {}, PlayerAI(player_state=None), tutorial=True)
check("6 turn 2 -> not forced", late and late.get('mode') == 'pass', f"-> {late and late.get('mode')}")

# ------------------------------------------------------------------------ cleanup
try:
    conn = sqlite3.connect(os.path.join(PROJECT_ROOT, "games", "games.db"))
    conn.execute("DELETE FROM games WHERE game_id=?", (gid,))
    conn.commit()
    conn.close()
    print(f"  (deleted test game {gid})")
except Exception as e:                                  # pragma: no cover
    print(f"  (cleanup failed: {e})")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
