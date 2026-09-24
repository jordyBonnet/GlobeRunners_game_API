# Hugging Face Space — GlobeRunners game UI (Docker SDK)
#
# Runs the game server (game_ui/app.py) on port 7860, the port HF Spaces proxies.
# `--app-dir game_ui` puts game_ui/ on sys.path (so `import ai_driver`,
# `import github_assets`, `import hf_backup` work) while the CWD (/app) keeps
# `engine/`, `models.py`, `cards/`, `games/` importable — the same layout as
# running locally from the project root.
#
# Art: LOCAL_TEST = False in game_ui/app.py
#   - main-card art  -> 302 redirect to GitHub Releases (jordyBonnet/GlobeRunners_images)
#   - support art    -> game_ui/hosted_assets/art (in-repo)
#   - board assets   -> game_ui/hosted_assets/assets + cards_ex (in-repo, pruned)
#
# Finished games are backed up to the HF dataset `jordyBonnet/globerunners-games`
# (HF_TOKEN is injected by HF Spaces automatically).

FROM python:3.12-slim

WORKDIR /app

# Runtime dependencies (kept explicit so the build does not need uv / network access to a lockfile)
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    websockets \
    pydantic \
    numpy \
    pillow \
    polars \
    requests \
    huggingface-hub

COPY . .

EXPOSE 7860

# ${PORT:-7860}: Render injects its own PORT for Docker runtimes; 7860 elsewhere.
# (shell form — an exec-form CMD would not expand the variable)
CMD sh -c "python -m uvicorn app:app --app-dir game_ui --host 0.0.0.0 --port ${PORT:-7860}"
