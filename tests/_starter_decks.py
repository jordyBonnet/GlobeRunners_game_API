"""Designer starter decks (cards/starter_decks/*.txt -> GET /starter_decks) — manual smoke test.

No games.db writes, no server needed (the loader + the deck rules are called directly).

Covers:
  * the 6 faction files are read by API.load_starter_decks() and keyed by card_id prefix
  * every list: exactly 20 cards, single-copy, ONE faction matching its key,
    every card in the PLAYABLE pool (ge.get_cardpool() — face_point_* excluded),
    no banned condition/effect, deck_rules.check_main_deck -> 0 problems
  * every main starter deck x each of the 3 support factions -> deck_rules.check_deck -> 0 problems
  * the GET /starter_decks route exists and the no-store middleware is registered
    (a stale browser cache would serve an OLD starter deck after the designer edits a file)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import API  # noqa: E402
import deck_rules  # noqa: E402
import engine.game_engine as ge  # noqa: E402

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, f"FAIL: {label}"
    PASS += 1
    print(f"  ok  {label}")


FACTION_KEYS = {"Dwa", "Dem", "Twi", "Mia", "Orc", "Mum"}
FACTION_NAMES = {"Dwa": "Dwarves", "Dem": "Demons", "Twi": "Twigs",
                 "Mia": "Miaous", "Orc": "Orcs", "Mum": "Mummies"}

# ---------------------------------------------------------------- files on disk
files = sorted(p.name for p in API.STARTER_DECKS_DIR.glob("*.txt"))
ok(API.STARTER_DECKS_DIR.is_dir(), "cards/starter_decks/ exists")
ok(len(files) == 6, f"6 starter-deck files (found {len(files)}: {files})")
for key, name in FACTION_NAMES.items():
    ok(f"GlobeRunners_{name}_starter_deck.txt" in files, f"file for {name} ({key})")

# ---------------------------------------------------------------- the loader
decks = API.load_starter_decks()
ok(set(decks) == FACTION_KEYS, f"load_starter_decks() keys == {sorted(FACTION_KEYS)} (got {sorted(decks)})")

pool = {r["card_id"]: r for r in ge.get_cardpool().iter_rows(named=True)}
banned = deck_rules.BANNED_VALUES

for key in sorted(FACTION_KEYS):
    ids = decks[key]
    ok(len(ids) == deck_rules.MAIN_DECK_SIZE, f"{key}: exactly {deck_rules.MAIN_DECK_SIZE} cards (has {len(ids)})")
    ok(len(set(ids)) == len(ids), f"{key}: single-copy format")
    ok(all(i in pool for i in ids),
       f"{key}: every card is in the playable pool ({len(set(ids) - set(pool))} missing)")
    rows = [pool[i] for i in ids if i in pool]
    facs = {r["faction"] for r in rows}
    ok(facs == {FACTION_NAMES[key]}, f"{key}: single faction = {FACTION_NAMES[key]} (found {sorted(facs)})")
    ok(not [r for r in rows if r["condition"] in banned or r["effect"] in banned],
       f"{key}: no banned face_point_* card")
    ok(deck_rules.check_main_deck(ids) == [], f"{key}: deck_rules.check_main_deck -> 0 problems")

# ---------------------------------------------------------------- main x support combinations
sup = API.load_support_cards()
sup_names = sup["card_name"].to_list() if not sup.is_empty() else []
sup_facs = sup["support_faction_name"].to_list() if not sup.is_empty() else []
support_factions = sorted(set(sup_facs))
ok(len(support_factions) == 3, f"3 support factions ({support_factions})")
for fac in support_factions:
    sdeck = [n for n, f in zip(sup_names, sup_facs) if f == fac for _ in range(2)]
    ok(len(sdeck) == deck_rules.SUPPORT_DECK_SIZE, f"support deck {fac}: {deck_rules.SUPPORT_DECK_SIZE} cards")
    for key in sorted(FACTION_KEYS):
        problems = deck_rules.check_deck(decks[key] + sdeck)
        ok(problems == [], f"{key} + {fac}: check_deck -> 0 problems{'' if not problems else ' -> ' + str(problems)}")

# ---------------------------------------------------------------- the endpoint
paths = [getattr(r, "path", None) for r in API.app.routes]
ok("/starter_decks" in paths, "GET /starter_decks is routed")
ok("/cardpool" in paths and "/support_factions" in paths, "/cardpool + /support_factions still routed")
ok(any((getattr(m, "kwargs", {}) or {}).get("dispatch") is not None and
       (getattr(m, "kwargs", {}) or {}).get("dispatch").__name__ == "no_store_card_data"
       for m in API.app.user_middleware),
   "the no-store middleware is registered (card data must not be browser-cached)")

print(f"\nSTARTER DECKS TEST PASSED ({PASS} checks)")
