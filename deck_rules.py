"""Deck construction rules — the single source of truth for validating a player's deck.

Rules (designer-confirmed 2026-09-24):
  * the MAIN deck is **exactly 20 cards** from **one main faction** only;
  * **single-copy format** — a card can only appear once in the deck;
  * **max 5 cards** sharing the same condition, where the numeric variants of a
    condition count as ONE condition:
      - `temp_inf_6`  ~ `temp_inf_11`
      - `temp_sup_9`  ~ `temp_sup_15`
      - `dist_ahead_sup_1`  ~ `dist_ahead_sup_3`
      - `dist_behind_sup_1` ~ `dist_behind_sup_3`
  * **max 5 cards** sharing the same effect — the numeric variants of an effect
    (e.g. `advancing` +1/+2/+3, stored in `effect_number`) are the SAME effect
    (they share the `effect` column value, so grouping by effect name covers them);
  * **`face_point_left` / `face_point_right` are refused for now** — they are not
    implemented in the online game yet (allowed later: just remove them from
    `BANNED_VALUES`).
  * the SUPPORT deck is **exactly 10 cards**: 2 copies of each of the 5 cards of
    ONE support faction (engineers / mages / doctors).

Used by:
  * `API.py` `check_deck` (server-side hard gate on /create_game, /join_game,
    /create_game_ai) — raises 400 with the list of violations;
  * the frontend (`cards.mjs` `checkMainDeck`) mirrors `check_main_deck` in JS
    (the browser cannot import Python) for live feedback + the launch guard;
  * `game_ui/ai_deck.py` — the AI deck builder respects the same rules.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

import engine.game_engine as ge

SUPPORT_CARDS_PATH = Path(__file__).resolve().parent / "cards" / "support_factions.parquet"

MAIN_DECK_SIZE = 20
SUPPORT_DECK_SIZE = 10
MAX_PER_CONDITION = 5
MAX_PER_EFFECT = 5

# Conditions that count as the SAME condition for the MAX_PER_CONDITION limit.
# Any condition NOT listed here is its own family (identity mapping).
COND_FAMILY = {
    "temp_inf_6": "temp_inf",
    "temp_inf_11": "temp_inf",
    "temp_sup_9": "temp_sup",
    "temp_sup_15": "temp_sup",
    "dist_ahead_sup_1": "dist_ahead",
    "dist_ahead_sup_3": "dist_ahead",
    "dist_behind_sup_1": "dist_behind",
    "dist_behind_sup_3": "dist_behind",
}

# Card `condition`/`effect` values refused in decks for now (not implemented in
# the online game yet — allowed later by removing them from this set).
BANNED_VALUES = {"face_point_left", "face_point_right"}


def cond_family(condition) -> str:
    """The condition family a card counts under (numeric variants merge)."""
    return COND_FAMILY.get(condition, condition)


def load_support_cards() -> pl.DataFrame:
    """The support faction card pool (empty DataFrame if the file is missing)."""
    if not SUPPORT_CARDS_PATH.exists():
        return pl.DataFrame()
    return pl.read_parquet(SUPPORT_CARDS_PATH)


def _main_by_id() -> dict:
    return {r["card_id"]: r for r in ge.get_cardpool().iter_rows(named=True)}


def check_main_deck(deck: list[str], by_id: dict | None = None) -> list[str]:
    """Validate a MAIN deck (list of main card ids).

    Returns a list of human-readable violations (empty list = valid deck).
    `by_id` (card_id -> pool row) can be injected for tests; defaults to the pool.
    """
    if by_id is None:
        by_id = _main_by_id()
    problems: list[str] = []

    unknown = [c for c in deck if c not in by_id]
    if unknown:
        problems.append(f"unknown card(s): {', '.join(unknown[:5])}")

    if len(deck) != MAIN_DECK_SIZE:
        problems.append(f"must contain exactly {MAIN_DECK_SIZE} cards (has {len(deck)})")

    # one main faction only
    facs = sorted({by_id[c]["faction"] for c in deck if c in by_id and by_id[c].get("faction")})
    if len(facs) > 1:
        problems.append(f"all {MAIN_DECK_SIZE} cards must come from a single main faction (found: {', '.join(facs)})")

    # single-copy format
    seen, dups = set(), set()
    for c in deck:
        if c in seen:
            dups.add(c)
        seen.add(c)
    if dups:
        problems.append(f"single-copy format — duplicate card(s): {', '.join(sorted(dups)[:5])}")

    # max 5 per condition family / per effect (known cards only)
    cond_count: dict[str, int] = {}
    eff_count: dict[str, int] = {}
    for c in deck:
        row = by_id.get(c)
        if row is None:
            continue
        cf = cond_family(row.get("condition"))
        if cf:
            cond_count[cf] = cond_count.get(cf, 0) + 1
        if row.get("effect"):
            eff_count[row["effect"]] = eff_count.get(row["effect"], 0) + 1
    for name, n in sorted(cond_count.items()):
        if n > MAX_PER_CONDITION:
            problems.append(f"max {MAX_PER_CONDITION} cards per condition — '{name}' has {n}")
    for name, n in sorted(eff_count.items()):
        if n > MAX_PER_EFFECT:
            problems.append(f"max {MAX_PER_EFFECT} cards per effect — '{name}' has {n}")

    # banned values (face_point_left / face_point_right — not implemented yet)
    banned = [
        c for c in deck
        if c in by_id and (by_id[c].get("condition") in BANNED_VALUES or by_id[c].get("effect") in BANNED_VALUES)
    ]
    if banned:
        problems.append(f"banned card(s) (face_point_left / face_point_right are not allowed yet): {', '.join(banned[:5])}")

    return problems


def check_support_deck(deck: list[str], sup: pl.DataFrame | None = None) -> list[str]:
    """Validate a SUPPORT deck (list of support card names).

    Valid = exactly 10 cards, 2 copies of each of the 5 cards of ONE support
    faction. Returns a list of violations (empty = valid).
    """
    if sup is None:
        sup = load_support_cards()
    problems: list[str] = []
    if sup.is_empty():
        return problems          # no support pool -> nothing to validate against
    by_name = {r["card_name"]: r for r in sup.iter_rows(named=True)}

    unknown = [c for c in deck if c not in by_name]
    if unknown:
        problems.append(f"unknown support card(s): {', '.join(unknown[:5])}")

    if len(deck) != SUPPORT_DECK_SIZE:
        problems.append(f"must contain exactly {SUPPORT_DECK_SIZE} support cards (has {len(deck)})")

    facs = sorted({by_name[c]["support_faction_name"] for c in deck if c in by_name})
    if len(facs) > 1:
        problems.append(f"support cards must all come from a single support faction (found: {', '.join(facs)})")
    if len(facs) == 1:
        names = [c for c in deck if c in by_name]
        counts = {n: names.count(n) for n in set(names)}
        over = sorted(n for n, k in counts.items() if k > 2)
        if over:
            problems.append(f"support card(s) appear more than twice (max 2 copies each): {', '.join(over[:5])}")
        under = sorted(n for n, k in counts.items() if k < 2)
        if under:
            problems.append(f"support card(s) appear fewer than twice (a deck carries 2 copies of each of the 5 cards): {', '.join(under[:5])}")

    return problems


def check_deck(deck: list[str]) -> list[str]:
    """Validate a FULL deck (20 main + 10 support, any order).

    Splits the ids into main pool / support pool / unknown and runs both checks.
    Returns a list of violations (empty = valid deck).
    """
    by_id = _main_by_id()
    sup = load_support_cards()
    support_names = set(sup["card_name"].to_list()) if not sup.is_empty() else set()

    main = [c for c in deck if c in by_id]
    support = [c for c in deck if c in support_names]
    unknown = [c for c in deck if c not in by_id and c not in support_names]
    if unknown:
        return [f"unknown card(s) in deck: {', '.join(unknown[:5])}"]
    problems = check_main_deck(main, by_id)
    problems += check_support_deck(support, sup)
    return problems
