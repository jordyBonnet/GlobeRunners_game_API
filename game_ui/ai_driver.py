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
import re

import engine.game_engine as ge
from player_ai.playerai import PlayerAI, validate_message

# "turn N - waiting for first/second player (NAME) to play"
PLAY_TURN_RE = re.compile(r"turn \d+ - waiting for (first|second) player \((.+?)\) to play")

# "turn N - waiting for NAME to discard K card(s)" (discard selection)
DISCARD_RE = re.compile(r"turn \d+ - waiting for (.+?) to discard (\d+) card\(s\)")

# Robot deck: built by `ai_deck.py` — a random main faction + a random support
# faction, and a 20-card main deck that follows the support-faction strategy
# rules (Engineers -> drop_on_board, Doctors -> pending, Mages -> temp/day-night/
# biome, …) while staying valid under the deck rules (deck_rules.py).
try:  # game_ui/ on sys.path (tests, app.py) → sibling import …
    import ai_deck  # noqa: E402
except ImportError:  # … or package import (game_ui.ai_driver)
    from game_ui import ai_deck  # noqa: E402


def random_ai_deck(n: int = 20) -> list[str]:
    """Starting deck for the robot: 20 main cards of a RANDOM main faction + a
    random support faction deck (10 cards) -> 30 total, shuffled — the same
    composition as a human's deck (20 main + 10 support).

    The main deck is a "not so dumb" build: it respects the deck rules
    (deck_rules.py) AND the support-faction strategy rules — see
    `ai_deck.build_main_deck` / the `ai_deck` module docstring.
    `n` is kept for signature compatibility (the main deck is always 20)."""
    return ai_deck.build_ai_deck()


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
        st.update(key=phase_key, state=state, acted=False, fallback=False,
                  tapped_this_turn=False)   # A.1: dwelling-tap bookkeeping (quick action)

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

    # 2. Mana/pass phase: put exactly 1 card in mana, or pass.
    #    I.1 (2026-09-24): the pool growth is a DECISION, not automatic — put_mana passes
    #    when the available mana already covers the TOTAL cost of the hand (the pool can
    #    already play everything the robot owns), and places a card only while the pool is
    #    still short of that total.
    if state == "waiting for both players to mana or pass":
        if st["acted"]:
            return None
        st["acted"] = True
        if not (me.hand or []):
            return {"cards": [], "to": "", "mode": "pass", "pendings": []}
        return ai.put_mana(1, in_turn=True)

    # 2.5 Discard selection: the trip chain is PAUSED
    #     waiting for the discarding player's choice -> pick the least valuable
    #     card(s) from the hand (to: 'discard_pile'). Deliberately NOT gated on
    #     st['acted']: the same state string can recur in a turn (two discard
    #     effects) and the robot must answer every pause.
    md = DISCARD_RE.match(state)
    if md and md.group(1) == ai_name:
        return ai.choose_discard(int(md.group(2)))

    # 3. Play phase — per-tick priority (A.1): dwelling tap -> threat response (nobodymoves)
    #     -> defend reaction -> main move card -> support fallback -> pass.
    #     The stopover of a move/defend play is the robot's next position in its OWN chain:
    #     played_before = me.play_count — the engine's own counter, which counts moves AND
    #     defends AND the pending/dwelling placements that also consume a position (the
    #     action_chain does not include those). The engine overwrites 'to' anyway; this is
    #     the frontend mirror convention for display.
    m = PLAY_TURN_RE.match(state)
    if m and m.group(2) == ai_name:
        if st["fallback"]:
            st["fallback"] = False
            return {"cards": [], "to": "", "mode": "pass", "pendings": []}

        # the robot's actions THIS turn (action_chain is reset at the end of each turn)
        acted = [a for a in (me.action_chain or []) if a]
        moved_this_turn = any(a.get("mode") == "move" and a.get("cards") for a in acted)

        # 3.1 DWELLING TAP (free QUICK action, once per turn) — do it FIRST: for a black_hole
        #     the rotation changes which biome is under each token, which affects the biome
        #     conditions of cards resolved later this SAME turn. Quick actions don't alternate,
        #     so the state string doesn't change; tapped_this_turn prevents re-sending (it is
        #     cleared on phase change / rejection by run_ai_loop).
        if me.dwelling and not me.dwelling_tapped and not st.get("tapped_this_turn"):
            tap = ai.tap_dwelling()
            if tap is not None:
                st["tapped_this_turn"] = True
                return tap

        # 3.2 THREAT RESPONSE (before committing my own movement this turn): nobodymoves locks
        #     ALL players' movement for the rest of the turn - only sensible as a first action,
        #     never after I have declared a move myself (A.3 refines the threat estimate).
        if not moved_this_turn:
            nm = ai.play_nobodymoves_threat()
            if nm is not None:
                nm["to"] = ge._player_stopover(game, me.name, me.play_count or 0)
                return nm

        # 3.3 REACTION (D #27-#29): CONCRETE declared threat only — choose_defend checks that the
        #     opponent's LATEST play is a MOVE recorded on exactly my next stopover column (the engine's
        #     block race compares columns, so it faces precisely my next entry). Under play alternation
        #     that is the ONLY moment a defend can answer something already declared: earlier positions
        #     are filled by my own plays, later ones face cards they have not declared yet. Defend-FIRST
        #     (#29) is allowed even before my first move this turn — choose_defend refuses when I hold an
        #     affordable winning move (self-denial guard), and a defend still leaves the turn open to my
        #     normal plays afterwards, so no deadlock mode appears. No coin flip: a value threshold decides.
        i_am_first = bool(getattr(game, "turn_order", None) and game.turn_order[0] == me.name)
        defend = ai.choose_defend(i_am_first=i_am_first)
        if defend is not None:
            # SAME ordering rule as the frontend / moves: the defend card(s) are the robot's next
            # entry in its own 1->5 order, placed on ITS next stopover (the engine overwrites 'to').
            defend["to"] = ge._player_stopover(game, me.name, me.play_count or 0)
            return defend

        # 3.4 MAIN: play a card (move) — the robot advances every turn (original behavior).
        msg = ai.play_card()
        if msg is not None and msg.get("mode") == "move" and msg.get("cards"):
            msg["to"] = ge._player_stopover(game, me.name, me.play_count or 0)
            return msg

        # 3.5 SUPPORT FALLBACK (A.1): no main move card playable/worth it -> spend the leftover
        #     mana on a support card: engineers' drops/dwelling, doctors' pending/laboratory,
        #     mages' instant cards. The builders already carry every required choice field.
        smsg = ai.choose_support_play()
        if smsg is not None:
            if smsg.get("mode") == "move" and smsg.get("cards"):
                smsg["to"] = ge._player_stopover(game, me.name, me.play_count or 0)
            return smsg

        # nothing playable left -> pass (ends my part of the play phase)
        return {"cards": [], "to": "", "mode": "pass", "pendings": []}

    return None


