# GlobeRunners — `game_ui/app.py` — living doc (`game_ui/APP_OVERVIEW.md`)

> Part of the project instructions, split out of `AGENTS.md` (2026-09-21).
> Back to [AGENTS.md](AGENTS.md). Read `game_ui/APP_OVERVIEW.md` before changing `game_ui/app.py`; update it after significant changes.

## `game_ui/app.py` — living doc (`game_ui/APP_OVERVIEW.md`)

`game_ui/APP_OVERVIEW.md` is the **living overview** of `game_ui/app.py` (what it does, route map, function-by-function, integration map, invariants, change log). Two mandatory habits:

1. **Before** making ANY change to `game_ui/app.py`, **read `game_ui/APP_OVERVIEW.md` first** — so you already know the file's architecture, route order and invariants before touching it.
2. **After** the change: if the modification is **significant** (new/removed route, changed middleware/mounts, changed AI wiring, changed deck validation, changed the layering/flow between components), update `game_ui/APP_OVERVIEW.md` **in the same session** — fix the affected sections and append a line to its *change log*. Cosmetic changes (typos, comments, variable names) do NOT require a doc update. If you notice the doc has drifted from the code for any other reason, re-sync it.
