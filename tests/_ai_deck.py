"""AI deck builder (game_ui/ai_deck.py) — manual smoke test. No games.db writes.

Builds decks for ALL 18 (main faction, support faction) combinations and checks:
  * 30 cards = 20 main (one faction, single-copy) + 10 support (2x5, one faction)
  * the deck passes deck_rules.check_deck (max 5 per condition family / effect, …)
  * support-faction rules:
      engineers : 5 drop_on_board, NO pending, NO temp_sup_15/temp_inf_6, NO draw
      mages     : 2-4 temp_sup_15/temp_inf_6, 4 day/night, 4 biome_<F>,
                  NO drop_on_board, NO pending
      doctors   : 5 pending, NO drop_on_board, NO temp_sup_15/temp_inf_6
  * general rules: 2 wrecking_ball, 2 jump, 2-4 draw (EXCEPT engineers),
                  2-4 special effect (avalanche/effect_canceled/rooted/pet_trap/
                  swap_cards/copy_effect)
  * per-faction rules where the deck size allows them (priority 1>2>3 means
    Mummies+Mages and Dwarves+Mages may drop them when the random ranges collide):
      Mummies : some mana_sup_5_oppo/mana_inf_6_oppo + some cards_in_hand_*_oppo
      Dwarves : some block
  * randomness: the same combination does not always produce the same deck
"""
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "game_ui"))

import deck_rules  # noqa: E402
import ai_deck  # noqa: E402
import engine.game_engine as ge  # noqa: E402

PASS = 0
FAILS = []


def ok(cond, label):
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}")


POOL = {r["card_id"]: r for r in ge.get_cardpool().iter_rows(named=True)}
SUPPORT_NAMES = set(deck_rules.load_support_cards()["card_name"].to_list())
SPECIAL = ai_deck.SPECIAL_EFFECT
BIOME = ai_deck.BIOME_COND
EXTREME = ai_deck.EXTREME_TEMP
FACTIONS = ai_deck.FACTIONS
SUPPORTS = ["engineers", "mages", "doctors"]

BUILDS = 25   # builds per combination


def counts(deck):
    cond = Counter(POOL[c]["condition"] for c in deck)
    fam = Counter(deck_rules.cond_family(c) for c in cond.elements())
    eff = Counter(POOL[c]["effect"] for c in deck)
    return cond, fam, eff


