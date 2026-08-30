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
		CARDS_DB_PATH = os.path.join(os.path.dirname(__file__), '../cards/cardpool.parquet')
		self.CARDS_DB = pl.read_parquet(CARDS_DB_PATH)

	def put_mana(self, num_cards=3, in_turn=False):
		"""Select cards with the highest mana value from hand."""
		hand_df = self.CARDS_DB.filter(pl.col('card_id').is_in(self.player_state.hand))
		top_mana_cards = hand_df.sort('mana', descending=True)['card_id'].to_list()[:num_cards]
		if not top_mana_cards:  # empty hand: nothing to put in mana, pass instead
			if in_turn:
				return {
					'cards': [],
					'to': '',
					'mode': 'pass',
					'pendings': []
				}
			return top_mana_cards
		if in_turn:
			return {
			'cards': top_mana_cards,        # cards selected by user - LIST (if move mode, max 1 card, if defend mode, 1-5)
			'to': 'mana',                   # destination selected by user - STRING [stopover_x, mana, pending_zone, dwelling, discard_pile]
			'mode': '',                     # mode selected by user - STRING ['', move, defend, dwelling_activation, pass]
			'pendings': []                  # cards in pendings zone that has to be added to a normal move card - LIST
		}
		return top_mana_cards

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
		
