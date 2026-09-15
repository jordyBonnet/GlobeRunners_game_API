# Manual smoke test: Engineers (support faction, rule of engine_version 12)
#  - 4 DROP cards (boost / trampoline / gluetrap / landmine):
#      * played in MOVE mode with a target cell (message 'cell', 0..23)
#      * INSTANT at play time: a token is placed on that cell (board_drops)
#      * the FIRST token arriving on the cell fires it, then it is consumed
#      * boost +2 (stepped) / trampoline +2 (jump) / gluetrap -1 (knockback)
#      * landmine: the arriving player is BLOCKED for the rest of the turn
#        (move cards canceled - no effect, no advancing - except unstoppable
#        cards with their condition met; cleared in the cleaning phase)
#      * placing on an occupied cell does NOT fire
#  - 1 DWELLING card (refinery):
#      * placed from the hand in the dedicated zone (1 at a time, cost 3)
#      * tapped once per turn (free) -> draw 1 card; untapped in cleaning
#      * removed by wrecking_ball
#      * placeholder slot (dwelling_slot): set at placement, cleared in the
#        cleaning phase so the placeholder only shows during the placement turn
#  - support cards cost their mana_cost when played (v12)
#  - drop_on_board condition counts engineer drops
#  - old games (engine_version < 12): drops are no-ops, dwelling actions rejected
# Run from the project root:  uv run python tests/_engineers.py
# NOTE: writes to games.db (like _diag.py / _rooted.py)
import sys, io, os, sqlite3, random
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB

def q(**kw):
    df = DB
    for k, v in kw.items():
        df = df.filter(pl.col(k) == v)
    return df

# a neutral advancing card for the opponent (so they can play something)
adv1 = q(effect='advancing', condition='no_condition', mana=1)['card_id'].to_list()
adv1 = adv1[0] if adv1 else q(effect='advancing', condition='no_condition')['card_id'].to_list()[0]

# an unstoppable card (condition met: no_condition) - the landmine exception.
# Pick a faction whose home biomes do NOT include OC (all test boards are OC)
# so the faction biome +1 bonus can't skew the expected position.
un1 = None
for fac in ('Miaous', 'Orcs', 'Mummies'):
    ids = q(effect='unstoppable', condition='no_condition', faction=fac)['card_id'].to_list()
    if ids:
        un1 = ids[0]
        break
assert un1 is not None, 'need an unstoppable/no_condition card (Miaous/Orcs/Mummies)'

# a real wrecking_ball card (to remove the refinery dwelling)
wb_ids = q(effect='wrecking_ball', condition='no_condition')['card_id'].to_list()
assert len(wb_ids) >= 1, 'need a wrecking_ball/no_condition card'
wb1 = wb_ids[0]

# filler cards (any faction) to pad the decks / pay mana
filler = [c for c in q(faction='Miaous')['card_id'].to_list() if c not in {adv1, un1, wb1}][:14]
assert len(filler) == 14, f'need 14 Miaous filler cards, got {len(filler)}'

ok = 0

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()

def new_game(a_cards, b_cards, version=None, keep=False):
    p1 = PlayerState(name='A', deck=list(a_cards) + filler)
    p2 = PlayerState(name='B', deck=list(b_cards) + filler)
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    if version is not None:
        gs.engine_version = version
    if not keep:
        _delete(gid)   # don't pollute the db with mid-test states
    return gs, gid

def set_biomes(gs, biome='OC'):
    # OC (ocean): NOT a home biome for Miaous/Orcs/Mummies -> no +1 bonus
    for cell in gs.earth:
        cell[0] = biome

def force_hand(gs, name, cards):
    p = gs.players[name]
    for c in cards:
        if c not in p.hand:
            if c in p.deck:
                p.deck.remove(c)
            p.hand.append(c)

def put_at(gs, name, pos):
    """teleport a player token to a cell (test helper - direct state set)"""
    p = gs.players[name]
    for cell in gs.earth:
        if p.name in cell:
            cell.remove(p.name)
    p.current_position = pos
    gs.earth[pos].append(p.name)

