# Manual smoke test: pet_trap (rule of engine_version 8)
#  - INSTANT effect: the drop token is placed at PLAY TIME (player_play hook)
#  - drop token trigger: a player token ARRIVING on the cell -> knockback -1 per token
#  - jump: landing cell checked, intermediate cells skipped
#  - stacking, clamping at cell 0, defend mode, engine_version pinning, blocked card
# Run from the project root:  uv run python tests/_pet_trap.py
# NOTE: writes to games.db (like _diag.py)
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import sqlite3
import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

def q(**kw):
    df = DB
    for k, v in kw.items():
        df = df.filter(pl.col(k) == v)
    return df

# card picks (real pool cards, all no_condition so conditions never interfere)
pt_adv0 = q(effect='pet_trap', condition='no_condition', advancing=0)['card_id'].to_list()[0]
pt_adv1 = q(effect='pet_trap', condition='no_condition', advancing=1)['card_id'].to_list()[0]
adv1    = q(effect='rooted', advancing=1, condition='no_condition')['card_id'].to_list()[0]   # rooted = no-op effect: plain advancing only
adv2    = q(effect='rooted', advancing=2, condition='no_condition')['card_id'].to_list()[0]
jump3   = q(effect='jump', condition='no_condition', advancing=3)['card_id'].to_list()[0]
neg1    = q(effect='rooted', condition='no_condition', advancing=-1)['card_id'].to_list()[0]
defend1 = q(condition='no_condition').filter(pl.col('shield') >= 1)['card_id'].to_list()[0]
assert {pt_adv0, pt_adv1, adv1, jump3, neg1, defend1}, 'need real pool cards'

# filler: single-faction (Miaous) so both players' faction is Miaous (home: DE/JU);
# the board is set to all-OC below -> the faction biome +1 bonus can never apply,
# keeping every position assertion exact.
specials = {pt_adv0, pt_adv1, adv1, adv2, jump3, neg1, defend1}
filler = [c for c in q(faction='Miaous')['card_id'].to_list() if c not in specials][:13]
assert len(filler) == 13, 'need 13 Miaous filler cards'

def new_game(a_specials, b_specials):
    p1 = PlayerState(name='A', deck=list(a_specials) + filler)
    p2 = PlayerState(name='B', deck=list(b_specials) + filler)
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    return gs

def set_biomes(gs, biome='OC'):
    for cell in gs.earth:
        cell[0] = biome

def move_to(gs, name, pos):
    p = gs.players[name]
    old = p.current_position or 0
    if old == pos:
        return
    gs.earth[old].remove(name)
    p.current_position = pos
    gs.earth[pos].append(name)

def force_hand(gs, name, specials):
    """ make sure the given cards are in the player's hand (deterministic plays;
        a card played on an earlier turn is brought back from the deck, as a
        re-draw would in a real game) """
    p = gs.players[name]
    for c in specials:
        if c not in p.hand:
            if c in p.deck:
                p.deck.remove(c)
            p.hand.append(c)

