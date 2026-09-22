# Manual smoke test (section G): information policy & fairness.
#   * #37 FAIR DEFAULT: PlayerAI / run_ai_loop are fair-play by default - the robot NEVER reads the
#     opponent's hidden hand/deck/mana contents. Only PUBLIC info + own state is used, and in the
#     nobodymoves latent-threat check the pool curve ("some card COULD win") drives a conservative
#     TRUE (a false positive the player can answer by actually declaring).
#   * #37 HARD MODE OPT-IN: run_ai_loop(..., hard_mode=True) / ai.hard_mode = True lets the robot read
#     THEIR hidden hand - but only to REMOVE those false positives: a latent threat requires an actual
#     winning card in their zone. Hard mode never adds threats fair play would not see (it can only
#     ever decline, never over-act).
#   * #38 OPPONENT HISTORY CALIBRATION (public log): a historically defensive opponent (defend rate >= 0.5)
#     discounts my unshielded forward movement by 1 - which can flip both the win detection and the card
#     ranking, verified with two real pool cards.
# No games are created; this test drives the AI internals directly (no cleanup needed).
import sys, io, os, inspect
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from types import SimpleNamespace
import polars as pl
import engine.game_engine as ge
from models import PlayerState
from player_ai.playerai import PlayerAI
from game_ui.ai_driver import run_ai_loop

DB = ge.CARDS_DB
ROWS = {r['card_id']: r for r in DB.iter_rows(named=True)}

ok = 0
def check(label, cond, detail=''):
    global ok
    assert cond, f"FAIL: {label} {detail}"
    ok += 1
    print(f"  ok - {label}")

# real pool cards used below (verified stats in the header of this file's design notes)
WINNER   = 'Orc43_6c53ce'   # m4 adv7 taxation, no_condition -> worst case 7 >= need 5 from pos 19, cost 4 <= zone 4
X        = WINNER                # the big mover of the #38 flip
Y        = 'Twi22_4725c4'   # m2 adv5 draw_oppo -> net 6 off-home (fwd 5 + tempo 1)
LOW      = ['Dem10_00d5a4', 'Dem10_01f560']   # two m1 low-advance cards - cannot win from anywhere near the finish


def fake_game():
    return SimpleNamespace(
        temperature=10, day_night='day', day_night_fixed=False,
        cataclysm_pile=['OC', 'MO', 'DE', 'JU'],
        earth=[['DE'] for _ in range(24)],     # off-home sea for the Dwarves player and Twigs/Orcs cards alike
        board_drops=[], drop_tokens={}, rooted_on_board=[], nobodymoves_active=False, log=[])


# =====================================================================================
# #37 - fair default: hidden state is NEVER read; latent threat stays a conservative pool-curve guess
# =====================================================================================
print("\n--- #37 fair-play defaults ---")
ai = PlayerAI(player_state=None)
check("fresh PlayerAI is FAIR by default", ai.hard_mode is False and ai._hard_oppo_win is None)

sig = inspect.signature(run_ai_loop)
check("run_ai_loop exposes the hard_mode opt-in with default False",
      'hard_mode' in sig.parameters and sig.parameters['hard_mode'].default is False, f"({sig})")

a = PlayerState(name='A', current_position=5, hand=['nobodymoves'], mana=['m'] * 3)
b_winner_in_hand = PlayerState(name='B', current_position=19, hand=[WINNER], mana=['m'] * 4)   # could win next action (need 5, worst case 7, cost 4 <= zone 4)

ai.update_player_state(a, b_winner_in_hand, fake_game())
check("fair mode never captures their hidden winning card", ai._hard_oppo_win is None)
check("fair mode only keeps the PUBLIC hand count (not contents)", ai.oppo_hand == 1 and not hasattr(ai, 'oppo_hand_contents'))
check("fair mode: pool curve says a cost<=4 winner COULD exist -> latent threat True (conservative false positive kept)",
      ai._nobodymoves_threat() is True)

# =====================================================================================
# #37 - hard mode REMOVES the false positives (and confirms real ones)
# =====================================================================================
print("\n--- #37 hard-mode opt-in ---")
b_no_winners = PlayerState(name='B', current_position=19, hand=list(LOW), mana=['m'] * 4)   # m1 low-advance: cannot win (need 5)

ai.hard_mode = True
ai.update_player_state(a, b_no_winners, fake_game())
check("hard mode reads the hidden hand and finds NO winning card", ai._hard_oppo_win is None)
check("hard mode removes the false positive -> latent threat False (fair mode kept it True)",
      ai._nobodymoves_threat() is False)
