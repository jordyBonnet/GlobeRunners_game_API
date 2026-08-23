"""Replay / reconstruction module for the games analysis web app.

`games.db` only stores the FINAL state of each game, but every accepted player
message is kept in `PlayerState.messages_history`, so a whole game can be
re-segmented into turns and re-resolved with the real engine functions to
recover each player's position after every trip chain.

Notes
-----
* Identities of cards drawn from (unknown) decks are tracked as placeholders:
  only zone COUNTS matter for condition evaluation, and counts stay exact.
* Earth biomes never change during a game, so the final state's earth is
  reused as-is (player tokens stripped).
* day/night starts on "day" and flips after every completed trip chain;
  temperature is fixed at game start -> both recovered from the stored state.
"""

from __future__ import annotations

import contextlib
import io
import json
import random
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import polars as pl  # noqa: E402
from engine import game_engine as ge  # noqa: E402
from models import GameState, PlayerState  # noqa: E402

# NOTE: stored winners always sit at position win_position-1 (23): the engine
# declares a win as soon as a step would reach/cross win_position WITHOUT updating
# the player's current_position, so no special handling is needed in the replay.
DB_PATH = PROJECT_ROOT / "games" / "games.db"
CARDS_DB = ge.CARDS_DB
_PLACEHOLDER = "UNK_"


# ------------------------------------------------------------------ db access
def list_games() -> list[dict]:
    """Light metadata for every stored game (for the selector)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT game_id, state_json FROM games ORDER BY game_id").fetchall()
    finally:
        conn.close()
    out = []
    for gid, sj in rows:
        st = json.loads(sj)
        names = list(st.get("players", {}).keys())
        out.append({
            "id": gid,
            "date_label": _date_label(gid),
            "players": names,
            "final_turn": int(st.get("turn") or 1),
            "winner": st.get("winner"),
            "state": st.get("state"),
        })
    return out


def load_game(game_id: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT state_json FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def _date_label(game_id: str) -> str:
    # id format: %y_%m_%d_%H_%M_%S_xxxxx
    parts = game_id.split("_")
    try:
        y, mo, d, h, mi, s = parts[:6]
        return f"{int(d):02d}/{int(mo):02d}/{y} {h}:{mi}"
    except Exception:
        return game_id


# ------------------------------------------------------------- segmentation
def _is_mana_msg(m: dict) -> bool:
    """A turn-setup message: 1 card (or pass) put into the mana zone."""
    return m.get("to") == "mana" and len(m.get("cards", [])) <= 1


def segment_history(history: list[dict]) -> list[dict]:
    """Split one player's messages_history into per-turn segments.

    history[0] is the initial 3-card mana put (turn 1 setup). Afterwards a
    `to == 'mana'` message closes the current group of moves and becomes the
    SETUP message of the NEXT turn (draw + mana put happen before that turn's
    play phase in the engine).

    Returns: [{'moves': [msg, ...], 'setup_mana': msg | None}, ...]
    """
    turns: list[dict] = []
    cur_moves: list[dict] = []
    pending_setup = None
    for m in history[1:]:
        if _is_mana_msg(m):
            turns.append({"moves": cur_moves, "setup_mana": pending_setup})
            pending_setup, cur_moves = m, []
        else:
            cur_moves.append(m)
    # trailing moves after the last mana msg (e.g. ending turn), or empty game
    if cur_moves or not turns:
        turns.append({"moves": cur_moves, "setup_mana": pending_setup})
    return turns


# ------------------------------------------------------------- state building
def _card_cost(card_id: str) -> int:
    row = CARDS_DB.filter(pl.col("card_id") == card_id)
    return int(row["mana"].sum()) if not row.is_empty() else 0


def _remove_from_hand(hand: list[str], card_id: str) -> None:
    """Remove a played/put card from hand, keeping counts exact.

    Identities of drawn cards are unknown (random deck), so when the real id
    is missing we drop any placeholder instead - zone counts stay correct and
    that is all condition evaluation needs."""
    if card_id in hand:
        hand.remove(card_id)
        return
    for i, c in enumerate(hand):
        if c.startswith(_PLACEHOLDER):
            del hand[i]
            return
    if hand:  # safety fallback (should not happen with a consistent deck size)
        hand.pop()


def _draw_for_next_turn(p: PlayerState) -> int:
    """Mirror of the engine's end-of-turn draw (3 cards, reshuffle discard)."""
    if p.deck is None:
        p.deck = []
    if p.hand is None:
        p.hand = []
    drawn = 0
    draw_n = min(ge.turn_n_draw_cards, len(p.deck))
    new_cards = p.deck[:draw_n]
    p.hand.extend(new_cards)
    for card in new_cards:
        p.deck.remove(card)
    drawn += draw_n
    if draw_n < ge.turn_n_draw_cards:
        if p.discard is None:
            p.discard = []
        p.deck.extend(p.discard)
        random.shuffle(p.deck)
        p.discard.clear()
        remaining = ge.turn_n_draw_cards - draw_n
        draw_n2 = min(remaining, len(p.deck))
        new_cards2 = p.deck[:draw_n2]
        p.hand.extend(new_cards2)
        for card in new_cards2:
            p.deck.remove(card)
        drawn += draw_n2
    return drawn


