# Manual smoke test: the turn-1 FLOW behind the TUTORIAL SHOWCASE (ai_driver 3.0)
#
# `tests/_ai_tutorial_showcase.py` checks the rule on synthetic states. This one plays
# REAL games through the engine — INIT mana, the play-phase alternation, the resolution —
# with the human seat driven straight through `handle_websocket_message` and the robot
# seat driven by `ai_driver.ai_decide(..., tutorial=…)`, exactly like `run_ai_loop` does.
#
# What it proves, over N random Robot decks (20 mains + 10 support, as in production):
#   A. HARD RULE (tutorial ON): if the robot's opening hand holds a MAIN card it can pay
#      for (the opening pool is the 3 INIT cards = 3 mana), then the turn-1 trip chain
#      ends with a main-faction card played by the robot. The only escape from the
#      promise is a hand nothing can afford — then no rule could put a card down.
#   B. The guaranteed play is always a MAIN card: a support card (dwelling tap, pending
#      placement, mage instant) never counts as satisfying the showcase.
#   C. Baseline (tutorial OFF) is reported, and the flag never LOSES a game it could win:
#      ON-success ⊇ OFF-success on the same deck.
#
# Run from the project root:  uv run python tests/_ai_tutorial_flow.py
# (deletes its own games)
import sys, io, os, json, sqlite3, random
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
MAINS = set(DB['card_id'].to_list())
SUP = pl.read_parquet(os.path.join(PROJECT_ROOT, "cards", "support_factions.parquet"))
COST = {r['card_id']: int(r['mana']) for r in DB.iter_rows(named=True)}
COST.update({n: int(c) for n, c in zip(SUP['card_name'].to_list(), SUP['mana_cost'].to_list())})
HU, AI = "FlowHuman", "Robot"
N = int(os.environ.get("TUTO_FLOW_TRIALS", "12"))

passed = failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label} {extra}")
    else:
        failed += 1
        print(f"  FAIL {label} {extra}")


def state_of(gid):
    conn, _, g = ge.get_current_game(gid); conn.close(); return g


def send(gid, name, msg):
    g = state_of(gid)
    p = g.players[name].model_copy(); p.message = msg
    r = ge.handle_websocket_message(gid, p)
    r = json.loads(r) if isinstance(r, str) else r
    info = r.get("message", {}) if isinstance(r, dict) else {}
    return info.get("success", True), info.get("message", "")


def run_turn1(robot_deck, hu_deck, tutorial):
    """Play turn 1 of a real game; returns what the robot put on the trip chain."""
    gid = ge.create_new_game(PlayerState(name=HU, deck=list(hu_deck)).model_dump())
    ge.p2_connect_to_game(PlayerState(name=AI, deck=list(robot_deck)).model_dump(), gid)
    try:
        opening = list(state_of(gid).players[AI].hand or [])
        # could this opening hand pay for ANY main card with the 3 INIT mana?
        payable = any(c in MAINS and COST.get(c, 99) <= ge.start_cards_in_mana for c in opening)
        ai = PlayerAI(player_state=None)
        st, human_played = {}, False
        for _ in range(80):
            g = state_of(gid)
            if g.state == "game over" or g.winner or g.turn > 1:
                break
            # ---- the human seat: 3 INIT mana, then one affordable card, then pass
            if "put 3 cards in hand" in g.state:
                if len(g.players[HU].mana or []) < ge.start_cards_in_mana:
                    send(gid, HU, {"cards": list(g.players[HU].hand or [])[:3], "to": "mana",
                                   "mode": "", "pendings": []})
            elif "mana or pass" in g.state:
                send(gid, HU, {"cards": [], "to": "", "mode": "pass", "pendings": []})
            elif "to play" in g.state and g.state.split("(")[1].split(")")[0] == HU:
                hu = g.players[HU]
                avail = len(hu.mana or []) - (hu.mana_spend or 0)
                pick = next((c for c in (hu.hand or []) if COST.get(c, 99) <= avail), None)
                if not human_played and pick:
                    human_played = True
                    send(gid, HU, {"cards": [pick], "to": "stopover_4", "mode": "move", "pendings": []})
                else:
                    send(gid, HU, {"cards": [], "to": "", "mode": "pass", "pendings": []})
            # ---- the robot seat: exactly what run_ai_loop does
            g = state_of(gid)
            msg = ai_driver.ai_decide(g, AI, st, ai, tutorial=tutorial)
            if msg is None:
                continue
            ok, why = send(gid, AI, msg)
            if not ok:
                st.update(acted=False, tapped_this_turn=False)
                if msg.get("mode") != "pass":
                    st["fallback"] = True
        t1 = (state_of(gid).log or [{}])[0]
        entries = [e for s in t1.get("stopovers", []) for e in s.get("entries", [])]
        rob = [e for e in entries if e["player"] == AI]
        return {"payable": payable,
                "main": sum(1 for e in rob if e["cards"] and e["cards"][0] in MAINS),
                "entries": len(rob),
                "support_only": any(e["cards"] and e["cards"][0] not in MAINS for e in rob)}
    finally:
        conn = sqlite3.connect(os.path.join(PROJECT_ROOT, "games", "games.db"))
        conn.execute("DELETE FROM games WHERE game_id=?", (gid,))
        conn.commit(); conn.close()


def human_deck(seed):
    rng = random.Random(seed)
    fac = rng.choice(['Dwarves', 'Demons', 'Twigs', 'Miaous', 'Orcs', 'Mummies'])
    ids = DB.filter(DB['faction'] == fac)['card_id'].to_list()
    rng.shuffle(ids)
    return ids[:20]


random.seed(11)
print(f"{N} random Robot decks, turn 1 played for real, same deck with the flag OFF and ON\n")
hard_ok = hard_total = on_ok = off_ok = 0
for i in range(N):
    robot_deck = ai_driver.random_ai_deck()
    hu = human_deck(i)
    off = run_turn1(robot_deck, hu, False)
    on = run_turn1(robot_deck, hu, True)
    tag = f"trial {i:>2}"
    if on["payable"]:
        hard_total += 1
        hard_ok += 1 if on["main"] else 0
        check(f"{tag}: payable opening hand -> a main card reaches the chain",
              on["main"] >= 1, f"(OFF {off['main']}/{off['entries']} entries, ON {on['main']}/{on['entries']})")
    else:
        print(f"  --   {tag}: opening hand cannot pay for any main card -> no play is possible "
              f"(OFF {off['entries']}, ON {on['entries']} entries)")
    check(f"{tag}: the showcase never plays a support card as the guaranteed play",
          not (on["payable"] and on["support_only"] and on["main"] == 0))
    on_ok += 1 if on["main"] else 0
    off_ok += 1 if off["main"] else 0
    check(f"{tag}: the flag never loses a play the robot would have made anyway",
          not (off["main"] and not on["main"] and on["payable"]))

print(f"\n  robot reached the turn-1 chain with a MAIN card:  OFF {off_ok}/{N}   ON {on_ok}/{N}"
      f"   (guarantee held {hard_ok}/{hard_total} payable hands)")
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