check("and therefore nobodymoves is NOT played for a phantom threat", ai.play_nobodymoves_threat() is None)

ai.update_player_state(a, b_winner_in_hand, fake_game())
check("hard mode CONFIRMS a real winning card in their zone (id captured)", ai._hard_oppo_win == WINNER, f"(got {ai._hard_oppo_win})")
check("...so the threat is True and nobodymoves IS played as the threat response",
      ai._nobodymoves_threat() is True)
nm = ai.play_nobodymoves_threat()
check("the nobodymoves message is a complete support play", nm is not None and nm['cards'] == ['nobodymoves'], f"(got {nm})")

# hard mode never ADDS threats: with an empty hand even fair play would see no latent threat
b_empty = PlayerState(name='B', current_position=19, hand=[], mana=['m'] * 4)
ai.update_player_state(a, b_empty, fake_game())
check("no cards left in their hand -> no threat in either mode", ai._nobodymoves_threat() is False)

# self-deny guard still wins over hard-mode knowledge: if I can win this turn, nobodymoves never fires
a_can_win = PlayerState(name='A', current_position=20, hand=[WINNER], mana=['m'] * 4)   # m4 adv7 from pos 20 -> I complete the game (need 4 <= 7)
ai.update_player_state(a_can_win, b_winner_in_hand, fake_game())
check("hard mode still never self-denies: an affordable winning card in MY hand suppresses nobodymoves",
      ai._nobodymoves_threat() is False)

# =====================================================================================
# #38 - defend-rate calibration flips the ranking (public log only)
# =====================================================================================
print("\n--- #38 opponent history calibrates forward movement ---")
ai2 = PlayerAI(player_state=None)


def set_ai2(pos, hand, history):
    ps = PlayerState(name='A', current_position=pos, faction='Dwarves', hand=list(hand), mana=['m'] * 5, mana_spend=0)
    ai2.player_state = ps
    ai2.CARDS_DB = DB
    ai2.MAIN_ROWS = ROWS
    ai2.oppo_position, ai2.oppo_mana, ai2.oppo_hand = 10, None, 1      # oppo_hand >= 1 is part of the discount gate
    ai2.oppo_actions = []
    ai2.earth_biomes = ['DE'] * 24                                     # off-home for everyone involved
    ai2.temperature, ai2.day_night, ai2.day_night_fixed = 10, 'day', False
    ai2.cataclysm_pile = ['OC', 'MO', 'DE', 'JU']
    ai2.drops_on_board, ai2.board_drops, ai2.drop_tokens = False, [], {}
    ai2.nobodymoves_active = False
    ps.landmine_blocked = False
    ai2.rooted_on_board = []
    ai2.occupied_cells = set()
    ai2.oppo_dwelling, ai2.oppo_faction, ai2.oppo_pending_slots = None, 'Twigs', []
    ai2.hard_mode, ai2._hard_oppo_win, ai2.oppo_history = False, None, history


# pos 17: X (m4 adv7) completes the game RAW (17+7 >= 24); Y (m2 adv5 draw) does not but has net 6 > X's net 5
set_ai2(17, [X, Y], [])
check("defend rate 0.0: the raw winner X is played first", ai2.play_card()['cards'][0] == X)

hist = [{'mode': 'defend', 'to': 'stopover_4', 'cards': ['Twi11_e4d118']},
        {'mode': 'move',   'to': 'stopover_4', 'cards': ['Twi11_2ba5b3']},
        {'mode': 'defend', 'to': 'stopover_3', 'cards': ['Mia11_c63a55']}]
set_ai2(17, [X, Y], hist)   # defend rate 2/3 >= 0.5 with a card in their hand -> -1 fwd on every unshielded candidate
check("defend rate 0.67: X loses its raw win (17+6 < 24 after the discount) and Y's net 5 beats X's net 4",
      ai2.play_card()['cards'][0] == Y, f"(got {ai2.play_card()})")

# a mover with no cards left in their hand: the discount does not apply (they cannot declare a defend)
set_ai2(17, [X, Y], hist)
ai2.oppo_hand = 0
check("no card left in THEIR hand -> they cannot defend anymore, X's raw win stands again",
      ai2.play_card()['cards'][0] == X)

print(f"\nALL {ok} CHECKS PASSED - section G information policy verified")
