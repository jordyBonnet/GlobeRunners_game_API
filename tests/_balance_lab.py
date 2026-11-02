"""Balance lab smoke test — the whole pipeline on 2 games, in a TEMP directory.

Checks (no games.db, no balance_lab/ outputs touched):
  1. `decks.py` registry: 18 labels, 171 distinct matchups, 18 mirrors;
  2. `run_lab.build_valid_deck`: every archetype builds a 30-card deck that passes
     `deck_rules.check_deck` (20 main single-faction + 10 support 2x5);
  3. `run_lab.play_one`: a real AI-vs-AI game through `handle_websocket_message`
     (both seats = `ai_driver.ai_decide`) reaches an end and logs a coherent result row;
  4. the game is written to the shard, `merge_shards` copies it into a
     `balance_lab.db`-shaped DB (`games(game_id TEXT PRIMARY KEY, state_json TEXT)`);
  5. `report.build` renders an HTML page from the logged rows (+ regenerates assets).

Run (from the project root):
    uv run python tests/_balance_lab.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "balance_lab"))

import deck_rules                          # noqa: E402
import decks as lab_decks                  # noqa: E402  (balance_lab/decks.py)
import run_lab                             # noqa: E402

FAILS = []


def check(cond, label, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}{'' if cond else f'  -> {detail}'}")
    if not cond:
        FAILS.append(label)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="balance_lab_test_")
    run_lab.SHARDS_DIR = os.path.join(tmp, "shards")
    run_lab.RESULTS_CSV = os.path.join(tmp, "results.csv")
    run_lab.LAB_DB = os.path.join(tmp, "balance_lab.db")
    saved_out = sys.stdout
    run_lab.worker_init()               # patches ge.DB_PATH to the temp shard
    sys.stdout = saved_out
    import engine.game_engine as ge
    check(os.path.abspath(ge.DB_PATH).startswith(tmp), "worker patched ge.DB_PATH to the temp shard",
          ge.DB_PATH)
    check(os.path.abspath(ge.DB_PATH) != os.path.abspath(os.path.join(ROOT, "games", "games.db")),
          "games/games.db is NOT the lab target")

    print("\n1. deck registry")
    check(len(lab_decks.DECK_LABELS) == 18, "18 deck labels", lab_decks.DECK_LABELS)
    check(len(lab_decks.matchups()) == 171, "171 distinct matchups", len(lab_decks.matchups()))
    check(sum(1 for a, b in lab_decks.matchups() if a == b) == 18, "18 mirrors included")
    check(all(lab_decks.split_label(l) for l in lab_decks.DECK_LABELS), "every label splits into 2 factions")

    print("\n2. deck building (all 18 archetypes, deck_rules validation)")
    for label in lab_decks.DECK_LABELS:
        deck = run_lab.build_valid_deck(label)
        problems = deck_rules.check_deck(deck)
        check(len(deck) == 30 and not problems, f"{label}: 30 cards, check_deck clean",
              f"n={len(deck)} {problems}")

    print("\n3. two AI vs AI games (mirror + cross)")
    idx = lab_decks.LABEL_INDEX
    tasks = [(idx["Mum-mag"], idx["Mum-mag"], 999, 7, 4000, 60),      # Mummies + Mages mirror
             (idx["Dwa-eng"], idx["Twi-doc"], 999, 7, 4000, 60)]      # Dwarves/eng vs Twigs/doc
    rows = []
    for task in tasks:
        res = run_lab.play_one(task)
        la, lb = res["label_a"], res["label_b"]
        print(f"  · {la} vs {lb}: end={res['end_reason']} winner={res['winner_label']} "
              f"turns={res['turns']} ticks={res['ticks']} pos={res['pos_a']}/{res['pos_b']} "
              f"{res['seconds']}s")
        check(res["end_reason"] in ("win", "draw", "turn_limit", "stall", "max_ticks"),
              f"{la} vs {lb}: game terminated", res["error"] or res["end_reason"])
        check(not res["error"], f"{la} vs {lb}: no exception", res["error"])
        check(len(res["deck_a"].split()) == 30 and len(res["deck_b"].split()) == 30,
              f"{la} vs {lb}: 30-card decks logged")
        check(int(res["turns"]) >= 1 and int(res["ticks"]) > 1,
              f"{la} vs {lb}: the robot actually played", f"{res['turns']} turns / {res['ticks']} ticks")
        rows.append(res)
    run_lab.append_results(rows)

    print("\n4. storage (shard -> balance_lab.db, games.db schema)")
    n = run_lab.merge_shards()
    check(n == len(rows), f"merge_shards copied {len(rows)} games", n)
    conn = sqlite3.connect(run_lab.LAB_DB)
    cols = [(r[1], r[2]) for r in conn.execute("PRAGMA table_info(games)")]
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='games'").fetchone()[0]
    check(cols == [("game_id", "TEXT"), ("state_json", "TEXT")], "games(game_id TEXT, state_json TEXT)", cols)
    check("PRIMARY KEY" in sql.upper(), "game_id is PRIMARY KEY", sql)
    ids = {r[0] for r in conn.execute("SELECT game_id FROM games")}
    check(ids == {r["game_id"] for r in rows}, "every played game is in the DB", ids)

    print("\n4b. card conservation (every deck card still exists at the end)")
    for r in rows:
        state = conn.execute("SELECT state_json FROM games WHERE game_id = ?", (r["game_id"],)).fetchone()
        if not state:
            check(False, f"{r['game_id']}: state found")
            continue
        g = json.loads(state[0])
        check((r["winner_seat"] == "draw") == (r["winner_label"] == "draw")
              and r["winner_seat"] in ("a", "b", "draw"),
              f"{r['label_a']} vs {r['label_b']}: winner seat/label coherent",
              f"{r['winner_seat']} / {r['winner_label']}")
        for seat, name in (("a", "P1"), ("b", "P2")):
            deck = (r["deck_a"] if seat == "a" else r["deck_b"]).split()
            led = run_lab.card_ledger(deck, run_lab.player_zone_cards(g["players"][name], g, name))
            check(led["missing"] == 0 and led["ghost"] == 0,
                  f"{r['label_a']} vs {r['label_b']} seat {seat.upper()}: no lost / phantom card "
                  f"({led['in_zones']} cards, +{led['generated']} generated epo)",
                  led)
    conn.close()

    print("\n5. report")
    import report as lab_report
    html_path = lab_report.build(rows=rows, out_path=os.path.join(tmp, "report.html"))
    html = open(html_path, encoding="utf-8").read()
    check(len(html) > 8000, "report.html generated", len(html))
    check("Dwa" in html and "Mum" in html, "deck labels rendered")
    check('assets/logo_Dwa.png' in html and os.path.isfile(os.path.join(lab_report.ASSETS_DIR, "logo_Dwa.png")),
          "faction logo asset generated + referenced")
    check("head-to-head matrix" in html.lower(), "head-to-head matrix section present")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nBALANCE LAB SMOKE TEST OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
