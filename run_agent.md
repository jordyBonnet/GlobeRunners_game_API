# GlobeRunners — Run

> Part of the project instructions, split out of `AGENTS.md` (2026-09-21).
> Back to [AGENTS.md](AGENTS.md). How to run the servers.

## Run

```bash
uv run python API.py                          # pure game server, :8000
uv run python game_ui/app.py                  # full web app, :8001
uv run uvicorn games.analysis.app:app --port 8017   # replay/analysis
```
