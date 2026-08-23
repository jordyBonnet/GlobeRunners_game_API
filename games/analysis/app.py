"""FastAPI web app for analyzing stored games from games/games.db.

Run (from the project root, with the venv activated):
    uvicorn analysis.app:app --reload --port 8000
or directly:
    python -m uvicorn games.analysis.app:app --port 8000   # see README in this folder

Endpoints:
    GET /                     -> static frontend (game selector + per-turn view)
    GET /api/games            -> metadata of every stored game
    GET /api/game/{game_id}   -> full trip-chain analysis for one game
    GET /card/{card_id}.png   -> card art served from the external art folder

Card IDs are only used internally to resolve images; they are never rendered
in the UI (only the card art + name are shown).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import replay  # noqa: E402

# external folder containing <card_id>.png for every card of the pool
ART_DIR = Path(r"C:\Users\jordy\Documents\python\projects\GenAI_TCG\lib\artdesign\cards_framed_0.6")

app = FastAPI(title="GlobeRunners - Analyse de parties")

_analysis_cache: dict[str, dict] = {}


# ------------------------------------------------------------- presentation
def _card_ref(card_id: str | None) -> dict | None:
    """Public reference for a card: image + name only (never the id)."""
    if not card_id:
        return None
    row = replay.CARDS_DB.filter(replay.pl.col("card_id") == card_id)
    name = row["name"].item() if not row.is_empty() else "Carte inconnue"
    faction = row["faction"].item() if not row.is_empty() else ""
    return {"img": f"/card/{card_id}.png", "name": name, "faction": faction}


def _effect_text(ev: dict) -> str:
    """Short French description of what the card did during the trip chain."""
    eff = ev.get("effect")
    n = ev.get("effect_number") or 0
    adv = ev.get("advancing") or 0
    if not ev.get("condition_met"):
        return f"Condition non remplie - avance réduite ({ev.get('mana_cost', 1) - 1})"
    table = {
        "advancing": lambda: f"Avance +{n} (total {adv + n})",
        "backward": lambda: f"Avance {adv}, puis recul {-n}",
        "jump": lambda: f"Saut direct de {adv} cases",
        "advancing_oppo": lambda: f"L'adversaire avance de {n}",
        "backward_oppo": lambda: f"L'adversaire recule de {-n}",
        "draw": lambda: f"Tire {abs(n)} carte(s)",
        "draw_oppo": lambda: f"L'adversaire tire {abs(n)} carte(s)",
        "discard": lambda: f"Jette {abs(n)} carte(s) de sa main",
        "discard_oppo": lambda: f"L'adversaire jette {abs(n)} carte(s)",
        "ramp": lambda: f"{n} carte(s) du deck vers la zone mana",
        "ramp_oppo": lambda: f"Adversaire : {n} carte(s) du deck vers son mana",
        "taxation": lambda: f"Taxe {n} carte(s) de sa zone mana",
        "taxation_oppo": lambda: "L'adversaire perd 1 carte de mana (taxe)",
    }
    if eff in table:
        return table[eff]()
    # unimplemented / special effects -> neutral label
    return f"Effet : {eff.replace('_', ' ')}"


def _action_public(ev: dict | None) -> dict | None:
    if ev is None:
        return None
    if ev.get("error"):
        return {"error": ev["error"]}
    delta = (ev.get("pos_after") or 0) - (ev.get("pos_before") or 0)
    return {
        "card": _card_ref(ev.get("card_id")),
        "condition_met": bool(ev.get("condition_met")),
        "effect_text": _effect_text(ev),
        "pos_before": ev.get("pos_before"),
        "pos_after": ev.get("pos_after"),
        "delta": delta,
    }


def get_analysis(game_id: str) -> dict | None:
    """Replay a stored game (cached) and return presentation-ready JSON."""
    if game_id in _analysis_cache:
        return _analysis_cache[game_id]

    state_dict = replay.load_game(game_id)
    if state_dict is None:
        return None

    res = replay.analyze_game(state_dict)
    names = list(state_dict.get("players", {}).keys())

    turns_out = []
    for t in res["turns"]:
        actions_pub = []
        for act in t["actions"]:
            actions_pub.append({n: _action_public(act.get(n)) for n in names})
        # turn 1: the 3 initial mana cards; later turns: the single card put to mana
        mana_puts = {n: [_card_ref(c) for c in (cids or [])] for n, cids in (t.get("mana_puts") or {}).items()}
        hands_start = {}
        for n, h in (t.get("hands_start") or {}).items():
            hands_start[n] = {
                "cards": [_card_ref(c) for c in (h.get("cards") or [])],
                "unknown_count": int(h.get("unknown_count") or 0),
            }
        turns_out.append({
            "turn": t["turn"],
            "day_night": t["day_night"],
            "order": t["order"],
            "mana_puts": mana_puts,
            "hands_start": hands_start,
            "actions": actions_pub,
            "positions_before": t["positions_before"],
            "positions_after": t["positions_after"],
        })

    out = {
        "game_id": game_id,
        "date_label": replay._date_label(game_id),
        "players": names,
        "temperature": state_dict.get("temperature"),
        "final_day_night": state_dict.get("day_night"),
        "winner": state_dict.get("winner"),
        "state": state_dict.get("state"),
        "turns": turns_out,
        "ended": res["ended"],
        "verified": res["verified"],
        "warnings": res["warnings"],
    }
    _analysis_cache[game_id] = out
    return out


# ------------------------------------------------------------------ routes
@app.get("/api/games")
def api_games():
    games = replay.list_games()
    for g in games:
        g["label"] = (
            f"{g['date_label']} - {' vs '.join(g['players'])} "
            f"(tour {g['final_turn']}, gagnant : {g['winner'] or 'égalité'})"
        )
    return games


@app.get("/api/game/{game_id}")
def api_game(game_id: str):
    data = get_analysis(game_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Partie introuvable")
    return data


@app.get("/card/{card_id}.png")
def card_image(card_id: str):
    # sanitize: only allow the expected id charset (letters, digits, underscore)
    if not card_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in card_id):
        raise HTTPException(status_code=400, detail="card_id invalide")
    path = ART_DIR / f"{card_id}.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image introuvable")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


# static frontend last so /api and /card win over the catch-all mount
app.mount("/", StaticFiles(directory=str(HERE / "static"), html=True), name="static")
