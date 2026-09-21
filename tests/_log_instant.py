# Manual smoke test: the turn log's INSTANT section (all instant effects + dwelling taps)
#  - The engine records EVERY instant effect in the CURRENT TURN's log entry, in a
#    new 'instant' section (a list of {'player', 'what'}), rendered at the TOP of the
#    turn (before the stopovers):
#        * play-time instant effects: pet_trap, engineer drops, wrecking_ball,
#          Celestial_reversal, nobodymoves, thermic_flux, Apocalypticritual
#        * dwelling taps: refinery (draw), laboratory (epo), black_hole (rotate)
#  - These notes are NOT in the per-card 'notes' arrays (they fire at PLAY TIME or on
#    a TAP — before / outside the trip chain). Resolution-time notes (cataclysm strike,
#    knockback suppression, grappling copy, ...) stay in 'notes'.
#  - A TAP-ONLY turn (a tap + both pass, no card played) KEEPS its turn log entry
#    (the 'instant' section is its only record — the engine must not drop it).
#  - Old games (engine_version < 22) have NO 'instant' key (Celestial_reversal is a
#    no-op, so nothing is recorded).
#  - The replay reconstructs the Apocalypticritual pile from the 'instant' section
#    (the ritual phrase keeps its '☄️ Apocalypticritual — cataclysm order set:' prefix).
# Run from the project root:  uv run python tests/_log_instant.py
# NOTE: writes to games.db (like _mages_celestial.py). Deletes its own games.
import sys, io, os, sqlite3, json, contextlib
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

# a no_condition advancing card (for a normal move play)
adv1 = q(condition='no_condition', advancing=2, mana=1)['card_id'].to_list()
adv1 = adv1[0] if adv1 else q(condition='no_condition')['card_id'].to_list()[0]

# filler cards
filler = [c for c in q(faction='Miaous')['card_id'].to_list() if c != adv1][:20]

def _delete(gid):
    conn = sqlite3.connect(ge.DB_PATH)
    conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
    conn.commit()
    conn.close()

def new_game(a_cards, b_cards, version=None):
    p1 = PlayerState(name='A', deck=list(a_cards) + filler)
    p2 = PlayerState(name='B', deck=list(b_cards) + filler)
    gid = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gid)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    gs.turn_order = ['A', 'B']
    if version is not None:
        gs.engine_version = version
    return gs, gid

def set_biomes(gs, biome='OC'):
    for cell in gs.earth:
        cell[0] = biome

def force_hand(gs, name, cards):
    p = gs.players[name]
    for c in cards:
        if c not in p.hand:
            if c in p.deck:
                p.deck.remove(c)
            p.hand.append(c)

def ensure_mana(gs, name, n):
    p = gs.players[name]
    while len(p.mana) - (p.mana_spend or 0) < n and p.deck:
        p.mana.append(p.deck.pop(0))

def play(gs, name, first_second, card, mode='move', to='stopover_4', **kw):
    p = gs.players[name]
    cost = max(ge._play_cost(gs, card), 1)
    ensure_mana(gs, name, cost)
    p.mana_spend = 0
    msg = {'cards': [card], 'to': to, 'mode': mode, 'pendings': []}
    for k, v in kw.items():
        if v is not None:
            msg[k] = v
    p.message = msg
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    return gs, okk, msgtxt