def _build_initial_state(state_dict: dict, names: list[str]) -> GameState:
    """Reconstruct the game state right after both players put 3 cards to mana."""
    final_turn = int(state_dict.get("turn") or 1)
    n_flips = max(final_turn - 1, 0)
    final_order = state_dict.get("turn_order") or names
    # turn_order flips after every completed trip chain -> recover initial order
    turn_order = list(final_order) if n_flips % 2 == 0 else list(reversed(final_order))

    players: dict[str, PlayerState] = {}
    for name in names:
        p = state_dict["players"][name]
        # total cards per player is conserved by every effect -> deck size known
        deck_size = (
            len(p.get("hand") or []) + len(p.get("deck") or [])
            + len(p.get("discard") or []) + len(p.get("mana") or [])
        )
        # at game start: 6 cards drawn to hand, then 3 put to mana -> 3 left in hand
        n_hand0 = max(min(ge.start_cards_in_hand - ge.start_cards_in_mana, deck_size), 0)
        hand0 = [f"{_PLACEHOLDER}{name}_{i}" for i in range(n_hand0)]

        history = p.get("messages_history") or []
        initial_mana = list(history[0].get("cards", [])) if history else []
        # the stored total already includes the initial mana cards -> subtract them too
        n_deck0 = max(deck_size - n_hand0 - len(initial_mana), 0)
        deck0 = [f"{_PLACEHOLDER}{name}_d{i}" for i in range(n_deck0)]

        players[name] = PlayerState(
            name=name,
            hand=hand0,
            mana=list(initial_mana),
            mana_spend=0,
            deck=deck0,
            discard=[],
            current_position=0,
            messages_history=[],
            action_chain=[],
        )

    # biomes are static during a game -> reuse final earth (strip player tokens)
    earth = [[cell[0]] for cell in state_dict.get("earth") or [] if cell]
    for name in turn_order:  # both players start on cell 0
        if earth:
            earth[0].append(name)

    return GameState(
        id=state_dict.get("id", "replay"),
        players=players,
        turn_order=turn_order,
        turn=1,
        state="replay",
        earth=earth,
        winner=None,
        temperature=state_dict.get("temperature"),
        day_night="day",
    )


# ------------------------------------------------------------- trip chain replay
def _process_action(cards_dict: dict, player: PlayerState, game: GameState) -> dict | None:
    """Run one action through the REAL engine process_card and record details."""
    card_ids = cards_dict.get("cards") or []
    if len(card_ids) != 1:
        return {"card_id": card_ids[0] if card_ids else None, "error": "unexpected_card_count"}
    cid = card_ids[0]
    row_df = CARDS_DB.filter(pl.col("card_id") == cid)
    if row_df.is_empty():
        return {"card_id": cid, "error": "unknown_card"}
    row = row_df.row(0, named=True)

    pos_before = player.current_position or 0
    # evaluate the condition with exactly the same state process_card sees it
    cond_met = ge.is_condition_met(row["condition"], player, game)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ge.process_card(cards_dict, player, game)

    return {
        "card_id": cid,
        "name": row["name"],
        "faction": row["faction"],
        "mana_cost": int(row["mana"]),
        "condition": row["condition"],
        "condition_met": bool(cond_met),
        "effect": row["effect"],
        "effect_number": int(row["effect_number"]) if row["effect_number"] is not None else 0,
        "advancing": int(row["advancing"]),
        "pos_before": pos_before,
        "pos_after": player.current_position or 0,
    }