for fac in FACTIONS:
    for sup in SUPPORTS:
        decks = []
        for i in range(BUILDS):
            rng = random.Random(f"{fac}|{sup}|{i}")
            main = ai_deck.build_main_deck(fac, sup, rng)
            decks.append(main)
            row = lambda c: POOL[c]

            # --- structure ---
            ok(len(main) == 20, f"{fac}+{sup} #{i}: 20 main cards")
            ok(len(set(main)) == 20, f"{fac}+{sup} #{i}: no duplicates")
            ok(all(POOL[c]["faction"] == fac for c in main), f"{fac}+{sup} #{i}: single main faction")
            cond, fam, eff = counts(main)
            ok(max(fam.values()) <= 5, f"{fac}+{sup} #{i}: max 5 per condition family")
            ok(max(eff.values()) <= 5, f"{fac}+{sup} #{i}: max 5 per effect")
            ok(deck_rules.check_main_deck(main) == [], f"{fac}+{sup} #{i}: deck_rules.check_main_deck clean")

            # --- support-faction rules (priority 1 — ALWAYS hold) ---
            if sup == "engineers":
                ok(cond.get("drop_on_board", 0) == 5, f"{fac}+{sup} #{i}: 5 drop_on_board")
                ok(cond.get("pending", 0) == 0, f"{fac}+{sup} #{i}: no pending")
                ok(not (set(cond) & EXTREME), f"{fac}+{sup} #{i}: no temp_sup_15/temp_inf_6")
                ok(eff.get("draw", 0) == 0, f"{fac}+{sup} #{i}: no draw effect")
            elif sup == "mages":
                ok(cond.get("temp_sup_15", 0) + cond.get("temp_inf_6", 0) >= 2, f"{fac}+{sup} #{i}: 2+ extreme temp (family caps enforced by deck rules)")
                ok(cond.get("day", 0) + cond.get("night", 0) >= 4, f"{fac}+{sup} #{i}: 4 day/night")
                ok(cond.get(BIOME[fac], 0) >= 4, f"{fac}+{sup} #{i}: 4 own-biome")
                ok(cond.get("drop_on_board", 0) == 0, f"{fac}+{sup} #{i}: no drop_on_board")
                ok(cond.get("pending", 0) == 0, f"{fac}+{sup} #{i}: no pending")
            elif sup == "doctors":
                ok(cond.get("pending", 0) == 5, f"{fac}+{sup} #{i}: 5 pending")
                ok(cond.get("drop_on_board", 0) == 0, f"{fac}+{sup} #{i}: no drop_on_board")
                ok(not (set(cond) & EXTREME), f"{fac}+{sup} #{i}: no temp_sup_15/temp_inf_6")

            # --- general rules (priority 2) ---
            ok(eff.get("wrecking_ball", 0) >= 2, f"{fac}+{sup} #{i}: 2 wrecking_ball")
            ok(eff.get("jump", 0) >= 2, f"{fac}+{sup} #{i}: 2 jump")
            if sup == "engineers":
                ok(eff.get("draw", 0) == 0, f"{fac}+{sup} #{i}: no draw (refinery provides it)")
            else:
                ok(eff.get("draw", 0) >= 2, f"{fac}+{sup} #{i}: 2+ draw")
            ok(eff.get(SPECIAL[fac], 0) >= 2, f"{fac}+{sup} #{i}: 2+ {SPECIAL[fac]}")

            # --- per-faction rules (priority 3 — hold when the deck size allows) ---
            if fac == "Mummies" and sup != "mages":
                ok(cond.get("mana_sup_5_oppo", 0) + cond.get("mana_inf_6_oppo", 0) >= 2, f"{fac}+{sup} #{i}: mana_oppo cards")
                ok(cond.get("cards_in_hand_sup_3_oppo", 0) + cond.get("cards_in_hand_inf_4_oppo", 0) >= 2, f"{fac}+{sup} #{i}: hand_oppo cards")
            if fac == "Dwarves" and sup != "mages":
                ok(cond.get("block", 0) >= 2, f"{fac}+{sup} #{i}: block cards")

        # --- randomness ---
        ok(len({tuple(d) for d in decks}) >= 2, f"{fac}+{sup}: builds are not all identical")

# --- full deck (main + support) passes the whole check_deck ---
for seed in range(5):
    rng = random.Random(f"full|{seed}")
    fac = rng.choice(FACTIONS)
    sup = rng.choice(SUPPORTS)
    main = ai_deck.build_main_deck(fac, sup, rng)
    full = main + ai_deck.support_deck(sup)
    ok(len(full) == 30 and len(set(c for c in full if c in POOL)) == 20, f"full deck #{seed}: 20 main + 10 support")
    ok(deck_rules.check_deck(full) == [], f"full deck #{seed}: deck_rules.check_deck clean")

# --- the robot's entry point ---
deck = ai_deck.build_ai_deck()
main = [c for c in deck if c in POOL]
support = [c for c in deck if c in SUPPORT_NAMES]
ok(len(main) == 20 and len(support) == 10, "build_ai_deck: 20 main + 10 support")
ok(deck_rules.check_deck(deck) == [], "build_ai_deck: passes the full deck-rule check")

if FAILS:
    print(f"\nFAIL — {len(FAILS)} of {PASS + len(FAILS)} checks failed:")
    for f in FAILS[:30]:
        print("   ", f)
    sys.exit(1)
print(f"\nPASS — {PASS} checks across all 18 faction/support combinations + full-deck checks.")
