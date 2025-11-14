import polars as pl
import os

class PlayerAI:
	def __init__(self, player_state):
		self.player_state = player_state
		CARDS_DB_PATH = os.path.join(os.path.dirname(__file__), '../cards/cardpool.parquet')
		self.CARDS_DB = pl.read_parquet(CARDS_DB_PATH)

	def put_mana(self, num_cards=3, in_turn=False):
		"""Select cards with the highest mana value from hand."""
		hand_df = self.CARDS_DB.filter(pl.col('card_id').is_in(self.player_state.hand))
		top_mana_cards = hand_df.sort('mana', descending=True)['card_id'].to_list()[:num_cards]
		if in_turn:
			return {
			'cards': top_mana_cards,        # cards selected by user - LIST (if move mode, max 1 card, if defend mode, no max)
			'to': 'mana',                   # destination selected by user - STRING [stopover_x, mana, pending_zone, dwelling, discard_pile]
			'mode': '',                     # mode selected by user - STRING ['', move, defend, dwelling_activation, pass]
			'pendings': []                  # cards in pendings zone that has to be added to a normal move card - LIST
		}
		return top_mana_cards

	def play_card(self):
		"""Select a card from hand with enough mana and highest advancing value."""
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

		best_card = playable.sort('advancing', descending=True)['card_id'].to_list()[0]
		out = {
			'cards': [best_card],     # cards selected by user - LIST (if move mode, max 1 card, if defend mode, no max)
			'to': 'stopover_x',                           # destination selected by user - STRING [stopover_x, mana, pending_zone, dwelling, discard_pile]
			'mode': 'move',                             # mode selected by user - STRING ['', move, defend, dwelling_activation, pass]
			'pendings': []                          # cards in pendings zone that has to be added to a normal move card - LIST
		}

		return out

	def update_player_state(self, player_state):
		"""Update the internal player state."""
		self.player_state = player_state
		
