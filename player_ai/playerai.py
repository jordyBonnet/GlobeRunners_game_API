"""PlayerAI: heuristics for the Robot player (driven by game_ui/ai_driver.py).

The AI mirrors the engine's rules with PUBLIC information only - it never reads the
opponent's hidden zones (hand/deck/mana contents), and it re-implements rule details
(condition evaluation, support-card classification) locally instead of importing the
engine, so player_ai stays standalone. See AGENTS.md for the architecture.

Since A.1 (see AI_improvement_list.md): the robot can play ALL support factions -
Engineers' drops + refinery, Doctors' pending cards + laboratory, Mages' instant cards
+ black_hole - with baseline choice heuristics (sections A.2-A.4 refine them). The
message builders below are the single dispatch point for every support play (mirror of
the frontend's dispatchPlay branches) and carry the required engine fields ('cell',
'day_night', 'temp_change', 'cataclysm_order', 'rotation'). put_mana no longer burns
support cards first: it sacrifices by a keep-value model so playable support stays in hand.
"""

import polars as pl
import os
import re

# --- Support-faction classification (MIRROR of engine/game_engine.py constants - keep in sync) ---
BIOMES = ('OC', 'MO', 'DE', 'JU')
ENGINEER_DROPS = ('boost', 'trampoline', 'gluetrap', 'landmine')
DWELLING_CARDS = {'refinery': 'Engineers', 'laboratory': 'Doctors', 'black_hole': 'Mages'}
DOCTOR_PENDING = ('epo', 'virus', 'bloodtest', 'mercurochrome')
MAGE_INSTANT_CARDS = ('Celestial_reversal', 'nobodymoves', 'thermic_flux', 'Apocalypticritual')

# Mirror of the engine's FACTION_BIOMES (biome_X conditions + the +1 faction biome bonus)
FACTION_BIOMES = {
    'Dwarves': ('MO', 'OC'),
    'Demons': ('OC', 'DE'),
    'Twigs': ('JU', 'OC'),
    'Miaous': ('DE', 'JU'),
    'Orcs': ('MO', 'JU'),
    'Mummies': ('DE', 'MO'),
}

# condition name -> faction (a biome_X condition is met when standing on one of faction X's two biomes)
BIOME_COND_FACTION = {
    'biome_Dwa': 'Dwarves',
    'biome_Dem': 'Demons',
    'biome_Twi': 'Twigs',
    'biome_Mia': 'Miaous',
    'biome_Orc': 'Orcs',
    'biome_Mum': 'Mummies',
}

# C #24: flat tempo value per effect type, added to a card's score when its condition is met and it is not
# blocked (zone effects fire even under the nobodymoves movement lock - see play_card). Movement itself is
# counted separately by _forward_if_met / _forward_if_unmet (#22); these values cover everything else.
TEMPO_VALUES = {
    'draw': 2,             # refills my hand
    'ramp': 3,             # permanent mana growth (deck -> mana zone)
    'rooted': 3,           # a rooted token grants an extra advance next turn
    'discard_oppo': 2,     # the opponent loses a hand card
    'taxation_oppo': 2,    # the opponent loses a mana card
    'backward_oppo': 3,    # knocks the opponent back (movement is worth more than cards)
    'draw_oppo': 1,         # fatigue: the opponent has to juggle extra cards
    'ramp_oppo': -1,        # the opponent gains a mana card
    'advancing_oppo': -3,   # pushes the opponent toward their win
    'discard': -2,          # I lose a hand card (I get to choose my least valuable one)
    'taxation': -2,         # I lose a mana card
    'effect_canceled': 1,   # defensive value against an opponent move on the same position
    'copy_effect': 1,       # situational: copies the facing card's effect (public chain)
    'grappling_hook': 2,    # situational: copies the facing card's advancing
    'pet_trap': 1,          # instant drop token on my cell + makes drop_on_board met (E #33 adds a situational term)
    'swap_cards': 0,        # E #31: the gain comes from _choose_swap_target (position re-ordering), not a flat value
}

# D #27: minimum value (cells + denied effects) of an opponent threat for the robot to spend cards
# and a position on blocking it. A declared move that would win them the game is ALWAYS blocked
# regardless of this threshold (survival first - see choose_defend).
DEFEND_THRESHOLD = 4

# E #31: minimum net-value gain (cells + denied effects, same scale as play_card's score) for a
# swap_cards play to actually carry a 'swap_with' target. Below that, the card plays in its natural slot.
SWAP_MIN_GAIN = 2


def validate_message(msg, dwelling=None, pendings_zone=None):
	"""Local pre-validation of a message BEFORE it is submitted (mirror of the engine's
	ge.message_check + the support-card field requirements in player_play). Returns
	(ok: bool, reason: str|None). The AI must never emit an invalid message - a rejected
	action costs the tick and trips the fallback-pass. `dwelling` is the card id of the
	dwelling on board (needed to require 'rotation' on a black_hole tap only); `pendings_zone`
	is my pending zone (names) - an attached pending must be one of its entries."""
	if not isinstance(msg, dict):
		return False, "message must be a dict"
	for k in ('cards', 'to', 'mode', 'pendings'):
		if k not in msg:
			return False, f"missing key '{k}'"
	cards = msg['cards']
	to = msg['to']
	mode = msg['mode']
	if not isinstance(cards, list) or not isinstance(msg['pendings'], list):
		return False, "'cards'/'pendings' must be lists"
	if len(msg['pendings']) > 1:
		return False, "at most 1 pending card can be attached"
	if len(msg['pendings']) == 1 and pendings_zone is not None:
		# engine: a pending attaches to a MOVE card only and must sit in my own pending zone
		if mode != 'move':
			return False, "a pending card can only attach to a move play"
		if msg['pendings'][0] not in pendings_zone:
			return False, f"{msg['pendings'][0]} is not in the pending zone"
	if to == 'mana' and len(cards) not in (1, 3):
		return False, "mana placement takes exactly 1 or 3 cards"
	if mode == 'move':
		if len(cards) != 1:
			return False, "move mode plays exactly 1 card"
		if not isinstance(to, str) or not to.startswith('stopover_'):
			return False, "move mode needs a stopover_x target"
		cid = cards[0]
		# support-card required choice fields (the engine rejects them without these)
		if cid in ENGINEER_DROPS:
			cell = msg.get('cell')
			if isinstance(cell, bool) or not isinstance(cell, int) or not (0 <= cell < 24):
				return False, f"engineer drop {cid} needs 'cell' 0..23"
		if cid == 'Celestial_reversal' and msg.get('day_night') not in ('day', 'night'):
			return False, "Celestial_reversal move play needs 'day_night': day|night"
		if cid == 'thermic_flux' and msg.get('temp_change') not in ('up', 'down'):
			return False, "thermic_flux move play needs 'temp_change': up|down"
		if cid == 'Apocalypticritual':
			order = msg.get('cataclysm_order')
			if (not isinstance(order, list) or len(order) != 4
					or any(b not in BIOMES for b in order) or len(set(order)) != 4):
				return False, "Apocalypticritual move play needs 'cataclysm_order' (permutation of the 4 biomes)"
	elif mode == 'defend':
		if not (1 <= len(cards) <= 5):
			return False, "defend mode plays 1-5 cards"
		if not isinstance(to, str) or not to.startswith('stopover_'):
			return False, "defend mode needs a stopover_x target"
	elif mode == 'dwelling_activation':
		pass   # validated below via to == 'dwelling'
	elif mode != '' and mode != 'pass':
		return False, f"unknown mode '{mode}'"
	if to == 'dwelling':
		if mode == 'dwelling_activation':
			if len(cards) != 0:
				return False, "a dwelling tap carries no cards"
			if dwelling == 'black_hole' and msg.get('rotation') not in ('cw', 'ccw'):
				return False, "a black_hole tap needs 'rotation': cw|ccw"
		elif len(cards) != 1:
			return False, "dwelling placement takes exactly 1 card"
	if to == 'pending_zone' and len(cards) != 1:
		return False, "pending zone placement takes exactly 1 card"
	return True, None


