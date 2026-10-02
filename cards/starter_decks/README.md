# Faction starter decks (recovered)

Designer-authored **20-card starter decks** for the 6 main factions — one `card_id`
per line, in the original order.

## Provenance

Recovered from the **`GlobeRunners_card_system`** repo git history. They lived at
`cardpooUI/decks_saved/GlobeRunners_<Faction>_starter_deck.txt` and were **deleted**
in commit `f5823c8` ("Remove unused CSS file and starter decks", 2026-02-09).
Last present at `f5823c8^` (i.e. commit `5d995c6`).

Recover any of them with:

```bash
git -C ../GlobeRunners_card_system show "f5823c8^:cardpooUI/decks_saved/GlobeRunners_Dwarves_starter_deck.txt"
```

Two generations existed: a **v0.5** set (added at `e889f0a`) and the **v0.6** set
below (added at `5d995c6`). Only the v0.6 set resolves against the current
`cards/cardpool.parquet`. The print-and-play PDFs of these decks are published on
GitHub Releases (`jordyBonnet/GlobeRunners_images`, tag `v0.6-other-elmts`,
`<Faction>_starter_deck.pdf`) — the deckbuild app downloads them from there.

## Status vs the CURRENT deck rules (`deck_rules.check_main_deck`)

**Designer-edited 2026-10-02** — the 5 `face_point_right` cards that the original v0.6
lists contained have been **replaced**. All 6 decks are now **VALID** (20 cards,
single faction, single-copy, ≤5 per condition family, ≤5 per effect, no
`face_point_left`/`face_point_right`, and every card is in the *playable* pool
`ge.get_cardpool()`):

| Faction | Verdict | mana total / avg | Σ advancing | Σ shield | rare |
|---|---|---|---|---|---|
| Dwarves | **VALID** | 60 / 3.00 | +46 | 76 | 1 |
| Orcs | **VALID** | 58 / 2.90 | +61 | 40 | 0 |
| Twigs | **VALID** | 49 / 2.45 | +61 | 52 | 0 |
| Miaous | **VALID** | 48 / 2.40 | +17 | 50 | 0 |
| Demons | **VALID** | 52 / 2.60 | +31 | 34 | 0 |
| Mummies | **VALID** | 44 / 2.20 | +33 | 27 | 0 |

Cards removed / added in the 2026-10-02 pass:

| Faction | removed (was banned / rebalance) | added |
|---|---|---|
| Demons | 17 cards (incl. `Dem21_9b804d` *Qorra Aziza* `face_point_right`) | 17 new cards |
| Dwarves | `Dwa34_15527d` *Elda Branka* (`face_point_right`), `Dwa34_6275f2`, `Dwa45_a125f5` (2× `cataclysm`), `Dwa34_90172d` | `Dwa34_d351c4`, `Dwa45_0bfcfe`, `Dwa34_c48b33`, `Dwa34_fe1b6b` |
| Miaous | `Mia22_fe9a44` *Lina Fang* (`face_point_right`), `Mia22_4e44e8` | `Mia22_0e5d70`, `Mia33_9e0317` |
| Mummies | `Mum21_c2269e` | `Mum22_d4e984` |
| Orcs | `Orc43_04affc` *Puga Dagra* (`face_point_right`) | `Orc43_b93b79` |
| Twigs | `Twi22_cb2fcb` *Rootstrider Bircha* (`face_point_right`) | `Twi45_2f57d1` |

> The **print-and-play PDFs** on GitHub Releases (`v0.6-other-elmts`) still show the
> **old** lists — they are now out of date w.r.t. these files.

## Wired into the game (2026-10-02)

- **Server** — `API.py`: `load_starter_decks()` reads these files once per process and
  serves them at **`GET /starter_decks`** → `{"Dwa": [20 card_id], "Dem": [...], …}`.
  The faction key comes from the **card_id prefix**, so the file *names* are not part of
  the contract; a file whose cards mix two faction prefixes is ignored (with a warning).
  Live on both servers: standalone `API.py` (:8000) and `game_ui` (:8001, through the
  catch-all mount). `/cardpool`, `/support_factions` and `/starter_decks` send
  `Cache-Control: no-store` so an edited file is picked up on a plain reload.
- **Frontend** — `cards.mjs` `STARTER_DECKS` / `loadStarterDecks()`; `setup.mjs`
  `buildStarterDeck()` serves the authored list **as-is** when it passes `checkMainDeck`,
  otherwise falls back to the seeded random build (silently while no list is loaded yet,
  with a **one-shot toast** when the authored list exists but breaks a deck rule).
- **To change a starter deck**: edit the `.txt`, restart the server, reload. The setup
  page shows the deck-rule issues panel, and `deck_rules.py` (`API.check_deck`) stays the
  hard gate at launch — an invalid list never reaches a game.

All 18 faction × support-faction combinations (`20 main + 10 support`) pass
`deck_rules.check_deck`.

