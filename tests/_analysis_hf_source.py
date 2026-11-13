# Manual smoke test: the analysis app reading the HF BACKUP DATASET
#
# WHY: finished games are uploaded to `jordyBonnet/globerunners-games` by
# `game_ui/hf_backup.py` (the deployed container disk is ephemeral, so the games
# played on the hosted deployment exist ONLY there). `games/analysis/hf_source.py`
# turns that dataset into a second source for the analysis app (port 8017), so
# those games can be replayed/analyzed like a local ones.
#
# Checks (no writes to games.db, no uploads):
#   A. id safety      - a URL-supplied game id can never escape path_in_repo/mirror
#   B. listing        - status()/list_games() metadata shape, never raises
#   C. hosted games   - a dataset game that is NOT in games.db loads and ANALYZES
#   D. offline        - mirror-first: no network needed once mirrored; a failing
#                       listing falls back to the mirror instead of an empty list
#   E. app wiring     - load_state source override, merged /api/games (db/both/hf),
#                       the All / 💾 Local / ☁️ HF source filter (the UI tabs),
#                       support-card refs (name + real art, no "Carte inconnue")
#
# Run from the project root:  uv run python tests/_analysis_hf_source.py
# (downloads the dataset games into games/analysis/hf_cache/, gitignored)
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "games", "analysis"))

import hf_source  # noqa: E402
import replay  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


# --------------------------------------------------------------- A. id safety
print("\n=== A. game-id safety (ids come from the URL) ===")
for good in ["26_11_09_10_08_57_xhw22", "26_09_24_21_58_52_QtP0v"]:
    check(hf_source._valid_id(good), f"accepts a real game id: {good}")
for bad in ["../../etc/passwd", "a/b", "..", "", "x" * 65, "id;rm -rf", "id.json", None, 12]:
    check(not hf_source._valid_id(bad), f"rejects {bad!r}")
check(hf_source.load_game("../../etc/passwd") is None, "load_game('../..') -> None (no request, no file)")
check(hf_source.download("../secrets") is None, "download('../secrets') -> None")
check(hf_source._mirror_path("../secrets").parent.name != "analysis",
      f"mirror path stays under hf_cache: {hf_source.CACHE_DIR.name}/")

# --------------------------------------------------------------- B. listing
print(f"\n=== B. dataset listing ({hf_source.REPO_ID}) ===")
st = hf_source.status()          # status() must list on its own (UI calls both in parallel)
check(st["listed"], "status() listed the dataset on its own")
check(st["error"] is None, f"listing succeeded (error={st['error']!r})")
games = hf_source.list_games()
check(isinstance(games, list), f"list_games() returned a list ({len(games)} games)")
print(f"  {st['n_remote']} file(s) on the dataset, {st['n_mirrored']} mirrored -> {hf_source.CACHE_DIR}")
REQUIRED = {"id", "players", "final_turn", "winner", "state", "source"}
bad = [g for g in games if REQUIRED - set(g)]
check(not bad, f"every entry carries the selector metadata {sorted(REQUIRED)}")
check(all(g["source"] == "hf" for g in games), "every dataset entry is tagged source='hf'")

# ------------------------------------------------------ C. hosted games analyze
print("\n=== C. a hosted game (not in games.db) is analyzable ===")
db_ids = {g["id"] for g in replay.list_games()}
only_hf = [g for g in games if g["id"] not in db_ids]
backed_up = [g for g in games if g["id"] in db_ids]
print(f"  dataset: {len(only_hf)} hosted-only, {len(backed_up)} also in games.db")
if not only_hf:
    print("  SKIP (every dataset game is also in games.db — nothing hosted-only to read)")
for g in only_hf:
    state = hf_source.load_game(g["id"])
    check(state is not None, f"{g['id']}: full state downloaded")
    if state is None:
        continue
    check(state.get("state") == "game over", f"{g['id']}: a finished game ({state.get('state')!r})")
    res = replay.analyze_game(state)
    check(len(res["turns"]) > 0, f"{g['id']}: replayed {len(res['turns'])} turn(s)")
    check(res["ended"] is not None, f"{g['id']}: replay reached the end "
                                    f"({(res['ended'] or {}).get('type')}/{(res['ended'] or {}).get('winner')})")
    print(f"       verified={res['verified']} warnings={len(res['warnings'])} "
          f"({' vs '.join(g['players'])}, winner {g['winner']})")
    if res["warnings"]:
        print(f"       notes: {res['warnings'][0]}")