def pas(gs, name, first_second):
    p = gs.players[name]
    p.message = {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    assert okk, f'pass rejected: {msgtxt}'
    return gs

def place_dwelling(gs, name, first_second, card):
    p = gs.players[name]
    cost = max(ge._play_cost(gs, card), 1)
    ensure_mana(gs, name, cost)
    p.mana_spend = 0
    p.message = {'cards': [card], 'to': 'dwelling', 'mode': '', 'pendings': []}
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    return gs, okk, msgtxt

def tap(gs, name, first_second, rotation=None):
    p = gs.players[name]
    msg = {'cards': [], 'to': 'dwelling', 'mode': 'dwelling_activation', 'pendings': []}
    if rotation is not None:
        msg['rotation'] = rotation
    p.message = msg
    gs.state = f"turn {gs.turn} - waiting for {first_second} player ({gs.turn_order[0]}) to play"
    p, gs, okk, msgtxt = ge.player_play(first_second, p, gs)
    return gs, okk, msgtxt

def ws(gid, name, message):
    """ drive the game through the REAL WS entry point (populates messages_history). """
    conn, _, gs = ge.get_current_game(gid)
    player = gs.players[name].model_copy()
    player.message = message
    conn.close()
    resp = ge.handle_websocket_message(gid, player)
    conn, _, gs = ge.get_current_game(gid)
    conn.close()
    if isinstance(resp, str):
        resp = json.loads(resp)
    return gs, (resp.get('message') or {}) if isinstance(resp, dict) else {}

def instant_of(gs, turn=None):
    """ the 'instant' section of the given turn (default: last turn entry). """
    for t in reversed(gs.log or []):
        if turn is None or t.get('turn') == turn:
            return t.get('instant') or []
    return []

def instant_whats(gs, turn=None):
    return [str(it.get('what')) for it in instant_of(gs, turn)]

def all_entry_notes(gs):
    """ every per-card 'notes' string across the log (the place instant notes must NOT be). """
    out = []
    for t in (gs.log or []):
        for sv in t.get('stopovers') or []:
            for e in sv.get('entries') or []:
                out.extend(str(n) for n in (e.get('notes') or []))
    return out

passed = 0
failed = 0
def check(condition, msg, extra=''):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {msg}")
    else:
        failed += 1
        print(f"  ✗ FAIL: {msg} {extra}")

# ============================================================================
print("=== 1) Mages instant cards land in the turn's 'instant' section ===")
# ============================================================================
gs, gid = new_game([ge.MAGE_CELASTIAL_REVERSAL, ge.MAGE_THERMIC_FLUX, adv1], [adv1], version=27)
set_biomes(gs, 'OC')
force_hand(gs, 'A', [ge.MAGE_CELASTIAL_REVERSAL, ge.MAGE_THERMIC_FLUX])
force_hand(gs, 'B', [adv1])
gs, ok, _ = play(gs, 'A', 'first', ge.MAGE_CELASTIAL_REVERSAL, day_night='night')
check(ok, "A's Celestial_reversal accepted")
gs, ok, _ = play(gs, 'B', 'second', adv1)
check(ok, "B's normal card accepted")
gs = ge.process_trip_chain(gs)
whats = instant_whats(gs)
check(any('Celestial_reversal' in w and 'night' in w for w in whats),
      f"instant section has A's Celestial_reversal note: {whats}")
# Celestial is a no-op on the trip chain -> its line must NOT carry the note in 'notes'
check(not any('Celestial_reversal' in n for n in all_entry_notes(gs)),
      "the Celestial note is NOT in the per-card 'notes' (it moved to 'instant')")

# ============================================================================
print("=== 2) nobodymoves + Apocalypticritual in the 'instant' section ===")
# ============================================================================
gs2, gid2 = new_game([ge.MAGE_NOBODYMOVES, adv1], [ge.MAGE_APOCALYPTICRITUAL, adv1], version=27)
set_biomes(gs2, 'OC')
force_hand(gs2, 'A', [ge.MAGE_NOBODYMOVES])
force_hand(gs2, 'B', [ge.MAGE_APOCALYPTICRITUAL])
order = ['JU', 'OC', 'DE', 'MO']
gs2, ok, _ = play(gs2, 'A', 'first', ge.MAGE_NOBODYMOVES)
check(ok, "A's nobodymoves accepted")
gs2, ok, _ = play(gs2, 'B', 'second', ge.MAGE_APOCALYPTICRITUAL, cataclysm_order=order)
check(ok, "B's Apocalypticritual accepted")
gs2 = ge.process_trip_chain(gs2)
whats2 = instant_whats(gs2)
check(any('nobodymoves' in w and 'movement locked' in w.lower() for w in whats2),
      f"instant section has A's nobodymoves note: {whats2}")
check(any('Apocalypticritual' in w and 'cataclysm order set' in w for w in whats2),
      f"instant section has B's Apocalypticritual note: {whats2}")
check(not any('MOVEMENT LOCKED' in n for n in all_entry_notes(gs2)),
      "the nobodymoves note is NOT in the per-card 'notes'")
check(not any('Apocalypticritual' in n and 'order set' in n for n in all_entry_notes(gs2)),
      "the Apocalypticritual note is NOT in the per-card 'notes'")

# ============================================================================
print("=== 3) pet_trap + engineer drop + wrecking_ball in the 'instant' section ===")
# ============================================================================
pt = q(effect='pet_trap')['card_id'].to_list()
pt = pt[0] if pt else None
wb = q(effect='wrecking_ball')['card_id'].to_list()
wb = wb[0] if wb else None
gs3, gid3 = new_game([pt, wb, adv1], ['refinery', adv1], version=27)
set_biomes(gs3, 'OC')
if pt:
    force_hand(gs3, 'A', [pt])
    force_hand(gs3, 'B', [adv1])
    gs3, ok, _ = play(gs3, 'A', 'first', pt)
    check(ok, "A's pet_trap accepted")
    check(any('pet_trap' in w and 'placed' in w for w in instant_whats(gs3)),
          f"instant section has A's pet_trap note: {instant_whats(gs3)}")
    check(not any('pet_trap' in n and 'placed' in n for n in all_entry_notes(gs3)),
          "the pet_trap placement note is NOT in the per-card 'notes'")
# wrecking_ball: give B a refinery dwelling, A wrecks it
gs3.players['B'].dwelling = 'refinery'
force_hand(gs3, 'A', [wb])
gs3, ok, _ = play(gs3, 'A', 'first', wb)
check(ok, "A's wrecking_ball accepted")
check(any('wrecking_ball' in w and 'dwelling' in w for w in instant_whats(gs3)),
      f"instant section has A's wrecking_ball note: {instant_whats(gs3)}")
check(not any('wrecking_ball' in n and 'dwelling' in n for n in all_entry_notes(gs3)),
      "the wrecking_ball note is NOT in the per-card 'notes'")

# ============================================================================
print("=== 4) dwelling taps (refinery / laboratory / black_hole) in the 'instant' section ===")
# ============================================================================
gs4, gid4 = new_game(['refinery', 'laboratory', 'black_hole', adv1], [adv1], version=26)
set_biomes(gs4, ['OC', 'MO', 'DE', 'JU'])
gs4.earth_initial_b0 = 'OC'
gs4.earth_rotation = 0
force_hand(gs4, 'A', ['refinery', 'laboratory', 'black_hole'])
# refinery tap
gs4, ok, _ = place_dwelling(gs4, 'A', 'first', 'refinery')
check(ok, "A placed the refinery")
gs4, ok, msgt = tap(gs4, 'A', 'first')
check(ok, f"refinery tap accepted ({msgt})")
check(any('refinery' in w and 'tap' in w for w in instant_whats(gs4)),
      f"instant section has the refinery tap note: {instant_whats(gs4)}")
# black_hole tap
gs4.players['A'].dwelling = 'black_hole'   # swap the dwelling (test shortcut)
gs4.players['A'].dwelling_tapped = False
gs4, ok, msgt = tap(gs4, 'A', 'first', 'cw')
check(ok, f"black_hole tap accepted ({msgt})")
check(any('black_hole' in w and 'clockwise' in w for w in instant_whats(gs4)),
      f"instant section has the black_hole tap note: {instant_whats(gs4)}")
# laboratory tap
gs4.players['A'].dwelling = 'laboratory'
gs4.players['A'].dwelling_tapped = False
gs4, ok, msgt = tap(gs4, 'A', 'first')
check(ok, f"laboratory tap accepted ({msgt})")
check(any('laboratory' in w and 'epo' in w for w in instant_whats(gs4)),
      f"instant section has the laboratory tap note: {instant_whats(gs4)}")

# ============================================================================
print("=== 5) a TAP-ONLY turn keeps its log entry (instant, no stopovers) ===")
# ============================================================================
gs5, gid5 = new_game(['refinery', adv1], [adv1], version=26)
set_biomes(gs5, 'OC')
force_hand(gs5, 'A', ['refinery'])
force_hand(gs5, 'B', [adv1])
# A places the refinery, taps it, then BOTH pass (no card played this turn)
gs5, ok, _ = place_dwelling(gs5, 'A', 'first', 'refinery')
check(ok, "A placed the refinery (turn 1)")
# resolve turn 1 (A played a dwelling, B passed) -> B draws, turn advances
gs5 = ge.process_trip_chain(gs5)
# --- turn 2: A taps the refinery, both pass (a TAP-ONLY turn) ---
n_turn = gs5.turn
n_log_before = len(gs5.log or [])
gs5, ok, msgt = tap(gs5, 'A', 'first')
check(ok, f"refinery tap on turn {n_turn} accepted ({msgt})")
gs5 = pas(gs5, 'A', 'first')
gs5 = pas(gs5, 'B', 'second')
gs5 = ge.process_trip_chain(gs5)
# the tap-only turn's log entry must SURVIVE (it has an 'instant' section, no stopovers)
found = None
for t in (gs5.log or []):
    if t.get('turn') == n_turn:
        found = t
        break
check(found is not None, f"the tap-only turn {n_turn} log entry exists")
check(bool(found) and (found.get('instant') or []) and not (found.get('stopovers') or []),
      f"the tap-only turn has an 'instant' section and no stopovers: {found}")

# ============================================================================
print("=== 6) old game (engine_version 21): NO 'instant' section ===")
# ============================================================================
gs6, gid6 = new_game([ge.MAGE_CELASTIAL_REVERSAL, adv1], [adv1], version=21)
set_biomes(gs6, 'OC')
force_hand(gs6, 'A', [ge.MAGE_CELASTIAL_REVERSAL])
force_hand(gs6, 'B', [adv1])
gs6, ok, _ = play(gs6, 'A', 'first', ge.MAGE_CELASTIAL_REVERSAL, day_night='night')
# v21: Celestial_reversal is a no-op -> it must NOT create an instant note
check(not any('Celestial_reversal' in w for w in instant_whats(gs6)),
      f"v21: no Celestial_reversal instant note: {instant_whats(gs6)}")
gs6, ok, _ = play(gs6, 'B', 'second', adv1)
gs6 = ge.process_trip_chain(gs6)
check(not any('Celestial_reversal' in w for w in instant_whats(gs6)),
      "v21: the instant section has no Celestial_reversal (old behavior)")

# ============================================================================
print("=== 7) replay reconstructs the Apocalypticritual pile from 'instant' ===")
# ============================================================================
CATACLYSM = q(condition='cataclysm')['card_id'].to_list()
CATACLYSM = CATACLYSM[0] if CATACLYSM else None
if CATACLYSM:
    # WS-driven game (so messages_history is populated - the replay reads it).
    RITUAL = ge.MAGE_APOCALYPTICRITUAL
    a_fillers = [c for c in q(faction='Orcs')['card_id'].to_list() if c not in (adv1, CATACLYSM)][:18]
    b_fillers = [c for c in q(faction='Demons')['card_id'].to_list() if c not in (adv1, CATACLYSM)][:20]
    a_deck = [RITUAL] + a_fillers
    b_deck = b_fillers
    p1 = PlayerState(name='A', deck=list(a_deck))
    p2 = PlayerState(name='B', deck=list(b_deck))
    gidr = ge.create_new_game(player=p1.model_dump())
    ge.p2_connect_to_game(player=p2.model_dump(), game_id=gidr)
    conn, _, gsr = ge.get_current_game(gidr)
    gsr.turn_order = ['A', 'B']
    for cell in gsr.earth:
        cell[0] = 'OC'
    ga, gb = gsr.players['A'], gsr.players['B']
    ga.hand = [RITUAL] + a_fillers[:5]
    ga.deck = a_fillers[5:]
    gb.hand = list(b_fillers[:6])
    gb.deck = b_fillers[6:]
    ga.mana, ga.discard, ga.dwelling = [], [], None
    gb.mana, gb.discard, gb.dwelling = [], [], None
    gsr.cataclysm_pile = ['JU', 'MO', 'DE', 'OC']
    conn.execute("UPDATE games SET state_json = ? WHERE game_id = ?", (gsr.to_json(), gidr))
    conn.commit(); conn.close()

    # init: both put 3 in mana
    gsr, ra = ws(gidr, 'A', {'cards': list(a_fillers[:3]), 'to': 'mana', 'mode': '', 'pendings': []})
    check(ra.get('success'), f"A init mana accepted: {ra}")
    gsr, rb = ws(gidr, 'B', {'cards': list(b_fillers[:3]), 'to': 'mana', 'mode': '', 'pendings': []})
    check(rb.get('success'), f"B init mana accepted: {rb}")

    # turn 1: A plays the ritual (sets the pile), B plays a filler
    chosen = ['OC', 'JU', 'MO', 'DE']
    gsr, ra = ws(gidr, 'A', {'cards': [RITUAL], 'to': 'stopover_4', 'mode': 'move', 'pendings': [],
                             'cataclysm_order': chosen})
    check(ra.get('success'), f"A ritual accepted (turn 1): {ra}")
    gsr, rb = ws(gidr, 'B', {'cards': [b_fillers[3]], 'to': 'stopover_4', 'mode': 'move', 'pendings': []})
    check(rb.get('success'), f"B filler accepted (turn 1): {rb}")
    gsr, _ = ws(gidr, 'A', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    gsr, _ = ws(gidr, 'B', {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []})
    check(gsr.turn == 2, f"turn 1 resolved (turn advanced to 2): turn={gsr.turn}")
    check(any('Apocalypticritual' in w and 'cataclysm order set' in w for w in instant_whats(gsr)),
          f"turn 1 instant has the ritual note: {instant_whats(gsr)}")

    # --- replay ---
    conn, _, gs2 = ge.get_current_game(gidr)
    conn.close()
    import games.analysis.replay as R
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = R.analyze_game(json.loads(gs2.to_json()))
    print(f"  replay: verified={res['verified']} turns={len(res['turns'])} warn={len(res['warnings'])}")
    for w in res['warnings']:
        print(f"    warn: {w}")
    check(not any('cataclysm' in w and 'pile' in w and 'diverge' in w for w in res['warnings']),
          'no "cataclysm pile diverges" warning (the pile IS mirrored)', f"(warnings={res['warnings']})")
    check(not any('Apocalypticritual' in w and 'unknown' in w for w in res['warnings']),
          'no "unknown card" warning for Apocalypticritual', f"(warnings={res['warnings']})")
    evs = [e for t in res['turns'] for e in t.get('events', []) if e.get('type') == 'apocalypticritual']
    check(len(evs) == 1 and evs[0].get('order') == chosen,
          'replay recorded the apocalypticritual event (from the instant section)', f"(events={evs})")
    _delete(gidr)
else:
    print("  (no cataclysm card in the pool — skipping the replay check)")

# --- cleanup the other games ---
for g in (gid, gid2, gid3, gid4, gid5, gid6):
    try:
        _delete(g)
    except Exception:
        pass

print(f"\n{'='*50}")
print(f"RESULTS: {passed} passed, {failed} failed")
print(f"{'='*50}")
if failed == 0:
    print("All tests passed!")
else:
    sys.exit(1)