async def run_ai_loop(game_id: str, ai_name: str, interval: float = 0.4, hard_mode: bool = False):
    """Background loop: as soon as it's the robot's turn, it plays.

    Stops when the game is over or if the game has disappeared from the DB.

    G #37 information policy: fair play by default - every decision uses PUBLIC info + the robot's
    own state only. `hard_mode=True` is an explicit opt-in (testing / difficulty tier): the robot may
    then read the opponent's hidden hand to sharpen threat detection (see PlayerAI.hard_mode).
    """
    ai = PlayerAI(player_state=None)
    if hard_mode:
        ai.hard_mode = True   # G #37: explicit opt-in - never on by default
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

            # A.1 local pre-validation: never submit a message the engine would reject -
            # a rejected action costs the tick and trips the fallback-pass.
            _me_pub = game.players[ai_name]
            ok_v, why_v = validate_message(msg, dwelling=_me_pub.dwelling,
                                           pendings_zone=_me_pub.pendings)
            if not ok_v:
                print(f"[ai] invalid local message for {ai_name}: {why_v} -> fallback pass")
                st.update(acted=False, tapped_this_turn=False)
                if msg.get("mode") != "pass":
                    st["fallback"] = True
                continue

            player = game.players[ai_name].model_copy()
            player.message = msg
            try:
                resp_raw = ge.handle_websocket_message(game_id, player)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"[ai] action failed for {ai_name}: {e} -> retry next cycle")
                st.update(acted=False, tapped_this_turn=False)   # allow a retry in this same phase
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
                st.update(acted=False, tapped_this_turn=False)   # the action was not applied (a rejected tap may be retried)
                if msg.get("mode") != "pass":
                    st["fallback"] = True   # next decision in the same phase -> pass
        except Exception:
            import traceback
            traceback.print_exc()
            print("[ai] unexpected error in AI loop -> will retry next cycle")
            st.update(acted=False, fallback=False, tapped_this_turn=False)
