import polars as pl
import os
import re

class PlayerAI:
	def __init__(self, player_state):
		self.player_state = player_state
		self.oppo_position = None   # opponent's current position (set via update_player_state)
		self.oppo_mana = None       # number of cards in opponent's mana zone
		self.oppo_hand = None       # number of cards in opponent's hand
		self.temperature = None     # planet temperature (set via update_player_state)
		self.day_night = None       # current day/night phase (set via update_player_state)
		self.drops_on_board = None  # any drop/trap on the earth (set via update_player_state)
		self.engine_version = None  # rules version of the game (set via update_player_state)
		CARDS_DB_PATH = os.path.join(os.path.dirname(__file__), '../cards/cardpool.parquet')
		self.CARDS_DB = pl.read_parquet(CARDS_DB_PATH)
		# support-faction cards (engineers/mages/doctors) - NOT in cardpool. The robot
		# may put ANY card in hand (main or support) into the mana zone (the engine
		# accepts both; each card in the zone counts as 1 mana), so put_mana needs them.
		SUPPORT_DB_PATH = os.path.join(os.path.dirname(__file__), '../cards/support_factions.parquet')
		self.SUPPORT_DB = pl.read_parquet(SUPPORT_DB_PATH) if os.path.exists(SUPPORT_DB_PATH) else pl.DataFrame()

	def put_mana(self, num_cards=3, in_turn=False):
		"""Select `num_cards` from the hand to put into the mana zone.

		The robot may put ANY card in hand (main OR support) - the engine accepts
		both, and each card in the zone counts as 1 mana, so which card it is does
		not change the mana total. We prefer support cards first (the robot never
		plays them, so they are best used as mana tokens - zero opportunity cost),
		then the most expensive main cards (sacrifice the ones least worth keeping
		playable).

		Returns exactly `num_cards` cards whenever the hand has that many (this is
		what lets the init phase put its mandatory 3 in one valid message even when
		the hand is a main+support mix). An empty hand returns [] (init) or a pass."""
		hand = list(self.player_state.hand or [])
		if not hand:
			if in_turn:  # empty hand: nothing to put in mana, pass instead
				return {'cards': [], 'to': '', 'mode': 'pass', 'pendings': []}
			return []
		main_ids = set(self.CARDS_DB['card_id'].to_list())
		mana_of = {r['card_id']: int(r['mana']) for r in self.CARDS_DB.iter_rows(named=True)}
		cost_of = {}
		if self.SUPPORT_DB is not None and not self.SUPPORT_DB.is_empty():
			cost_of = {r['card_name']: int(r['mana_cost']) for r in self.SUPPORT_DB.iter_rows(named=True)}

		def rank(cid):
			if cid in main_ids:
				return (1, -mana_of.get(cid, 0))   # main: group 1, by mana desc
			return (0, -cost_of.get(cid, 0))       # support: group 0 (preferred), by mana_cost desc

		ranked = sorted(hand, key=rank)
		top = ranked[:num_cards]
		if in_turn:
			return {
				'cards': top,      # cards selected - LIST
				'to': 'mana',      # destination - STRING [stopover_x, mana, pending_zone, dwelling, discard_pile]
				'mode': '',        # mode - STRING ['', move, defend, dwelling_activation, pass]
				'pendings': []     # cards in pendings zone to add to a normal move card - LIST
			}
		return top

	def _condition_met(self, condition):
		"""Mirror of game_engine.is_condition_met (only the conditions it can evaluate with the state it has).
			Default for any not-yet-implemented condition: MET (same canonical default as the engine's catch-all)."""
		if condition == 'no_condition':
			return True
		if condition == 'cataclysm':
			# trigger condition: the engine fires the cataclysm strike when the card
			# resolves; the condition itself is always treated as met (effect fires).
			# The AI cannot evaluate the strike (hidden pile) and doesn't need to.
			return True
		if 'biome' in condition:
			return False  # cannot evaluate without board info -> treat as not met
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
			# any drop/trap on the earth (rule of engine_version 9), computed in
			# update_player_state from the public board state (True for old games
			# < 9: canonical default = met). Unknown (no board info) -> not met,
			# like the other board-dependent conditions (biome).
			return bool(self.drops_on_board)
		if condition == 'pending':
			# pending (rule of engine_version 21): met iff at least one pending card in
			# the player's OWN pending zone (the doctors' pending zone). Old games
			# (< 21) keep the canonical default = met (the condition was unimplemented
			# in those games). The robot never plays support cards, so its pending zone
			# is always empty -> the condition is correctly not met for it.
			if self.engine_version is not None and self.engine_version < 21:
				return True
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

	def play_card(self):
		"""Select a card from hand with enough mana, preferring cards whose condition is met."""
		mana_available = len(self.player_state.mana) - self.player_state.mana_spend
		hand_df = self.CARDS_DB.filter(pl.col('card_id').is_in(self.player_state.hand))
		playable = hand_df.filter(pl.col('mana') <= mana_available)
		if playable.is_empty():
			return {
				'cards': [],
				'to': '',
				'mode': 'pass',
				'pendings': []
			}

		# prefer cards whose condition is met, then highest advancing value
		def score(row):
			met = 1 if self._condition_met(row['condition']) else 0
			return (met, row['advancing'])
		best_card = max(playable.iter_rows(named=True), key=score)['card_id']
		out = {
			'cards': [best_card],     # cards selected by user - LIST (if move mode, max 1 card, if defend mode, 1-5)
			'to': 'stopover_x',                           # destination selected by user - STRING [stopover_x, mana, pending_zone, dwelling, discard_pile]
			'mode': 'move',                             # mode selected by user - STRING ['', move, defend, dwelling_activation, pass]
			'pendings': []                          # cards in pendings zone that has to be added to a normal move card - LIST
		}

		return out

	def defend_card(self):
		"""Select a card from hand with enough mana to play in DEFEND mode (sideways, blocks the
			opponent card on the same stopover). Prefers the highest shield value.
			Returns None if no affordable card (caller should pass instead)."""
		mana_available = len(self.player_state.mana) - self.player_state.mana_spend
		hand_df = self.CARDS_DB.filter(pl.col('card_id').is_in(self.player_state.hand))
		playable = hand_df.filter(pl.col('mana') <= mana_available)
		if playable.is_empty():
			return None
		best_card = max(playable.iter_rows(named=True), key=lambda r: (r['shield'], -r['mana']))['card_id']
		return {
			'cards': [best_card],
			'to': 'stopover_x',   # filled by the caller (the stopover of the opponent card to block)
			'mode': 'defend',
			'pendings': []
		}

	def choose_discard(self, num_cards=1):
		"""Discard selection (rule of engine_version 13): pick `num_cards` from the
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
		"""Update the internal player state (optionally with opponent info and global game info)."""
		self.player_state = player_state
		if oppo_state is not None:
			self.oppo_position = oppo_state.current_position
			self.oppo_mana = len(oppo_state.mana)
			self.oppo_hand = len(oppo_state.hand)
		if game is not None:
			self.temperature = game.temperature
			self.day_night = game.day_night
			# drop_on_board (engine_version 9): any drop token / trap on the earth
			if (game.engine_version or 0) < 9:
				self.drops_on_board = True   # old rules: unimplemented -> canonical default (met)
			else:
				self.drops_on_board = (
					any(n > 0 for n in (game.drop_tokens or {}).values())
					or any((c and ('trap' in c or 'drop' in c)) for c in (game.earth or []))
					or any((d and d.get('cell') is not None) for d in (game.board_drops or []))   # engineers' drops (engine_version 12)
				)
			self.engine_version = game.engine_version
		
