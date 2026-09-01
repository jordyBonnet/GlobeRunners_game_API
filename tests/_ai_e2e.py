"""E2E smoke test: a human (WS) plays a full game against the AI (/create_game_ai).

The robot must act in the background (initial mana, mana/pass, plays).
Run (server must be up on port 8001):
    python tests/_ai_e2e.py
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


async def main():
    pool = http_get("/cardpool")
    deck1 = [c["card_id"] for c in pool if c["faction"].startswith("Dwa")][:15]

    r = http_post("/create_game_ai", {"name": "Alice", "deck": deck1})
    assert r["success"], r
    gid = r["game_id"]
    oppo = r.get("opponent", "Robot")
    print(f"created AI game {gid} vs {oppo}")

    # the game must already be initialized (2 players)
    st = http_get(f"/api/state/{gid}/Alice")
    assert st["players"] and len(st["players"]) == 2, f"opponent not in game: {st.get('players')}"

    ws_url = f"ws://127.0.0.1:8001/ws/{gid}/Alice"
    async with websockets.connect(ws_url) as ws:
        first = json.loads(await ws.recv())
        assert "players" in first

        async def send(cards, to, mode, pendings=None):
            for attempt in range(40):
                await ws.send(json.dumps({"cards": cards, "to": to, "mode": mode, "pendings": pendings or []}))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if resp.get("success") is False:
                    msg = resp.get("message", "")
                    if "not your turn" in msg:
                        await asyncio.sleep(0.5)
                        continue
                    raise RuntimeError(f"rejected {cards}->{to}/{mode}: {msg}")
                    if "tried to play" in msg or "not enough mana" in msg:
                        # fallback : passer ce tour
                        await ws.send(json.dumps({"cards": [], "to": "", "mode": "pass", "pendings": []}))
                        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                        if resp.get("success") is False:
                            raise RuntimeError(f"pass rejected: {resp.get('message')}")
                        return resp
                    raise RuntimeError(f"Alice rejected {cards}->{to}/{mode}: {msg}")
                return resp
            raise TimeoutError("still not my turn after 40 retries")

        for step in range(450):
            st = await asyncio.to_thread(http_get, f"/api/state/{gid}/Alice")
            phase = st["state"]
            if "game over" in phase or st.get("winner"):
                print(f"game over at turn {st.get('turn')}, winner={st.get('winner')}")
                break

            me_p = st["players"]["Alice"]
            acted = False
            if "waiting for both players to put" in phase:
                if len(me_p.get("mana") or []) < 3 and me_p["hand"]:
                    card = me_p["hand"][0]
                    await send([card], "mana", "")
                    acted = True
            elif "waiting for both players to mana or pass" in phase:
                if me_p["hand"]:
                    await send([me_p["hand"][0]], "mana", "")
                else:
                    await send([], "", "pass")
                acted = True
            elif f"(Alice) to play" in phase:
                mana_avail = len(me_p["mana"]) - me_p.get("mana_spend", 0)
                played = False
                for cid in me_p["hand"]:
                    row = next((c for c in pool if c["card_id"] == cid), None)
                    if row and row["mana"] <= mana_avail:
                        await send([cid], "stopover_0", "move")
                        played = True
                        break
                if not played:
                    await send([], "", "pass")
                acted = True

            if not acted:
                await asyncio.sleep(0.4)
        else:
            raise RuntimeError("game did not end after 450 steps")

    # the robot must have actually played (non-empty message history)
    full = http_get(f"/game/{gid}")
    rob = full["players"][oppo]
    assert len(rob["messages_history"]) >= 2, f"robot barely played: {len(rob['messages_history'])} messages"

    # the robot must play its cards in order on the stopovers: each turn
    # restarts at stopover 1 (column 4) then 3, 2, 1, 0 (action_chain is reset
    # at the end of each turn). Both 'move' AND 'defend' plays consume a slot in
    # that order (same rule as the frontend), so the sequence must be strictly
    # decreasing within a turn and no slot may repeat.
    plays = [m["to"] for m in rob["messages_history"] if m.get("mode") in ("move", "defend") and m.get("cards")]
    assert plays, "robot never played a card on a stopover"
    cs = [int(m.rsplit("_", 1)[1]) for m in plays]
    assert cs[0] == 4, f"first stopover of the first turn must be 4: {plays}"
    for prev, c in zip(cs, cs[1:]):
        assert c == 4 or c == prev - 1, f"robot stopover order wrong: {plays} (at {prev} -> {c})"
    print(f"robot played {len(rob['messages_history'])} actions ({len(plays)} stopovers in order, defends included) — E2E OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"E2E FAILED: {e}")
        sys.exit(1)