def ensure_mana(gs, name, n):
    """top up the mana zone from the deck (no card duplication)"""
    p = gs.players[name]
    while len(p.mana) - (p.mana_spend or 0) < n and p.deck:
        p.mana.append(p.deck.pop(0))

def play(gs, name, first_second, card, mode='move', to='stopover_4', cell=None):
    p = gs.players[name]
    cost = max(ge._play_cost(gs, card), 1)
    ensure_mana(gs, name, cost)
    p.mana_spend = 0
    msg = {'cards': [card], 'to': to, 'mode': mode, 'pendings': []}
    if cell is not None:
        msg['cell'] = cell
    p.message = msg
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    assert okk, f'play rejected: {msgtxt}'
    return gs

def pas(game, name, first_second):
    p = game.players[name]
    p.message = {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []}
    game.state = f"turn {game.turn} - waiting for {first_second} player ({game.turn_order[0]}) to play"
    p, game, okk, msgtxt = ge.player_play(first_second, p, game)
    assert okk, f'pass rejected: {msgtxt}'
    return game

def dwelling(gs, name, first_second, cards=None, mode=''):
    p = gs.players[name]
    ensure_mana(gs, name, 3)
    p.mana_spend = 0
    p.message = {'cards': cards or [], 'to': 'dwelling', 'mode': mode, 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    return gs, okk, msgtxt

def next_turn(gs):
    """mirror the WS handler's end-of-turn block (order flip, resets, draw 3)"""
    gs.turn_order = gs.turn_order[::-1]
    gs.first_player_passed = False
    gs.second_player_passed = False
    gs.turn += 1
    gs.day_night = 'night' if gs.day_night == 'day' else 'day'
    for p in gs.players.values():
        p.mana_spend = 0
        p.action_chain = []
        p.landmine_blocked = False
        p.dwelling_tapped = False
        p.dwelling_slot = None   # placeholder slot cleared in the cleaning phase (mirrors _end_turn)
        draw_n = min(ge.turn_n_draw_cards, len(p.deck))
        new_cards = p.deck[:draw_n]
        p.hand.extend(new_cards)
        for c in new_cards:
            p.deck.remove(c)
        if draw_n < ge.turn_n_draw_cards:
            p.deck.extend(p.discard)
            random.shuffle(p.deck)
            p.discard.clear()
            remaining = ge.turn_n_draw_cards - draw_n
            draw_n2 = min(remaining, len(p.deck))
            new_cards2 = p.deck[:draw_n2]
            p.hand.extend(new_cards2)
            for c in new_cards2:
                p.deck.remove(c)
    return gs

def all_zones(gs):
    zones = []
    for p in gs.players.values():
        zones.extend(p.hand or [])
        zones.extend(p.deck or [])
        zones.extend(p.discard or [])
        zones.extend(p.mana or [])
        if p.dwelling:
            zones.append(p.dwelling)
    return zones

# ============================================================
# 1) COST: support cards cost their mana_cost when played (v12)
# ============================================================
gs, _ = new_game([], [])
assert ge._play_cost(gs, 'boost') == 1, "boost should cost 1"
assert ge._play_cost(gs, 'landmine') == 3, "landmine should cost 3"
assert ge._play_cost(gs, 'refinery') == 3, "refinery should cost 3"
assert ge._play_cost(gs, adv1) >= 1, "main card should cost its pool mana"
gs_old, _ = new_game([], [], version=11)
assert ge._play_cost(gs_old, 'boost') == 0, "v11: support cards cost 0 (old behavior)"
print("1) COST: support cards pay their mana_cost (v12), 0 (v<12) -> PASS")
ok += 1

# ============================================================
# 2) DROP PLACEMENT: boost played with a cell -> token on board_drops
# ============================================================
gs, _ = new_game([], [])
set_biomes(gs, 'OC')
force_hand(gs, 'A', ['boost', adv1]); force_hand(gs, 'B', [adv1])
gs = play(gs, 'A', 'first', 'boost', cell=3)
assert gs.board_drops == [{'cell': 3, 'kind': 'boost', 'owner': 'A'}], \
    f"boost token should be on cell 3, got {gs.board_drops}"
action = gs.players['A'].action_chain[-1]
assert action.get('drop_cell') == 3 and action.get('drop_kind') == 'boost', \
    f"action should annotate the drop, got {action.get('drop_cell')}/{action.get('drop_kind')}"
# the play cost 1 mana
assert gs.players['A'].mana_spend == 1, f"boost should cost 1 mana, spent {gs.players['A'].mana_spend}"
print("2) DROP PLACEMENT: boost token placed on cell 3 (cost 1) -> PASS")
ok += 1

# ============================================================
# 2b) DROP PLACEMENT WITHOUT A CELL -> rejected
# ============================================================
gs, _ = new_game([], [])
set_biomes(gs, 'OC')
force_hand(gs, 'A', ['boost'])
p = gs.players['A']
ensure_mana(gs, 'A', 1)
p.mana_spend = 0
p.message = {'cards': ['boost'], 'to': 'stopover_4', 'mode': 'move', 'pendings': []}
gs.state = f"turn {gs.turn} - waiting for first player (A) to play"
p, gs, okk, msgtxt = ge.player_play('first', p, gs)
assert not okk and 'cell' in msgtxt, f"should be rejected with a cell hint, got ok={okk} msg={msgtxt}"
assert gs.board_drops == [], "no token should be placed"
assert 'boost' in (gs.players['A'].hand or []), "the card should stay in hand"
print("2b) DROP PLACEMENT: no cell -> play rejected, card kept in hand -> PASS")
ok += 1

# ============================================================
# 3) BOOST TRIGGER: a token stepping onto the cell gets +2 (stepped)
# ============================================================
gs, _ = new_game([], [])
set_biomes(gs, 'OC')
gs.board_drops.append({'cell': 3, 'kind': 'boost', 'owner': 'B'})
put_at(gs, 'A', 1)
gs = ge.process_advancing(2, gs.players['A'], gs)   # 1 -> 2 -> 3 (trigger) -> +2 -> 5
assert gs.players['A'].current_position == 5, f"boost should push A to 5, got {gs.players['A'].current_position}"
assert gs.board_drops == [], "the boost token should be consumed"
# earth consistency: A exactly once, on cell 5
count = sum(1 for cell in gs.earth if 'A' in cell)
assert count == 1, f"A should be on exactly one cell, got {count}"
assert 'A' in gs.earth[5], f"A should be on cell 5, earth={gs.earth}"
print("3) BOOST TRIGGER: 1 -> 2 -> 3 (fires) -> 5, token consumed, earth clean -> PASS")
ok += 1

# ============================================================
# 4) TRAMPOLINE TRIGGER: a token landing (or stepping) on it jumps +2
# ============================================================
gs, _ = new_game([], [])
set_biomes(gs, 'OC')
gs.board_drops.append({'cell': 4, 'kind': 'trampoline', 'owner': 'B'})
put_at(gs, 'A', 2)
gs = ge.process_advancing(2, gs.players['A'], gs)   # 2 -> 3 -> 4 (trigger) -> jump 6
assert gs.players['A'].current_position == 6, f"trampoline should push A to 6, got {gs.players['A'].current_position}"
assert gs.board_drops == [], "the trampoline token should be consumed"
count = sum(1 for cell in gs.earth if 'A' in cell)
assert count == 1 and 'A' in gs.earth[6], f"A should be on exactly cell 6, earth={gs.earth}"
print("4) TRAMPOLINE TRIGGER: 2 -> 4 (fires) -> jump to 6 -> PASS")
ok += 1

# ============================================================
# 5) GLUETRAP TRIGGER: -1 knockback (direct, clamped at 0)
# ============================================================
gs, _ = new_game([], [])
set_biomes(gs, 'OC')
gs.board_drops.append({'cell': 2, 'kind': 'gluetrap', 'owner': 'B'})
put_at(gs, 'A', 1)
gs = ge.process_advancing(1, gs.players['A'], gs)   # 1 -> 2 (trigger) -> 1
assert gs.players['A'].current_position == 1, f"gluetrap should knock A back to 1, got {gs.players['A'].current_position}"
assert gs.board_drops == [], "the gluetrap token should be consumed"
# at cell 0: clamped (a BACKWARD arrival also fires the drop)
gs.board_drops.append({'cell': 0, 'kind': 'gluetrap', 'owner': 'B'})
put_at(gs, 'A', 1)
gs = ge.process_advancing(-1, gs.players['A'], gs)   # 1 -> 0 (trigger) -> clamped 0
assert gs.players['A'].current_position == 0, f"gluetrap at cell 0 clamps at 0, got {gs.players['A'].current_position}"
print("5) GLUETRAP TRIGGER: -1 knockback, clamped at cell 0 -> PASS")
ok += 1

# ============================================================
# 6) LANDMINE TRIGGER: blocks the arriving player, stops the current movement
# ============================================================
gs, _ = new_game([], [])
set_biomes(gs, 'OC')
gs.board_drops.append({'cell': 2, 'kind': 'landmine', 'owner': 'B'})
put_at(gs, 'A', 1)
gs = ge.process_advancing(2, gs.players['A'], gs)   # 1 -> 2 (landmine fires, blocked) -> 3 CANCELED
assert gs.players['A'].current_position == 2, f"landmine should stop the move at cell 2, got {gs.players['A'].current_position}"
assert gs.players['A'].landmine_blocked is True, "A should be landmine-blocked"
assert gs.board_drops == [], "the landmine token should be consumed"
assert ge._player_blocked(gs.players['A'], gs), "A should read as blocked"
print("6) LANDMINE TRIGGER: move stopped at the mine, A blocked -> PASS")
ok += 1

# ============================================================
# 7) LANDMINE BLOCK: A's next move card is canceled (no effect, no advancing)
# ============================================================
# continue from test 6's state (A blocked)
gs.players['A'].hand.append(adv1)
p = gs.players['A']
ensure_mana(gs, 'A', 5)
p.mana_spend = 0
p.message = {'cards': [adv1], 'to': 'stopover_3', 'mode': 'move', 'pendings': []}
gs.state = f"turn {gs.turn} - waiting for first player (A) to play"
p, gs, okk, _ = ge.player_play('first', p, gs)
assert okk, "the play itself is accepted (the block applies at resolution)"
gs = ge.process_trip_chain(gs)
assert gs.players['A'].current_position == 2, \
    f"the blocked card must not advance (A stays at 2), got {gs.players['A'].current_position}"
assert adv1 in (gs.players['A'].discard or []), "the canceled card goes to the discard"
print("7) LANDMINE BLOCK: A's move card canceled (no advancing, no effect) -> PASS")
ok += 1

# ============================================================
# 8) LANDMINE EXCEPTION: unstoppable (condition met) still advances
# ============================================================
gs, _ = new_game([], [])
set_biomes(gs, 'OC')
gs.players['A'].landmine_blocked = True   # already blocked
put_at(gs, 'A', 1)
gs.players['A'].hand.append(un1)
p = gs.players['A']
ensure_mana(gs, 'A', 5)
p.mana_spend = 0
p.message = {'cards': [un1], 'to': 'stopover_3', 'mode': 'move', 'pendings': []}
gs.state = f"turn {gs.turn} - waiting for first player (A) to play"
p, gs, okk, _ = ge.player_play('first', p, gs)
assert okk
gs = ge.process_trip_chain(gs)
adv_un = int(DB.filter(pl.col('card_id') == un1)['advancing'][0])
expected = 1 + adv_un
if expected >= 24:
    expected = 0   # win -> back at 0
assert gs.players['A'].current_position == expected, \
    f"unstoppable should advance through the block (1 + {adv_un} = {expected}), got {gs.players['A'].current_position}"
print(f"8) LANDMINE EXCEPTION: unstoppable card advanced (1 -> {expected}) -> PASS")
ok += 1

# ============================================================
# 9) CLEANING: the landmine block is cleared at the start of the next turn
# ============================================================
next_turn(gs)
assert not ge._player_blocked(gs.players['A'], gs), "the block should be cleared"
print("9) CLEANING: landmine block cleared for the next turn -> PASS")
ok += 1

# ============================================================
# 10) DWELLING PLACE: refinery from the hand -> the dwelling slot (cost 3)
# ============================================================
gs, _ = new_game([], [])
set_biomes(gs, 'OC')
force_hand(gs, 'A', ['refinery'])
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=['refinery'])
assert okk, f"dwelling placement rejected: {msgtxt}"
assert gs.players['A'].dwelling == 'refinery', f"refinery should be on the dwelling spot, got {gs.players['A'].dwelling}"
assert 'refinery' not in (gs.players['A'].hand or []), "refinery should leave the hand"
assert gs.players['A'].mana_spend == 3, f"placement should cost 3 mana, spent {gs.players['A'].mana_spend}"
# the placeholder slot is set at placement time (the frontend renders the
# placeholder image in that stopover column during the placement turn)
assert gs.players['A'].dwelling_slot is not None, "dwelling_slot should be set at placement time"
# the second placement is rejected (one dwelling card at a time)
force_hand(gs, 'A', ['refinery'])
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=['refinery'])
assert not okk and 'already' in msgtxt, f"2nd placement should be rejected, got ok={okk} msg={msgtxt}"
print("10) DWELLING PLACE: refinery placed (cost 3), one at a time -> PASS")
ok += 1

