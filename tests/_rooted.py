# Manual smoke test: rooted (rule of engine_version 10)
#  - effect == 'rooted' fires (condition met, not blocked, not canceled) -> the card
#    earns a rooted token (rooted_this_turn) and survives the cleaning phase
#  - end of turn: the card is pulled out of the discard and placed onto a FREE
#    stopover (rooted_on_board), in play order, no stacking
#  - the token is ONE-SHOT: the card loses it when placed; the NEXT end-of-turn
#    discards it (card conservation)
#  - cooldown: a card that earned a token the PREVIOUS turn cannot earn another
#  - old games (engine_version < 10): rooted is a no-op (no token, card discarded)
#  - card conservation: every card is in exactly one zone
# Run from the project root:  uv run python tests/_rooted.py
# NOTE: writes to games.db (like _diag.py)
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import sqlite3
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

def q(**kw):
    df = DB
    for k, v in kw.items():
        df = df.filter(pl.col(k) == v)
    return df

# a real rooted card (no_condition so the effect always fires)
rooted_ids = q(effect='rooted', condition='no_condition')['card_id'].to_list()
assert len(rooted_ids) >= 1, 'need a rooted/no_condition card'
rooted1 = rooted_ids[0]

# a second rooted card (may be the same id if only one exists; use a distinct one if available)
rooted2 = rooted_ids[1] if len(rooted_ids) >= 2 else rooted_ids[0]

# a neutral advancing card for the opponent
adv1 = q(effect='rooted', condition='no_condition', advancing=1)['card_id'].to_list()
adv1 = adv1[0] if adv1 else q(effect='advancing', condition='no_condition')['card_id'].to_list()[0]

filler = [c for c in q(faction='Miaous')['card_id'].to_list() if c not in {rooted1, rooted2, adv1}][:14]
assert len(filler) == 14, f'need 14 Miaous filler cards, got {len(filler)}'

def new_game(a_cards, b_cards):
    p1 = PlayerState(name='A', deck=list(a_cards) + filler)
    p2 = PlayerState(name='B', deck=list(b_cards) + filler)
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    return gs

def set_biomes(gs, biome='OC'):
    # OC (ocean): NOT a home biome for Miaous -> the faction +1 bonus never applies
    for cell in gs.earth:
        cell[0] = biome

def force_hand(gs, name, cards):
    p = gs.players[name]
    for c in cards:
        if c not in p.hand:
            if c in p.deck:
                p.deck.remove(c)
            p.hand.append(c)

