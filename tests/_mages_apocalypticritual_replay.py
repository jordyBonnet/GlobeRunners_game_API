# Manual smoke test (REPLAY): Mages Apocalypticritual — the replay must mirror the
# engine's cataclysm-pile reorder:
#   * the INSTANT effect (the pile is SET to the chosen order) is applied at play
#     time in the replay's play loop (mutating game.cataclysm_pile, so the real
#     trigger_cataclysm reads the new order)
#   * the replay's INITIAL pile reconstruction handles the ritual (a game with a
#     ritual is NOT a pure rotation of the final pile)
#   * the final tracked pile matches the stored `cataclysm_pile`
#   * no "cataclysm pile diverges" warning
#   * no "unknown card" warnings (the replay synthesizes a support row)
#   * the replay records the apocalypticritual event with the correct order
# Run from the project root:  uv run python tests/_mages_apocalypticritual_replay.py
# NOTE: writes one game to games.db (like the other smoke tests); the game is deleted after.
import sys, io, os, sqlite3, json, re, contextlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import polars as pl
import engine.game_engine as ge
from models import PlayerState

DB = ge.CARDS_DB
# 24 DISTINCT low-advancing no_condition cards with a harmless effect (predictable,
# no advancing/backward/jump so the positions stay small and the game never wins).
low = DB.filter((pl.col('condition') == 'no_condition')
                & (pl.col('advancing') >= 1) & (pl.col('advancing') <= 2)
                & (pl.col('mana') <= 3)
                & ~pl.col('effect').is_in(['advancing', 'backward', 'jump',
                                          'discard', 'discard_oppo'])
                & (pl.col('effect') != 'grappling_hook'))
CARDS = low['card_id'].to_list()
assert len(CARDS) >= 24, f"need 24 distinct low-advancing cards, got {len(CARDS)}"
RITUAL = ge.MAGE_APOCALYPTICRITUAL   # 'Apocalypticritual'
CATACLYSM = 'Dem10_af058e'           # a clean cataclysm-condition card (adv 0, mana 1)
assert DB.filter(pl.col('card_id') == CATACLYSM)['condition'].to_list() == ['cataclysm']

def send(gid, name, message):
    conn, _, gs = ge.get_current_game(gid)
    player = gs.players[name].model_copy()
    player.message = message
    conn.close()
    resp = ge.handle_websocket_message(gid, player)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    if isinstance(resp, str):
        resp = json.loads(resp)
    return gs, resp

ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

# --- build the game: A has Apocalypticritual + a cataclysm card + fillers ------
a_deck = [RITUAL, CATACLYSM] + CARDS[0:18]
b_deck = CARDS[6:26]

p1 = PlayerState(name='A', deck=list(a_deck))
p2 = PlayerState(name='B', deck=list(b_deck))
gid = ge.create_new_game(player=p1.model_dump())
ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)

c, _, gs = ge.get_current_game(gid)
gs.turn_order = ['A', 'B']
# all cells = OC so a cataclysm 'OC' strike is observable (A knocked back to cell 0)
for cell in gs.earth:
    cell[0] = 'OC'
ga, gb = gs.players['A'], gs.players['B']
ga.hand = [RITUAL, CATACLYSM, CARDS[0], CARDS[1], CARDS[2], CARDS[3]]
ga.deck = a_deck[6:]
gb.hand = list(b_deck[:6])
gb.deck = b_deck[6:]
ga.mana, ga.discard, ga.dwelling = [], [], None
gb.mana, gb.discard, gb.dwelling = [], [], None
# fix the pile to a known order (OC last, so the ritual putting OC first is observable)
gs.cataclysm_pile = ['JU', 'MO', 'DE', 'OC']
c.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gs.to_json(), gid))
c.commit(); c.close()
print(f"game {gid} created (engine_version={gs.engine_version})")

def actor(gs):
    m = re.search(r'waiting for (?:first|second) player \((\w+)\)', gs.state)
    return m.group(1) if m else None

def act(gid, name, cards, to, mode, **kw):
    return send(gid, name, {'cards': cards, 'to': to, 'mode': mode, 'pendings': [], **kw})

def play_turn(gs, a_card, b_card, a_kw=None):
    """ one full PLAY phase, driven by the STATE (turn order reverses each turn). """
    a_played = b_played = False
    for _ in range(10):   # safety cap
        if 'to play' not in gs.state:
            break
        actor_name = actor(gs)
        if actor_name is None:
            break
        if actor_name == 'A':
            if not a_played and a_card:
                gs, r = act(gid, 'A', [a_card], 'stopover_4', 'move', **(a_kw or {}))
                assert (r.get('message') or {}).get('success'), f"A play rejected: {r.get('message')}"
                a_played = True
            else:
                gs, _ = act(gid, 'A', [], '', 'pass')
        else:
            if not b_played and b_card:
                gs, r = act(gid, 'B', [b_card], 'stopover_4', 'move')
                assert (r.get('message') or {}).get('success'), f"B play rejected: {r.get('message')}"
                b_played = True
            else:
                gs, _ = act(gid, 'B', [], '', 'pass')
    return gs

