Bug fixes
- bug at the start position, it seems it starts at 0, so it create a shift of one all along the rest of the game.

Visuals
- add the possibility to click on the discard to see tehm all in a popup
- [x] In the game ui system log don't put:  "— the placeholder STAYS in place (pommi's stopover slot stays occupied for the rest of the turn)" part of text, keep it short and simple (apply this short and simple rule on all other game ui logs) — DONE 2026-07-15: all engine log lines (instant section, notes, negatives) trimmed to short, simple, one-line events (engine/game_engine.py); specs updated in cond_effects_agent.md / game_log_agent.md / sup_fact_agent.md; smoke tests pass

AI


Game Analysis
- support faciton cards not shown
- add instant cards report in the game analysis like in the ingame log system