"""AI deck builder — a "not so dumb" random deck for the Robot.

Picks a RANDOM main faction and a RANDOM support faction, then builds a 20-card
main deck that (a) satisfies the deck rules (`deck_rules.py`: single faction,
single-copy, max 5 per condition family / effect) and (b) follows the
support-faction strategy rules (designer-specified 2026-09-24):

  Engineers : exactly 5 `drop_on_board` condition cards
              NO `pending` condition • NO `temp_sup_15` / `temp_inf_6`
              NO `draw` effect (the draw comes from the refinery dwelling)
  Mages     : NO `drop_on_board` • NO `pending`
              2-4 `temp_sup_15` / `temp_inf_6` condition cards (whichever the
              main faction has access to) • exactly 4 `day`/`night` condition
              cards • exactly 4 own-biome (`biome_<faction>`) condition cards
  Doctors   : NO `drop_on_board` • NO `temp_sup_15` / `temp_inf_6`
              exactly 5 `pending` condition cards

  General   : 2 `wrecking_ball` • 2 `jump` • 2-4 `draw` (EXCEPT Engineers)
              2-4 special main-faction effect cards — Dwarves: avalanche,
              Demons: effect_canceled, Twigs: rooted, Miaous: pet_trap,
              Orcs: swap_cards, Mummies: copy_effect
  Per-faction: Mummies — some `mana_sup_5_oppo` / `mana_inf_6_oppo` AND some
              `cards_in_hand_sup_3_oppo` / `cards_in_hand_inf_4_oppo` cards
               Dwarves  — some `block` condition cards

Priority when the combination of rules is not feasible (1 = kept first):
  1. support-faction rules  2. general rules  3. per-faction rules.
When the 20-card size makes the combination tight, the builder first relaxes
the GENERAL 2-4 ranges (still inside their allowed range) to keep the
per-faction flavor cards — the priority drop (per-faction first) only kicks in
when NO in-range combination fits (that is exactly Mummies+Mages: the minimums
already total 22, so the Mummies oppo-state cards are the ones dropped).

Randomness is kept: the CONCRETE cards are drawn at random (rarity-weighted),
only the composition constraints are enforced. A single card may count toward
several requirements (e.g. a `day` card with the `wrecking_ball` effect serves
both) — the requirements describe the deck's composition, not disjoint sets.
"""

from __future__ import annotations

import random
from collections import Counter

import polars as pl

import engine.game_engine as ge
from deck_rules import MAIN_DECK_SIZE, MAX_PER_CONDITION, MAX_PER_EFFECT, cond_family, load_support_cards

FACTIONS = ["Dwarves", "Demons", "Twigs", "Miaous", "Orcs", "Mummies"]

# the faction's own-biome condition
BIOME_COND = {
    "Dwarves": "biome_Dwa", "Demons": "biome_Dem", "Twigs": "biome_Twi",
    "Miaous": "biome_Mia", "Orcs": "biome_Orc", "Mummies": "biome_Mum",
}

# the special main-faction effect of each faction
SPECIAL_EFFECT = {
    "Dwarves": "avalanche", "Demons": "effect_canceled", "Twigs": "rooted",
    "Miaous": "pet_trap", "Orcs": "swap_cards", "Mummies": "copy_effect",
}

EXTREME_TEMP = {"temp_sup_15", "temp_inf_6"}

# requirement shape: {"kind": "cond"|"eff", "values": set, "count": int}
SUPPORT_PROFILES: dict[str, dict] = {
    "engineers": {
        "exclude_cond": {"pending"} | EXTREME_TEMP,
        "exclude_effect": {"draw"},
        "reqs": [{"kind": "cond", "values": {"drop_on_board"}, "count": 5}],
    },
    "mages": {
        "exclude_cond": {"drop_on_board", "pending"},
        "exclude_effect": set(),
        "reqs": [
            {"kind": "cond", "values": set(EXTREME_TEMP), "count": 2, "max": 4},   # random 2-4
            {"kind": "cond", "values": {"day", "night"}, "count": 4},
        ],
    },
    "doctors": {
        "exclude_cond": {"drop_on_board"} | EXTREME_TEMP,
        "exclude_effect": set(),
        "reqs": [{"kind": "cond", "values": {"pending"}, "count": 5}],
    },
}

