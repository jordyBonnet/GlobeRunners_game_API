"""Backup finished games to a Hugging Face dataset (one JSON file per game).

Registered on the engine's game-over hook (`ge.set_game_over_hook`) by
`game_ui/app.py`. When a game ends, its **full** state (unmasked — both hands,
decks, discards, mana, board, log) is uploaded as `games/<game_id>.json` to the
dataset (default `jordyBonnet/globerunners-games`).

Design rules:
- the upload runs in a **background daemon thread** — a slow network must never
  block the WebSocket response;
- failures are **logged, never raised** — a backup hiccup must never break a game;
- `huggingface_hub` is imported lazily, so the game works without it installed.

Token resolution (via huggingface_hub defaults — no token argument needed):
- on a Hugging Face **Space**, `HF_TOKEN` is injected automatically;
- locally, any `hf auth login` cached token (or an `HF_TOKEN` env var) works.
The dataset id can be overridden with `GLOBE_BACKUP_REPO`.
"""

from __future__ import annotations

import os
import threading

DEFAULT_REPO = os.environ.get("GLOBE_BACKUP_REPO", "jordyBonnet/globerunners-games")


def upload_game(game_id: str, game_json: str) -> None:
    """Upload one finished game's full state to the HF dataset (blocking)."""
    from huggingface_hub import upload_file  # lazy import — not a hard dependency

    from io import BytesIO

    upload_file(
        path_or_fileobj=BytesIO(game_json.encode("utf-8")),
        path_in_repo=f"games/{game_id}.json",
        repo_id=DEFAULT_REPO,
        repo_type="dataset",
    )


def _upload_safe(game_id: str, payload: str) -> None:
    try:
        upload_game(game_id, payload)
        print(f"[hf_backup] game {game_id} backed up to {DEFAULT_REPO}")
    except Exception as e:  # noqa: BLE001 — a backup failure must never surface
        print(f"[hf_backup] backup FAILED for game {game_id}: {e!r}")


def on_game_over(game_id: str, current_game) -> None:
    """Engine game-over hook: fire-and-forget upload of the full game state."""
    try:
        payload = current_game.to_json()
    except Exception as e:  # noqa: BLE001
        print(f"[hf_backup] could not serialize game {game_id} — backup skipped: {e!r}")
        return
    threading.Thread(target=_upload_safe, args=(game_id, payload), daemon=True).start()


def register() -> None:
    """Register the hook on the engine (idempotent)."""
    import engine.game_engine as ge

    ge.set_game_over_hook(on_game_over)
    print(f"[hf_backup] game-over hook registered (dataset: {DEFAULT_REPO})")