def _replay_trip_chain(game: GameState, order: list[str], chains: dict[str, list[dict]]) -> list[dict]:
    """Mirror of ge.process_trip_chain (parallel by index) with instrumentation.

    The engine that generated the stored games returned immediately as soon as a
    player won mid-chain, so remaining queued actions were never processed."""
    first_name, second_name = order[0], order[1]
    chain_f, chain_s = chains[first_name], chains[second_name]
    actions: list[dict] = []
    i = 0
    while True:
        ev_f = _process_action(chain_f[i], game.players[first_name], game) if i < len(chain_f) else None
        ev_s = None
        if game.state != "game over" and i < len(chain_s):
            ev_s = _process_action(chain_s[i], game.players[second_name], game)
        actions.append({first_name: ev_f, second_name: ev_s})

        if game.state == "game over":
            return actions
        if i >= len(chain_f) and i >= len(chain_s):
            return actions
        i += 1


# ------------------------------------------------------------------ main entry
def analyze_game(state_dict: dict) -> dict:
    """Replay a stored final state; returns per-turn trip-chain data + verification."""
    names = list(state_dict.get("players", {}).keys())
    final_turn = int(state_dict.get("turn") or 1)
    warnings: list[str] = []

    game = _build_initial_state(state_dict, names)
    segs = {n: segment_history(state_dict["players"][n].get("messages_history") or []) for n in names}
    max_groups = max((len(segs[n]) for n in names), default=0)
    total_turns = max(max_groups, final_turn)

    # initial mana put (turn 1 setup) for display: the 3 cards put to mana at game start
    def _initial_mana_puts() -> dict[str, list[str]]:
        out = {}
        for n in names:
            h = state_dict["players"][n].get("messages_history") or []
            out[n] = list(h[0].get("cards", [])) if h else []
        return out

    turns_out: list[dict] = []
    turn_order = list(game.turn_order)
    day_night = "day"
    ended: dict | None = None

    for t in range(1, total_turns + 1):
        seg_t = {n: (segs[n][t - 1] if t - 1 < len(segs[n]) else {"moves": [], "setup_mana": None}) for n in names}

        # --- setup phase (engine does it right after the previous trip chain) ---
        mana_puts: dict[str, list[str]] = {}
        if t == 1:
            mana_puts = _initial_mana_puts()
        else:
            for n in names:
                _draw_for_next_turn(game.players[n])
            for n in names:
                sm = seg_t[n].get("setup_mana")
                cids: list[str] = []
                if sm and sm.get("mode") != "pass" and (sm.get("cards") or []):
                    cid = sm["cards"][0]
                    _remove_from_hand(game.players[n].hand, cid)
                    game.players[n].mana.append(cid)
                    cids = [cid]
                mana_puts[n] = cids

        # hand size at the start of the play phase (before cards are played)
        hand_start_counts = {n: len(game.players[n].hand or []) for n in names}

        # --- play phase: cards leave the hand when played (as in player_play) ---
        chains: dict[str, list[dict]] = {}
        played_cids: dict[str, list[str]] = {n: [] for n in names}
        for n in names:
            chain = []
            p = game.players[n]
            for m in seg_t[n]["moves"]:
                if m.get("mode") == "pass":
                    continue
                cids = m.get("cards") or []
                if len(cids) != 1:
                    warnings.append(f"tour {t} - {n}: message avec {len(cids)} cartes (ignoré)")
                    continue
                cid = cids[0]
                if CARDS_DB.filter(pl.col("card_id") == cid).is_empty():
                    warnings.append(f"tour {t} - {n}: carte inconnue du cardpool (ignorée)")
                    continue
                _remove_from_hand(p.hand, cid)
                p.mana_spend += _card_cost(cid)
                chain.append(m)
                played_cids[n].append(cid)
            chains[n] = chain

        # starting hand of the turn: identities are known for the cards actually
        # played this turn; drawn cards have unknown identity (random deck order) -> "?" backs
        hands_start: dict[str, dict] = {}
        for n in names:
            known = list(played_cids[n])
            hands_start[n] = {
                "cards": known,
                "unknown_count": max(hand_start_counts[n] - len(known), 0),
            }

        positions_before = {n: game.players[n].current_position or 0 for n in names}

        # --- trip chain resolution (real engine code) ---
        actions = _replay_trip_chain(game, turn_order, chains)
        positions_after = {n: game.players[n].current_position or 0 for n in names}

        turns_out.append({
            "turn": t,
            "day_night": day_night,
            "order": list(turn_order),
            "mana_puts": mana_puts,
            "hands_start": hands_start,
            "actions": actions,
            "positions_before": positions_before,
            "positions_after": positions_after,
        })

        # --- end-of-turn checks (mirror of the engine) ---
        if game.state == "game over":
            ended = {"type": "finish", "winner": game.winner, "turn": t}
            break
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            deadlock = ge._check_deadlock(game)
        if deadlock:
            ended = {"type": "deadlock", "winner": game.winner, "turn": t}
            break

        # prepare next turn (engine order: flip order, reset spend/chains, flip day/night)
        turn_order = list(reversed(turn_order))
        day_night = "night" if day_night == "day" else "day"
        for n in names:
            game.players[n].mana_spend = 0
            game.players[n].action_chain = []

    # stored state says the game ended but our count-based checks did not fire
    # (e.g. a missing setup message in the history) -> infer the ending
    if ended is None and state_dict.get("state") == "game over":
        ended = {
            "type": "deadlock",
            "winner": state_dict.get("winner"),
            "turn": final_turn,
            "inferred": True,
        }

    # ------------------------------------------------------------- verification
    stored_pos = {n: (state_dict["players"][n].get("current_position") or 0) for n in names}
    replayed_pos = {n: (game.players[n].current_position or 0) for n in names}
    positions_ok = stored_pos == replayed_pos

    if state_dict.get("state") == "game over" and ended is not None:
        if ended["winner"] != state_dict.get("winner"):
            warnings.append(f"gagnant rejoué ({ended['winner']}) différent du gagnant stocké ({state_dict.get('winner')})")

    for n in names:
        rp, sp = game.players[n], state_dict["players"][n]
        for zone in ("hand", "deck", "discard", "mana"):
            r_len, s_len = len(getattr(rp, zone) or []), len(sp.get(zone) or [])
            if r_len != s_len:
                warnings.append(f"{n}: {zone} rejoué={r_len} stocké={s_len}")

    verified = positions_ok and (state_dict.get("state") != "game over" or ended is not None)
    if not positions_ok:
        warnings.append(
            f"positions finales divergentes (rejouées {replayed_pos}, stockées {stored_pos}) "
            "- partie probablement générée par une ancienne version du moteur"
        )

    return {
        "game_id": state_dict.get("id"),
        "turns": turns_out,
        "ended": ended,
        "verified": bool(verified),
        "warnings": warnings,
    }


# ------------------------------------------------------------------ self test
if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT game_id, state_json FROM games ORDER BY game_id").fetchall()
    conn.close()
    n_ok = 0
    for gid, sj in rows:
        st = json.loads(sj)
        res = analyze_game(st)
        ok = "OK " if res["verified"] else "FAIL"
        n_ok += int(res["verified"])
        end = res["ended"] or {}
        print(
            f"{ok} {gid}  turns={len(res['turns']):2d} final_turn={st.get('turn'):2d} "
            f"end={end.get('type','-')}/{end.get('winner','-')} "
            f"warn={len(res['warnings'])}"
        )
    print(f"\n{n_ok}/{len(rows)} games verified")
