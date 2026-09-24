"""Deck rules (deck_rules.py) — manual smoke test. No games.db writes.

Covers:
  * a valid 20-card single-faction deck -> 0 problems
  * mixed factions / wrong size / duplicates -> flagged
  * max-5-per-condition-family (numeric variants merged: 3x temp_inf_6 + 3x temp_inf_11 = 6)
  * 5 per family is OK, 6 per effect flagged
  * face_point_left / face_point_right refused (synthetic rows — not in the pool yet)
  * support deck: 2x5 one faction OK, 3 copies / mixed factions / short deck flagged
  * full 30-card deck check (main + support split)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deck_rules  # noqa: E402
import engine.game_engine as ge  # noqa: E402

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, f"FAIL: {label}"
    PASS += 1
    print(f"  ok  {label}")


pool = [r for r in ge.get_cardpool().iter_rows(named=True)]
dwa = [r for r in pool if r["faction"] == "Dwarves"]
orc = [r for r in pool if r["faction"] == "Orcs"]


def greedy(rows, n, avoid=None, pre=None):
    """Pick n distinct rows respecting the max-5 family/effect caps (test helper).
    `pre` = already-picked rows (their families/effects count against the caps)."""
    from collections import Counter
    avoid = avoid or set()
    deck, fam, eff = [], Counter(), Counter()
    for c in (pre or []):
        fam[deck_rules.cond_family(c["condition"])] += 1
        eff[c["effect"]] += 1
    for c in rows:
        if len(deck) >= n or c["card_id"] in avoid:
            continue
        f = deck_rules.cond_family(c["condition"])
        if fam[f] >= 5 or eff[c["effect"]] >= 5:
            continue
        fam[f] += 1
        eff[c["effect"]] += 1
        deck.append(c)
    return deck


print("== main deck rules ==")
valid = greedy(dwa, 20)
ok(len(valid) == 20, "greedy helper built a 20-card deck")
ok(deck_rules.check_main_deck([c["card_id"] for c in valid]) == [], "valid deck -> 0 problems")

# mixed factions
mix = [c["card_id"] for c in dwa[:10]] + [c["card_id"] for c in orc[:10]]
ok(any("single main faction" in p for p in deck_rules.check_main_deck(mix)), "mixed factions flagged")

# wrong size
ok(any("exactly 20" in p for p in deck_rules.check_main_deck([c["card_id"] for c in dwa[:19]])), "19 cards flagged")

# duplicates
dup = [dwa[0]["card_id"]] * 2 + [c["card_id"] for c in dwa[1:19]]
ok(any("duplicate" in p for p in deck_rules.check_main_deck(dup)), "duplicate card flagged")

# condition family: 3x temp_inf_6 + 3x temp_inf_11 = ONE family of 6
t6 = [c for c in dwa if c["condition"] == "temp_inf_6"][:3]
t11 = [c for c in dwa if c["condition"] == "temp_inf_11"][:3]
rest = greedy(dwa, 14, avoid={c["card_id"] for c in t6 + t11}, pre=t6 + t11)
bad = [c["card_id"] for c in t6 + t11 + rest]
ok(any("temp_inf' has 6" in p for p in deck_rules.check_main_deck(bad)), "3x temp_inf_6 + 3x temp_inf_11 flagged as one family of 6")

# 5 per family is OK
five = [c for c in dwa if c["condition"] == "temp_inf_6"][:5]
rest5 = greedy(dwa, 15, avoid={c["card_id"] for c in five}, pre=five)
ok(not any("temp_inf" in p for p in deck_rules.check_main_deck([c["card_id"] for c in five + rest5])), "5 in one family is OK")

# 6 per effect
jmp = [c for c in dwa if c["effect"] == "jump"][:6]
restj = greedy(dwa, 14, avoid={c["card_id"] for c in jmp}, pre=jmp)
ok(any("'jump' has 6" in p for p in deck_rules.check_main_deck([c["card_id"] for c in jmp + restj])), "6 jump cards flagged")

# banned values (synthetic rows — face_point_* are not in the pool yet)
syn = {
    f"X{i:02d}_deadbeef": {"card_id": f"X{i:02d}_deadbeef", "faction": "Dwarves",
                           "condition": "face_point_left", "effect": "advancing"}
    for i in range(3)
}
syn.update({c["card_id"]: c for c in dwa[:17]})
banned_ids = list(syn)
ok(any("banned" in p for p in deck_rules.check_main_deck(banned_ids, by_id=syn)), "face_point_left cards refused")
syn2 = dict(syn)
syn2["X00_deadbeef"] = {"card_id": "X00_deadbeef", "faction": "Dwarves", "condition": "no_condition", "effect": "face_point_right"}
ok(any("banned" in p for p in deck_rules.check_main_deck(list(syn2), by_id=syn2)), "face_point_right (as effect) refused")

# unknown cards
ok(any("unknown" in p for p in deck_rules.check_main_deck([c["card_id"] for c in valid[:19]] + ["NOPE_1234"])), "unknown card flagged")

print("== support deck rules ==")
sup = deck_rules.load_support_cards()
eng = sorted({r["card_name"] for r in sup.iter_rows(named=True) if r["support_faction_name"] == "engineers"})
ok(len(eng) == 5, "engineers have 5 support cards")
ok(deck_rules.check_support_deck([n for n in eng for _ in range(2)]) == [], "2x5 one faction -> 0 problems")

three = eng[:4] * 2 + [eng[0]]
ok(any("more than twice" in p for p in deck_rules.check_support_deck(three)), "3 copies of a support card flagged")

mag = sorted({r["card_name"] for r in sup.iter_rows(named=True) if r["support_faction_name"] == "mages"})
mixed = [n for n in eng[:4] for _ in range(2)] + mag[:2]
ok(any("single support faction" in p for p in deck_rules.check_support_deck(mixed)), "mixed support factions flagged")

short = [n for n in eng for _ in range(2)][:8]
ok(any("exactly 10" in p for p in deck_rules.check_support_deck(short)), "8 support cards flagged")

print("== full 30-card deck ==")
full = [c["card_id"] for c in valid] + [n for n in eng for _ in range(2)]
ok(deck_rules.check_deck(full) == [], "20 main + 2x5 engineers -> 0 problems")
ok(any("unknown" in p for p in deck_rules.check_deck(full + ["NOPE_1234"])), "unknown card in full deck flagged")
ok(any("exactly 20" in p for p in deck_rules.check_deck([c["card_id"] for c in valid[:18]] + [n for n in eng for _ in range(2)])), "18 main cards flagged in full deck")

print(f"\nPASS — {PASS} checks, all deck-rule assertions held.")
