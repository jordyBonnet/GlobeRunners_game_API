# Manual smoke test: the engine's game-over hook (ge.set_game_over_hook)
#
# The hook is called ONCE per game, when the state transitions to "game over",
# from the single exit of handle_websocket_message (human OR robot — both go
# through that function). game_ui/hf_backup.py registers a callback that
# uploads the full game state to the HF dataset `jordyBonnet/globerunners-games`
# (games/<game_id>.json) in a background thread.
#
# This test:
#   1. stub hook        -> fires exactly once on the winning move, not again on
#                          a stray post-game-over message;
#   2. real hf_backup   -> the game is uploaded to the dataset (needs HF_TOKEN
#                          or a cached `hf auth login` token);
#   3. cleans up: deletes the test game from games.db and the uploaded file
#                          from the dataset (when a token is available).
#
# Run from the project root:  uv run python tests/_game_over_hook.py
import sys, io, os, sqlite3, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'game_ui'))

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

# A's winning card: no_condition, advancing 4, effect backward -1, mana 2
# (at cell 23: backward -1 -> 22, then +4 -> 26 >= 24 -> WIN)
A_WIN = 'Dwa23_4c60c6'
r = DB.filter(pl.col('card_id') == A_WIN).row(0, named=True)
assert r['faction'] == 'Dwarves' and r['condition'] == 'no_condition' and r['advancing'] == 4, r

FILLER_A = [c for c in DB.filter(pl.col('faction') == 'Dwarves')['card_id'].to_list()
            if c != A_WIN][:16]
FILLER_B = [c for c in DB.filter(pl.col('faction') == 'Demons')['card_id'].to_list()][:19]

passed = 0
failed = 0

def check(condition, msg):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()

def new_game():
    p1 = PlayerState(name='A', deck=[A_WIN] + FILLER_A)
    p2 = PlayerState(name='B', deck=list(FILLER_B))
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    # both players place 3 mana cards -> play phase of turn 1
    for name in ('A', 'B'):
        conn, _, g = ge.get_current_game(gid)
        conn.close()
        me = g.players[name]
        for _ in range(3):
            cid = me.hand[0]
            me.message = {'cards': [cid], 'to': 'mana', 'mode': '', 'pendings': []}
            ge.handle_websocket_message(gid, me)
    return gid

def _mutate(gid, fn):
    """test-only: mutate the stored game state (like a debug tool)"""
    conn = sqlite3.connect(ge.DB_PATH)
    import json
    row = conn.execute("SELECT state_json FROM games WHERE game_id = ?", (gid,)).fetchone()
    state = json.loads(row[0])
    fn(state)
    conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?",
                 (json.dumps(state), gid))
    conn.commit()
    conn.close()

def _set_position(gid, name, pos):
    _mutate(gid, lambda s: s['players'][name].__setitem__('current_position', pos))

def _ensure_in_hand(gid, name, card_id):
    """move card_id into the player's hand (from the deck) if it is not there"""
    def fn(s):
        p = s['players'][name]
        if card_id not in p['hand'] and card_id in p['deck']:
            p['deck'].remove(card_id)
            p['hand'].append(card_id)
    _mutate(gid, fn)

# ---------------------------------------------------------------- 1) stub hook
print("== 1) hook fires exactly once on the transition ==")
fires = []
ge.set_game_over_hook(lambda gid, g: fires.append((gid, g.state, g.winner)))

gid = new_game()
_set_position(gid, 'A', 23)
_ensure_in_hand(gid, 'A', A_WIN)

def _win_with_a(gid):
    """A plays the winning card, then both players pass — the play phase ends
    ONLY when both have passed, and only then the trip chain resolves -> A wins."""
    conn, _, g = ge.get_current_game(gid); conn.close()
    a = g.players['A']
    a.message = {'cards': [A_WIN], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}
    ge.handle_websocket_message(gid, a)
    for name in ('A', 'B'):   # passes until the phase ends (or the game is over)
        conn, _, g = ge.get_current_game(gid); conn.close()
        if g.state == 'game over':
            break
        if 'to play' not in g.state:
            break
        p = g.players[name]
        p.message = {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []}
        ge.handle_websocket_message(gid, p)

_win_with_a(gid)

conn, _, g = ge.get_current_game(gid); conn.close()
check(g.state == 'game over', f"state is 'game over' (got {g.state!r})")
check(g.winner == 'A', f"winner is A (got {g.winner!r})")
check(len(fires) == 1, f"hook fired exactly once (got {len(fires)} fires)")

# a stray message after game over must NOT re-fire the hook
conn, _, g = ge.get_current_game(gid); conn.close()
a = g.players['A']
a.message = {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []}
ge.handle_websocket_message(gid, a)
check(len(fires) == 1, f"stray post-game-over message does not re-fire (got {len(fires)} fires)")

# ------------------------------------------------- 2) real hf_backup upload
print("== 2) hf_backup uploads the game to the HF dataset ==")
import hf_backup
hf_backup.register()

gid2 = new_game()
_set_position(gid2, 'A', 23)
_ensure_in_hand(gid2, 'A', A_WIN)
_win_with_a(gid2)

# the upload is a background thread — give it a few seconds
deadline = time.time() + 30
uploaded = False
while time.time() < deadline:
    try:
        from huggingface_hub import list_repo_files
        if f"games/{gid2}.json" in list_repo_files(hf_backup.DEFAULT_REPO, repo_type='dataset'):
            uploaded = True
            break
    except Exception:
        break  # no token / no network — handled below
    time.sleep(1)

if uploaded:
    check(True, f"games/{gid2}.json present in {hf_backup.DEFAULT_REPO}")
    # cleanup: remove the test upload from the dataset
    try:
        from huggingface_hub import delete_file
        delete_file(path_in_repo=f"games/{gid2}.json", repo_id=hf_backup.DEFAULT_REPO,
                    repo_type='dataset')
        print(f"  (cleaned up dataset file games/{gid2}.json)")
    except Exception as e:
        print(f"  (could not delete dataset file: {e!r})")
else:
    check(False, f"games/{gid2}.json in {hf_backup.DEFAULT_REPO} "
                 "(needs HF_TOKEN or `hf auth login`; see the [hf_backup] log line above)")

# --------------------------------------------------------------- cleanup
_delete(gid)
_delete(gid2)
print(f"  (deleted test games {gid} and {gid2} from games.db)")

ge.set_game_over_hook(None)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
