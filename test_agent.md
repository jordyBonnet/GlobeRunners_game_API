# GlobeRunners — Test

> Part of the project instructions, split out of `AGENTS.md` (2026-09-21).
> Back to [AGENTS.md](AGENTS.md). Manual smoke tests (`tests/*` — no pytest).

## Test

```bash
uv run python tests/_diag.py       # engine unit tests (NOTE: writes to games.db)
uv run python tests/_copy_effect.py # copy_effect rule (also writes to games.db)
uv run python tests/_pet_trap.py    # pet_trap rule (also writes to games.db; 2 games are persisted for the replay self-test)
uv run python tests/_drop_on_board.py # drop_on_board condition (also writes to games.db)
uv run python tests/_pending_condition.py # pending condition (empty zone -> not met, >=1 pending -> met, not-met resolution, AI mirror; also writes to games.db)
uv run python tests/_rooted.py        # rooted effect (also writes to games.db; 1 game is persisted for the replay self-test)
uv run python tests/_wrecking_ball.py # wrecking_ball effect (also writes to games.db)
uv run python tests/_wrecking_placeholder.py # wrecking_ball: the dwelling PLACEHOLDER STAYS IN PLACE (placeholder stays + card to discard + next play after the placeholder, re-place after wreck, pending-after-wreck, WS entry-point path (model_copy), replay verification; deletes its own games)
uv run python tests/_swap_cards.py               # swap_cards effect (56 engine-level checks: all 4 target types (play/pending/dwelling/rooted) + optional swap_with (no swap) + defend no-op + self-swap no-op + invalid-target rejection + log note + conservation; deletes its own games)
uv run python tests/_swap_cards_e2e.py          # swap_cards through the REAL handle_websocket_message entry point with model_copy() (27 checks: game 1 play<->pending swap (annotations, position rewrites, chain resolution, conservation); game 2 play<->dwelling swap (the dwelling_slot sync-back through the copy path); deletes its own games)
uv run python tests/_swap_cards_replay.py       # swap_cards REPLAY fidelity (12 checks: the replay mirrors the swap — the SWAP card resolves at its swapped position (position 1), the pending placeholder at position 2, verified=True, the swap_cards event recorded with the pending target, no unknown-card warnings; deletes its own game)
uv run python tests/_engineers.py    # Engineers support faction (drops + refinery dwelling + support cost; also writes to games.db; 1 game is persisted for the replay self-test)
uv run python tests/_doctors.py     # Doctors support faction (pending zone + attachment + laboratory tap + mercurochrome unstoppable + pair bookkeeping + same-turn attachment keeps the placeholder; also writes to games.db)
uv run python tests/_mages_celestial.py        # Mages Celestial_reversal (31 engine-level checks: day/night fix + persistence + day/night condition eval + rejection + defend no-op + log note; also writes to games.db)
uv run python tests/_mages_celestial_e2e.py    # Mages Celestial_reversal through the REAL handle_websocket_message entry point with model_copy() (14 checks; deletes its own games)
uv run python tests/_mages_celestial_replay.py # Mages Celestial_reversal REPLAY fidelity (8 checks: the replay mirrors the day/night fix — turn 1 fixed, turns 2+ do NOT flip, no unknown-card warnings, event recorded; deletes its own game)
uv run python tests/_mages_nobodymoves.py     # Mages nobodymoves (34 engine-level checks: movement locked + both players + unstoppable exempt + a NON-movement effect (draw) still fires + a movement effect (advancing) suppressed + unstoppable still moves + lock cleared in cleaning + defend no-op + pending exclusion + log note; also writes to games.db)
uv run python tests/_mages_nobodymoves_e2e.py  # Mages nobodymoves through the REAL handle_websocket_message entry point with model_copy() (12 checks; deletes its own games)
uv run python tests/_mages_nobodymoves_replay.py # Mages nobodymoves REPLAY fidelity (9 checks: the replay mirrors the lock — turn 1 movement locked, turn 2 advances, no unknown-card warnings, event recorded; deletes its own game)
uv run python tests/_mages_thermic_flux.py     # Mages thermic_flux (33 engine-level checks: +4 °C / −4 °C + clamping up (caps 20) / down (floors 1) + persistence + temp_* condition eval + rejection + defend no-op + log note; also writes to games.db)
uv run python tests/_mages_thermic_flux_e2e.py  # Mages thermic_flux through the REAL handle_websocket_message entry point with model_copy() (15 checks; deletes its own games)
uv run python tests/_mages_thermic_flux_replay.py # Mages thermic_flux REPLAY fidelity (10 checks: the replay seeds temperature from temperature_initial, mirrors the ±4 delta, records the event with from/to, no temperature-diverges warning, no unknown-card warnings; deletes its own game)
uv run python tests/_mages_apocalypticritual.py # Mages Apocalypticritual (36 engine-level checks: the pile is SET to the chosen order + the chosen top strikes + permanence + rejection + defend no-op + log note + conservation; also writes to games.db)
uv run python tests/_mages_apocalypticritual_e2e.py # Mages Apocalypticritual through the REAL handle_websocket_message entry point with model_copy() (19 checks: 2-turn game — ritual sets the pile, then the cataclysm strikes the chosen top; deletes its own games)
uv run python tests/_mages_apocalypticritual_replay.py # Mages Apocalypticritual REPLAY fidelity (15 checks: the replay reconstructs the initial pile with the ritual, mirrors the pile reorder, the cataclysm strikes the chosen top, no pile-diverges warning, no unknown-card warnings, event recorded; deletes its own game)
uv run python tests/_mages_black_hole.py # Mages black_hole dwelling (37 engine-level checks: dwelling placement + CW/CCW rotation + tokens stay on their cell + cumulative rotation + tap-without-direction rejection + message_check rejection + once-per-turn + biome conditions re-read the rotated earth + log note; also writes to games.db)
uv run python tests/_mages_black_hole_e2e.py # Mages black_hole through the REAL handle_websocket_message entry point with model_copy() (11 checks: the rotation persists to the DB through the copy path + earth_rotation survives the turn end + dwelling_tapped resets in cleaning + card conservation; deletes its own game)
uv run python tests/_mages_black_hole_replay.py # Mages black_hole REPLAY fidelity (14 checks: the replay reconstructs the initial earth, mirrors the tap rotation, NO earth-biomes-diverge warning, the black_hole_tap event recorded with the direction, no unknown-card warnings; deletes its own game)
uv run python tests/_mages_nobodymoves_cataclysm.py # nobodymoves + cataclysm interaction (18 checks: the cataclysm KNOCKBACK is a MOVEMENT — it is SUPPRESSED during a nobodymoves turn (the pile still rotates + the biome is announced + a suppression note) + control (no nobodymoves → the knockback FIRES); deletes its own games)
uv run python tests/_grappling_placeholder.py # placeholders occupy trip-chain positions (grappling vs pending placeholder: a grappling hook facing a placeholder copies nothing; deletes its own games)
uv run python tests/_tap_dup.py     # refinery TAP draw must not duplicate a card (2026-09-04: the tap branch used to drop the copy's deck/discard sync-back; drives the WS model_copy() path like API.py/ai_driver; deletes its own game)
uv run python tests/_midchain_flush.py # check_deck validation (support cap, main uniqueness) + mid-chain win: phantom action_chain cleared, card conservation, flush to discard, game-over guard (uses a TEMP DB — does NOT touch games.db)
uv run python tests/_choose_discard.py # discard selection (discard/discard_oppo pause + choice, chain continuation; deletes its own games)
uv run python tests/_rooted_ui_game.py # LAYERED state: a rooted card on stopover_4 + a played card on the same stopover_4 (the pre-skip-rule stacked state, for rendering/debugging; writes to games.db, game is kept)
uv run python tests/_rooted_quick_test.py # turn-1 play-phase game with a rooted card in Al's hand (Al plays → pass → rooted survives to the board; prints the game ID to join in the UI)
uv run python tests/_stopover_skip.py # "occupied stopovers are skipped" rule — engine as single source of truth (ge._next_free_stopover / ge._occupied_stopover_cols): helper, rooted placement, dwelling PLACE all skip reserved columns (test 5 writes one game to games.db then deletes it)
uv run python tests/_rooted_skip_test.py # UI game for the skip rule: a rooted card is ALREADY on stopover_4 (Bo, last turn); turn 2, Al to play — Al's 1st play must land on stopover_3 (SKIP the reserved stopover_4), not stack on it (prints the game ID to join in the UI)
uv run python tests/_rooted_v15_multiturn.py # per-player rooted: 2-turn engine game, rooted card advances on the board turn 2 (per-player positions, basic advancing only, blockable); deletes its own game
uv run python tests/_rooted_v15_replay.py  # replay verification: full engine flow (2 turns) + analyze_game -> verified=True (confirms the replay mirrors the engine for a rooted card on the board); deletes its own game
# the server on :8001 MUST be running for these two:
uv run python tests/_ui_e2e.py     # 2 fake players via REST+WS
uv run python tests/_ai_e2e.py     # human vs Robot
uv run python -m games.analysis.replay   # replay auto-test
```