def play(gs, name, first_second, card, mode='move', to='stopover_4'):
    p = gs.players[name]
    mana = int(DB.filter(pl.col('card_id') == card)['mana'][0])
    p.mana = [filler[0]] * mana
    p.mana_spend = 0
    p.message = {'cards': [card], 'to': to, 'mode': mode, 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, ok, msg = ge.player_play(first_second, p, gs)
    assert ok, f'play rejected: {msg}'
    return gs

def all_card_zones(gs):
    """ return a flat list of every card_id across hand/deck/discard/mana (both
        players) + rooted_on_board, to check card conservation """
    zones = []
    for p in gs.players.values():
        zones.extend(p.hand or [])
        zones.extend(p.deck or [])
        zones.extend(p.discard or [])
        zones.extend(p.mana or [])
    for e in (gs.rooted_on_board or []):
        zones.append(e['card_id'])
    for e in (gs.rooted_this_turn or []):
        zones.append(e['card_id'])
    return zones

def count_card(gs, card_id):
    return all_card_zones(gs).count(card_id)

ok = 0

# ============================================================
# 1) TOKEN GRANT: rooted effect fires -> card is in rooted_this_turn (not discarded)
# ============================================================
gs = new_game([rooted1], [adv1])
set_biomes(gs, 'OC')
force_hand(gs, 'A', [rooted1]); force_hand(gs, 'B', [adv1])
gs = play(gs, 'A', 'first', rooted1)
gs = play(gs, 'B', 'second', adv1)
gs = ge.process_trip_chain(gs)
# the rooted card should be in rooted_this_turn (it earned a token)
assert any(e['card_id'] == rooted1 for e in gs.rooted_this_turn), \
    f"rooted card {rooted1} should be in rooted_this_turn, got {gs.rooted_this_turn}"
print(f"1) TOKEN GRANT: rooted card {rooted1} is in rooted_this_turn -> PASS")
ok += 1

# ============================================================
# 2) END-OF-TURN PLACEMENT: card moves from discard to rooted_on_board (free stopover)
# ============================================================
# call the engine's end-of-turn settlement (normally called by handle_websocket_message)
gs = ge._process_rooted_cards(gs)
# the card should now be in rooted_on_board (on a stopover), NOT in rooted_this_turn
assert any(e['card_id'] == rooted1 for e in gs.rooted_on_board), \
    f"rooted card {rooted1} should be in rooted_on_board, got {gs.rooted_on_board}"
assert not any(e['card_id'] == rooted1 for e in gs.rooted_this_turn), \
    f"rooted card {rooted1} should NOT be in rooted_this_turn after placement"
# the card should be on stopover_4 (first free stopover)
entry = next(e for e in gs.rooted_on_board if e['card_id'] == rooted1)
assert entry['stopover'] == 'stopover_4', f"expected stopover_4, got {entry['stopover']}"
# card conservation: the card should be in EXACTLY ONE zone (rooted_on_board)
count = count_card(gs, rooted1)
assert count == 1, f"card conservation: expected 1, got {count} (rooted_on_board={gs.rooted_on_board}, discard={gs.players['A'].discard})"
print(f"2) END-OF-TURN: card on {entry['stopover']}, count={count} -> PASS")
ok += 1

# ============================================================
# 3) SURVIVAL + DISCARD: next end-of-turn discards the card (one-shot)
# ============================================================
# simulate the next turn: the card is still on the board (rooted_on_board)
assert any(e['card_id'] == rooted1 for e in gs.rooted_on_board), "card should still be on the board"
# now process the rooted cards again (end of the NEXT turn) -> the card is discarded
gs = ge._process_rooted_cards(gs)
# the card should be DISCARDED now (removed from rooted_on_board, added to discard)
assert not any(e['card_id'] == rooted1 for e in gs.rooted_on_board), \
    f"card {rooted1} should be discarded (not in rooted_on_board), got {gs.rooted_on_board}"
assert rooted1 in (gs.players['A'].discard or []), \
    f"card {rooted1} should be in A's discard, got {gs.players['A'].discard}"
print(f"3) SURVIVAL+DISCARD: card discarded after one extra turn -> PASS")
ok += 1

# ============================================================
# 4) COOLDOWN: a card that earned a token the PREVIOUS turn cannot earn another
# ============================================================
# set up: the card earned a token on turn T (rooted_history[rooted1] = T)
gs2 = new_game([rooted1, rooted1], [adv1])  # two copies of the same rooted card
set_biomes(gs2, 'OC')
gs2.turn = 5  # simulate turn 5
gs2.rooted_history[rooted1] = 4  # the card earned a token on turn 4 (previous turn)
# now try to grant a token on turn 5 (should be blocked by cooldown)
gs2 = ge._grant_rooted_token(rooted1, gs2.players['A'], gs2)
# the card should NOT be in rooted_this_turn (cooldown blocked it)
assert not any(e['card_id'] == rooted1 for e in gs2.rooted_this_turn), \
    f"card {rooted1} should NOT be in rooted_this_turn (cooldown), got {gs2.rooted_this_turn}"
print(f"4) COOLDOWN: card on cooldown (rooted last turn) -> no token -> PASS")
ok += 1

# 4b) cooldown EXPIRES: a card rooted 2+ turns ago CAN earn a token again
gs3 = new_game([rooted1], [adv1])
set_biomes(gs3, 'OC')
gs3.turn = 6  # simulate turn 6
gs3.rooted_history[rooted1] = 4  # the card earned a token on turn 4 (2 turns ago)
gs3 = ge._grant_rooted_token(rooted1, gs3.players['A'], gs3)
# the card SHOULD be in rooted_this_turn (cooldown expired)
assert any(e['card_id'] == rooted1 for e in gs3.rooted_this_turn), \
    f"card {rooted1} should be in rooted_this_turn (cooldown expired), got {gs3.rooted_this_turn}"
print(f"4b) COOLDOWN EXPIRES: card rooted 2 turns ago can earn a token again -> PASS")
ok += 1

# ============================================================
# 5) NO STACKING: multiple rooted cards get distinct stopovers (play order)
# ============================================================
gs4 = new_game([rooted1, rooted2], [adv1])
set_biomes(gs4, 'OC')
force_hand(gs4, 'A', [rooted1, rooted2]); force_hand(gs4, 'B', [adv1])
gs4 = play(gs4, 'A', 'first', rooted1, to='stopover_4')
gs4 = play(gs4, 'A', 'first', rooted2, to='stopover_3')  # second card, next slot
gs4 = play(gs4, 'B', 'second', adv1)
gs4 = ge.process_trip_chain(gs4)
# both cards should be in rooted_this_turn
ids_this_turn = [e['card_id'] for e in gs4.rooted_this_turn]
assert rooted1 in ids_this_turn, f"rooted1 should be in rooted_this_turn, got {ids_this_turn}"
if rooted2 != rooted1:
    assert rooted2 in ids_this_turn, f"rooted2 should be in rooted_this_turn, got {ids_this_turn}"
# place them
gs4 = ge._process_rooted_cards(gs4)
# the cards should be on DISTINCT stopovers (no stacking)
stopovers = [e['stopover'] for e in gs4.rooted_on_board]
assert len(set(stopovers)) == len(stopovers), f"stopovers should be distinct, got {stopovers}"
# the first rooted card should be on stopover_4, the second on stopover_3
assert stopovers[0] == 'stopover_4', f"first card should be on stopover_4, got {stopovers}"
if rooted2 != rooted1:
    assert stopovers[1] == 'stopover_3', f"second card should be on stopover_3, got {stopovers}"
print(f"5) NO STACKING: cards on distinct stopovers {stopovers} -> PASS")
ok += 1

# ============================================================
# 6) OLD GAME (engine_version < 10): rooted is a NO-OP (no token, card discarded)
# ============================================================
gs5 = new_game([rooted1], [adv1])
set_biomes(gs5, 'OC')
gs5.engine_version = 9  # old game: rooted is a no-op
force_hand(gs5, 'A', [rooted1]); force_hand(gs5, 'B', [adv1])
gs5 = play(gs5, 'A', 'first', rooted1)
gs5 = play(gs5, 'B', 'second', adv1)
gs5 = ge.process_trip_chain(gs5)
# the card should NOT be in rooted_this_turn (old game: no token)
assert not any(e['card_id'] == rooted1 for e in gs5.rooted_this_turn), \
    f"old game: card {rooted1} should NOT be in rooted_this_turn, got {gs5.rooted_this_turn}"
# the card should be in the discard (discarded as usual)
assert rooted1 in (gs5.players['A'].discard or []), \
    f"old game: card {rooted1} should be in A's discard, got {gs5.players['A'].discard}"
print(f"6) OLD GAME (v9): rooted is a no-op, card discarded -> PASS")
ok += 1

# ============================================================
# 7) BLOCKED rooted card: no token (the effect does not fire)
# ============================================================
gs6 = new_game([rooted1], [adv1])
set_biomes(gs6, 'OC')
force_hand(gs6, 'A', [rooted1]); force_hand(gs6, 'B', [adv1])
# B defends with a high-shield card to block A's rooted card
# find a defend card with high shield
defend_card = q(shield=6, condition='no_condition')['card_id'].to_list()
defend_card = defend_card[0] if defend_card else q(shield=4, condition='no_condition')['card_id'].to_list()[0]
force_hand(gs6, 'B', [defend_card])
gs6 = play(gs6, 'A', 'first', rooted1, to='stopover_4')
gs6 = play(gs6, 'B', 'second', defend_card, mode='defend', to='stopover_4')
gs6 = ge.process_trip_chain(gs6)
# the rooted card was blocked -> no token
assert not any(e['card_id'] == rooted1 for e in gs6.rooted_this_turn), \
    f"blocked rooted card {rooted1} should NOT be in rooted_this_turn, got {gs6.rooted_this_turn}"
print(f"7) BLOCKED: rooted card blocked -> no token -> PASS")
ok += 1

# ============================================================
# 8) CARD CONSERVATION: full game, every card is in exactly one zone
#    (hand + deck + discard + rooted_on_board + rooted_this_turn; the mana zone
#     is excluded because the test harness stuffs filler cards into it to pay
#     costs, which is a test artifact, not part of the conservation invariant)
# ============================================================
def conservation_count(g):
    t = sum(len(g.players[n].deck or []) + len(g.players[n].hand or [])
            + len(g.players[n].discard or []) for n in ('A', 'B'))
    t += len(g.rooted_on_board or []) + len(g.rooted_this_turn or [])
    return t

gs7 = new_game([rooted1, rooted2], [adv1])
set_biomes(gs7, 'OC')
force_hand(gs7, 'A', [rooted1, rooted2]); force_hand(gs7, 'B', [adv1])
total_before = conservation_count(gs7)
gs7 = play(gs7, 'A', 'first', rooted1)
gs7 = play(gs7, 'B', 'second', adv1)
gs7 = ge.process_trip_chain(gs7)
gs7 = ge._process_rooted_cards(gs7)
total_after = conservation_count(gs7)
assert total_before == total_after, f"card conservation: before={total_before}, after={total_after}"
# also verify the rooted card is NOT in the discard (it was pulled out)
assert rooted1 not in (gs7.players['A'].discard or []), \
    f"rooted card {rooted1} should NOT be in A's discard, got {gs7.players['A'].discard}"
print(f"8) CARD CONSERVATION: {total_before} cards before, {total_after} after -> PASS")
ok += 1

# ============================================================
# 9) LEGIT full game with a rooted card -> persisted as a finished game so the
#    REPLAY self-test (games.analysis.replay) verifies it end-to-end: the replay
#    must (a) grant the token via the real engine, (b) settle it onto a stopover
#    (ge._process_rooted_cards), and (c) keep card conservation + positions.
#    A plays rooted1 (adv -1: 0 -> clamped 0), B plays adv1 (0 -> 1).
# ============================================================
gs8 = new_game([rooted1], [adv1])
set_biomes(gs8, 'OC')
force_hand(gs8, 'A', [rooted1]); force_hand(gs8, 'B', [adv1])
gs8 = play(gs8, 'A', 'first', rooted1)
gs8 = play(gs8, 'B', 'second', adv1)
gs8 = ge.process_trip_chain(gs8)
gs8 = ge._process_rooted_cards(gs8)
a_pos = gs8.players['A'].current_position
b_pos = gs8.players['B'].current_position
# rooted1 is adv -1 (a recoil): A stays at 0; B is adv 1 -> 1
assert a_pos == 0 and b_pos == 1, (a_pos, b_pos)
# the rooted card must be on the board (in rooted_on_board), not lost from every zone
assert any(e['card_id'] == rooted1 for e in gs8.rooted_on_board), \
    f"rooted card {rooted1} should be on the board, got {gs8.rooted_on_board}"
# the rooted card must NOT be in the discard (it was pulled out)
assert rooted1 not in (gs8.players['A'].discard or []), \
    f"rooted card {rooted1} should NOT be in A's discard, got {gs8.players['A'].discard}"
gs8.state = "game over"   # terminal state (test artifact) -> replay self-test picks it up
gs8.winner = None
conn = sqlite3.connect(ge.DB_PATH)
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs8.to_json(), gs8.id))
conn.commit()
conn.close()
print(f"9) LEGIT game with rooted card persisted as {gs8.id}: A at {a_pos}, B at {b_pos}, "
      f"rooted_on_board={gs8.rooted_on_board} -> PASS (replay self-test will verify)")
ok += 1

print(f"\nALL {ok} rooted TESTS PASSED")
