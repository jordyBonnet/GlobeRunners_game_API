"""E2E smoke test for game_ui/app.py : simulates 2 players through REST + WS.

Run (server must be up on port 8001) :
    python tests/_ui_e2e.py
"""
import asyncio
import json
import sys
import urllib.request

import websockets

BASE = "http://127.0.0.1:8001"


def http_get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode())


def http_post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def pick_deck(pool, faction_prefix, n=15):
    ids = [c["card_id"] for c in pool if c["faction"].startswith(faction_prefix)]
    return ids[:n]


async def main():
    pool = http_get("/cardpool")
    assert len(pool) > 1000, "cardpool too small"

    # UI routes sanity checks
    with urllib.request.urlopen(BASE + "/", timeout=15) as r:
        idx_text = r.read().decode()
    assert "GlobeRunners" in idx_text
    for p in ("/static/style.css", "/static/app.js", "/placeholder.svg"):
        with urllib.request.urlopen(BASE + p, timeout=15) as r:
            assert r.status == 200

    deck1 = pick_deck(pool, "Dwa")
    deck2 = pick_deck(pool, "Orc")
    r = http_post("/create_game", {"name": "Alice", "deck": deck1})
    assert r["success"], r
    gid = r["game_id"]
    print(f"created game {gid}")

    # p1 waits for opponent via /api/state polling (like the UI does)
    state_url = f"/api/state/{gid}/Alice"
    st = http_get(state_url)
    assert "players" in st and len(st["players"]) == 1

    r2 = http_post(f"/join_game/{gid}", {"name": "Bob", "deck": deck2})
    assert "players" in r2, f"join failed: {r2}"
    print("bob joined")

    # hidden info check : Alice must not see Bob's hand/mana/deck
    st = http_get(state_url)
    bob_view = st["players"]["Bob"]
    assert bob_view["hand"] == [] and bob_view["deck"] == [], "hidden info leaked!"

    log = []

    async def player(name, faction):
        ws_url = f"ws://127.0.0.1:8001/ws/{gid}/{name}"
        async with websockets.connect(ws_url) as ws:
            first = json.loads(await ws.recv())
            assert "players" in first
            log.append(f"[{name}] connected")

            async def send(cards, to, mode, pendings=None):
                """send an action; if rejected 'not your turn', wait and retry (max 20x)"""
                for attempt in range(20):
                    await ws.send(json.dumps({"cards": cards, "to": to, "mode": mode, "pendings": pendings or []}))
                    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    if resp.get("success") is False:
                        msg = resp.get("message", "")
                        if "not your turn" in msg:
                            await asyncio.sleep(0.4)   # opponent is acting; wait and retry
                            continue
                        raise RuntimeError(f"{name} rejected {cards}->{to}/{mode}: {msg}")
                    return resp
                raise TimeoutError(f"{name}: still not my turn after 20 retries")

            me = first["players"][name]
            # 1) init mana : put 3 cards (one at a time, small delay to avoid races)
            for i in range(3):
                st_now = await asyncio.to_thread(http_get, f"/api/state/{gid}/{name}")
                me_p = st_now["players"][name]
                if len(me_p.get("mana") or []) >= 3:
                    break   # already done (e.g. retried)
                card = me_p["hand"][0]
                resp = await send([card], "mana", "")
                me = resp["players"][name]
                await asyncio.sleep(0.15)

            # 2) play turns until game over (max 80 actions to avoid infinite loop)
            for step in range(80):
                st = await asyncio.to_thread(http_get, f"/api/state/{gid}/{name}")
                phase = st["state"]
                if "game over" in phase or st.get("winner"):
                    log.append(f"[{name}] game over: winner={st.get('winner')} state={phase}")
                    return

                me_p = st["players"][name]
                acted = False
                if f"waiting for both players to mana or pass" in phase:
                    # put 1 card in mana (or pass if empty hand) — only once per player per phase
                    is_first = st["turn_order"][0] == name
                    already_acted = st.get("first_player_passed") if is_first else st.get("second_player_passed")
                    if not already_acted:
                        if me_p["hand"]:
                            await send([me_p["hand"][0]], "mana", "")
                        else:
                            await send([], "", "pass")
                        acted = True
                elif f"({name}) to play" in phase:
                    # play the first affordable card, or pass
                    mana_avail = len(me_p["mana"]) - me_p.get("mana_spend", 0)
                    played = False
                    for cid in me_p["hand"]:
                        row = next((c for c in pool if c["card_id"] == cid), None)
                        if row and row["mana"] <= mana_avail:
                            target = min(23, max(0, (me_p["current_position"] or 0) + max(row["advancing"], 0)))
                            await send([cid], f"stopover_{target}", "move")
                            played = True
                            break
                    if not played:
                        await send([], "", "pass")
                    acted = True

                if not acted:
                    await asyncio.sleep(0.3)   # not my turn / already acted this phase

    async def run():
        await asyncio.gather(player("Alice", "Dwa"), player("Bob", "Orc"))

    await run()
    print("\n".join(log))
    print("E2E OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"E2E FAILED: {e}")
        sys.exit(1)