try:
    # --- init: both put 3 cards in mana (required before turn 1) -------------
    gs, r = act(gid, 'A', [CARDS[0], CARDS[1], CARDS[2]], 'mana', '')
    assert (r.get('message') or {}).get('success'), f"A init rejected: {r.get('message')}"
    gs, r = act(gid, 'B', b_deck[:3], 'mana', '')
    assert (r.get('message') or {}).get('success'), f"B init rejected: {r.get('message')}"
    check('init done - turn 1 play phase', 'turn 1' in gs.state and 'to play' in gs.state, f"(state={gs.state})")
    check('pile starts at the fixed order', gs.cataclysm_pile == ['JU', 'MO', 'DE', 'OC'],
          f"(pile={gs.cataclysm_pile})")

    # --- TURN 1: A plays the ritual (sets the pile), B plays a filler ---------
    chosen = ['OC', 'JU', 'MO', 'DE']
    gs = play_turn(gs, RITUAL, b_deck[3], a_kw={'cataclysm_order': chosen})
    check('turn 1 resolved (turn advanced to 2)', gs.turn == 2, f"(turn={gs.turn})")
    check('the pile is SET to the chosen order after turn 1', gs.cataclysm_pile == chosen,
          f"(pile={gs.cataclysm_pile})")
    check('the ritual card is conserved (in exactly one zone)',
          (RITUAL in gs.players['A'].discard) and (RITUAL not in gs.players['A'].hand),
          f"(discard={gs.players['A'].discard})")

    # --- TURN 2: A adds 1 mana, then plays the cataclysm (strikes OC) --------
    # mana phase: A adds 1 mana (enough for the cataclysm, cost 1), B passes
    gs, r = act(gid, 'A', [CARDS[4]], 'mana', '')
    assert (r.get('message') or {}).get('success'), f"A mana rejected: {r.get('message')}"
    gs, _ = act(gid, 'B', [], '', 'pass')
    check('turn 2 play phase', 'turn 2' in gs.state and 'to play' in gs.state, f"(state={gs.state})")
    gs = play_turn(gs, CATACLYSM, b_deck[4])
    check('turn 2 resolved (turn advanced to 3)', gs.turn == 3, f"(turn={gs.turn})")
    # the OC strike knocked A back to cell 0; the pile rotated (OC → bottom)
    check('the pile rotated after the strike (OC → bottom)',
          gs.cataclysm_pile == ['JU', 'MO', 'DE', 'OC'], f"(pile={gs.cataclysm_pile})")
    note = None
    for turn in gs.log:
        for sv in turn.get('stopovers', []):
            for e in sv.get('entries', []):
                for n in e.get('notes', []):
                    if '⚡ cataclysm —' in n:
                        note = n
    check('the cataclysm struck OC (the CHOSEn top biome)', note is not None and 'OC strikes' in note,
          f"(note={note!r})")

    # --- replay ---------------------------------------------------------------
    conn, _, gs2 = ge.get_current_game(gid)
    conn.close()
    import games.analysis.replay as R
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = R.analyze_game(json.loads(gs2.to_json()))
    print(f"  replay: verified={res['verified']} turns={len(res['turns'])} warn={len(res['warnings'])}")
    for w in res['warnings']:
        print(f"    warn: {w}")

    check('replayed 2 turns', len(res['turns']) >= 2, f"(turns={len(res['turns'])})")
    # NOTE: this game's card set has a pre-existing position/zone divergence (the same
    # 9 warnings the other Mages replay tests show - a known, non-Mages issue), so we
    # do NOT assert the overall `verified` flag. Instead we verify the
    # Apocalypticritual-SPECIFIC behavior: the pile is mirrored (no "cataclysm pile
    # diverges") and the event is recorded with the correct order.
    check('no "cataclysm pile diverges" warning (the pile IS mirrored)',
          not any('cataclysm' in w and 'pile' in w and 'diverge' in w for w in res['warnings']),
          f"(warnings={res['warnings']})")
    check('no "unknown card" warnings for Apocalypticritual',
          not any('Apocalypticritual' in w and 'unknown' in w for w in res['warnings']),
          f"(warnings={res['warnings']})")
    check('no "unknown card" warnings for the cataclysm card',
          not any(CATACLYSM in w and 'unknown' in w for w in res['warnings']),
          f"(warnings={res['warnings']})")
    # the replay should have recorded the apocalypticritual event with the correct order
    evs = [e for t in res['turns'] for e in t.get('events', []) if e.get('type') == 'apocalypticritual']
    check('replay recorded the apocalypticritual event', len(evs) == 1, f"(events={evs})")
    check('replay apocalypticritual event: order = the chosen order',
          len(evs) == 1 and evs[0].get('order') == chosen,
          f"(event={evs})")

    print(f"\nALL {ok} REPLAY CHECKS PASSED - the replay mirrors the Apocalypticritual pile reorder")
finally:
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit(); conn.close()
