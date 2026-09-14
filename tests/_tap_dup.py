# Manual smoke test: refinery TAP draw must not duplicate a card (2026-09-04).
#
# Bug: the WS layer (API.py) and the AI driver pass `player` as a model_copy()
# of the stored PlayerState. The dwelling-tap branch drew with `_draw_cards(player, 1)`
# (popping the COPY's deck, appending to the COPY's hand), then the sync-back
# loop copied `hand` (and mana/dwelling) to the authoritative state but NOT
# `deck`/`discard` -> the drawn card stayed in the persisted deck (card exists
# in two zones) and was re-drawn on the next draw (cleaning / ramp / ...) ->
# duplicate card in hand.
#
# NOTE: the old tests (_engineers.py etc.) call ge.player_play() directly with
# gs.players[name] (the authoritative object), so they never saw the bug.
# This test drives every action through ge.handle_websocket_message() with a
# model_copy() - exactly like API.py / ai_driver.py.
#
# Run from the project root:  uv run python tests/_tap_dup.py
# NOTE: writes to games.db (like the other smoke tests); the game is deleted after.
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
main = DB.filter(pl.col('faction') == 'Miaous')['card_id'].to_list()[:20]

def zones(gs, name):
    p = gs.players[name]
    return {
        'hand': list(p.hand or []), 'mana': list(p.mana or []),
        'deck': list(p.deck or []), 'discard': list(p.discard or []),
        'dwelling': [p.dwelling] if p.dwelling else [],
    }

def all_cards(z):
    return [c for lst in z.values() for c in lst]

def send(gid, name, message):
    """ drive the engine exactly like the WS layer / AI driver do """
    conn, _, gs = ge.get_current_game(gid)
    player = gs.players[name].model_copy()
    player.message = message
    conn.close()
    resp = ge.handle_websocket_message(gid, player)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    return gs, resp

ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

# --- build a deterministic game ---------------------------------------------
# A's deck (30 cards, in order):
#   hand (first 6):  [f0, f1, REFINERY, f2, f3, f4]
#   deck pile:       [BOOST, f5, f6, ..., f19, s1, s2]   <- BOOST is the top card
a_deck = [main[0], main[1], 'refinery', main[3], main[4], main[5],
          'boost', main[6], main[7], main[8], main[9], main[10], main[11],
          main[12], main[13], main[14], main[15], main[16], main[17], main[18],
          'trampoline', 'gluetrap']
b_deck = [main[19]] * 30   # filler deck (B only mana-places + passes)

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

# --- deterministic zones (create_new_game random-samples the hand) ----------
c, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
ga, gb = gs.players['A'], gs.players['B']
ga.hand = [main[0], main[1], 'refinery', main[3], main[4], main[5]]
ga.deck = ['boost', main[6], main[7], main[8], main[9], main[10], main[11],
           main[12], main[13], main[14], main[15], main[16], main[17], main[18],
           'trampoline', 'gluetrap']
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.hand = [main[19]] * 6
gb.deck = [main[19]] * 24
gb.mana, gb.discard, gb.dwelling = [], [], None
c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
c.commit()
c.close()
print(f"game {gid} created (engine_version={gs.engine_version})")

try:
    # --- init: both put 3 cards in mana (A keeps the refinery in hand) ------
    gs, _ = send(gid, 'A', {'cards': [main[0], main[1], main[3]], 'to': 'mana', 'mode': '', 'pendings': []})
    gs, _ = send(gid, 'B', {'cards': b_deck[:3], 'to': 'mana', 'mode': '', 'pendings': []})
    check('init done - turn 1 play phase',
          'turn 1' in gs.state and 'to play' in gs.state, f"(state={gs.state})")

    # --- A (first player) places the refinery -------------------------------
    gs, resp = send(gid, 'A', {'cards': ['refinery'], 'to': 'dwelling', 'mode': '', 'pendings': []})
    check('refinery placed in the dwelling zone', gs.players['A'].dwelling == 'refinery')

    # --- B passes, so it is A's turn again -----------------------------------
    gs, _ = send(gid, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})

    # --- A TAPS the refinery (the buggy path) --------------------------------
    before = zones(gs, 'A')
    top_of_deck = before['deck'][0]
    gs, resp = send(gid, 'A', {'cards': [], 'to': 'dwelling', 'mode': 'dwelling_activation', 'pendings': []})
    after = zones(gs, 'A')

    check('tap drew exactly 1 card', len(after['hand']) == len(before['hand']) + 1,
          f"(hand {before['hand']} -> {after['hand']})")
    check('drew the top card of the deck', top_of_deck in after['hand'],
          f"(expected {top_of_deck})")
    check('BUG GONE: drawn card removed from the deck', top_of_deck not in after['deck'],
          f"(deck still starts with {after['deck'][:4]})")
    check('card conservation after tap', len(all_cards(after)) == len(a_deck),
          f"({len(all_cards(after))} cards across zones, deck has {len(a_deck)})")

    # --- A passes -> both passed -> resolution + cleaning draw ---------------
    gs, _ = send(gid, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    final = zones(gs, 'A')

    from collections import Counter
    final_counts = Counter(all_cards(final))
    original_counts = Counter(a_deck)
    check('BUG GONE: no duplicated card after the cleaning draw',
          all(final_counts[c] <= original_counts[c] for c in final_counts),
          f"({dict(final_counts)})")
    check('card conservation after cleaning', len(all_cards(final)) == len(a_deck),
          f"({len(all_cards(final))} cards across zones, deck has {len(a_deck)})")
    check('hand gained 3 cards (cleaning draw)',
          len(final['hand']) == len(after['hand']) + 3,
          f"(hand now {final['hand']})")

    print(f"\nALL {ok} CHECKS PASSED - refinery tap draw no longer duplicates cards")
finally:
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()
