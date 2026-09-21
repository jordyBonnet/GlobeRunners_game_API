# GlobeRunners — Open issue — hand-accounting discrepancy (UNRESOLVED, revisit later)

> Part of the project instructions, split out of `AGENTS.md` (2026-09-21).
> Back to [AGENTS.md](AGENTS.md). Park it on purpose; re-investigate with a bigger sample.

## Open issue — hand-accounting discrepancy (UNRESOLVED, revisit later)

> **Status: OPEN.** A replay-fidelity failure was found that we could not root-cause. It is **parked on purpose**: the plan is to let it accumulate over several more games, then re-investigate with a bigger sample to find the pattern. Do **not** "fix" it by guesswork.

**The symptom.** Game **`26_08_31_21_38_24_w4LNX`** (created **2026‑08‑31 21:38:24**, i.e. a *fresh* game from the `_ai_e2e` run — **not** an old artifact) fails the replay self-test. The first divergence is at **turn 3**:

| | Graceful Leap Whispering Willow (`Mia11_b8b17e`, `cards_in_hand_sup_3_oppo`, `adv −1`, `mana 1`), played by the **Robot** |
|---|---|
| **Engine** (logged `condition_met`) | **NOT met** → the opponent (Alice) had **≤ 3** cards in hand → reduced advancing `mana − 1 = 0` → Robot stays at cell 4 |
| **Replay** (reconstructed hand) | **met** → Alice had **~8** in hand → full advancing `−1` → Robot 4→3 |

That 1-cell difference cascades into a final-position mismatch (Robot 22 vs 23) and a pet-trap token-cell mismatch, so the game is marked **not verified**.

**Why it's suspicious.** By every check, the **replay looks right (~8)** and the **engine's logged `≤3` is the anomaly** — yet the engine is what actually ran the game. A correct simulation of the rules (and controlled runs of the real engine) put Alice at **7–8** by turn 3.

**Already ruled out** (each verified directly against the code / DB):
- Not a replay **double-draw** — `_pick_draw` only reorders the deck; `_draw_for_next_turn` draws, and it mirrors the engine's draw exactly.
- Not a **wrong-player** lookup — `_get_oppo` is correct; `get_current_game` is a fresh DB read (no caching).
- Not a **card loss** — every card Alice and the Robot played is present in a final zone.
- Not a **hand-reducing effect** — the only `discard_oppo` cards in turns 1–3 (Aethel = defend, Graceful Leap = its own condition not met) never fired; nothing else reduces Alice's hand.
- Not a **missing draw** — the engine *does* draw 3/turn (confirmed in controlled games); the game ran 7 turns so turns 1–6 draws all happened.

**What we do NOT yet know.** The exact mechanism that made the engine's live Alice-hand drop to `≤3`. The engine's actual in-game state disagrees with a correct rule simulation, and we could not reproduce a path that produces it. It is a **pre-existing** issue in `cards_in_hand_*_oppo` hand-accounting that the `drop_on_board` change only *exposed* (the AI picks a different card sequence, which surfaced it) — it is **not** caused by `drop_on_board`.

> **New data point (2026-09-02).** Game **`26_09_02_12_38_02_SfPzi`** (an AI-e2e game) also fails the replay self-test with a **zone-identity/count** mismatch (Alice hand replayed=6/stored=0, mana 9/15; Robot hand 5/10, deck 7/4, discard 2/5). **Critically, it never exercised a support-faction mechanic** (`board_drops: []`, both `dwelling: None`, no landmine) — the Robot's support cards (Mages) simply sat unplayed in its deck/hand. So this is the **same pre-existing hand-accounting issue surfacing in a different game**, **not** an Engineers bug. The games that *did* play engineer drops (`…LFYFE`, `…0OmJ6`) verify **clean** (only the known zone-identity warnings). This is the "accumulate over more games" the plan calls for.

> **Do not confuse with the duplicate-deck games (RESOLVED, see above).** `26_09_02_16_27_30_11VCs` and `26_09_02_18_54_10_PyaZf` also fail the replay self-test, but their root cause is **known and is NOT this hand-accounting issue**: their decks were invalid at creation (31 cards — a support card ×3 / main cards duplicated — accepted by the old `check_deck`). They now fail *by design* (conservation/identity vs a non-standard deck). The genuinely unexplained failures remain `w4LNX` and `SfPzi` (and `26_09_01_20_14_31_WzWsv` — unanalyzed, candidate for the next investigation pass).

**Planned next step (later, after more games).** Re-run the replay self-test once several more games exist and collect **every** `cards_in_hand_*_oppo` divergence (not just `w4LNX`). A recurring pattern (same turn shape, same player, same preceding effect) will pin the mechanism. A useful diagnostic to build then: re-derive each player's hand **turn-by-turn** from `messages_history` + deck and flag where the engine's logged `condition_met` disagrees with the reconstructed hand — that turns this from a black box into a reproducible case.

**Do not** mark `w4LNX` (or its siblings) as a known-bad game to force a 100% pass — that hides the bug. Keep the self-test failing until the root cause is understood.
