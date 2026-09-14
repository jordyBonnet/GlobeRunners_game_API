"""Mid-chain win regression test (runs against a TEMP database, NOT games.db).

Covers the fixes for the duplicate-cards investigation (game 26_09_02_18_54_10_PyaZf):

1. check_deck rejects a support card appearing more than twice (3x refinery was
   accepted before and produced a 31-card deck -> duplicate cards in hand).
2. check_deck still accepts: a standard 20-main + 2x5-support deck, a main-dup-free
   deck with exactly 2 support copies, and the 15-card E2E test decks.
3. On a mid-chain win the engine flushes the unresolved cards to discard AND clears
   both players' action_chain (the stored final state used to keep a PHANTOM chain
   whose cards were already in the discard).
4. The final state conserves every card of the deck (zones only, no phantom chain).
5. After "game over" no message can move any card (a stray tap/play from the client
   is rejected — the user's "I tap the refinery and I draw again" scenario).
6. create_new_game prints a loud warning for a deck with duplicate ids (defense
   in depth for clients that bypass check_deck).

Run:  uv run python tests/_midchain_flush.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import engine.game_engine as ge
from models import PlayerState

TMPDIR = tempfile.mkdtemp(prefix="gr_midchain_")
ge.DB_PATH = os.path.join(TMPDIR, "test_games.db")   # temp DB — do NOT touch games.db

CARD = "Dwa45_74544f"   # advancing 8, condition 'day', effect 'backward -2', mana 4
DECK = [CARD] * 20


def zones(state_dict, name):
    p = state_dict["players"][name]
    out = {}
    for f in ("hand", "deck", "discard", "mana"):
        out[f] = list(p.get(f) or [])
    if p.get("dwelling"):
        out["dwelling"] = [p["dwelling"]]
    return out


def total(state_dict, name):
    return sum(len(v) for v in zones(state_dict, name).values())


def deck_multiset(state_dict, name):
    from collections import Counter
    c = Counter()
    for v in zones(state_dict, name).values():
        c.update(v)
    return c


def full_state(gid):
    _, _, st = ge.get_current_game(gid)
    return json.loads(st.to_json())


def send(gid, name, message):
    conn, _, cur = ge.get_current_game(gid)
    player = cur.players[name].model_copy()
    player.message = message
    conn.close()
    return json.loads(ge.handle_websocket_message(gid, player))


def state_str(gid):
    return full_state(gid)["state"]


# --------------------------------------------------------------------------- 1-2. check_deck
from API import check_deck
from fastapi import HTTPException

main20 = [f"T{i:03d}_abcd{i:02d}" for i in range(20)]
# build real ids from the pool for realism
import polars as pl
pool = ge.get_cardpool()
real_mains = [c for c in pool['card_id'].to_list() if c.startswith("Dwa")][:20]
assert len(set(real_mains)) == 20
sup_eng = ["boost", "trampoline", "gluetrap", "landmine", "refinery"]
std_deck = real_mains + sup_eng * 2                      # 20 + 10 = 30 (canonical)

try:
    check_deck(std_deck); ok_std = True
except HTTPException as e:
    ok_std = False; print("FAIL std deck rejected:", e.detail)

try:
    check_deck(real_mains + sup_eng * 2 + ["refinery"]); ok_3ref = True
except HTTPException as e:
    ok_3ref = False; detail_3ref = e.detail

try:
    check_deck(real_mains[:19] + [real_mains[0]]); ok_dupmain = True
except HTTPException as e:
    ok_dupmain = False; detail_dup = e.detail

try:
    check_deck(real_mains[:15]); ok_e2e = True
except HTTPException as e:
    ok_e2e = False; print("FAIL 15-card E2E deck rejected:", e.detail)

print(f"check_deck: standard 30-card deck accepted:  {ok_std}")
print(f"check_deck: 3x refinery rejected:            {not ok_3ref}  ({detail_3ref})")
print(f"check_deck: duplicate main rejected:         {not ok_dupmain}")
print(f"check_deck: 15-card E2E deck accepted:       {ok_e2e}")
assert ok_std and not ok_3ref and not ok_dupmain and ok_e2e, "check_deck validation broken"

# --------------------------------------------------------------------------- 3-5. engine game
p1 = PlayerState(name="Al", deck=list(DECK))
p2 = PlayerState(name="Bo", deck=list(DECK))
gid = ge.create_new_game(p1.model_dump())
ge.p2_connect_to_game(p2.model_dump(), gid)
print(f"\ntest game: {gid}")

# init: both put 3 cards in mana
send(gid, "Al", {"cards": [CARD, CARD, CARD], "to": "mana", "mode": "", "pendings": []})
send(gid, "Bo", {"cards": [CARD, CARD, CARD], "to": "mana", "mode": "", "pendings": []})

turn = 0
while turn < 80:
    st = state_str(gid)
    if st == "game over":
        win_turn = full_state(gid).get("turn")
        break
    if st == "waiting for both players to mana or pass":
        for who in ("Al", "Bo"):
            send(gid, who, {"cards": [CARD], "to": "mana", "mode": "", "pendings": []})
        continue
    if "waiting for first player" in st:
        who = "Al" if "(Al)" in st else "Bo"
    elif "waiting for second player" in st:
        who = "Al" if "(Al)" in st else "Bo"
    else:
        raise RuntimeError(f"unexpected state: {st}")
    fs = full_state(gid)
    p = fs["players"][who]
    payable = (len(p["mana"]) - (p.get("mana_spend") or 0)) >= 4
    if payable and CARD in (p.get("hand") or []):
        send(gid, who, {"cards": [CARD], "to": "stopover_4", "mode": "move", "pendings": []})
    else:
        send(gid, who, {"cards": [], "to": "", "mode": "pass", "pendings": []})
    turn += 1

fs = full_state(gid)
winner = fs.get("winner")
# the winner is whoever moves first on a DAY turn (turn 1, 3, 5, 7 -> the turn-1 first player)
first_t1 = fs.get("turn_order", [None, None])[0]   # turn order flips each turn; turn 7 first == turn 1 first
print(f"final state: {fs['state']}  winner: {winner}  turn-1 first player: {first_t1}  (turn {fs.get('turn')}, {turn} actions)")
assert fs["state"] == "game over", f"game did not end within {turn} actions"
assert winner is not None and winner in ("Al", "Bo"), "no winner declared"

# (3) phantom chain must be cleared
for who in ("Al", "Bo"):
    ch = fs["players"][who].get("action_chain") or []
    print(f"  {who} action_chain: {len(ch)} entries")
    assert not ch, f"PHANTOM action_chain still stored for {who}: {ch}"

# (4) conservation: zones == original deck multiset
for who in ("Al", "Bo"):
    got = deck_multiset(fs, who)
    expect = {CARD: 20}
    print(f"  {who}: zones total = {total(fs, who)} (expected 20)")
    assert got == expect, f"card conservation broken for {who}: {dict(got)}"

# (4b) flush verified: the LOSER's last (winning-index) card was unprocessed ->
# flushed to the discard; the winner's last card resolved into the discard.
loser = "Bo" if winner == "Al" else "Al"
for who in ("Al", "Bo"):
    disc = fs["players"][who]["discard"]
    print(f"  {who} discard: {len(disc)} card(s) ({'flushed last play included' if who == loser else 'all plays resolved'})")
    assert disc.count(CARD) >= 1, f"{who} discard is empty — chain cards not conserved: {disc}"
assert len(fs["players"][loser]["discard"]) == len(fs["players"][winner]["discard"]), \
    "loser's unprocessed card was not flushed to the discard"
print("  flush verified: loser's unprocessed card is in the discard pile")

# (5) game-over guard: a stray message must not move any card
before = json.dumps(zones(fs, "Al"), sort_keys=True) + json.dumps(zones(fs, "Bo"), sort_keys=True)
resp = send(gid, "Al", {"cards": [], "to": "dwelling", "mode": "dwelling_activation", "pendings": []})
resp2 = send(gid, "Al", {"cards": [CARD], "to": "stopover_4", "mode": "move", "pendings": []})
after_fs = full_state(gid)
after = json.dumps(zones(after_fs, "Al"), sort_keys=True) + json.dumps(zones(after_fs, "Bo"), sort_keys=True)
# the engine reports success inside the state's 'message' field
ok1 = (resp.get("message") or {}).get("success")
ok2 = (resp2.get("message") or {}).get("success")
print(f"  game-over guard: tap success={ok1} ({(resp.get('message') or {}).get('message')!r})")
print(f"  game-over guard: play success={ok2} ({(resp2.get('message') or {}).get('message')!r})")
print(f"  zones unchanged after stray messages: {before == after}")
assert ok1 is False, f"stray tap after game over was accepted: {resp.get('message')}"
assert ok2 is False, f"stray play after game over was accepted: {resp2.get('message')}"
assert before == after, "stray messages after game over moved cards!"

# --------------------------------------------------------------------------- 6. duplicate-deck warning
import io, contextlib
p3 = PlayerState(name="Dup", deck=["X1_abcd01"] * 2 + ["X2_abcd02"])
# X ids are not in the pool — the warning only cares about duplicate ids, not validity
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    gid2 = ge.create_new_game(p3.model_dump())
out = buf.getvalue()
print(f"\nduplicate-deck warning logged: {'DUPLICATE' in out.upper()}")
assert "duplicate" in out.lower(), f"no duplicate warning in log:\n{out}"

print("\nALL MID-CHAIN / CONSERVATION / GAME-OVER CHECKS PASSED")
