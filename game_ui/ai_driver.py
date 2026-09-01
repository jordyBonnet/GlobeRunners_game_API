"""Drives an AI player (Robot) that plays against the human in the web UI.

The AI acts directly on the engine (`ge.handle_websocket_message`), bypassing the
WebSocket the human uses. The loop reads the official game state from the DB, and
whenever it's the robot's turn it computes its action with PlayerAI
(player_ai/playerai.py) and submits it.

The human sees the robot's actions through their usual polling (/api/state/...):
no protocol change is needed.
"""

from __future__ import annotations

import asyncio
import json
import random
import re

import polars as pl

import engine.game_engine as ge
from player_ai.playerai import PlayerAI

# "turn N - waiting for first/second player (NAME) to play"
PLAY_TURN_RE = re.compile(r"turn \d+ - waiting for (first|second) player \((.+?)\) to play")


def random_ai_deck(n: int = 30) -> list[str]:
    """Starting deck for the robot: one random faction, n distinct cards (30 by default).

    Same logic as the frontend starter deck (`buildStarterDeck`): we only draw from a
    single faction (no mixing), and rare cards are less frequent (weighting rare x1 /
    other x3, draw without replacement or duplicates)."""
    df = ge.get_cardpool()
    faction = random.choice(df["faction"].unique().to_list())
    sub = df.filter(pl.col("faction") == faction)

    # weighted bag: rare x1, other x3
    bag: list[str] = []
    for row in sub.iter_rows(named=True):
        bag.extend([row["card_id"]] * (1 if row["rare"] else 3))

    # weighted draw without replacement, no duplicates
    deck: list[str] = []
    while len(deck) < n and bag:
        card = random.choice(bag)
        bag.remove(card)
        if card not in deck:
            deck.append(card)
    return deck


def ai_decide(game, ai_name: str, st: dict, ai: PlayerAI):
    """Computes the robot's next action from the current state.

    Returns the message {'cards','to','mode','pendings'} to submit, or None if the
    robot should not (yet) act. `st` carries the loop's phase flags (acted / fallback)
    so that only one action is played per phase.
    """
    state = game.state
    # reset the phase flags when the state changes.
    # WARNING: the key must include the turn NUMBER — the mana-phase state string
    # is identical every turn ("waiting for both players to mana or pass"); if we only
    # keyed on the string, a whole missed turn (opponent finished the play phase before
    # the robot ticked) would leave acted=True stuck and the robot would refuse to play
    # in the next mana phase -> permanent deadlock.
    phase_key = (state, game.turn)
    if st.get("key") != phase_key:
        st.update(key=phase_key, state=state, acted=False, fallback=False)

    me = game.players.get(ai_name)
    if me is None:
        return None
    oppo = next((p for n, p in game.players.items() if n != ai_name), None)
    ai.update_player_state(me, oppo, game)

    # 1. Initialization: put 3 cards in mana (from the 6 in hand)
    init_state = f"waiting for both players to put {ge.start_cards_in_mana} cards in hand"
    if state == init_state:
        need = ge.start_cards_in_mana - len(me.mana or [])
        if need > 0 and (me.hand or []):
            cards = ai.put_mana(need, in_turn=False)
            return {"cards": cards, "to": "mana", "mode": "", "pendings": []}
        return None

    # 2. Mana/pass phase: put exactly 1 card in mana, or pass
    if state == "waiting for both players to mana or pass":
        if st["acted"]:
            return None
        st["acted"] = True
        if not (me.hand or []):
            return {"cards": [], "to": "", "mode": "pass", "pendings": []}
        return ai.put_mana(1, in_turn=True)

    # 3. Play phase: play an affordable card (or pass)
    m = PLAY_TURN_RE.match(state)
    if m and m.group(2) == ai_name:
        if st["fallback"]:
            st["fallback"] = False
            return {"cards": [], "to": "", "mode": "pass", "pendings": []}

        # the robot's actions THIS turn (action_chain is reset at the end of each turn)
        acted = [a for a in (me.action_chain or []) if a]
        moved_this_turn = any(a.get("mode") == "move" and a.get("cards") for a in acted)
        defended_this_turn = any(a.get("mode") == "defend" and a.get("cards") for a in acted)
        oppo_moves = [
            a for a in ((oppo.action_chain or []) if oppo is not None else [])
            if a and a.get("mode") == "move" and a.get("cards")
        ]

        # 3.a REACTION (only after having already moved this turn): if the opponent played
        #     a card (move) this turn and the robot hasn't defended yet -> answer in DEFEND
        #     mode (card played sideways at 90°). Since the robot already advanced, the game
        #     always progresses -> no deadlock.
        if moved_this_turn and not defended_this_turn and oppo_moves and random.random() < 0.5:
            defend = ai.defend_card()
            if defend is not None:
                # SAME ordering rule as the frontend / moves: the defend card is the
                # robot's next card in the 1->5 order, placed on ITS next stopover.
                # (It blocks the opponent card on this SAME stopover — each player's
                # k-th card — and does NOT overlap the cards the robot already played.)
                played = sum(
                    1 for a in (me.action_chain or [])
                    if a and a.get("mode") in ("move", "defend") and a.get("cards")
                )
                defend["to"] = f"stopover_{4 - min(played, 4)}"
                return defend

        # 3.b MAIN: play a card (move) — the robot advances every turn (original behavior).
        msg = ai.play_card()
        # ordering rule (same as the frontend's nextSlotCol): the k-th card of the
        # turn — move OR defend — goes to stopover k -> columns 4, 3, 2, 1, 0
        # (stopover 1 = right-most column). BOTH modes consume a slot, so no two
        # cards ever land on the same stopover, and the robot's k-th defend sits
        # on the same stopover as the opponent's k-th card (which it blocks).
        if msg.get("mode") == "move" and msg.get("cards"):
            played = sum(
                1 for a in (me.action_chain or [])
                if a and a.get("mode") in ("move", "defend") and a.get("cards")
            )
            msg["to"] = f"stopover_{4 - min(played, 4)}"
        return msg

    return None