# --------------------------------------------------------------- D. offline
print("\n=== D. offline behaviour (mirror-first, no raise) ===")
probe = (only_hf or games or [None])[0]
if probe and hf_source._mirror_path(probe["id"]).is_file():
    def _no_hub():
        raise RuntimeError("network down (simulated)")

    real_hub = hf_source._hub
    hf_source._hub = _no_hub
    try:
        st2 = hf_source.load_game(probe["id"])
        check(st2 is not None, f"{probe['id']}: served from the mirror with no network")
    finally:
        hf_source._hub = real_hub
else:
    print("  SKIP (nothing mirrored yet — run with network once to build the mirror)")

real_repo, real_state = hf_source.REPO_ID, (hf_source._remote_ids, hf_source._listed_at, hf_source._last_error)
hf_source.REPO_ID = "bogus/not-a-dataset-repo"
hf_source._remote_ids, hf_source._listed_at, hf_source._last_error = [], 0.0, None
try:
    fallback = hf_source.list_games()
    check(hf_source.status()["error"] is not None, "an unreachable dataset records the error (never raises)")
    check(isinstance(fallback, list) and len(fallback) == len(real_state[0]),
          f"a failed listing falls back to the mirror ({len(fallback)} entries)")
finally:
    hf_source.REPO_ID, (hf_source._remote_ids, hf_source._listed_at, hf_source._last_error) = real_repo, real_state

# ------------------------------------------------------------ E. app wiring
print("\n=== E. app wiring (merged sources + support cards) ===")
import app as analysis_app  # noqa: E402  (games/analysis/app.py)

if only_hf:
    gid = only_hf[0]["id"]
    check(analysis_app.load_state(gid, "db") == (None, None), f"{gid}: forced db source -> not found")
    s, src = analysis_app.load_state(gid, "hf")
    check(s is not None and src == "hf", f"{gid}: forced hf source -> state + source 'hf'")
    s, src = analysis_app.load_state(gid)
    check(src == "hf", f"{gid}: default source resolution falls back to the dataset")
merged = analysis_app.api_games()
check(len(merged) == len(db_ids) + len(only_hf),
      f"/api/games merges both sources: {len(merged)} = {len(db_ids)} db + {len(only_hf)} hosted-only")
check(all(g.get("label") and g.get("source") for g in merged), "every merged entry has a label + a source")
check(sorted(g["id"] for g in merged) == [g["id"] for g in merged], "the merged list stays chronological")
srcs = {g["id"]: g["source"] for g in merged}
check(all(srcs[g["id"]] == "both" for g in backed_up if g["id"] in srcs) if backed_up else True,
      f"a game present on both sides is tagged 'both' ({[g['id'] for g in backed_up][:2]})")

# the source filter (the UI's All / 💾 Local / ☁️ HF tabs) — the same semantics are
# implemented client-side in static/app.js (FILTERS): the tabs must agree with the API
print("\n=== E2. /api/games?filter= (the All / 💾 Local / ☁️ HF tabs) ===")
import fastapi  # noqa: E402

ids = lambda rows: {g["id"] for g in rows}                    # noqa: E731
local_ids = {g["id"] for g in analysis_app.api_games(filter="local")}
hf_ids = {g["id"] for g in analysis_app.api_games(filter="hf")}
check(local_ids == db_ids, f"filter=local == the games.db set ({len(local_ids)} games)")
check(hf_ids == hf_source.known_ids(), f"filter=hf == the dataset set ({len(hf_ids)} games)")
check(ids(analysis_app.api_games(filter="hosted-only")) == hf_ids - db_ids,
      f"filter=hosted-only == dataset minus local ({len(hf_ids - db_ids)} games)")
check(ids(analysis_app.api_games(filter="all")) == local_ids | hf_ids,
      "filter=all == local ∪ hf (a game on both sides is listed once)")
check(all(g["source"] in analysis_app.GAME_FILTERS["local"]
          for g in analysis_app.api_games(filter="local")), "no filtered entry leaks another source")
try:
    analysis_app.api_games(filter="everything")
    check(False, "an unknown filter is refused")
except fastapi.HTTPException as e:
    check(e.status_code == 400, f"an unknown filter is a 400 (detail: {e.detail!r})")

print("\n=== E3. card references (support cards resolve to their real art) ===")
sup = analysis_app._card_ref("boost")
check(sup is not None and sup.get("support") and sup["img"] == "/card/Eng_boost.png",
      f"support card 'boost' resolves: {sup}")
main_id = replay.CARDS_DB["card_id"][0]
main = analysis_app._card_ref(main_id)
check(main is not None and not main.get("support") and main["img"] == f"/card/{main_id}.png",
      f"main card {main_id} still resolves: {main['name']}")
check(analysis_app._card_ref("not_a_card")["name"] == "Carte inconnue", "an unknown id keeps the fallback label")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