# per-faction (tier 3) rules
PER_FACTION: dict[str, list[dict]] = {
    "Mummies": [
        {"kind": "cond", "values": {"mana_sup_5_oppo", "mana_inf_6_oppo"}, "count": 2},
        {"kind": "cond", "values": {"cards_in_hand_sup_3_oppo", "cards_in_hand_inf_4_oppo"}, "count": 2},
    ],
    "Dwarves": [
        {"kind": "cond", "values": {"block"}, "count": 2, "max": 3},   # random 2-3
    ],
}


def random_support_faction() -> str | None:
    """A random support faction name ('engineers' | 'mages' | 'doctors'), or None
    if the support data file is missing (the deck then stays main-only)."""
    sup = load_support_cards()
    if sup.is_empty():
        return None
    return random.choice(sup["support_faction_name"].unique().to_list())


def support_deck(support: str) -> list[str]:
    """2 copies of each of the 5 cards of `support` (10 total), shuffled."""
    sup = load_support_cards()
    names = sup.filter(pl.col("support_faction_name") == support)["card_name"].to_list()
    deck = [n for n in names for _ in range(2)]
    random.shuffle(deck)
    return deck


def _matches(req: dict, row) -> bool:
    key = "condition" if req["kind"] == "cond" else "effect"
    return row.get(key) in req["values"]


def _weighted_sample(cands: list, k: int, rng: random.Random) -> list:
    """Sample k distinct cards from cands (rare x1 / other x3 weighting)."""
    bag: list = []
    for c in cands:
        bag.extend([c] * (1 if c.get("rare") else 3))
    out: list = []
    seen: set = set()
    while len(out) < k and bag:
        c = bag.pop(rng.randrange(len(bag)))
        if c["card_id"] not in seen:
            seen.add(c["card_id"])
            out.append(c)
    return out


