"""Second game source for the analysis app: the Hugging Face **backup dataset**.

`game_ui/hf_backup.py` uploads every FINISHED game (full, unmasked `GameState`
JSON) to the dataset `jordyBonnet/globerunners-games` as `games/<game_id>.json`.
That is where the games played on a hosted deployment live: the container disk
(HF Space / Render) is ephemeral, so those games are NOT in `games/games.db` —
without this module the analysis app simply cannot see them.

The dataset JSON is exactly `GameState.to_json()` — the same shape as the
`games.db` `state_json` column — so `replay.analyze_game(state_dict)` works on
it unchanged. This module only fetches; it never writes to the dataset.

Local mirror: `games/analysis/hf_cache/`
    games/<game_id>.json   the downloaded games (plain JSON, browsable/diffable)
    index.json             metadata cache for the game selector (players, turn,
                           winner) so listing never re-downloads known games
A game is downloaded ONCE and then served from the mirror (finished games are
immutable), so the app stays usable offline once a game is mirrored. `force`
bypasses both the mirror and the metadata cache.

Robustness rules (the analysis app must work with no network / no token):
- `huggingface_hub` is imported lazily and all network errors are caught:
  listing returns the last known (possibly empty) list and records the error in
  `status()["error"]` — nothing ever raises to the caller;
- reads of a PUBLIC dataset need no token; if the dataset is ever made private,
  the standard `HF_TOKEN` (or a cached `hf auth login`) is picked up
  automatically by `huggingface_hub`.

Configuration (environment variables, all optional):
    GLOBE_BACKUP_REPO        dataset id (same override as `hf_backup.py`,
                             default `jordyBonnet/globerunners-games`)
    GLOBE_ANALYSIS_HF        "0"/"off"/"false" disables the source entirely
    GLOBE_ANALYSIS_HF_TTL    seconds the remote file listing is reused (default 300)

Manual check (from the project root):
    python -m games.analysis.hf_source
"""

from __future__ import annotations

import json
import os
import shutil
import string
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

REPO_ID = os.environ.get("GLOBE_BACKUP_REPO", "jordyBonnet/globerunners-games")
SUBDIR = "games/"                      # path_in_repo prefix of the game files
ENABLED = os.environ.get("GLOBE_ANALYSIS_HF", "1").strip().lower() not in (
    "0", "off", "false", "no", "")
try:
    LIST_TTL = float(os.environ.get("GLOBE_ANALYSIS_HF_TTL", "300"))
except ValueError:
    LIST_TTL = 300.0

CACHE_DIR = HERE / "hf_cache"          # local mirror (gitignored)
MIRROR_DIR = CACHE_DIR / SUBDIR.strip("/")  # .../hf_cache/games — the mirrored JSON files
INDEX_PATH = CACHE_DIR / "index.json"  # game_id -> selector metadata

# Windows without Developer Mode cannot symlink the HF cache -> noisy warning on
# every call; the mirror below is a plain file copy anyway, so silence it.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

_lock = threading.RLock()
_index: dict[str, dict] | None = None   # game_id -> metadata (loaded lazily)
_remote_ids: list[str] = []            # ids currently known to be on the dataset
_listed_at: float = 0.0
_last_error: str | None = None


# --------------------------------------------------------------------- helpers
# A game id is `%y_%m_%d_%H_%M_%S_xxxxx` — and the ids reaching this module come
# from the URL, so anything outside that charset (path separators, "..") is
# refused before it becomes a path_in_repo / mirror path.
_ID_CHARS = set(string.ascii_letters + string.digits + "_")


def _valid_id(game_id) -> bool:
    return (isinstance(game_id, str) and 0 < len(game_id) <= 64
            and all(ch in _ID_CHARS for ch in game_id))


def _hub():
    """Lazy `huggingface_hub` import (the analysis app must run without it)."""
    try:
        import huggingface_hub as hub
    except ImportError as e:  # pragma: no cover - dependency missing locally
        raise RuntimeError(
            "huggingface-hub is not installed - `uv pip install huggingface-hub` "
            "to read the HF backup dataset"
        ) from e
    return hub


def _filename(game_id: str) -> str:
    return f"{SUBDIR}{game_id}.json"


def _mirror_path(game_id: str) -> Path:
    return MIRROR_DIR / f"{game_id}.json"


def _read_mirror(game_id: str) -> dict | None:
    p = _mirror_path(game_id)
    try:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _load_index() -> dict[str, dict]:
    global _index
    if _index is None:
        try:
            _index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            if not isinstance(_index, dict):
                _index = {}
        except Exception:
            _index = {}
    return _index