async def run_ai_loop(game_id: str, ai_name: str, interval: float = 0.4):
    """Background loop: as soon as it's the robot's turn, it plays.

    Stops when the game is over or if the game has disappeared from the DB.
    """
    ai = PlayerAI(player_state=None)
    st: dict = {}
    await asyncio.sleep(interval)   # give the human frontend time to set up
    while True:
        await asyncio.sleep(interval)
        try:
            conn, _, game = ge.get_current_game(game_id)
            conn.close()
        except Exception:
            break                   # game deleted -> stop
        if game.state == "game over" or game.winner:
            break

        # NOTE: everything below is protected — an error (transient DB lock, unexpected
        # exception, rejected action...) must NEVER kill the robot task: we log, reset the
        # phase flags and retry on the next cycle.
        try:
            msg = ai_decide(game, ai_name, st, ai)
            if msg is None:
                continue

            player = game.players[ai_name].model_copy()
            player.message = msg
            try:
                resp_raw = ge.handle_websocket_message(game_id, player)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"[ai] action failed for {ai_name}: {e} -> retry next cycle")
                st["acted"] = False   # allow a retry in this same phase
                continue

            # the engine returns a JSON (str or dict) with {'message': {'success': bool, ...}}
            try:
                resp = json.loads(resp_raw) if isinstance(resp_raw, str) else resp_raw
            except Exception:
                resp = {}
            info = resp.get("message", {}) if isinstance(resp, dict) else {}
            ok = info.get("success", True)
            if not ok:
                print(f"[ai] action rejected for {ai_name}: {info.get('message')} -> retry next cycle")
                st["acted"] = False   # the action was not applied -> we can retry
                if msg.get("mode") != "pass":
                    st["fallback"] = True   # next decision in the same phase -> pass
        except Exception:
            import traceback
            traceback.print_exc()
            print("[ai] unexpected error in AI loop -> will retry next cycle")
            st.update(acted=False, fallback=False)