def build_main_deck(faction: str, support: str | None, rng: random.Random | None = None) -> list[str]:
    """Build a 20-card main deck for `faction` + `support` following the rules.

    Requirement tiers (priority when infeasible): 1 = support profile,
    2 = general, 3 = per-faction.
    """
    rng = rng or random
    pool = [
        r for r in ge.get_cardpool().iter_rows(named=True)
        if r["faction"] == faction
    ]
    prof = SUPPORT_PROFILES.get(support or "", {"exclude_cond": set(), "exclude_effect": set(), "reqs": []})
    pool = [
        c for c in pool
        if c["condition"] not in prof["exclude_cond"] and c["effect"] not in prof["exclude_effect"]
    ]

    # ---------- 1. requirement counts (random ranges, slot-aware) ----------
    # each requirement: {kind, values, count, min} — `min` is the floor the
    # count can shrink to (support floors are their required value: never dropped)
    # tier 1: support profile (random counts within their ranges)
    tier1 = [
        {"kind": r["kind"], "values": set(r["values"]),
         "count": rng.randint(r["count"], r.get("max", r["count"])), "min": r["count"]}
        for r in prof["reqs"]
    ]
    # Mages: the 4 own-biome cards (a fixed support-profile requirement)
    if support == "mages":
        tier1.append({"kind": "cond", "values": {BIOME_COND[faction]}, "count": 4, "min": 4})
    # tier 2: general (min 2 each — the random range is 2..4)
    tier2 = [
        {"kind": "eff", "values": {"wrecking_ball"}, "count": 2, "min": 2},
        {"kind": "eff", "values": {"jump"}, "count": 2, "min": 2},
    ]
    if support != "engineers":
        tier2.append({"kind": "eff", "values": {"draw"}, "count": rng.randint(2, 4), "min": 2})
    tier2.append({"kind": "eff", "values": {SPECIAL_EFFECT[faction]}, "count": rng.randint(2, 4), "min": 2})
    # tier 3: per-faction (min 0 — these are the FIRST to be dropped)
    tier3 = [
        {"kind": r["kind"], "values": set(r["values"]),
         "count": rng.randint(r["count"], r.get("max", r["count"])), "min": 0}
        for r in PER_FACTION.get(faction, [])
    ]

    # feasibility: make the total fit in 20 without breaking the "always add"
    # rules — first relax the general 2-4 ranges (still inside range) so the
    # per-faction flavor cards survive; only when NO in-range combination fits
    # (Mummies+Mages: minimums total 22) drop the per-faction rules (priority 3
    # is last); the support minimums always fit (worst case Mages: 2+4+4 +
    # 2+2+2+2 = 18 <= 20).
    def total() -> int:
        return sum(r["count"] for r in tier1 + tier2 + tier3)

    if total() > MAIN_DECK_SIZE:
        for r in tier2:
            r["count"] = r["min"]          # general ranges -> their minimum
    if total() > MAIN_DECK_SIZE:
        for r in tier3:
            r["count"] = r["min"]          # per-faction rules dropped (priority 3)
    if total() > MAIN_DECK_SIZE:
        for r in tier1:
            r["count"] = r["min"]          # support ranges -> their minimum

    # ---------- 2. pick the requirement cards ----------
    deck: list[str] = []
    used: set = set()
    fam_count: Counter = Counter()
    eff_count: Counter = Counter()

    def caps_ok(row) -> bool:
        return fam_count[cond_family(row["condition"])] < MAX_PER_CONDITION \
            and eff_count[row["effect"]] < MAX_PER_EFFECT

    for req in tier1 + tier2 + tier3:
        if req["count"] <= 0:
            continue
        have = sum(1 for cid in deck if _matches(req, _row(cid)))
        need = req["count"] - have
        if need <= 0:
            continue
        # pick ONE at a time, re-checking the max-5 caps after EVERY pick — a
        # batch taken against a single cap snapshot could push a family/effect
        # over the cap (e.g. a 2-card batch when the effect was already at 4)
        picked_ids: set = set()
        while need > 0 and len(deck) < MAIN_DECK_SIZE:
            cands = [
                c for c in pool
                if c["card_id"] not in used and c["card_id"] not in picked_ids
                and _matches(req, c) and caps_ok(c)
            ]
            if not cands:
                break   # cap or pool exhausted — take what we could (priority order)
            c = _weighted_sample(cands, 1, rng)[0]
            picked_ids.add(c["card_id"])
            used.add(c["card_id"])
            deck.append(c["card_id"])
            fam_count[cond_family(c["condition"])] += 1
            eff_count[c["effect"]] += 1
            need -= 1

    # ---------- 3. filler (random, rarity-weighted, caps respected) ----------
    while len(deck) < MAIN_DECK_SIZE:
        cands = [c for c in pool if c["card_id"] not in used and caps_ok(c)]
        if not cands:
            cands = [c for c in pool if c["card_id"] not in used]
        if not cands:
            break
        c = _weighted_sample(cands, 1, rng)[0]
        used.add(c["card_id"])
        deck.append(c["card_id"])
        fam_count[cond_family(c["condition"])] += 1
        eff_count[c["effect"]] += 1

    return deck


_POOL: dict | None = None


def _row(card_id: str):
    global _POOL
    if _POOL is None:
        _POOL = {r["card_id"]: r for r in ge.get_cardpool().iter_rows(named=True)}
    return _POOL[card_id]


def build_ai_deck() -> list[str]:
    """The Robot's full deck: 20 main cards (random faction) + 10 support cards
    (random support faction), shuffled together — the same 30-card composition as
    a human's deck. The main deck follows the support-faction strategy rules
    (see the module docstring)."""
    faction = random.choice(FACTIONS)
    support = random_support_faction()
    main = build_main_deck(faction, support)
    deck = main + (support_deck(support) if support else [])
    random.shuffle(deck)
    return deck