# ============================================================
# 11) DWELLING TAP: once per turn -> draw 1 card; second tap rejected
# ============================================================
hand_before = len(gs.players['A'].hand or [])
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=None, mode='dwelling_activation')
assert okk, f"tap rejected: {msgtxt}"
assert gs.players['A'].dwelling_tapped is True, "the refinery should be tapped"
assert len(gs.players['A'].hand or []) == hand_before + 1, "the tap should draw 1 card"
assert gs.players['A'].mana_spend == 0, "the tap is free (no extra mana)"
# second tap this turn -> rejected
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=None, mode='dwelling_activation')
assert not okk and 'tapped' in msgtxt, f"2nd tap should be rejected, got ok={okk} msg={msgtxt}"
print("11) DWELLING TAP: draw 1, once per turn -> PASS")
ok += 1

# ============================================================
# 11b) DWELLING PLACEHOLDER SLOT: cleared in the cleaning phase (so the
#      placeholder does not reappear at every new turn) — drives the REAL
#      engine turn-end block (ge._end_turn), not the test mirror.
#      (runs AFTER the tap: the tap needs a deck card to draw)
# ============================================================
gs, _ok, _msg = ge._end_turn(gs, 'test')
assert gs.players['A'].dwelling_slot is None, f"dwelling_slot should be cleared in the cleaning phase, got {gs.players['A'].dwelling_slot}"
assert gs.players['A'].dwelling == 'refinery', "the dwelling card itself must STAY on the board (only the placeholder slot is cleared)"
assert gs.turn >= 1, "the turn should have advanced"
print("11b) DWELLING PLACEHOLDER SLOT: set at placement, cleared in the cleaning phase, dwelling card kept -> PASS")
ok += 1