class PlayerAI:
	def __init__(self, player_state):
		self.player_state = player_state
		self.oppo_position = None   # opponent's current position (set via update_player_state)
		self.oppo_mana = None       # number of cards in opponent's mana zone
		self.oppo_hand = None       # number of cards in opponent's hand
		self.oppo_faction = None    # opponent's main faction - PUBLIC (deck-derived, set via update_player_state)
		self.temperature = None     # planet temperature (set via update_player_state)
		self.day_night = None       # current day/night phase (set via update_player_state)
		self.day_night_fixed = False  # True once a Celestial_reversal has been played by anyone
		self.drops_on_board = None  # any drop/trap on the earth (set via update_player_state)
		self.earth_biomes = None    # [biome code per cell index] - public board info (A.1 support decisions)
		self.cataclysm_pile = list(BIOMES)   # current cataclysm pile order (public)
		self.occupied_cells = set()  # cells with a token/drop on them already (public board info)
		# A.2 attachment decisions (all PUBLIC info, captured in update_player_state):
		self.rooted_on_board = []    # rooted cards on the board [{card_id, owner, stopover}] - they shift trip-chain positions
		self.oppo_actions = []       # the opponent's action_chain THIS turn (public: mode/to/cards of every play)
		self.board_drops = []        # engineers' drop tokens on the board [{'cell','kind','owner'}] (public)
		self.drop_tokens = {}         # C #23: pet_trap tokens {cell: count} (public - knockback per token on arrival)
		self.oppo_dwelling = None    # C #24: the opponent's dwelling card name (public board furniture, wrecking_ball target)
		self.nobodymoves_active = False  # nobodymoves lock active this turn (game-level, public)
		# G #37 information policy: FAIR PLAY by default - every decision uses PUBLIC info + my own state
		# only. hard_mode is an explicit opt-in (testing / difficulty tier): when True the robot may also
		# read the opponent's hidden hand to sharpen threat detection. See AGENTS.md "Information policy".
		self.hard_mode = False
		self._hard_oppo_win = None   # G #37 hard mode ONLY: a card in THEIR hidden hand that could win them this turn
		# G #38 opponent modeling from the PUBLIC turn log (last 5 turns): what they actually declared
		self.oppo_history = []       # [{'mode': move|defend, 'to', 'cards'}] of the opponent's past plays
		# A.3 threat estimate: opponent's visible placements THIS turn + win-cost curves from the pool
		self.oppo_pending_slots = []     # opponent's pending placeholders [[card_name, slot], ...] (public board furniture)
		self._win_cost_curves = {}       # bonus flag -> [d] = cheapest pool card that could move >= d cells
		CARDS_DB_PATH = os.path.join(os.path.dirname(__file__), '../cards/cardpool.parquet')
		self.CARDS_DB = pl.read_parquet(CARDS_DB_PATH)
		# O(1) main-pool lookup by card_id (rows as dicts)
		self.MAIN_ROWS = {r['card_id']: r for r in self.CARDS_DB.iter_rows(named=True)}
		# support-faction cards (engineers/mages/doctors) - NOT in cardpool. The robot may
		# put ANY card in hand into the mana zone, and since A.1 it can also PLAY them:
		# SUPPORT_INFO maps card_name -> {faction, cost, kind} with kind in
		# {'drop','dwelling','pending','instant_mage'} (None = unknown support -> never played).
		SUPPORT_DB_PATH = os.path.join(os.path.dirname(__file__), '../cards/support_factions.parquet')
		self.SUPPORT_DB = pl.read_parquet(SUPPORT_DB_PATH) if os.path.exists(SUPPORT_DB_PATH) else pl.DataFrame()
		self.SUPPORT_INFO = {}
		if self.SUPPORT_DB is not None and not self.SUPPORT_DB.is_empty():
			for r in self.SUPPORT_DB.iter_rows(named=True):
				name = r['card_name']
				if name in ENGINEER_DROPS:
					kind = 'drop'
				elif name in DWELLING_CARDS:
					kind = 'dwelling'
				elif name in DOCTOR_PENDING:
					kind = 'pending'
				elif name in MAGE_INSTANT_CARDS:
					kind = 'instant_mage'
				else:
					kind = None   # unknown support card - defensive, never played
				self.SUPPORT_INFO[name] = {
					'faction': r['support_faction_name'],
					'cost': int(r['mana_cost']),
					'kind': kind,
				}

	def _support_value(self, name):
		"""Utility of playing/keeping this support card RIGHT NOW (same scale for put_mana and
		choose_support_play). A.2-A.4 replaced the A.1 baselines with situational tactics per branch;
		§B/§C refine them further (condition-aware values, opponent modeling)."""
		me = self.player_state
		if name in ENGINEER_DROPS:
			# A.4 #16: drops are cheap setup, but their value is situational - a self-buff only pays
			# while MY token will actually step on the cell (and before the opponent can), and denial
			# only pays while the OPPONENT's token is about to land there.
			if name == 'landmine':
			# A.4 #16 landmine: endgame denial - worth a lot ONLY while the opponent can plausibly
			# finish this turn (declared winning move, or cell >= 20 with cards in hand) - else dead weight.
				return 45 if self._oppo_finish_threat() else 18
			cell = self._choose_drop_cell(name)
			if cell is None:
				return 6   # no valid target right now (occupied/busy, or nobody to trap) - sacrifice candidate
			v = 20
			if name in ('boost', 'trampoline'):
				if cell in self._my_reach_cells():
					v += 6   # my token steps on it THIS turn -> immediate +2 tempo
			elif cell in self._oppo_reach_cells():
				v += 8   # gluetrap: the opponent is about to land on it this turn
			return v
		if name in DWELLING_CARDS:
			# a dwelling is a long-term engine; the 2nd copy is only useful after a wrecking_ball
			return 50 if not (me.dwelling) else 4
		if name in DOCTOR_PENDING:
			# bloodtest = 18: the OPPONENT discards 1 card (a discard_oppo effect) - tempo + denial,
			# no longer self-penalizing; virus = 15: -1 knockback to me, still self-penalizing.
			base = {'epo': 30, 'mercurochrome': 29, 'bloodtest': 18, 'virus': 15}[name]
			# A.2 #7/#9: the FIRST card into an empty zone is worth more when my deck actually has
			# `pending`-condition mains to unlock (they are always-not-met for me while the zone is empty)
			if not (self.player_state.pendings or []) and self._pending_condition_count() > 0:
				base += 6
			return base
		if name == 'Celestial_reversal':
			# strong permanent lever ONLY when a strictly better phase exists for my own deck
			return 45 if self._choose_day_night() is not None else 12
		if name == 'thermic_flux':
			return 42 if self._choose_temp_change() is not None else 8
		if name == 'nobodymoves':
			return 48 if self._nobodymoves_threat() else 6
		if name == 'Apocalypticritual':
			return 40 if self._choose_cataclysm_order() is not None else 12
		return 5

	def _mana_keep_value(self, cid):
		"""Baseline keep-value of a hand card for MANA purposes (higher = worth keeping in
		hand; put_mana sacrifices the lowest values first). A.1 baseline - sections B/C
		replace it with a real value model:
		  * main cards: expensive ones sacrificed first (previous behavior);
		  * support cards: their current play-utility (_support_value) - dead copies
		    (2nd dwelling with the zone full, a useless instant...) sink below mains,
		    playable ones stay in hand.
		"""
		row = self.MAIN_ROWS.get(cid)
		if row is not None:
			return 18 - int(row['mana'])   # m5=13 ... m1=17 (high mana sacrificed first, as before)
		info = self.SUPPORT_INFO.get(cid)
		if info is None or info['kind'] is None:
			return 0.0                     # unknown support card - pure dead weight
		return self._support_value(cid)

	def put_mana(self, num_cards=3, in_turn=False):
		"""Select `num_cards` from the hand to put into the mana zone.

		The robot may put ANY card in hand (main OR support) - the engine accepts both and
		each card counts as 1 mana - but a mana-zone card is never played again (except via
		taxation), so the selection is a SACRIFICE: lowest keep-value first (_mana_keep_value).
		Since A.1 support cards are no longer burned unconditionally: playable support stays
		in hand, dead copies and expensive mains go to mana.

		Returns exactly `num_cards` cards whenever the hand has that many (this is what lets
		the init phase put its mandatory 3 in one valid message even when the hand is a
		main+support mix). An empty hand returns [] (init) or a pass."""
		hand = list(self.player_state.hand or [])
		if not hand:
			if in_turn:  # empty hand: nothing to put in mana, pass instead
				return {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []}
			return []
		ranked = sorted(hand, key=self._mana_keep_value)   # stable sort: ties keep hand order
		top = ranked[:num_cards]
		if in_turn:
			return {
				'cards': top,      # cards selected - LIST
				'to': 'mana',      # destination - STRING [stopover_x, mana, pending_zone, dwelling, discard_pile]
				'mode': '',        # mode - STRING ['', move, defend, dwelling_activation, pass]
				'pendings': []     # cards in pendings zone to add to a normal move card - LIST
			}
		return top

	# ------------------------------------------------------------------
	# Support-faction support (A.1) - baseline choices + message builders.
	# build_support_message / tap_dwelling are the SINGLE dispatch point for every
	# support play (mirror of the frontend's dispatchPlay branch order); each returns
	# a complete engine-valid message or None when the card is not currently playable.
	# The choice functions below are BASELINE heuristics - sections A.2-A.4 refine them.
	# ------------------------------------------------------------------

	def _my_position(self):
		return self.player_state.current_position or 0

	def _earth_biome(self, pos):
		"""Biome code of a cell index from public board info (None = unknown)."""
		eb = self.earth_biomes
		if not eb or pos is None:
			return None
		return eb[int(pos) % len(eb)]

	def _my_reach_cells(self):
		"""A.4 #16: cells my token will likely step on THIS turn (pos+1 .. pos+reach), from public info +
		my own declared/planned plays. Declared move plays resolve one after another, so their estimates sum;
		if I have not declared a move yet, the best estimate of an AFFORDABLE main in hand stands in for it
		(what play_card would likely commit to next). Empty when my movement is locked this turn."""
		if self._movement_locked():
			return []
		pos = self._my_position()
		reach = 0
		for a in (self.player_state.action_chain or []):
			if (a or {}).get('mode') == 'move' and (a or {}).get('cards'):
				row = self.MAIN_ROWS.get(a['cards'][0])
				if row is not None:
					reach += max(0, int(self._move_estimate(row)))
		if reach <= 0:
			mana_available = len(self.player_state.mana or []) - (self.player_state.mana_spend or 0)
			for cid in (self.player_state.hand or []):
				row = self.MAIN_ROWS.get(cid)
				if row is None or int(row['mana']) > mana_available:
					continue
				reach = max(reach, max(0, int(self._move_estimate(row))))
		if reach <= 0:
			return []
		return list(range(pos + 1, min(pos + reach, 23) + 1))

	def _oppo_reach_cells(self):
		"""A.4 #16: cells the OPPONENT's token will likely step on THIS turn (public info only): the sum of
		the potential advances of their declared move plays; when they have not declared one yet - or it is
		suppressed by this turn's movement lock - a typical range 1-4 ahead of them."""
		if self.oppo_position is None or self.oppo_position >= 24:
			return []
		pos = self.oppo_position
		reach = 0
		if not self.nobodymoves_active:
			for a in (self.oppo_actions or []):
				if (a or {}).get('mode') == 'move' and (a or {}).get('cards'):
					row = self.MAIN_ROWS.get(a['cards'][0]) if len(a['cards']) else None
					if row is not None:
						reach += max(0, int(self._oppo_potential_adv(row)))
		if reach <= 0:
			return list(range(pos + 1, min(pos + 4, 23) + 1))   # typical advance range 1..4 ahead
		return list(range(pos + 1, min(pos + reach, 23) + 1))

	def _oppo_finish_threat(self):
		"""A.4 #16: can the opponent plausibly cross cell 23 THIS turn (public estimate; no self-deny guard -
		I only use it to value denial drops)? A declared move play whose potential reach crosses the finish,
		or they stand at cell >= 20 with cards left in hand."""
		if self.oppo_position is None:
			return False
		pos = self.oppo_position
		for a in (self.oppo_actions or []):
			if (a or {}).get('mode') == 'move' and (a or {}).get('cards'):
				row = self.MAIN_ROWS.get(a['cards'][0]) if len(a['cards']) else None
				if row is not None and pos + max(0, int(self._oppo_potential_adv(row))) >= 24:
					return True
		if pos >= 20 and (self.oppo_hand or 0) >= 1:
			return True
		return False

	def _choose_drop_cell(self, name):
		"""A.4 #16: target cell for an engineer drop (move mode + `cell`). The token fires on the FIRST
		arrival of ANY token (stepping or a jump landing) and is then consumed; knockbacks do not trigger it;
		a placement on an occupied cell stays dormant until that token leaves AND returns. So:
		  boost/trampoline (self-buff): prefer a cell my own predicted path crosses THIS turn (immediate +2),
		      else 1-6 cells ahead of me; skip cells the opponent reaches strictly SOONER than I do (they would
		      consume it first - tokens only move forward, so any cell BEHIND their token is always safe).
		  gluetrap/landmine (denial): the EARLIEST cell of the opponent's predicted path, so the trap fires
		      before they have advanced far; NEVER on my own predicted path - whoever arrives FIRST eats the
		      drop, and my declared moves may well resolve first (a landmine there would block ME).
		Never an occupied cell or one that already carries a drop token. None when no valid target -> skip."""
		my_pos = self._my_position()
		busy = set(self.occupied_cells) | {d.get('cell') for d in (self.board_drops or []) if isinstance(d, dict)}
		if name in ('boost', 'trampoline'):
			ordered, seen = [], set()
			for src in (self._my_reach_cells(), range(my_pos + 1, min(my_pos + 7, 24))):   # on-path cells first
				for c in src:
					if c <= my_pos or c in seen or c in busy:
						continue
					seen.add(c)
					if (self.oppo_position is not None and self.oppo_position < c
						and (c - self.oppo_position) < (c - my_pos)):
						continue   # the opponent reaches it strictly sooner -> they would consume it first
					ordered.append(c)
			return ordered[0] if ordered else None
		# gluetrap / landmine - denial on the opponent's path, never on my own
		my_reach = set(self._my_reach_cells())
		for c in self._oppo_reach_cells():
			if c == my_pos or c in busy or c in my_reach:
				continue
			return c
		return None

	def _cond_delta(self, row):
		"""A.3 value model: the marginal forward-movement GAIN of making this card's condition
		met = advancing − (mana − 1), its reduced not-met fallback move. Negative for cards whose
		not-met fallback moves them further than their full advance (their EFFECT still fires when
		met - unknown value, §C refines)."""
		return int(row['advancing'] or 0) - (int(row['mana']) - 1)

	def _choose_day_night(self):
		"""A.3 #11: fix the day/night phase that makes more of my own day/night-condition mains
		MET, weighted by marginal value (_cond_delta) instead of raw counts. The robot knows its
		full hand+deck; fixing the CURRENT phase also keeps every unplayed card of it met for the
		rest of THIS turn's plays ("free"), so a strictly better current phase is played immediately.
		Returns 'day'/'night', or None when neutral / already locked to my preference."""
		w = {'day': 0, 'night': 0}
		for cid in (self.player_state.hand or []) + (self.player_state.deck or []):
			row = self.MAIN_ROWS.get(cid)
			if row is None:
				continue
			if row['condition'] == 'day':
				w['day'] += self._cond_delta(row)
			elif row['condition'] == 'night':
				w['night'] += self._cond_delta(row)
		if w['day'] == w['night']:
			return None   # neutral for me - don't spend the card
		preferred = 'day' if w['day'] > w['night'] else 'night'
		if preferred == self.day_night and self.day_night_fixed:
			return None   # already locked to my preference - nothing left to gain
		return preferred

	def _temp_weight(self, temp):
		"""A.3 #12: total marginal value of my OWN temp_* mains (hand+deck) that would be MET at
		`temp`, weighted by _cond_delta. LEVEL-based on purpose: comparing candidate temperatures
		against each other automatically prices in the cards that would become NOT met."""
		w = 0
		for cid in (self.player_state.hand or []) + (self.player_state.deck or []):
			row = self.MAIN_ROWS.get(cid)
			if row is None:
				continue
			m = re.match(r'^temp_(inf|sup)_(\d+)$', row['condition'])
			if not m:
				continue
			t = int(m.group(2))
			if (temp < t if m.group(1) == 'inf' else temp > t):
				w += self._cond_delta(row)
		return w

	def _choose_temp_change(self):
		"""A.3 #12: pick the direction whose resulting temperature (clamped 1..20, like the engine)
		has a strictly higher OWN weighted met-total than now; None when neither improves - the card
		then stays in hand for a future flux. Opponent conditions are unknown under fair play."""
		T = self.temperature
		if T is None:
			return None
		cur = self._temp_weight(T)
		best, best_dir = cur, None
		for d in ('up', 'down'):
			t2 = max(1, min(20, T + (4 if d == 'up' else -4)))
			c = self._temp_weight(t2)
			if c > best:   # strict '>' : a tie keeps the previous value (deterministic)
				best, best_dir = c, d
		return best_dir

	def _cataclysm_soon(self):
		"""A.3 #14 gate: do I actually have a cataclysm card resolving SOON - an affordable
		cataclysm-condition main in hand, or my own already-declared (unresolved) move play with
		the condition this turn? Without one the reorder has no near-term value."""
		me = self.player_state
		mana_available = len(me.mana or []) - (me.mana_spend or 0)
		for cid in (me.hand or []):
			row = self.MAIN_ROWS.get(cid)
			if row is not None and row['condition'] == 'cataclysm' and int(row['mana']) <= mana_available:
				return True
		for a in (me.action_chain or []):
			if a.get('mode') != 'move':
				continue
			for cid in (a.get('cards') or []):
				row = self.MAIN_ROWS.get(cid)
				if row is not None and row['condition'] == 'cataclysm':
					return True
		return False

	def _biome_knockback(self, pos, biom):
		"""A.3 #14 public estimate of how many cells the token at `pos` is knocked back if biome
		`biom` strikes (tokens land on their segment's FIRST cell). 0 when the token is not on it."""
		if pos is None or pos < 0 or pos >= 24 or not self.earth_biomes:
			return 0
		try:
			if self._earth_biome(pos) != biom:
				return 0
		except IndexError:
			return 0
		for i, b in enumerate(self.earth_biomes):
			if b == biom:
				return max(0, pos - i)
		return 0

	def _pile_value(self, order):
		"""A.3 #14 public estimate of a strike order's relative value (higher = better). A strike
		knocks EVERY token on that biome back to the segment start, so one strike is worth
		`opponent_knockback - my_knockback` cells (both positions are public). Earlier strikes weigh
		more: the game may end before later ones land and positions drift between them."""
		me_pos = self._my_position() if self.player_state is not None else None
		total = 0
		for i, biom in enumerate(list(order)[:4]):
			weight = 4 - i   # the first strike counts most
			total += weight * (self._biome_knockback(self.oppo_position, biom)
			                   - self._biome_knockback(me_pos, biom))
		return total

	def _choose_cataclysm_order(self):
		"""A.3 #14: reorder the pile ONLY when a strike is coming (_cataclysm_soon) AND some order
		strictly improves over the current one (otherwise 3 mana are wasted). All 24 permutations are
		scored exactly under _pile_value, which handles shared biomes, empty biomes and how deep each
		token sits in its segment uniformly."""
		if not self._cataclysm_soon():
			return None
		import itertools as _it
		pile = list(self.cataclysm_pile or BIOMES)
		if len(pile) < 4:
			return None
		cur_val = self._pile_value(pile)
		best, best_val = None, cur_val
		for cand in _it.permutations(pile):
			v = self._pile_value(cand)
			if v > best_val:
				best, best_val = list(cand), v
		return best

	def _min_win_cost(self, need, bonus):
		"""A.3 #13: cheapest pool card that could move a token at least `need` cells forward when
		standing with biome-bonus flag `bonus` (0/1) - precomputed once from the full pool.
		Worst case for me per card: its full advancing path if the condition is met, else the
		reduced mana−1 advance. None = no pool card can do it."""
		key = int(bonus)
		if key not in self._win_cost_curves:
			curve = [None] * 25
			for row in self.MAIN_ROWS.values():
				adv = int(row['advancing'] or 0)
				mana = int(row['mana'])
				met_path = adv + (1 if (key and adv > 0) else 0)
				if row['effect'] == 'backward':
					met_path = 0   # recoil - never wins
				elif row['effect'] == 'advancing':
					met_path += int(row.get('effect_number') or 0)
				best_adv = max(met_path, mana - 1)
				if best_adv <= 0:
					continue
				for d in range(1, min(best_adv, 24) + 1):
					c = curve[d]
					if c is None or mana < c:
						curve[d] = mana
			self._win_cost_curves[key] = curve
		return self._win_cost_curves[key][max(0, min(int(need), 24))]

	def _oppo_condition_eval(self, cond):
		"""A.3 #13: evaluate an OPPONENT card's condition from PUBLIC info only (their position,
		the global board facts). True/False when decidable; None when it depends on hidden state
		(above all their pending zone) - the caller then assumes MET for threat purposes."""
		if cond == 'no_condition':
			return True
		m = re.match(r'^temp_(inf|sup)_(\d+)$', cond)
		if m:
			if self.temperature is None:
				return None
			t = int(m.group(2))
			return self.temperature < t if m.group(1) == 'inf' else self.temperature > t
		if cond in ('day', 'night'):
			return self.day_night is not None and self.day_night == cond
		fac = BIOME_COND_FACTION.get(cond)
		if fac:
			if self.oppo_position is None or not self.earth_biomes:
				return None
			biome = self._earth_biome(self.oppo_position)
			home = FACTION_BIOMES.get(fac)
			if biome is None or not home:
				return None
			return biome in home
		dm = re.match(r'^dist_(ahead|behind)_sup_(\d+)$', cond)
		if dm and self.oppo_position is not None:
			thr = int(dm.group(2))
			# from THEIR perspective: dist_ahead = they are ahead of ME by more than thr
			return ((self.oppo_position - self._my_position()) > thr if dm.group(1) == 'ahead'
					else (self._my_position() - self.oppo_position) > thr)
		if cond == 'mana_inf_6':
			return self.oppo_mana is not None and self.oppo_mana < 6
		if cond == 'mana_sup_5':
			return self.oppo_mana is not None and self.oppo_mana > 5
		if cond == 'cards_in_hand_inf_4':
			return self.oppo_hand is not None and self.oppo_hand < 4
		if cond == 'cards_in_hand_sup_3':
			return self.oppo_hand is not None and self.oppo_hand > 3
		if cond == 'drop_on_board':
			return bool(self.drops_on_board)
		if cond == 'cataclysm':
			return True   # trigger condition: always met (engine semantics)
		return None   # pending / opponent-zone conditions / others - hidden

	def _oppo_potential_adv(self, row):
		"""A.3 #13: worst-case forward cells of an OPPONENT move card from their current position.
		Condition decidable and NOT met -> the reduced mana−1 advance; decidable and met (or unknown
		-> assume it could be) -> full path = advancing + their biome bonus at their cell +
		'advancing'-effect cells. 'backward' cards recoil when met."""
		adv = int(row['advancing'] or 0)
		mana = int(row['mana'])
		met_path = adv
		if self.oppo_position is not None and self.earth_biomes:
			home_oppo = FACTION_BIOMES.get(self.oppo_faction) if self.oppo_faction else None
			if home_oppo and adv > 0 and self._earth_biome(self.oppo_position) in home_oppo:
				met_path += 1   # their faction-biome bonus - public (position + earth)
		if row['effect'] == 'backward':
			met_path = 0
		elif row['effect'] == 'advancing':
			met_path += int(row.get('effect_number') or 0)
		unmet_path = mana - 1
		ev = self._oppo_condition_eval(row['condition'])
		if ev is True:
			return met_path
		if ev is False:
			return unmet_path
		return max(met_path, unmet_path)   # unknown -> the scenario worse for me

	def _my_shields_at(self, to):
		"""Sum of MY declared defend shields on stopover column `to` this turn (public - my own chain)."""
		total = 0
		for a in (self.player_state.action_chain or []):
			if not (a.get('mode') == 'defend' and a.get('to') == to):
				continue
			for cid in (a.get('cards') or []):
				row = self.MAIN_ROWS.get(cid)
				total += int(row['shield']) if row else 0
		return total

	def _oppo_visible_spend(self):
		"""A.3 #13: mana the opponent visibly spent THIS turn (public): every declared play's cost
		+ their pending placements' costs. Their true spend can only be higher (a dwelling placement
		is indistinguishable) - the estimate is used to LOWER my threat assumption, conservatively."""
		spent = 0
		for a in (self.oppo_actions or []):
			for cid in (a.get('cards') or []):
				row = self.MAIN_ROWS.get(cid)
				if row is not None:
					spent += int(row['mana'])
				else:
					info = self.SUPPORT_INFO.get(cid)
					spent += info['cost'] if (info is not None and info['kind']) else 5   # unknown -> assume the max
		for entry in (self.oppo_pending_slots or []):
			name = entry[0] if isinstance(entry, (list, tuple)) and entry else None
			info = self.SUPPORT_INFO.get(name)
			if info:
				spent += info['cost']
		return spent

	def _nobodymoves_threat(self):
		"""A.3 #13: play nobodymoves when the OPPONENT can still win this turn AND I cannot -
		it locks ALL players' movement, so it must never self-deny my own winning move.
		Public estimate only (their position/hand/mana counts + their declared plays):
		* DECLARED threat: one of their unresolved MOVE plays has a worst-case advance reaching
		  cell 24 from their position - unless MY defends on that stopover out-shield its cost
		  (an unstoppable card whose condition is not provably unmet ignores the block);
		* LATENT threat: they can still act this turn (cards left in hand) and hold enough visible
		  mana to afford some pool card whose worst-case advance wins from their position."""
		if self.oppo_position is None or not self.earth_biomes:
			return False
		if self.nobodymoves_active:
			return False   # already locked this turn - a 2nd copy buys nothing
		oppo_pos = self.oppo_position
		need = 24 - oppo_pos
		if need <= 0:
			return True   # shouldn't happen (the game would be over)
		# 1) never self-deny: an affordable main in hand could win ME the game this turn
		me_pos = self._my_position()
		mana_available = len(self.player_state.mana or []) - (self.player_state.mana_spend or 0)
		for cid in (self.player_state.hand or []):
			row = self.MAIN_ROWS.get(cid)
			if row is None or int(row['mana']) > mana_available:
				continue
			if self._condition_met(row['condition']):
				best = max(self._move_estimate(row), int(row['mana']) - 1)
			else:
				best = int(row['mana']) - 1
			if best >= 24 - me_pos:
				return False
		# 2) their already-declared unresolved move plays (conditions from public facts only)
		for a in (self.oppo_actions or []):
			if a.get('mode') != 'move':
				continue
			to = a.get('to')
			my_shields = self._my_shields_at(to) if to else 0
			for cid in (a.get('cards') or []):
				row = self.MAIN_ROWS.get(cid)
				if row is None:
					continue
				blocked = my_shields >= int(row['mana'])
				unstoppable = (row['effect'] == 'unstoppable'
						and self._oppo_condition_eval(row['condition']) is not False)
				if (not blocked or unstoppable) and self._oppo_potential_adv(row) >= need:
					return True
		# 3) they can still DECLARE a winning card this turn
		if (self.oppo_hand or 0) < 1:
			return False
		home_oppo = FACTION_BIOMES.get(self.oppo_faction) if self.oppo_faction else None
		bonus = 1 if (home_oppo and self._earth_biome(oppo_pos) in home_oppo) else 0
		min_cost = self._min_win_cost(need, bonus)
		if min_cost is None:
			return False
		running_mana = (self.oppo_mana or 0) - self._oppo_visible_spend()
		if running_mana < min_cost:
			return False
		if self.hard_mode:
			# G #37 perfect info: the pool curve says "some card COULD" - their ACTUAL hand decides.
			# _hard_oppo_win is None unless a specific winning card sits in their hidden hand (and is
			# affordable within their visible zone), so hard mode only ever REMOVES false positives.
			return self._hard_oppo_win is not None
		return True

	def _find_hard_win_card(self, oppo_state):
		"""G #37 (HARD MODE ONLY - reads the opponent's hidden hand): first card in their hand that could
		win them the game this turn from their position (worst-case advance >= need, affordable within
		their visible mana zone). None = no winning card in hand."""
		if self.oppo_position is None:
			return None
		need = 24 - self.oppo_position
		if need <= 0:
			return 'any'   # degenerate - the game should already be over
		zone = len(getattr(oppo_state, 'mana', None) or [])
		for cid in (getattr(oppo_state, 'hand', None) or []):
			row = self.MAIN_ROWS.get(cid)
			if row is not None and int(row['mana']) <= zone and self._oppo_potential_adv(row) >= need:
				return cid
		return None

	def _oppo_history_moves(self):
		"""G #38: MAIN rows of the MOVE cards the opponent actually played in recent turns (public log)."""
		rows = []
		for h in self.oppo_history or []:
			if h.get('mode') != 'move':
				continue
			for cid in (h.get('cards') or []):
				row = self.MAIN_ROWS.get(cid)
				if row is not None:
					rows.append(row)
		return rows

	def _oppo_defend_rate(self):
		"""G #38: fraction of the opponent's recent declared actions that were DEFENDS (public log).
		A defensive opponent keeps reacting to my declared moves, so a column without visible shields
		yet still carries real block risk (plays alternate - they act right after me)."""
		moves = sum(1 for h in self.oppo_history or [] if h.get('mode') == 'move')
		defs = sum(1 for h in self.oppo_history or [] if h.get('mode') == 'defend')
		tot = moves + defs
		return (defs / tot) if tot else 0.0

	def _biome_cond_weight(self, biome_code):
		"""A.3 #15: total marginal value of my OWN biome_*-condition mains (hand+deck) that would
		be MET while standing on a cell of `biome_code` (weight = _cond_delta)."""
		w = 0
		for cid in (self.player_state.hand or []) + (self.player_state.deck or []):
			row = self.MAIN_ROWS.get(cid)
			if row is None:
				continue
			fac = BIOME_COND_FACTION.get(row['condition'])
			if fac and biome_code in FACTION_BIOMES[fac]:
				w += self._cond_delta(row)
		return w

	def _choose_rotation(self):
		"""A.3 #15: score cw vs ccw by what each rotation does for ME - the faction-biome bonus I
		keep (+2, it recurs every turn) + my biome_*-condition mains that become met at my new cell
		(_biome_cond_weight, scaled 0.5 - §C refines) - MINUS the opponent's kept bonus (−2). Ties ->
		'cw'. Engine convention: after a CW tap the biome on cell i is old[(i-3) % 24]; CCW uses
		old[(i+3) % 24]. The rotation is cumulative, so with a free tap per turn this tracks my
		position and keeps me on-bonus nearly every turn."""
		eb = self.earth_biomes
		if not eb:
			return 'cw'
		my_pos = self._my_position()
		home_me = FACTION_BIOMES.get(getattr(self.player_state, 'faction', None))
		home_oppo = FACTION_BIOMES.get(self.oppo_faction) if self.oppo_faction else None

		def score(d):
			k = -3 if d == 'cw' else 3   # biome landing on cell i after the tap: old[(i+k) % 24]
			s = 0.0
			new_me = eb[(my_pos + k) % len(eb)]
			if home_me and new_me in home_me:
				s += 2.0
			if new_me is not None:
				s += 0.5 * self._biome_cond_weight(new_me)
			if (home_oppo and self.oppo_position is not None
					and eb[((self.oppo_position % len(eb)) + k) % len(eb)] in home_oppo):
				s -= 2.0   # take the opponent off its bonus biome if possible
			return s

		scw, sccw = score('cw'), score('ccw')
		return 'cw' if scw >= sccw else 'ccw'

	def build_support_message(self, name):
		"""Complete engine-valid message for PLAYING this support card (or None when it is not
		currently playable: unknown/unaffordable/no valid choice or target cell/dwelling zone
		already full). The caller fills the stopover of a move-mode play via ge._player_stopover.
		Message shapes: drops/mages -> mode 'move' + required choice field ('cell', 'day_night',
		'temp_change', 'cataclysm_order'); dwellings -> to 'dwelling'; pendings -> to 'pending_zone'."""
		info = self.SUPPORT_INFO.get(name)
		if info is None or info['kind'] is None or name not in (self.player_state.hand or []):
			return None
		mana_available = len(self.player_state.mana or []) - (self.player_state.mana_spend or 0)
		if info['cost'] > mana_available:
			return None
		me = self.player_state
		if info['kind'] == 'dwelling':
			if me.dwelling:   # engine rejects a 2nd dwelling while one is on the board
				return None
			return {'cards': [name], 'to': 'dwelling', 'mode': '', 'pendings': []}
		if info['kind'] == 'pending':
			return {'cards': [name], 'to': 'pending_zone', 'mode': '', 'pendings': []}
		# move-mode cards (drops + instant mages): the stopover is a placeholder, filled by the caller
		msg = {'cards': [name], 'to': 'stopover_x', 'mode': 'move', 'pendings': []}
		if info['kind'] == 'drop':
			cell = self._choose_drop_cell(name)
			if cell is None:
				return None
			msg['cell'] = cell
			return msg
		# instant mages - required choice fields (A.3 refines the choices themselves)
		if name == 'Celestial_reversal':
			dn = self._choose_day_night()
			if dn is None:
				return None
			msg['day_night'] = dn
		elif name == 'thermic_flux':
			tc = self._choose_temp_change()
			if tc is None:
				return None
			msg['temp_change'] = tc
		elif name == 'Apocalypticritual':
			order = self._choose_cataclysm_order()
			if order is None:
				return None
			msg['cataclysm_order'] = order
		# nobodymoves needs no choice field
		return msg

	def tap_dwelling(self):
		"""Message to TAP my dwelling (free QUICK action, once per turn): refinery draws 1,
		laboratory adds an 'epo' pending card, black_hole rotates the earth (needs a direction).
		None when there is nothing to tap / it was already tapped this turn."""
		me = self.player_state
		if not me.dwelling or me.dwelling_tapped:
			return None
		msg = {'cards': [], 'to': 'dwelling', 'mode': 'dwelling_activation', 'pendings': []}
		if me.dwelling == 'black_hole':
			msg['rotation'] = self._choose_rotation()
		return msg

	def play_nobodymoves_threat(self):
		"""Threat response: play nobodymoves when the opponent threatens to win this turn.
		Must happen BEFORE I commit my own movement (it locks ALL players' movement, me included).
		Returns a complete move message or None."""
		if not self._nobodymoves_threat():
			return None
		return self.build_support_message('nobodymoves')

	def choose_support_play(self):
		"""Play-phase FALLBACK: no main move card is playable/worth it -> spend the leftover
		mana on my best support card (baseline value order; A.2-A.4 refine). 'nobodymoves' is
		excluded here - it only makes sense as the threat response above (played after I have
		moved, it would lock my own declared cards too). Returns a complete message or None."""
		cands = [n for n in (self.player_state.hand or [])
			       if self.SUPPORT_INFO.get(n) and self.SUPPORT_INFO[n]['kind'] is not None
			       and n != 'nobodymoves']
		cands.sort(key=lambda n: (-self._support_value(n), self.SUPPORT_INFO[n]['cost'], n))
		for name in cands:
			msg = self.build_support_message(name)
			if msg is not None:
				return msg
		return None

	def _condition_met(self, condition):
		"""Mirror of game_engine.is_condition_met. Since section B (#19) EVERY pool condition is
		evaluable - biome_* included, from public board info only; no pool condition depends on hidden
		contents (the *_oppo ones are counts). Default for any unknown condition: MET (the engine's
		'canonical catch-all').
		
		Section B #20 note (play phase vs resolution time): the engine evaluates a card's condition at
		RESOLUTION, this mirror scores during the PLAY phase. The only global values that can change
		between my declaration and its resolution are day/night and temperature - and Celestial_reversal /
		thermic_flux are INSTANT (applied at play time), so a fresh update_player_state each tick already
		reflects any change made earlier THIS turn, by me or the opponent. No projection is needed for my
		own plays; the only residual uncertainty is an opponent instant I cannot see yet (hidden hand -
		unknowable under fair play, #21). A drop placed this turn makes drop_on_board met at resolution:
		board_drops are public and re-read every tick. """
		if condition == 'no_condition':
			return True
		if condition == 'cataclysm':
			# trigger condition: the engine fires the cataclysm strike when the card
			# resolves; the condition itself is always treated as met (effect fires).
			# The AI cannot evaluate the strike (hidden pile) and doesn't need to.
			return True
		if condition == 'block':
			# a defend-card MARKER, not a state to evaluate. Engine fix (2026-09-22, bug in game
			# 26_09_22_20_55_31_VESGf): is_condition_met now returns False for 'block' - it is only
			# ever evaluated on the MOVE path (defend plays return before condition evaluation), so
			# a block-condition card played in move mode is NOT met: no effect, reduced advancing
			# (mana - 1). Its effect fires only when played in defend mode AND it actually blocks.
			return False
		if 'biome' in condition:
			# B #19 (P0 fix): biome_X conditions are fully evaluable from PUBLIC info - my cell's board
			# layout (earth_biomes) + FACTION_BIOMES. Exact engine mirror: met iff the biome of the cell I
			# stand on is one of faction X's two home biomes. Unknown biome_* name -> permissive default
			# (MET), like the engine; no board info at all -> not met (conservative).
			fac = BIOME_COND_FACTION.get(condition)
			if fac is None:
				return True
			biome = self._earth_biome(self.player_state.current_position or 0) if self.earth_biomes else None
			home = FACTION_BIOMES.get(fac)
			if biome is None or not home:
				return False
			return biome in home
		if condition == 'mana_inf_6':
			return len(self.player_state.mana) < 6
		if condition == 'mana_sup_5':
			return len(self.player_state.mana) > 5
		if condition == 'mana_inf_6_oppo':
			return self.oppo_mana is not None and self.oppo_mana < 6
		if condition == 'mana_sup_5_oppo':
			return self.oppo_mana is not None and self.oppo_mana > 5
		if condition == 'cards_in_hand_inf_4':
			return len(self.player_state.hand) < 4
		if condition == 'cards_in_hand_sup_3':
			return len(self.player_state.hand) > 3
		if condition == 'cards_in_hand_inf_4_oppo':
			return self.oppo_hand is not None and self.oppo_hand < 4
		if condition == 'cards_in_hand_sup_3_oppo':
			return self.oppo_hand is not None and self.oppo_hand > 3
		temp_match = re.match(r'^temp_(inf|sup)_(\d+)$', condition)
		if temp_match:
			if self.temperature is None:
				return False   # temperature unknown -> treat as not met
			threshold = int(temp_match.group(2))
			return self.temperature < threshold if temp_match.group(1) == 'inf' else self.temperature > threshold
		if condition in ('day', 'night'):
			return self.day_night == condition
		if condition == 'drop_on_board':
			# any drop/trap on the earth, computed in update_player_state from the
			# public board state. Unknown (no board info) -> not met, like the other
			# board-dependent conditions (biome).
			return bool(self.drops_on_board)
		if condition == 'pending':
			# pending: met iff at least one pending card in the player's OWN pending
			# zone (the doctors' pending zone). Since A.1 the robot can fill its own
			# zone (doctors' pending placement / laboratory taps), so this reads the
			# live value - it is met exactly when the zone is non-empty.
			return len(self.player_state.pendings or []) >= 1
		dist_match = re.match(r'^dist_(ahead|behind)_sup_(\d+)$', condition)
		if dist_match:
			if self.oppo_position is None:
				return False   # opponent position unknown -> treat as not met
			gap = self.player_state.current_position - self.oppo_position
			threshold = int(dist_match.group(2))
			return gap > threshold if dist_match.group(1) == 'ahead' else -gap > threshold
		# any other not-yet-implemented condition: assume met (canonical default,
		# same as the engine's is_condition_met catch-all)
		return True

	# ------------------------------------------------------------------
	# A.2 - Doctors: pending-zone attachment policy (a main MOVE play may carry pendings: [name])
	# The engine CONSUMES an attached pending either way (removed from the zone at PLAY time,
	# flushed to the discard at resolution) and its effect fires ONLY when the main card's
	# condition is met AND not blocked/canceled - so we attach only when the effect can fire.
	# ------------------------------------------------------------------
	def _my_next_stopover(self):
		"""Mirror of ge._player_stopover(game, my_name, play_count): the stopover column my NEXT
		move/defend play will occupy (my own rooted cards from last turn take the leading positions)."""
		name = self.player_state.name
		rooted = sum(1 for r in (self.rooted_on_board or []) if (r or {}).get('owner') == name)
		pos = rooted + (self.player_state.play_count or 0) + 1
		return f'stopover_{max(5 - pos, 0)}'

	def _oppo_shields_at(self, to):
		"""Sum of the shield values the opponent declared on stopover column `to` this turn
		(public action_chain; mirror of ge._oppo_defend_shields)."""
		total = 0
		for a in (self.oppo_actions or []):
			if not (a.get('mode') == 'defend' and a.get('to') == to):
				continue
			for cid in (a.get('cards') or []):
				row = self.MAIN_ROWS.get(cid)
				total += int(row['shield']) if row else 0
		return total

	def _oppo_cancel_facing(self, to):
		"""Baseline (conservative): the opponent has a MOVE-mode effect_canceled card on my facing
		column. Its condition is evaluated against THEIR state (not fully mirrorable yet - see §B),
		skipping the attach when one faces me never wastes a pending card."""
		for a in (self.oppo_actions or []):
			if not (a.get('mode') == 'move' and a.get('to') == to):
				continue
			for cid in (a.get('cards') or []):
				row = self.MAIN_ROWS.get(cid)
				if row is not None and row['effect'] == 'effect_canceled':
					return True
		return False

	# ------------------------------------------------------------------
	# E - Reactive / instant main-pool effects: FACING-AWARE scoring. My next position is known (my own
	# chain, public) and the opponent's declared plays are PUBLIC on their recorded columns, so a card I
	# place there FACES exactly what they declared: effect_canceled denies its effect (#32), grappling_hook
	# copies its advancing (#34), copy_effect replays it with me as actor (#34). The gain is computed per
	# candidate in play_card, which also makes such cards WAIT their turn until a strong card of theirs
	# lands on my facing column (turn-level ordering #35b - greedy + facing bonus = the right sequence
	# under play alternation; no explicit planner needed).
	# ------------------------------------------------------------------
	def _facing_move_at(self, to):
		"""E #32/#34: MAIN row of the opponent's declared MOVE card on stopover column `to`, or None."""
		for a in (self.oppo_actions or []):
			if not (a.get('mode') == 'move' and a.get('to') == to):
				continue
			for cid in (a.get('cards') or []):
				row = self.MAIN_ROWS.get(cid)
				if row is not None:
					return row
		return None

	def _denied_effect_value(self, t):
		"""E #32: value of DENYING the declared opponent card `t`'s effect (my facing effect_canceled).
		The engine still applies its BASIC advancing; denied = TEMPO + situational + the extra movement
		the effect itself would have caused ('advancing' adds en cells - also canceled). Denying a
		self-recoil 'backward' HELPS them, so it prices NEGATIVE (we never face our cancel there)."""
		v = max(0, TEMPO_VALUES.get(t['effect'], 0)) + self._situational_tempo(t)
		en = int(t.get('effect_number') or 0)
		if t['effect'] == 'advancing':
			v += abs(en)
		elif t['effect'] == 'backward':
			v -= abs(en)
		return v

	def _facing_gain(self, row, to):
		"""E #32/#34: extra value of playing `row` on column `to` BECAUSE of what the opponent declared
		here. Returns (extra_fwd, extra_tempo). Gated on MY condition being met - an unmet card copies
		nothing and cancels nothing; during a nobodymoves lock the copied MOVEMENT is suppressed (the
		engine gates movement copies) but a cancel still denies non-movement effects."""
		if not self._condition_met(row['condition']):
			return 0, 0
		fx = row['effect']
		if fx not in ('effect_canceled', 'grappling_hook', 'copy_effect'):
			return 0, 0
		t = self._facing_move_at(to)
		if t is None:
			return 0, 0
		locked = self._movement_locked()
		if fx == 'effect_canceled':
			return 0, self._denied_effect_value(t)
		if fx == 'grappling_hook':
			# the trip chain copies the facing card's TOTAL forward movement - public worst case: their
			# potential advance (drops / a later cancel of theirs on it are a deliberate P2 refinement)
			return (0 if locked else max(0, self._oppo_potential_adv(t))), 0
		# copy_effect: I apply THEIR effect with me as actor. Movement effects -> forward cells (my
		# estimator on their stats); zone effects -> their TEMPO + situational value for ME.
		if t['effect'] in ('advancing', 'backward', 'jump'):
			return (0 if locked else max(0, self._forward_if_met(t))), 0
		return 0, max(0, TEMPO_VALUES.get(t['effect'], 0)) + self._situational_tempo(t)

	def _entry_value(self, row, to):
		"""E #31: net expected value (fwd+tempo) of MY card `row` if it resolved on column `to` - the
		play_card score model without the win flag, facing interactions included. Used to price swap
		targets; mercurochrome buy-through is approximated as "in my zone + blocked"."""
		met = self._condition_met(row['condition'])
		if self._oppo_shields_at(to) >= int(row['mana']):
			buythrough = met and (row['effect'] == 'unstoppable'
					or 'mercurochrome' in (self.player_state.pendings or []))
			if not buythrough:
				return 0
		if self._movement_locked():
			tempo = TEMPO_VALUES.get(row['effect'], 0) if met else 0
			cf, ct = self._facing_gain(row, to)
			return tempo + ct   # the lock suppresses all of my movement (cf is already 0 inside)
		fwd = self._forward_if_met(row) if met else self._forward_if_unmet(row)
		tempo = (TEMPO_VALUES.get(row['effect'], 0) + self._situational_tempo(row)) if met else 0
		if fwd > 0:
			fwd += self._drop_path_bonus(row, fwd)
		cf, ct = self._facing_gain(row, to)
		return (fwd + cf) + (tempo + ct)

	def _choose_swap_target(self, row_s):
		"""E #31: for a swap_cards candidate (its natural column = my next stopover), find one of MY
		existing MOVE plays to swap with - the net gain must reach SWAP_MIN_GAIN. Swapping sends `row_s`
		to that play's position and pushes it where row_s would have gone; targets are my own move plays
		only (swapping a defend away from its column tears down my declared block, and placeholder / rooted
		furniture swaps have no modeled value yet). Returns (target POSITION 1..5, gain) or None."""
		nat = self._my_next_stopover()
		best_pos, best_gain = None, 0
		for a in (self.player_state.action_chain or []):
			if not (a.get('mode') == 'move' and isinstance(a.get('to'), str)
					and a['to'].startswith('stopover_')):
				continue
			try:
				pcol = int(a['to'].split('_')[1])
			except (ValueError, IndexError):
				continue
			rows_p = [self.MAIN_ROWS[cid] for cid in (a.get('cards') or []) if self.MAIN_ROWS.get(cid) is not None]
			if len(rows_p) != 1:
				continue   # a move play carries exactly one main card
			row_p = rows_p[0]
			pcol_s = f'stopover_{pcol}'
			gain = (self._entry_value(row_s, pcol_s) + self._entry_value(row_p, nat)
					- self._entry_value(row_s, nat) - self._entry_value(row_p, pcol_s))
			if gain > best_gain:
				best_pos, best_gain = 5 - pcol, gain
		return (best_pos, best_gain) if best_gain >= SWAP_MIN_GAIN else None

	def _movement_locked(self):
		"""My movement is locked/canceled this turn: nobodymoves (game-level) or my own landmine block."""
		return bool(getattr(self.player_state, 'landmine_blocked', False)) or bool(self.nobodymoves_active)

	def _landmine_on_path(self, est_adv):
		"""A landmine drop sits on one of the cells my next move would step through (est_adv forward)."""
		if not est_adv or int(est_adv) <= 0:
			return False
		pos = self.player_state.current_position or 0
		path = set(range(pos + 1, pos + 1 + int(est_adv)))
		for d in (self.board_drops or []):
			if d.get('kind') == 'landmine' and isinstance(d.get('cell'), int) and d['cell'] in path:
				return True
		return False

	def _move_estimate(self, row):
		"""Baseline estimate of the total FORWARD cells this card moves me when its condition is met
		and it is not blocked: base advancing + faction-biome bonus (+1 while my token stands on a
		home biome - forward only, like the engine) + the extra 'advancing'-effect cells. Kept as the shared
		baseline estimator for win-completion detection (the epo attach rule and the threat responses); the full
		§C scoring model is _forward_if_met / _forward_if_unmet + _drop_path_bonus."""
		est = int(row['advancing'] or 0)
		if est > 0:
			my_pos = self.player_state.current_position or 0
			biome = self.earth_biomes[my_pos] if (self.earth_biomes and len(self.earth_biomes) > my_pos) else None
			home = FACTION_BIOMES.get(getattr(self.player_state, 'faction', None))
			if biome is not None and home and biome in home:
				est += 1
			if row['effect'] == 'advancing':
				est += int(row.get('effect_number') or 0)
		return est

	def _home_bonus(self):
		"""C #22: the +1 faction biome bonus for forward movement - the engine applies it once per
		process_advancing / jump call, when my token STARTS on one of my two home biomes (public info)."""
		my_pos = self.player_state.current_position or 0
		biome = self.earth_biomes[my_pos] if (self.earth_biomes and len(self.earth_biomes) > my_pos) else None
		home = FACTION_BIOMES.get(getattr(self.player_state, 'faction', None))
		return 1 if (biome is not None and home and biome in home) else 0

	def _forward_if_met(self, row):
		"""C #22: the forward cells this card moves me when its condition IS met, it is not blocked
		and my movement is not locked - an exact mirror of the engine's resolution:
		  * 'advancing' effect -> base + effect_number (the home bonus applies to the total if forward);
		  * 'backward'         -> base (+bonus) first, then recoil by effect_number;
		  * 'jump'             -> teleport of base (+bonus), only the landing cell is checked;
		  * everything else    -> basic advancing = base (+bonus for a positive value)."""
		base = int(row['advancing'] or 0)
		fx, en = row['effect'], int(row.get('effect_number') or 0)
		bonus = self._home_bonus()
		if fx == 'jump':
			return base + (bonus if base > 0 else 0)
		if fx == 'advancing':
			total = base + en
			return total + (bonus if total > 0 else 0)
		if fx == 'backward':
			return base + (bonus if base > 0 else 0) - abs(en)
		return base + (bonus if base > 0 else 0)

	def _forward_if_unmet(self, row):
		"""C #22: condition NOT met -> no effect fires; the card still advances by mana-1 through
		process_advancing (the home bonus applies to a positive reduced value too - it can be <= 0).
		The nobodymoves / my own landmine movement lock suppresses even that."""
		if self._movement_locked():
			return 0
		r = int(row['mana']) - 1
		return r + (self._home_bonus() if r > 0 else 0)

	def _will_be_blocked(self, row):
		"""C #25: would this card be BLOCKED at resolution? The opponent's declared plays are PUBLIC
		(action_chain); the block race is sum of their shields on my facing position vs my mana cost.
		The engine exceptions are kept: an 'unstoppable' effect whose condition is met, or a
		mercurochrome I would attach (see _choose_attachment - it does exactly that), buy through."""
		to = self._my_next_stopover()
		if self._oppo_shields_at(to) < int(row['mana']):
			return False
		met = self._condition_met(row['condition'])
		if met and (row['effect'] == 'unstoppable' or 'mercurochrome' in (self.player_state.pendings or [])):
			return False
		return True

	def _drop_path_bonus(self, row, forward):
		"""C #23: board drops adjust the reach of a positive move - public tokens only (board_drops +
		pet_trap drop_tokens), counted once each (no chain recursion - deliberate P1 approximation):
		  * step movement -> every cell pos+1..pos+forward is stepped: boosts / trampolines add +2,
		    gluetraps cost -1, pet_trap tokens knock back per token on arrival, a landmine CANCELS the
		    remaining steps there (the engine stops the current movement at it);
		  * jump -> only the landing cell (pos+forward) can fire anything.
		Returns the net extra forward cells (may be negative)."""
		if not forward or int(forward) <= 0:
			return 0
		pos = self.player_state.current_position or 0
		cells = [pos + int(forward)] if row['effect'] == 'jump' else range(pos + 1, pos + 1 + int(forward))
		drops_by_cell = {}
		for d in (self.board_drops or []):
			if isinstance(d.get('cell'), int) and d.get('kind'):
				drops_by_cell.setdefault(d['cell'], []).append(d['kind'])
		bonus = 0
		for c in cells:
			n_trap = self.drop_tokens.get(c, 0)
			if n_trap:
				bonus -= n_trap                    # pet_trap recoil per token on arrival (also on a jump landing)
			for kind in drops_by_cell.get(c, []):
				if kind in ('boost', 'trampoline'):
					bonus += 2
				elif kind == 'gluetrap':
					bonus -= 1
				elif kind == 'landmine':
					return bonus                   # the remaining steps are canceled at this cell
		return bonus

	def _completes_win(self, row, fwd, blocked):
		"""C #23: does this play WIN the game at resolution? pos + effective forward >= 24 (drops on my
		path are already included in `fwd`), or an attachable epo (+1) completes it - mirroring
		_choose_attachment's epo rule with its own estimator (_move_estimate)."""
		if blocked or self._movement_locked():
			return False
		pos = self.player_state.current_position or 0
		if pos + fwd >= 24:
			return True
		if 'epo' in (self.player_state.pendings or []):
			est = self._move_estimate(row)   # the same estimator _choose_attachment uses for the attach
			if pos + est == 23:
				return True
		return False

	def _situational_tempo(self, row):
		"""C #24: public-info adjustments on top of the flat TEMPO_VALUES (only when the effect fires)."""
		fx = row['effect']
		if fx == 'avalanche':   # every token on MO is knocked back to its first cell - mine included
			my_pos = self.player_state.current_position or 0
			me_on_mo = bool(self.earth_biomes and len(self.earth_biomes) > my_pos and self.earth_biomes[my_pos] == 'MO')
			oppo_on_mo = (self.oppo_position is not None and self.earth_biomes
						  and len(self.earth_biomes) > self.oppo_position
						  and self.earth_biomes[self.oppo_position] == 'MO')
			if oppo_on_mo:
				return 3
			if me_on_mo:
				return -2
			return 0
		if fx == 'wrecking_ball':   # INSTANT at play time: the opponent's dwelling (if any) is destroyed
			return 4 if self.oppo_dwelling else 0
		if fx == 'pet_trap':   # E #33: the trap sits on MY cell - it pays when the opponent is BEHIND me
			my_pos = self.player_state.current_position or 0
			if self.oppo_position is not None and 0 < my_pos - self.oppo_position <= 8:
				return 2   # they will step over my cell while advancing (each arrival knocks them back)
			return 0
		return 0



	def _pending_condition_count(self):
		"""How many `pending`-condition MAIN cards I own (hand+deck) - each becomes fully effective
		once my pending zone is non-empty (they advance by mana−1 and gain no effect while it's empty)."""
		n = 0
		for cid in (self.player_state.hand or []) + (self.player_state.deck or []):
			row = self.MAIN_ROWS.get(cid)
			if row is not None and row['condition'] == 'pending':
				n += 1
		return n

	def _choose_attachment(self, row):
		"""A.2 #8/#9: which pending card (if any) to attach to this main MOVE play.

		Baseline policy (§C/E refine):
		* mercurochrome = UNSTOPPABLE modifier (only while my condition is met): attach when the card
		  would be BLOCKED by shields on the facing column, or a landmine sits on its path, or my movement
		  is locked this turn (nobodymoves / my own landmine block) - it buys back the whole play.
		* epo = +1 advance: DECISIVE when it completes the win (pos+est == 23); otherwise tempo — attach
		  only with a laboratory dwelling (it refills every turn) or ≥2 pendings in zone (so at least one
		  stays to keep the `pending` condition met, #9).
		* bloodtest = the OPPONENT discards 1 card (treated as a `discard_oppo` effect): tempo +
		  denial - attach when the effect can fire, the play is unblocked and the opponent has
		  cards in hand (public count), keeping ≥1 pending in the zone while I hold
		  `pending`-condition mains.
		* virus is self-penalizing (-1 knockback to me): NEVER auto-attached; it stays in the
		  zone where it still counts for the `pending` condition.
		Never attaches when the effect cannot fire (my condition not met, or a facing move-mode
		effect_canceled card) — an attached pending is consumed even then."""
		zone = self.player_state.pendings or []
		if not zone:
			return None
		cond_met = self._condition_met(row['condition'])
		to = self._my_next_stopover()
		shields = self._oppo_shields_at(to)
		self_unstoppable = (row['effect'] == 'unstoppable' and cond_met)
		will_be_blocked = shields >= int(row['mana']) and not self_unstoppable
		if not cond_met or self._oppo_cancel_facing(to):
			return None   # the pending effect would not fire - don't waste the card
		pos = self.player_state.current_position or 0
		est = self._move_estimate(row)
		# --- mercurochrome: buys unstoppable (block / landmine path / movement lock) ---
		if 'mercurochrome' in zone and (
				will_be_blocked
				or self._movement_locked()
				or self._landmine_on_path(max(est, 1))
		):
			return 'mercurochrome'
		# --- epo: +1 advance ---
		if 'epo' in zone and not will_be_blocked and not self._movement_locked():
			if pos + est == 23:
				return 'epo'             # the +1 crosses the finish line (pos+est >= 24 already wins without it)
			if self.player_state.dwelling == 'laboratory':
				return 'epo'             # the lab refills one epo per turn - take the tempo
			if len(zone) >= 2:
				return 'epo'             # keep at least one pending in zone for the `pending` condition
		# --- bloodtest: the OPPONENT discards 1 card (a discard_oppo effect) ---
		# Tempo + denial (≈ TEMPO_VALUES['discard_oppo']): the guard above already ensures my
		# condition is met and no facing effect_canceled; the play must also be unblocked
		# (a blocked play does not activate the pending effect). It is a ZONE effect, so the
		# nobodymoves movement lock does NOT suppress it. Like epo: keep ≥1 pending in the
		# zone while I hold `pending`-condition mains in hand+deck.
		if 'bloodtest' in zone and not will_be_blocked and (self.oppo_hand or 0) >= 1:
			if len(zone) >= 2 or self._pending_condition_count() == 0:
				return 'bloodtest'
		# --- virus: self-penalizing (-1 knockback to me), never auto-attached (still counts for `pending`) ---
		return None

	def play_card(self):
		"""C #22-#26: pick the MAIN card with positive expected value from hand (affordable).
		Score = (win?, net expected value = fwd+tempo, raw forward, -cost):
		  * effective movement is the REAL resolution model (#22): met condition -> effect-aware
		    advance; unmet -> mana-1 reduced; blocked by declared shields on my facing position -> 0
		    (unstoppable / mercurochrome buy through, #25); the nobodymoves lock suppresses movement
		    but zone effects still fire;
		  * win detection (#23): pos + forward >= 24 (drops on my path included) or an attachable epo
		    completes the distance - winning cards always go first;
		  * tempo values (#24): flat TEMPO_VALUES per effect type + public situational adjustments.
		Pass discipline (#26): returns None when even the best card has no positive expected value
		(no win and net forward+tempo <= 0) - unused mana carries over to next turn. The chosen
		card may carry an attached pending card (A.2 #8). Support cards are deliberately NOT
		candidates here: choose_support_play / play_nobodymoves_threat handle them.
		Facing awareness (§E): the score includes what my next column FACES publicly -
		effect_canceled denies a declared opponent effect (#32), grappling_hook copies its advancing
		(#34), copy_effect replays it (#34) - which also sequences such cards AFTER weaker moves, so
		they land on the position facing a strong card (F #35b). A swap_cards winner may carry
		'swap_with' (E #31); a historically defensive opponent discounts unshielded forward movement
		(G #38)."""
		mana_available = len(self.player_state.mana) - self.player_state.mana_spend
		hand_df = self.CARDS_DB.filter(pl.col('card_id').is_in(self.player_state.hand))
		playable = hand_df.filter(pl.col('mana') <= mana_available)
		if playable.is_empty():
			return None

		# E #31: swap_cards candidates are scored INCLUDING their best-swap gain - a negative-net swap
		# card can still be the right play when swapping rescues a better earlier play (decoy swap).
		swap_opts = {}

		def score(row):
			met = self._condition_met(row['condition'])
			blocked = self._will_be_blocked(row)
			if blocked:
				fwd, tempo = 0, 0        # Step A before Step B: no effect AND no advancing at all
			elif self._movement_locked():
				# nobodymoves / my landmine lock suppresses ALL my movement this turn; zone effects still fire
				fwd = 0
				tempo = TEMPO_VALUES.get(row['effect'], 0) if met else 0
				cf, ct = self._facing_gain(row, self._my_next_stopover())   # E: a cancel still denies under lock
				tempo += ct
			else:
				fwd = self._forward_if_met(row) if met else self._forward_if_unmet(row)
				tempo = (TEMPO_VALUES.get(row['effect'], 0) + self._situational_tempo(row)) if met else 0
				if fwd > 0:
					fwd += self._drop_path_bonus(row, fwd)     # C #23: drops on my path adjust the reach
				# E #32/#34: what my NEXT column FACES is public - cancel / copy / grappling gains fold in
				# here (and make such cards wait for a strong facing card - turn-level ordering, F #35b)
				cf, ct = self._facing_gain(row, self._my_next_stopover())
				fwd += cf
				tempo += ct
				# G #38 calibration: a historically defensive opponent can still DECLARE a defend on my column
				# (plays alternate) - discount unshielded forward movement when they have done so often.
				if self._oppo_defend_rate() >= 0.5 and (self.oppo_hand or 0) >= 1:
					fwd = max(0, fwd - 1)
			wins = 1 if self._completes_win(row, fwd, blocked) else 0
			net = fwd + tempo
			if row['effect'] == 'swap_cards' and not blocked and not self._movement_locked():
				opt = self._choose_swap_target(row)
				if opt is not None:
					pos, gain = opt
					swap_opts[row['card_id']] = pos
					net += gain   # the value of the re-ordering itself (decoy / promotion)
			# C #24: rank by the NET expected value first (movement + card/tempo effects), then raw
			# movement over card advantage at equal net, then the cheaper cost.
			return (wins, net, fwd, -int(row['mana']))

		rows = list(playable.iter_rows(named=True))
		best_row = max(rows, key=score)
		wins, net, fwd, _neg_cost = score(best_row)
		# C #26 pass discipline: do not spend a card + slot when the play has no positive expected
		# value - unused mana carries over (the zone persists; only mana_spend resets each turn).
		if not wins and net <= 0:
			return None

		best_card = best_row['card_id']
		out = {
			'cards': [best_card],     # cards selected by user - LIST (if move mode, max 1 card, if defend mode, 1-5)
			'to': 'stopover_x',                           # destination selected by user - STRING [stopover_x, mana, pending_zone, dwelling, discard_pile]
			'mode': 'move',                             # mode selected by user - STRING ['', move, defend, dwelling_activation, pass]
			'pendings': []                          # cards in pendings zone that has to be added to a normal move card - LIST
		}
		# A.2 #8: attach the best pending card from my zone (if any) - e.g. epo completing a win,
		# mercurochrome dodging a block/landmine/lock. _choose_attachment never attaches when the
		# effect cannot fire, so this only ever adds value.
		row = self.MAIN_ROWS.get(best_card)
		if row is not None:
			pending = self._choose_attachment(row)
			if pending is not None:
				out['pendings'] = [pending]
			# E #31: carry the pre-priced swap target (gain already >= SWAP_MIN_GAIN). The engine
			# applies the swap INSTANTLY at play time; without it the card plays in its natural slot.
			pos = swap_opts.get(best_card)
			if pos is not None:
				out['swap_with'] = pos

		return out

	# ------------------------------------------------------------------
	# D - Defense: react to a CONCRETE declared threat on my next position. The engine's block race
	# compares the recorded stopover COLUMNS, so my defend (my next play) faces exactly the opponent
	# card recorded on that same column. Under play alternation their latest declared card is the only
	# one that can sit there: earlier columns are already filled by my own plays, later ones face cards
	# they have not declared yet.
	# ------------------------------------------------------------------
	def _threat_value(self, row):
		"""D #27: what is at stake if this DECLARED opponent move resolves unblocked - their worst-case
		advance from their position (_oppo_potential_adv, public data only) + the value of denying its
		effect. TEMPO_VALUES is written for ME playing a card; the per-case adjustments below give what
		DENYING it is worth."""
		v = self._oppo_potential_adv(row)
		eff = row['effect']
		en = int(row.get('effect_number') or 0)
		if eff == 'advancing_oppo':
			return v - max(2, en)   # it pushes ME forward toward my win - blocking costs me those cells
		v += max(0, TEMPO_VALUES.get(eff, 0))   # their card advantage (draw/ramp/rooted/oppo-harm...)
		if eff == 'avalanche':
			my_pos = self.player_state.current_position or 0
			me_on_mo = bool(self.earth_biomes and len(self.earth_biomes) > my_pos
							and self.earth_biomes[my_pos] == 'MO')
			oppo_on_mo = (self.oppo_position is not None and self.earth_biomes
						  and len(self.earth_biomes) > self.oppo_position
						  and self.earth_biomes[self.oppo_position] == 'MO')
			if me_on_mo and not oppo_on_mo:
				v += 3   # only I would be knocked back - deny it
			elif oppo_on_mo and not me_on_mo:
				v -= 3   # only they would - let it fire
		return v

	def _blocking_combo(self, target_shields, mana_available):
		"""D #28: the cheapest set of <=5 affordable hand cards whose shields SUM >= `target_shields`
		(one defend action may stack 1-5 cards on the same position; the block race is sum vs their
		mana cost). Exact by subset enumeration (hands are small). Tie-breaks, in order: cheaper total
		mana, then include a `condition == 'block'` card (#30 - its effect fires because we ARE blocking),
		then fewer cards, then sacrifice the SLOWEST movers (their advancing is lost this turn)."""
		cands = []
		for cid in (self.player_state.hand or []):
			r = self.MAIN_ROWS.get(cid)
			if r is not None and int(r['shield']) > 0 and int(r['mana']) <= mana_available:
				cands.append((cid, int(r['mana']), int(r['shield']), int(r['advancing'] or 0),
							r['condition'] == 'block'))
		best_key, best = None, None
		for mask in range(1, 1 << len(cands)):
			if bin(mask).count('1') > 5:
				continue
			mana = sh = adv = 0
			block_card = False
			ids = []
			for i in range(len(cands)):
				if mask >> i & 1:
					cid, m, s, a, b = cands[i]
					mana += m
					sh += s
					adv += a
					block_card |= b
					ids.append(cid)
			if mana > mana_available or sh < target_shields:
				continue
			key = (mana, 0 if block_card else 1, len(ids), adv)
			if best_key is None or key < best_key:
				best_key, best = key, ids
		return best

	def _i_can_win_now(self):
		"""D #29 self-denial guard: do I hold an AFFORDABLE main card that wins the game this turn?
		Same model as play_card's win flag (effective forward + drops, epo attach included), minus any
		card a declared block would cancel. Used to refuse defending when taking my own win is better."""
		me_pos = self.player_state.current_position or 0
		mana_available = len(self.player_state.mana or []) - (self.player_state.mana_spend or 0)
		for cid in (self.player_state.hand or []):
			row = self.MAIN_ROWS.get(cid)
			if row is None or int(row['mana']) > mana_available:
				continue
			if self._will_be_blocked(row):
				continue   # a declared block would cancel it anyway - not a clean win
			met = self._condition_met(row['condition'])
			fwd = self._forward_if_met(row) if met else self._forward_if_unmet(row)
			if not self._movement_locked() and fwd > 0:
				fwd += self._drop_path_bonus(row, fwd)
			if self._completes_win(row, fwd, False):
				return True
		return False

	def choose_defend(self, i_am_first=False):
		"""D #27-#30: the full defend decision (replaces the old 50% coin flip + single-card pick).
		React ONLY to a concrete declared threat: the opponent's LATEST play is a MOVE recorded on my
		next stopover column, so at resolution it faces exactly my next entry. Refusals:
		* nothing declared / not a move / not on my column -> None (my normal move plays there);
		* `pet_trap` - INSTANT at play time, a block cannot stop it; an UNSTOPPABLE card whose condition
		  is not provably unmet, or one carrying a mercurochrome pending - the block fails by rule;
		* D #29 self-denial: I hold an affordable winning move -> None (take my win) - EXCEPT when their
		  threat wins THEM the game and they resolve first (then survival beats my win); when WE are the
		  first player, both entries at the same position resolve in our favor, so we take our win;
		* otherwise block iff the threat would win them the game (always) or its value >= DEFEND_THRESHOLD.
		The defend stacks the cheapest shield combo that wins the race (#28; up to 5 cards per action).
		Returns {'cards': [...], 'to': <my next column>, 'mode': 'defend', 'pendings': []} or None."""
		last = (self.oppo_actions or [None])[-1]
		a_cards = last.get('cards') if last else None
		if not (a_cards and last.get('mode') == 'move'):
			return None   # their latest declared play is not a move -> nothing to react to here
		to = self._my_next_stopover()
		if last.get('to') != to:
			return None   # it faces one of my already-declared entries (or none) - no reaction possible
		cid = a_cards[0]   # move mode carries exactly 1 card (engine rule)
		row = self.MAIN_ROWS.get(cid)
		if row is None:
			return None   # not a main-pool card -> no public data to value it with
		eff = row['effect']
		if eff == 'pet_trap':
			return None   # fires at play time - blocking would be wasted shields
		if eff == 'unstoppable' and self._oppo_condition_eval(row['condition']) is not False:
			return None   # buys through any block (engine exception 1; unknown -> assume it could)
		if 'mercurochrome' in (last.get('pendings') or []):
			return None   # they attached a mercurochrome - the block fails by rule

		v = self._threat_value(row)
		their_win = (self.oppo_position is not None and self._oppo_potential_adv(row) >= 24 - self.oppo_position)
		if their_win:
			i_can_win = self._i_can_win_now()
			if i_can_win and i_am_first:
				return None   # my winning entry resolves before theirs at the same position - take it
		elif self._i_can_win_now():
			return None      # D #29: never trade a winning move for a defend of a non-winning threat

		if not their_win and v < DEFEND_THRESHOLD:
			return None
		mana_available = len(self.player_state.mana or []) - (self.player_state.mana_spend or 0)
		combo = self._blocking_combo(int(row['mana']), mana_available)
		if combo is None:
			return None   # cannot win the shield race affordably - let it through
		return {'cards': combo, 'to': to, 'mode': 'defend', 'pendings': []}

	def choose_discard(self, num_cards=1):
		"""Discard selection: pick `num_cards` from the
		hand when a discard / discard_oppo effect triggers. Heuristic: discard the
		LEAST valuable cards first (a card whose condition is met is worth keeping,
		then high advancing / high shield / low cost). Support cards are low
		priority (dead weight in the robot's hand, but still usable as mana tokens).
		Returns the standard message with to: 'discard_pile' (as many cards as the
		hand allows - the engine caps the demand at the hand size)."""
		hand = list(self.player_state.hand or [])
		n = max(0, min(num_cards, len(hand)))
		if n == 0:
			return {'cards': [], 'to': 'discard_pile', 'mode': '', 'pendings': []}
		main_df = self.CARDS_DB.filter(pl.col('card_id').is_in(hand))
		main = {r['card_id']: r for r in main_df.iter_rows(named=True)}

		def value(cid):
			if cid in main:
				r = main[cid]
				met = 1 if self._condition_met(r['condition']) else 0
				return (met, 2 * r['advancing'] + r['shield'] - r['mana'])
			return (0, -0.5)   # support card: dead weight, but still a mana token

		chosen = sorted(hand, key=value)[:n]
		return {'cards': chosen, 'to': 'discard_pile', 'mode': '', 'pendings': []}

	def update_player_state(self, player_state, oppo_state=None, game=None):
		"""Update the internal player state (optionally with opponent info and global game info).
		Public information only - never reads the opponent's hand/deck/mana contents."""
		self.player_state = player_state
		if oppo_state is not None:
			self.oppo_position = oppo_state.current_position
			self.oppo_mana = len(oppo_state.mana)
			self.oppo_hand = len(oppo_state.hand)
			self.oppo_faction = getattr(oppo_state, 'faction', None)   # public (derived from the deck at game creation)
			# A.2: the plays/defends the opponent declared THIS turn are PUBLIC (they sit on the
			# shared stopovers - shield values let me evaluate the block race of my facing card)
			self.oppo_actions = [a for a in (oppo_state.action_chain or []) if a]
			# A.3: their pending placeholders are public board furniture too ([card_name, slot] pairs)
			self.oppo_pending_slots = list(getattr(oppo_state, 'pending_slots', None) or [])
			# C #24: their dwelling token is public board furniture too (the wrecking_ball target)
			self.oppo_dwelling = getattr(oppo_state, 'dwelling', None)
			# G #37 (HARD MODE ONLY - explicit opt-in): read their hidden hand to sharpen the nobodymoves
			# latent-threat check. In fair-play default mode this stays None and is NEVER populated.
			self._hard_oppo_win = None
			if self.hard_mode:
				self._hard_oppo_win = self._find_hard_win_card(oppo_state)
		if game is not None:
			self.temperature = game.temperature
			self.day_night = game.day_night
			# A.1 support decisions: the rest of the PUBLIC game state they need
			self.day_night_fixed = bool(getattr(game, 'day_night_fixed', False))
			self.cataclysm_pile = list(getattr(game, 'cataclysm_pile') or BIOMES)
			# public board info: the biome code of every cell (earth[i][0]; tokens follow)
			self.earth_biomes = [c[0] if c else None for c in (game.earth or [])]
			occupied = set()
			for d in (getattr(game, 'board_drops', None) or []):
				if d and isinstance(d.get('cell'), int):
					occupied.add(d['cell'])
			for cell, n in (getattr(game, 'drop_tokens', None) or {}).items():
				try:
					cell = int(cell)
				except (TypeError, ValueError):
					continue
				if n:
					occupied.add(cell)
			self.occupied_cells = occupied
			# drop_on_board: any drop token / trap on the earth
			self.drops_on_board = (
				any(n > 0 for n in (game.drop_tokens or {}).values())
				or any((c and ('trap' in c or 'drop' in c)) for c in (game.earth or []))
				or any((d and d.get('cell') is not None) for d in (game.board_drops or []))   # engineers' drops
			)
			# A.2 attachment decisions: rooted tokens (public board furniture), drop tokens,
			# the nobodymoves movement lock
			self.rooted_on_board = list(getattr(game, 'rooted_on_board', None) or [])
			self.board_drops = [dict(d) for d in (getattr(game, 'board_drops', None) or []) if d]
			self.nobodymoves_active = bool(getattr(game, 'nobodymoves_active', False))
			# C #23: pet_trap drop tokens (cell -> count), public - each arrival is knocked back per token
			self.drop_tokens = {}
			for _ct, _cn in (getattr(game, 'drop_tokens', None) or {}).items():
				try:
					_ct = int(_ct)
				except (TypeError, ValueError):
					continue
				if _cn:
					self.drop_tokens[_ct] = int(_cn)
			# G #38: the opponent's PUBLIC play history from the turn log (last 5 turns) - what they
		# actually declared, not pool worst case. Feeds _oppo_defend_rate() (calibrates how much a
		# defensive opponent punishes my big moves) and is available for logging (#42).
			hist = []
			my_name = self.player_state.name if self.player_state else None
			for tlog in list(getattr(game, 'log', None) or [])[-5:]:
				for sv in (tlog.get('stopovers') or []):
					for e in (sv.get('entries') or []):
						e = e or {}
						if my_name and e.get('player') == my_name:
							continue
						if e.get('mode') in ('move', 'defend'):
							hist.append({'mode': e['mode'], 'to': e.get('to'), 'cards': list(e.get('cards') or [])})
			self.oppo_history = hist
		