def play(gs, name, first_second, card, mode='move', mana=None):
    """ play one card through the REAL player_play (costs mana, appends to the chain) """
    p = gs.players[name]
    if mana is None:
        mana = int(DB.filter(pl.col('card_id') == card)['mana'][0])   # pay the real cost
    p.mana = [filler[0]] * mana
    p.mana_spend = 0
    p.message = {'cards': [card], 'to': 'stopover_4', 'mode': mode, 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, ok, msg = ge.player_play(first_second, p, gs)
    assert ok, f'play rejected: {msg}'
    return gs

def reset_turn(gs):
    """ engine's start-of-next-turn bookkeeping (clear the resolved chains) """
    for p in gs.players.values():
        p.action_chain = []
        p.mana_spend = 0

def play_and_chain(gs):
    """ both players passed -> run the trip chain (cards stay in hand in this harness) """
    return ge.process_trip_chain(gs)

ok = 0

# 1) INSTANT placement + basic trigger: token on the player's cell (at play time),
#    opponent ARRIVES on the cell -> -1 knockback, token consumed
gs = new_game([pt_adv0], [adv1])
set_biomes(gs, 'OC')
move_to(gs, 'A', 2); move_to(gs, 'B', 1)
force_hand(gs, 'A', [pt_adv0]); force_hand(gs, 'B', [adv1])
gs = play(gs, 'A', 'first', pt_adv0)
print(f"1a) after play: drop_tokens={gs.drop_tokens} A at {gs.players['A'].current_position}")
assert gs.drop_tokens == {2: 1}, gs.drop_tokens          # token placed at PLAY TIME
assert gs.players['A'].current_position == 2            # ...but no trigger (A already on the cell)
assert gs.players['A'].message.get('drop_placed_on') == [2]   # engine annotation for the log
gs = play(gs, 'B', 'second', adv1)
gs = play_and_chain(gs)
a_pos = gs.players['A'].current_position; b_pos = gs.players['B'].current_position
print(f"1b) after chain: A at {a_pos} (expected 2)  B at {b_pos} (expected 1)  tokens={gs.drop_tokens}")
assert a_pos == 2 and b_pos == 1, (a_pos, b_pos)        # B: 1 -> 2 (drop) -> 1
assert gs.drop_tokens == {}, gs.drop_tokens             # token consumed by the trigger
ok += 1

# 2) JUMP OVER: intermediate cells are skipped -> no trigger
gs = new_game([pt_adv0], [jump3])
set_biomes(gs, 'OC')
move_to(gs, 'A', 2); move_to(gs, 'B', 1)
force_hand(gs, 'A', [pt_adv0]); force_hand(gs, 'B', [jump3])
gs = play(gs, 'A', 'first', pt_adv0)
gs = play(gs, 'B', 'second', jump3)
gs = play_and_chain(gs)
b_pos = gs.players['B'].current_position
print(f"2) jump over cell 2: B at {b_pos} (expected 4)  tokens={gs.drop_tokens} (expected {{2: 1}})")
assert b_pos == 4, b_pos                                 # 1 + jump 3, landing cell 4 has no drop
assert gs.drop_tokens == {2: 1}, gs.drop_tokens          # drop on cell 2 untouched
ok += 1

# 3) JUMP LANDING: the landing cell IS checked -> trigger
gs = new_game([pt_adv0], [jump3])
set_biomes(gs, 'OC')
move_to(gs, 'A', 7); move_to(gs, 'B', 4)
force_hand(gs, 'A', [pt_adv0]); force_hand(gs, 'B', [jump3])
gs = play(gs, 'A', 'first', pt_adv0)
gs = play(gs, 'B', 'second', jump3)
gs = play_and_chain(gs)
b_pos = gs.players['B'].current_position
print(f"3) jump landing on cell 7: B at {b_pos} (expected 6)  tokens={gs.drop_tokens}")
assert b_pos == 6, b_pos                                 # 4 + jump 3 = 7 (drop) -> 6
assert gs.drop_tokens == {}, gs.drop_tokens
ok += 1

# 4) STACKING + both already on the cell: two tokens, no trigger while standing on it,
#    then one player leaves and returns -> -2 knockback
gs = new_game([pt_adv0, adv1, neg1], [pt_adv0])
set_biomes(gs, 'OC')
move_to(gs, 'A', 2); move_to(gs, 'B', 2)                 # BOTH players on cell 2
force_hand(gs, 'A', [pt_adv0, adv1, neg1]); force_hand(gs, 'B', [pt_adv0])
gs = play(gs, 'A', 'first', pt_adv0)
gs = play(gs, 'B', 'second', pt_adv0)
gs = play_and_chain(gs)
print(f"4a) both on cell 2: tokens={gs.drop_tokens} (expected {{2: 2}})  A at {gs.players['A'].current_position} B at {gs.players['B'].current_position}")
assert gs.drop_tokens == {2: 2}, gs.drop_tokens          # stacked, no trigger (already there)
assert gs.players['A'].current_position == 2 and gs.players['B'].current_position == 2
# turn 2: A leaves (2->3) and returns (3->2) -> the two drops fire (-2), B never leaves
reset_turn(gs)
force_hand(gs, 'A', [adv1, neg1])
gs = play(gs, 'A', 'first', adv1)
gs = play_and_chain(gs)                                  # A: 2->3, B: passed -> no drops yet
assert gs.players['A'].current_position == 3 and gs.drop_tokens == {2: 2}, (gs.players['A'].current_position, gs.drop_tokens)
reset_turn(gs)
gs = play(gs, 'A', 'second', neg1)                        # A returns: 3->2 (drop x2) -> 0
gs = play_and_chain(gs)
a_pos = gs.players['A'].current_position
print(f"4b) A returns to cell 2: A at {a_pos} (expected 0)  tokens={gs.drop_tokens}")
assert a_pos == 0, a_pos
assert gs.drop_tokens == {}, gs.drop_tokens
ok += 1

# 5) CLAMP at cell 0: drop on cell 0, arrival triggers it but the knockback clamps
gs = new_game([pt_adv0], [neg1])
set_biomes(gs, 'OC')
move_to(gs, 'A', 0); move_to(gs, 'B', 1)
force_hand(gs, 'A', [pt_adv0]); force_hand(gs, 'B', [neg1])
gs = play(gs, 'A', 'first', pt_adv0)
gs = play(gs, 'B', 'second', neg1)
gs = play_and_chain(gs)
b_pos = gs.players['B'].current_position
print(f"5) clamp at 0: B at {b_pos} (expected 0)  tokens={gs.drop_tokens}")
assert b_pos == 0, b_pos                                 # 1 -> 0 (drop) -> max(0, -1) = 0
assert gs.drop_tokens == {}, gs.drop_tokens
ok += 1

# 6) engine_version pinning: v7 game -> the INSTANT hook is off, no token at all
gs = new_game([pt_adv0], [adv1])
gs.engine_version = 7
set_biomes(gs, 'OC')
move_to(gs, 'A', 2); move_to(gs, 'B', 1)
force_hand(gs, 'A', [pt_adv0]); force_hand(gs, 'B', [adv1])
gs = play(gs, 'A', 'first', pt_adv0)
print(f"6a) v7 play: tokens={gs.drop_tokens} (expected empty)  annotation={gs.players['A'].message.get('drop_placed_on')}")
assert gs.drop_tokens == {}, gs.drop_tokens
assert 'drop_placed_on' not in gs.players['A'].message
gs = play(gs, 'B', 'second', adv1)
gs = play_and_chain(gs)
b_pos = gs.players['B'].current_position
print(f"6b) v7 chain: B at {b_pos} (expected 2 - no drop to trigger)")
assert b_pos == 2, b_pos
ok += 1

# 7) DEFEND mode: a pet_trap played sideways plays no effect -> no token
gs = new_game([pt_adv0], [])
set_biomes(gs, 'OC')
move_to(gs, 'A', 2)
force_hand(gs, 'A', [pt_adv0])
gs = play(gs, 'A', 'first', pt_adv0, mode='defend')
print(f"7) defend play: tokens={gs.drop_tokens} (expected empty)  annotation={gs.players['A'].message.get('drop_placed_on')}")
assert gs.drop_tokens == {}, gs.drop_tokens
assert 'drop_placed_on' not in gs.players['A'].message
ok += 1

# 8) BLOCKED pet_trap: the token was placed at play time -> it STAYS (instant = un-blockable)
gs = new_game([pt_adv1], [defend1])
set_biomes(gs, 'OC')
move_to(gs, 'A', 2)
force_hand(gs, 'A', [pt_adv1]); force_hand(gs, 'B', [defend1])
gs = play(gs, 'A', 'first', pt_adv1)                     # token on cell 2 (instant)
gs = play(gs, 'B', 'second', defend1, mode='defend')
gs = play_and_chain(gs)
a_pos = gs.players['A'].current_position
print(f"8) blocked pet_trap: A at {a_pos} (expected 2 - blocked)  tokens={gs.drop_tokens} (expected {{2: 1}})")
assert a_pos == 2, a_pos                                 # blocked: no advancing
assert gs.drop_tokens == {2: 1}, gs.drop_tokens          # but the trap is still on the board
ok += 1

# 9) LOG: placement note on the card's line, trigger note on the steppers' line
gs = new_game([pt_adv0], [adv1])
set_biomes(gs, 'OC')
move_to(gs, 'A', 2); move_to(gs, 'B', 1)
force_hand(gs, 'A', [pt_adv0]); force_hand(gs, 'B', [adv1])
gs = play(gs, 'A', 'first', pt_adv0)
gs = play(gs, 'B', 'second', adv1)
a, b = gs.players['A'], gs.players['B']
entry_a = ge.new_log_entry(a, a.action_chain[0], 1)
entry_b = ge.new_log_entry(b, b.action_chain[0], 2)
gs, *_ = ge.process_card(a.action_chain[0], a, gs, log_entry=entry_a, oppo_entry=entry_b)
gs, *_ = ge.process_card(b.action_chain[0], b, gs, log_entry=entry_b, oppo_entry=entry_a)
print(f"9) log: A notes={entry_a['notes']}")
print(f"      B notes={entry_b['notes']}")
# placement note: play-time instant effect -> the turn's 'instant' section (gs.log)
inst = [str(it.get('what')) for t in gs.log for it in (t.get('instant') or [])]
assert any('pet_trap' in w and 'placed' in w for w in inst), inst
assert any('drop' in n and 'knocked back' in n for n in entry_b['notes']), entry_b['notes']
ok += 1

# 10) LEGIT full game (no teleports - every position derived from plays, from cell 0)
#     -> persisted as a finished game so the REPLAY self-test verifies it
#     (A drops the trap on cell 0 where it stands, B leaves 0->1 and returns 1->0,
#      stepping on the drop -> knocked back, clamped at 0)
gs = new_game([pt_adv0], [adv1, neg1])
set_biomes(gs, 'OC')
force_hand(gs, 'A', [pt_adv0]); force_hand(gs, 'B', [adv1, neg1])
gs = play(gs, 'A', 'first', pt_adv0)
gs = play(gs, 'B', 'second', adv1)
gs = play(gs, 'B', 'second', neg1)
gs = play_and_chain(gs)
a_pos = gs.players['A'].current_position; b_pos = gs.players['B'].current_position
assert a_pos == 0 and b_pos == 0, (a_pos, b_pos)
assert gs.drop_tokens == {}, gs.drop_tokens
gs.state = "game over"          # terminal state (test artifact) -> replay self-test picks it up
gs.winner = None
conn = sqlite3.connect(ge.DB_PATH)
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gs.id))
conn.commit()
conn.close()
print(f"10) legit game persisted as {gs.id}: A at {a_pos}, B at {b_pos}, tokens={gs.drop_tokens}")
ok += 1