# ============================================================
# 12) WRECKING BALL: removes the (now placeable) refinery dwelling
# ============================================================
gs, _ = new_game([], [])
set_biomes(gs, 'OC')
gs.players['B'].dwelling = 'refinery'   # B has its refinery on the board
force_hand(gs, 'A', [wb1]); force_hand(gs, 'B', [adv1])
gs = play(gs, 'A', 'first', wb1)
assert gs.players['B'].dwelling is None, "the refinery should be wrecked"
assert 'refinery' in (gs.players['B'].discard or []), "the refinery should go to B's discard"
print("12) WRECKING BALL: refinery dwelling removed -> discard -> PASS")
ok += 1

# ============================================================
# 13) drop_on_board: met when an engineer drop is on the board
# ============================================================
gs, _ = new_game([], [])
set_biomes(gs, 'OC')
assert ge.is_condition_met('drop_on_board', gs.players['A'], gs) is False, "clean board -> not met"
gs.board_drops.append({'cell': 5, 'kind': 'boost', 'owner': 'A'})
assert ge.is_condition_met('drop_on_board', gs.players['A'], gs) is True, "engineer drop -> met"
print("13) drop_on_board: engineer drops count -> PASS")
ok += 1

# ============================================================
# 14) OLD GAME (engine_version < 12): drops inert, dwelling rejected
# ============================================================
gs_old, _ = new_game([], [], version=11)
set_biomes(gs_old, 'OC')
gs_old.board_drops.append({'cell': 2, 'kind': 'boost', 'owner': 'B'})   # hypothetical leftover
put_at(gs_old, 'A', 1)
gs_old = ge.process_advancing(1, gs_old.players['A'], gs_old)   # 1 -> 2: the drop must NOT fire
assert gs_old.players['A'].current_position == 2, "v11: the drop must not fire"
assert gs_old.board_drops, "v11: the token must stay on the board"
# dwelling actions are rejected
force_hand(gs_old, 'A', ['refinery'])
gs_old, okk, msgtxt = dwelling(gs_old, 'A', 'first', cards=['refinery'])
assert not okk, "v11: dwelling placement must be rejected"
print("14) OLD GAME (v11): drops inert, dwelling rejected -> PASS")
ok += 1