def _save_index() -> None:
    """Best effort: a missing/unwritable cache folder must not break anything."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        INDEX_PATH.write_text(
            json.dumps(_load_index(), indent=1, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def _mirror_ids() -> list[str]:
    """Game ids already present in the local mirror (works with no network)."""
    try:
        return sorted(p.stem for p in MIRROR_DIR.glob("*.json") if _valid_id(p.stem))
    except Exception:
        return []


def _metadata(state: dict) -> dict:
    """Selector metadata of one game (same keys as `replay.list_games` entries)."""
    return {
        "id": state.get("id"),
        "players": list((state.get("players") or {}).keys()),
        "final_turn": int(state.get("turn") or 1),
        "winner": state.get("winner"),
        "state": state.get("state"),
        "engine_version": state.get("engine_version"),
    }


# ------------------------------------------------------------------ network IO
def _list_remote(force: bool = False) -> list[str]:
    """Game ids present on the dataset (TTL-cached; stale list kept on error)."""
    global _remote_ids, _listed_at, _last_error
    with _lock:
        if not ENABLED:
            return []
        if not force and _listed_at and (time.time() - _listed_at) < LIST_TTL:
            return list(_remote_ids)
        try:
            hub = _hub()
            paths = hub.HfApi().list_repo_files(REPO_ID, repo_type="dataset")
            ids = sorted(p[len(SUBDIR):-len(".json")]
                         for p in (paths or [])
                         if p.startswith(SUBDIR) and p.endswith(".json"))
            _remote_ids, _listed_at, _last_error = ids, time.time(), None
            return list(ids)
        except Exception as e:  # offline, private repo, typo in repo id, ...
            _last_error = f"{type(e).__name__}: {e}"
            # nothing known yet -> fall back to the local mirror, so a machine that
            # already mirrored the hosted games can still analyze them offline
            if not _remote_ids:
                _remote_ids = _mirror_ids()
            return list(_remote_ids)


def download(game_id: str, force: bool = False) -> Path | None:
    """Fetch `games/<game_id>.json` into the local mirror; return its path.

    Serves the mirror when present (finished games are immutable) unless
    `force`. Returns None when the game cannot be fetched (logged in `status`).
    """
    if not ENABLED or not _valid_id(game_id):
        return None
    mirror = _mirror_path(game_id)
    if mirror.is_file() and not force:
        return mirror
    with _lock:
        try:
            hub = _hub()
            kwargs = dict(repo_type="dataset")
            try:
                path = hub.hf_hub_download(REPO_ID, _filename(game_id),
                                           local_dir=str(CACHE_DIR), **kwargs)
            except TypeError:
                # very old huggingface_hub without `local_dir`: copy it in
                path = hub.hf_hub_download(REPO_ID, _filename(game_id), **kwargs)
                mirror.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, mirror)
                path = str(mirror)
            _last_error = None
            return Path(path)
        except Exception as e:
            _last_error = f"{type(e).__name__}: {e}"
            return None


def load_game(game_id: str, force: bool = False) -> dict | None:
    """Full state dict of a dataset game (mirror first, then download)."""
    global _last_error
    if not ENABLED or not _valid_id(game_id):
        return None
    if not force:
        st = _read_mirror(game_id)
        if st is not None:
            return st
    path = download(game_id, force=force)
    if path is None:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        return None


# ---------------------------------------------------------------- public API
def list_games(force: bool = False) -> list[dict]:
    """Metadata of every dataset game, newest mirror downloads done on demand.

    Mirrors the shape of `replay.list_games()` (plus `source`/`engine_version`),
    so `app.py` can merge both sources. Never raises.
    """
    if not ENABLED:
        return []
    ids = _list_remote(force=force)
    with _lock:
        index = _load_index()
        out, dirty = [], False
        for gid in ids:
            meta = index.get(gid)
            if meta is None or force:
                st = load_game(gid, force=force)
                if st is None:
                    continue        # undownloadable for now -> retried next list
                meta = _metadata(st)
                meta["id"] = gid          # the file name is the join key with games.db
                index[gid] = meta
                dirty = True
            out.append(dict(meta))
        if dirty:
            _save_index()
    for m in out:
        m["source"] = "hf"
    return out


def known_ids() -> set[str]:
    """Ids currently known on the dataset (no download)."""
    return set(_list_remote())


def status() -> dict:
    """What the UI shows about this source (never raises).

    Lists on demand when nothing was listed yet, so the status line is correct
    even when the UI asks for it BEFORE the game list (the two calls race).
    """
    if not _listed_at:
        _list_remote()
    with _lock:
        index = _load_index()
        mirrored = sum(1 for gid in _remote_ids if _mirror_path(gid).is_file())
        return {
            "enabled": ENABLED,
            "repo": REPO_ID,
            "url": f"https://huggingface.co/datasets/{REPO_ID}/tree/main/games",
            "listed": bool(_listed_at),
            "n_remote": len(_remote_ids),
            "n_mirrored": mirrored,
            "n_indexed": len(index),
            "listed_at": _listed_at or None,
            "ttl": LIST_TTL,
            "error": _last_error,
        }


if __name__ == "__main__":
    games = list_games(force=True)
    st = status()
    print(f"dataset {REPO_ID}  ({st['n_remote']} files, {st['n_mirrored']} mirrored"
          f"{' - ' + st['error'] if st['error'] else ''})")
    for g in games:
        print(f"  {g['id']}  turns={g['final_turn']:3d} winner={str(g['winner']):12s} "
              f"{' vs '.join(g['players'])}")
    print(f"mirror folder: {CACHE_DIR}")