# 11) 2-turn LEGIT game - the DISCRIMINATING replay case: the drop lands on a cell
#     the opponent reaches only on turn 2; if the replay mirror misses the placement,
#     B would end at 2 instead of 1 -> position divergence -> FAIL.
#     t1: A 0->2 (adv2), B 0->1 (adv1)
#     t2: A plays pet_trap (drop on A's cell = 2), B 1->2 ARRIVES -> drop -> 1
gs = new_game([adv2, pt_adv0], [adv1])
set_biomes(gs, 'OC')
force_hand(gs, 'A', [adv2, pt_adv0]); force_hand(gs, 'B', [adv1])
gs = play(gs, 'A', 'first', adv2)
gs = play(gs, 'B', 'second', adv1)
gs = play_and_chain(gs)
assert gs.players['A'].current_position == 2 and gs.players['B'].current_position == 1
# turn-2 setup (engine order: flip turn order; mana phase messages delimit the turns in the history)
gs.turn += 1
gs.turn_order = ['B', 'A']
reset_turn(gs)
for n in ('A', 'B'):
    p = gs.players[n]
    p.messages_history.append({'cards': [], 'to': 'mana', 'mode': 'pass', 'pendings': []})
force_hand(gs, 'B', [adv1])                   # B's turn-1 card comes back (re-draw)
gs = play(gs, 'A', 'first', pt_adv0)          # drop on A's cell (2) at play time
gs = play(gs, 'B', 'second', adv1)            # 1 -> 2 (drop) -> 1
gs = play_and_chain(gs)
a_pos = gs.players['A'].current_position; b_pos = gs.players['B'].current_position
assert a_pos == 2 and b_pos == 1, (a_pos, b_pos)
assert gs.drop_tokens == {}, gs.drop_tokens
gs.state = "game over"; gs.winner = None
conn = sqlite3.connect(ge.DB_PATH)
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gs.id))
conn.commit()
conn.close()
print(f"11) 2-turn legit game persisted as {gs.id}: A at {a_pos}, B at {b_pos}, tokens={gs.drop_tokens}")
ok += 1

print(f"\n{ok}/11 pet_trap tests passed")