# ============================================================
# 15) FULL GAME (kept for the replay self-test): place a boost, place the
#     refinery, tap it, wreck it with a wrecking_ball - then verify the whole
#     v12 flow + card conservation
# ============================================================
gs, gid = new_game([], [], keep=True)
set_biomes(gs, 'OC')
force_hand(gs, 'A', ['boost', 'refinery'])
force_hand(gs, 'B', [wb1])
initial_total = len(all_zones(gs))
# --- turn 1: A places a boost on cell 3; B passes; A passes -> chain ---
gs = play(gs, 'A', 'first', 'boost', cell=3)
gs = pas(gs, 'B', 'second')
gs = pas(gs, 'A', 'first')
gs = ge.process_trip_chain(gs)
assert gs.board_drops == [{'cell': 3, 'kind': 'boost', 'owner': 'A'}], \
    f"boost should be on the board, got {gs.board_drops}"
gs = next_turn(gs)
# --- turn 2: A places the refinery, B passes, A taps the refinery,
#             B plays wrecking_ball (wrecks the refinery), A passes -> chain ---
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=['refinery'])
assert okk, f"refinery placement should succeed in the kept game: {msgtxt}"
gs = pas(gs, 'B', 'second')
gs, okk, msgtxt = dwelling(gs, 'A', 'first', cards=None, mode='dwelling_activation')
assert okk, f"refinery tap should succeed in the kept game: {msgtxt}"
gs = play(gs, 'B', 'second', wb1)   # INSTANT: wrecks A's refinery
gs = pas(gs, 'A', 'first')
gs = ge.process_trip_chain(gs)
assert gs.players['A'].dwelling is None, "the refinery should be wrecked in the kept game"
assert 'refinery' in (gs.players['A'].discard or []), "the wrecked refinery should be in A's discard"
# finalize the turn (end-of-turn block: next turn starts in the mana phase) so
# the replay self-test sees a "complete" game state, not an unresolved trip chain
gs = next_turn(gs)
gs.state = "waiting for both players to mana or pass"
# card conservation: every card is in exactly one place
total = len(all_zones(gs))
assert total == initial_total, f"card conservation: expected {initial_total}, got {total}"
# persist this game for the replay self-test
conn = sqlite3.connect(ge.DB_PATH)
conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
conn.commit()
conn.close()
print(f"15) FULL GAME (v12): boost + refinery + wreck flow, conservation ok -> PASS  (kept game: {gid})")
ok += 1

print(f"\nALL {ok} ENGINEER TESTS PASSED")
print(f"Kept game for the replay self-test: {gid}")
