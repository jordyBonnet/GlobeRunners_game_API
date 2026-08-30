"""Replay / reconstruction module for the games analysis web app.

`games.db` only stores the FINAL state of each game, but every accepted player
message is kept in `PlayerState.messages_history`, so a whole game can be
re-segmented into turns and re-resolved with the real engine functions to
recover each player's position after every trip chain.

Card identities (hands)
-----------------------
The shuffles of the original game were random and unseeded, so the exact deck
order cannot be recovered. Instead, the replay reconstructs a CONSISTENT
history: the multiset of every player's cards is known (final zones), the
played cards and the cards put to mana are known from the message history, and
a greedy simulator (driven by the final-zone constraints) assigns the unknown
cards to the draw events, right before the real engine code moves them.
Played / put / initial-mana cards are always exact; the rest of the hand is a
valid reconstruction, and the verification step flags it when it does not
match the stored final state.

Notes
-----
* Earth biomes never change during a game, so the final state's earth is
  reused as-is (player tokens stripped).
* day/night starts on "day" and flips after every completed trip chain;
  temperature is fixed at game start -> both recovered from the stored state.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from collections import Counter
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


# ------------------------------------------------------------- card helpers
_CARD_ROWS: dict[str, dict | None] = {}


def _card_row(card_id: str | None) -> dict | None:
    if not card_id:
        return None
    if card_id not in _CARD_ROWS:
        df = CARDS_DB.filter(pl.col("card_id") == card_id)
        _CARD_ROWS[card_id] = df.row(0, named=True) if not df.is_empty() else None
    return _CARD_ROWS[card_id]


def _card_cost(card_id: str) -> int:
    row = _card_row(card_id)
    return int(row["mana"]) if row else 0


# ------------------------------------------------------------- segmentation
def segment_history(history: list[dict]) -> dict:
    """Split one player's messages_history into initial mana puts + per-turn segments.

    The initial phase may be a single message (3 cards) OR several 1-card
    messages (the engine accepts both): every LEADING `to == 'mana'` message
    belongs to the initial mana put. Afterwards, each `to == 'mana'` message
    (1 card, or pass) is the SETUP of the NEXT turn; the moves between two of
    them are the play phase of that turn.

    Returns: {'initial_mana': [msg, ...], 'turns': [{'moves', 'setup_mana'}, ...]}
    """
    i = 0
    while i < len(history) and history[i].get("to") == "mana":
        i += 1
    initial = history[:i]
    turns: list[dict] = []
    cur_moves: list[dict] = []
    pending_setup = None
    for m in history[i:]:
        if m.get("to") == "mana":
            turns.append({"moves": cur_moves, "setup_mana": pending_setup})
            pending_setup, cur_moves = m, []
        else:
            cur_moves.append(m)
    # trailing moves after the last mana msg (e.g. ending turn), or empty game
    if cur_moves or not turns:
        turns.append({"moves": cur_moves, "setup_mana": pending_setup})
    return {"initial_mana": initial, "turns": turns}


# ------------------------------------------------------- identity reconstruction
# Every card of a player is in exactly one of: hand / deck / discard / mana.
# A draw (or ramp) can take any card that is currently in deck+discard, which
# is exactly the complement of hand+mana. So the only real unknown is WHICH
# cards come out of the deck at each draw event. We choose them greedily from
# the known constraints (plays, mana puts, final zones), then physically put
# them on top of the deck so the REAL engine code moves them.

_ZONE_EFFECTS = {
    "draw": "draw", "draw_oppo": "draw",
    "ramp": "ramp", "ramp_oppo": "ramp",
    "discard": "discard", "discard_oppo": "discard",
    "taxation": "tax", "taxation_oppo": "tax",
}


def _effect_events(effect: str | None, effect_number) -> tuple[str, int, bool] | None:
    """Zone-moving event of a card effect (mirrors ge.apply_effect).

    Returns (kind, n, own_player) or None when the effect does not move cards."""
    if effect not in _ZONE_EFFECTS or not effect_number:
        return None
    return (_ZONE_EFFECTS[effect], abs(int(effect_number)), not effect.endswith("_oppo"))


def _recon_ctx(stored: dict, seg: dict) -> dict:
    """Per-player context used to reconstruct card identities."""
    initial_mana: list[str] = []
    for m in seg["initial_mana"]:
        for c in (m.get("cards") or []):
            if c not in initial_mana:
                initial_mana.append(c)
    # (turn, card) events where the card MUST be in hand at that moment
    needs: list[tuple[int, str]] = []
    # upper bound on how many cards will be removed from hand/mana by effects
    discard_capacity = 0
    for ti, t in enumerate(seg["turns"], start=1):
        sm = t.get("setup_mana")
        if sm and sm.get("mode") != "pass":
            for c in (sm.get("cards") or []):
                needs.append((ti, c))
        for m in t["moves"]:
            if m.get("mode") == "pass":
                continue
            cids = m.get("cards") or []
            if len(cids) != 1:
                continue
            row = _card_row(cids[0])
            if row is None:
                continue
            needs.append((ti, cids[0]))
            ev = _effect_events(row["effect"], row["effect_number"])
            if ev and ev[0] in ("discard", "tax"):
                discard_capacity += ev[1]
    return {
        "initial_mana": set(initial_mana),
        "needs": needs,
        "final_hand": set(stored.get("hand") or []),
        "final_discard": set(stored.get("discard") or []),
        "final_mana": set(stored.get("mana") or []),
        "drawn": Counter(),
        "discard_capacity": discard_capacity,
    }


def _arrange_deck(p: PlayerState, chosen: list[str]) -> None:
    """Put the chosen cards on top of the deck, merge deck+discard behind them.

    Drawable cards = deck + discard, so the multiset of zones is conserved and
    the engine will pop exactly `chosen` (in order) on the next draw/ramp."""
    chosen = list(chosen)
    if not chosen:
        return
    pool = list(p.deck or []) + list(p.discard or [])
    rest = [c for c in pool if c not in set(chosen)]
    p.deck = list(chosen) + rest
    p.discard = []


def _arrange_tail(p: PlayerState, chosen: list[str], zone: str) -> None:
    """Move the chosen cards to the END of hand/mana (the engine takes the last ones)."""
    lst = getattr(p, zone) or []
    if not chosen:
        return
    keep = list(lst)
    out = []
    for c in chosen:
        if c in keep:
            keep.remove(c)
            out.append(c)
    lst[:] = keep + out


def _pick_draw(p: PlayerState, n: int, ctx: dict, cur_turn: int) -> list[str]:
    """Choose the n cards that will come out of the deck, and arrange them there."""
    pool = list(p.deck or []) + list(p.discard or [])
    poolset = set(pool)
    in_hand = set(p.hand or [])
    chosen: list[str] = []

    def take(c):
        if len(chosen) < n and c not in chosen and c in poolset:
            chosen.append(c)
            poolset.discard(c)

    # 1) cards that must be in hand for upcoming plays / mana puts (earliest first)
    for ti, c in ctx["needs"]:
        if len(chosen) >= n:
            break
        if ti < cur_turn or c in in_hand:
            continue
        take(c)
    # 2) cards that must survive in the final hand
    for c in sorted(ctx["final_hand"]):
        if len(chosen) >= n:
            break
        if c in in_hand or c in ctx["initial_mana"]:
            continue
        take(c)
    # 3) cards that must end in the discard pile (drawn now, removed by a later effect)
    if ctx["discard_capacity"] > 0:
        for c in sorted(ctx["final_discard"]):
            if len(chosen) >= n:
                break
            if c in in_hand or c in ctx["initial_mana"] or ctx["drawn"][c]:
                continue
            take(c)
    # 4) filler: prefer cards that must end in deck/mana
    needed = {c for (ti, c) in ctx["needs"] if ti >= cur_turn}
    for c in pool:
        if len(chosen) >= n:
            break
        if c in chosen or c in needed:
            continue
        if c in ctx["final_hand"] or c in ctx["final_discard"] or c in ctx["final_mana"]:
            continue
        take(c)
    # 5) last resort: whatever remains
    for c in pool:
        if len(chosen) >= n:
            break
        take(c)

    chosen = chosen[:n]
    _arrange_deck(p, chosen)
    ctx["drawn"].update(chosen)
    return chosen


def _pick_ramp(p: PlayerState, n: int, ctx: dict, cur_turn: int) -> list[str]:
    """Choose the n cards moved from deck to mana (same pool as a draw)."""
    pool = list(p.deck or []) + list(p.discard or [])
    poolset = set(pool)
    in_mana = set(p.mana or [])
    chosen: list[str] = []

    def take(c):
        if len(chosen) < n and c not in chosen and c in poolset:
            chosen.append(c)
            poolset.discard(c)

    # 1) cards that must end in the mana zone (ramp is the only way in, besides puts)
    for c in sorted(ctx["final_mana"]):
        if len(chosen) >= n:
            break
        if c in in_mana or c in ctx["initial_mana"]:
            continue
        take(c)
    # 2) cards that must end in the discard pile (ramped now, taxed later)
    if ctx["discard_capacity"] > 0:
        for c in sorted(ctx["final_discard"]):
            if len(chosen) >= n:
                break
            if c in in_mana or c in ctx["initial_mana"] or ctx["drawn"][c]:
                continue
            take(c)
    # 3) filler: never needed cards, never protected-zone cards
    needed = {c for (ti, c) in ctx["needs"] if ti >= cur_turn}
    for c in pool:
        if len(chosen) >= n:
            break
        if c in chosen or c in needed:
            continue
        if c in ctx["final_hand"] or c in ctx["final_discard"] or c in ctx["final_mana"]:
            continue
        take(c)
    # 4) last resort: whatever remains
    for c in pool:
        if len(chosen) >= n:
            break
        take(c)

    chosen = chosen[:n]
    _arrange_deck(p, chosen)
    ctx["drawn"].update(chosen)
    return chosen


def _pick_discard(p: PlayerState, n: int, ctx: dict, cur_turn: int) -> list[str]:
    """Choose which n cards of the hand the discard effect will remove."""
    hand = list(p.hand or [])
    n = min(n, len(hand))
    if n <= 0:
        return []
    needed_later = {c for (ti, c) in ctx["needs"] if ti >= cur_turn}
    chosen = [c for c in hand if c in ctx["final_discard"]]
    chosen += [c for c in hand if c not in chosen and c not in needed_later]
    chosen += [c for c in hand if c not in chosen]
    chosen = chosen[:n]
    _arrange_tail(p, chosen, "hand")
    ctx["discard_capacity"] = max(0, ctx["discard_capacity"] - n)
    return chosen


def _pick_tax(p: PlayerState, n: int, ctx: dict) -> list[str]:
    """Choose which n cards of the mana zone the tax effect will remove."""
    mana = list(p.mana or [])
    n = min(n, len(mana))
    if n <= 0:
        return []
    chosen = [c for c in mana if c in ctx["final_discard"]]
    chosen += [c for c in mana if c not in chosen]
    chosen = chosen[:n]
    _arrange_tail(p, chosen, "mana")
    ctx["discard_capacity"] = max(0, ctx["discard_capacity"] - n)
    return chosen


def _other_player(game: GameState, player: PlayerState) -> PlayerState | None:
    for p in game.players.values():
        if p.name != player.name:
            return p
    return None


# ------------------------------------------------------------- state building
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
        p.discard.clear()
        remaining = ge.turn_n_draw_cards - draw_n
        draw_n2 = min(remaining, len(p.deck))
        new_cards2 = p.deck[:draw_n2]
        p.hand.extend(new_cards2)
        for card in new_cards2:
            p.deck.remove(card)
        drawn += draw_n2
    return drawn


def _build_initial_state(state_dict: dict, names: list[str], segs: dict) -> tuple[GameState, dict]:
    """Reconstruct the game state right after both players put their initial mana.

    All cards of a player are known (final zones); the initial hand is chosen
    from that set so that turn-1 plays (which can only come from the initial
    hand) and final-hand cards are favoured."""
    final_turn = int(state_dict.get("turn") or 1)
    n_flips = max(final_turn - 1, 0)
    final_order = state_dict.get("turn_order") or names
    # turn_order flips after every completed trip chain -> recover initial order
    turn_order = list(final_order) if n_flips % 2 == 0 else list(reversed(final_order))

    players: dict[str, PlayerState] = {}
    ctxs: dict[str, dict] = {}
    for name in names:
        p = state_dict["players"][name]
        ctx = _recon_ctx(p, segs[name])
        ctxs[name] = ctx
        all_cards: set[str] = set()
        for z in ("hand", "deck", "discard", "mana"):
            all_cards.update(p.get(z) or [])
        pool = [c for c in all_cards if c not in ctx["initial_mana"]]
        # at game start: 6 cards drawn to hand, then the initial mana put.
        # Cards played on turn 1 can ONLY come from the initial hand (no draw
        # happened yet) -> they must all fit in it; older engine versions let a
        # player play more than 3 cards on turn 1, so allow the hand to be
        # larger (the overflow is dumped to the discard pile after turn 1).
        t1 = {c for (ti, c) in ctx["needs"] if ti == 1}
        n_hand0 = max(ge.start_cards_in_hand - len(ctx["initial_mana"]), len(t1), 0)
        n_hand0 = min(n_hand0, len(pool))
        chosen = [c for c in pool if c in t1]
        for c in sorted(ctx["final_hand"]):
            if len(chosen) >= n_hand0:
                break
            if c in pool and c not in chosen:
                chosen.append(c)
        for c in sorted(pool):
            if len(chosen) >= n_hand0:
                break
            if c not in chosen:
                chosen.append(c)
        hand0 = chosen[:n_hand0]
        deck0 = [c for c in pool if c not in set(hand0)]
        # old-engine turn-1 overflow: more cards in hand than the current engine
        # starts with -> they were dumped to the discard pile at end of turn 1
        standard_hand = ge.start_cards_in_hand - len(ctx["initial_mana"])
        overflow: list[str] = []
        if len(hand0) > standard_hand:
            overflow = hand0[standard_hand:]
            hand0 = hand0[:standard_hand]

        players[name] = PlayerState(
            name=name,
            hand=hand0,
            mana=list(ctx["initial_mana"]),
            mana_spend=0,
            deck=deck0,
            discard=list(overflow),
            current_position=0,
            messages_history=[],
            action_chain=[],
        )

    # biomes are static during a game -> reuse final earth (strip player tokens)
    earth = [[cell[0]] for cell in state_dict.get("earth") or [] if cell]
    for name in turn_order:  # both players start on cell 0
        if earth:
            earth[0].append(name)

    game = GameState(
        id=state_dict.get("id", "replay"),
        players=players,
        turn_order=turn_order,
        turn=1,
        state="replay",
        earth=earth,
        winner=None,
        temperature=state_dict.get("temperature"),
        day_night="day",
        engine_version=state_dict.get("engine_version"),
    )

    # --- cataclysm pile (rule of engine_version 3) ---
    # The stored FINAL state holds the pile AFTER all its rotations. Each trigger
    # takes the top card and puts it at the bottom (pure rotation), so the cycle
    # order is identical and only the starting card must be rewound:
    #   initial = final rotated RIGHT by k, where k = number of triggers in the game.
    # A trigger happens for every RESOLVED (move-mode, non-blocked) action whose
    # card has condition 'cataclysm'. (A cataclysm card that was BLOCKED mid-game
    # is a rare edge case: it would over-rewind by one position.)
    final_pile = list(state_dict.get("cataclysm_pile") or [])
    if (game.engine_version or 0) >= 3 and final_pile:
        k = 0
        for seg in segs.values():
            for t in seg.get("turns") or []:
                for m in t.get("moves") or []:
                    if (m.get("mode") or "") != "move":
                        continue
                    for cid in m.get("cards") or []:
                        row = _card_row(cid)
                        if row and row.get("condition") == "cataclysm":
                            k += 1
        k %= len(final_pile)
        if k:
            final_pile = final_pile[-k:] + final_pile[:-k]   # rewind the rotations
    game.cataclysm_pile = final_pile or None   # None -> trigger is a no-op (pre-cataclysm rules)
    return game, ctxs


# ------------------------------------------------------------- trip chain replay
def _process_action(game: GameState, name: str, msg: dict, ctxs: dict, cur_turn: int) -> dict | None:
    """Run one action through the REAL engine process_card with reconstructed cards."""
    p = game.players[name]
    card_ids = msg.get("cards") or []
    if len(card_ids) != 1:
        return {"card_id": card_ids[0] if card_ids else None, "error": "unexpected_card_count"}
    cid = card_ids[0]
    row = _card_row(cid)
    if row is None:
        return {"card_id": cid, "error": "unknown_card"}

    pos_before = p.current_position or 0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cond_met = ge.is_condition_met(row["condition"], p, game)

    # reconstruct the unknown cards the effect will move, so the real engine
    # code below moves exactly those cards (hand / deck / discard / mana)
    ev = _effect_events(row["effect"], row["effect_number"])
    if cond_met and msg.get("mode") != "defend" and ev:
        kind, n, own = ev
        target = p if own else _other_player(game, p)
        if target is not None:
            ctx = ctxs[target.name]
            if kind == "draw":
                _pick_draw(target, n, ctx, cur_turn)
            elif kind == "ramp":
                _pick_ramp(target, n, ctx, cur_turn)
            elif kind == "discard":
                _pick_discard(target, n, ctx, cur_turn)
            elif kind == "tax":
                _pick_tax(target, n, ctx)

    grappling_activated = False
    with contextlib.redirect_stdout(buf):
        _, grappling_activated = ge.process_card(msg, p, game)

    out = buf.getvalue()
    return {
        "card_id": cid,
        "name": row["name"],
        "faction": row["faction"],
        "mode": msg.get("mode"),   # "move" | "defend" (shown at 90° in the analysis UI)
        "mana_cost": int(row["mana"]),
        "condition": row["condition"],
        "condition_met": bool(cond_met),
        # blocked = the engine's block check vetoed this move (opponent defend
        # card(s) on the same stopover with enough shields): no effect, no advancing
        "blocked": ("BLOCKED by" in out),
        # cancelled = the opponent's valid effect_canceled card (same stopover) canceled
        # this card's effect: it did not fire, only the basic advancing was applied
        "cancelled": ("CANCELED by" in out),
        "effect": row["effect"],
        "effect_number": int(row["effect_number"]) if row["effect_number"] is not None else 0,
        "advancing": int(row["advancing"]),
        "pos_before": pos_before,
        "pos_after": p.current_position or 0,
        # grappling_hook: this card will copy its facing card's advancement (applied
        # in _replay_trip_chain, mirroring ge.process_trip_chain)
        "grappling_activated": bool(grappling_activated),
    }


def _replay_trip_chain(game: GameState, order: list[str], chains: dict, ctxs: dict, cur_turn: int) -> list[dict]:
    """Mirror of ge.process_trip_chain (parallel by index) with instrumentation.

    The engine that generated the stored games returned immediately as soon as a
    player won mid-chain, so remaining queued actions were never processed."""
    first_name, second_name = order[0], order[1]
    chain_f, chain_s = chains[first_name], chains[second_name]
    # expose each player's full turn chain on the player object so the engine's
    # block check (opponent defend cards on the same stopover, shields vs mana)
    # resolves exactly as in a real game
    game.players[first_name].action_chain = list(chain_f)
    game.players[second_name].action_chain = list(chain_s)
    try:
        actions: list[dict] = []
        i = 0
        over = False
        while not over:
            row = {}
            for nm, chain in ((first_name, chain_f), (second_name, chain_s)):
                if i < len(chain):
                    row[nm] = _process_action(game, nm, chain[i], ctxs, cur_turn)
                    if game.state == "game over":
                        over = True
                        break
                else:
                    row[nm] = None
            actions.append({first_name: row.get(first_name), second_name: row.get(second_name)})

            # grappling_hook copies (mirror of ge.process_trip_chain): after both
            # facing cards at this index have resolved, each validated grappling card
            # copies the total advancement of the facing card. Uses the BASE
            # advancement (pos_after - pos_before), so the two copies don't recurse.
            f = row.get(first_name)
            s = row.get(second_name)
            if f and f.get("grappling_activated"):
                s_adv = (s.get("pos_after", 0) - s.get("pos_before", 0)) if s else 0
                ge.apply_grappling_copy(game, game.players[first_name],
                                       ge.grappling_copy_amount(game, s_adv))
            if game.state != "game over" and s and s.get("grappling_activated"):
                f_adv = (f.get("pos_after", 0) - f.get("pos_before", 0)) if f else 0
                ge.apply_grappling_copy(game, game.players[second_name],
                                       ge.grappling_copy_amount(game, f_adv))

            if over:
                return actions
            if i >= len(chain_f) and i >= len(chain_s):
                return actions
            i += 1
        return actions
    finally:
        game.players[first_name].action_chain = []
        game.players[second_name].action_chain = []


# ------------------------------------------------------------------ main entry
def analyze_game(state_dict: dict) -> dict:
    """Replay a stored final state; returns per-turn trip-chain data + verification."""
    names = list(state_dict.get("players", {}).keys())
    final_turn = int(state_dict.get("turn") or 1)
    warnings: list[str] = []

    # Old engine quirk: some played cards are missing from ALL final zones
    # (storage bug). In the current engine a played card always ends up in the
    # discard pile, so we reconstruct them there to complete the identity pool.
    state_dict = dict(state_dict)
    fixed_players: dict[str, dict] = {}
    for n, p in state_dict["players"].items():
        final_set: set[str] = set()
        for z in ("hand", "deck", "discard", "mana"):
            final_set.update(p.get(z) or [])
        hist = {c for m in (p.get("messages_history") or []) for c in (m.get("cards") or [])}
        missing = sorted(hist - final_set)
        if missing:
            p = dict(p)
            p["discard"] = list(p.get("discard") or []) + missing
            fixed_players[n] = p
        else:
            fixed_players[n] = p
    state_dict["players"] = fixed_players

    segs = {n: segment_history(state_dict["players"][n].get("messages_history") or []) for n in names}
    max_groups = max((len(segs[n]["turns"]) for n in names), default=0)
    total_turns = max(max_groups, final_turn)

    game, ctxs = _build_initial_state(state_dict, names, segs)

    # rule version pinning: games created before engine_version 2 were played WITHOUT the
    # faction biome bonus -> replay them without it, otherwise every forward move on a home
    # biome drifts by +1 and the final positions can never verify against the stored state
    _saved_on_home_biome = ge._on_home_biome
    if (game.engine_version or 0) < 2:
        ge._on_home_biome = lambda p, g: False

    turns_out: list[dict] = []
    turn_order = list(game.turn_order)
    day_night = "day"
    ended: dict | None = None

    for t in range(1, total_turns + 1):
        seg_t = {n: (segs[n]["turns"][t - 1] if t - 1 < len(segs[n]["turns"])
                     else {"moves": [], "setup_mana": None}) for n in names}

        # --- setup phase (engine does it right after the previous trip chain) ---
        mana_puts: dict[str, list[str]] = {}
        if t == 1:
            # the 3 initial mana cards (one message of 3, or 3 messages of 1)
            for n in names:
                mana_puts[n] = [c for m in segs[n]["initial_mana"] for c in (m.get("cards") or [])]
        else:
            for n in names:
                _pick_draw(game.players[n], ge.turn_n_draw_cards, ctxs[n], t)
                _draw_for_next_turn(game.players[n])
            for n in names:
                sm = seg_t[n].get("setup_mana")
                cids: list[str] = []
                if sm and sm.get("mode") != "pass" and (sm.get("cards") or []):
                    for cid in sm["cards"]:
                        if cid in (game.players[n].hand or []):
                            game.players[n].hand.remove(cid)
                            game.players[n].mana.append(cid)
                            cids.append(cid)
                        else:
                            warnings.append(f"turn {t} - {n}: mana card not found in hand (reconstruction)")
                mana_puts[n] = cids

        # hand at the start of the play phase: full reconstructed identities
        hands_start = {
            n: {"cards": list(game.players[n].hand or []), "reconstructed": True}
            for n in names
        }

        # --- play phase: cards leave the hand when played (as in player_play) ---
        chains: dict[str, list[dict]] = {}
        for n in names:
            chain = []
            p = game.players[n]
            for m in seg_t[n]["moves"]:
                if m.get("mode") == "pass":
                    continue
                cids = m.get("cards") or []
                if len(cids) != 1:
                    warnings.append(f"turn {t} - {n}: message with {len(cids)} cards (ignored)")
                    continue
                cid = cids[0]
                if _card_row(cid) is None:
                    warnings.append(f"turn {t} - {n}: card unknown to the cardpool (ignored)")
                    continue
                if cid in (p.hand or []):
                    p.hand.remove(cid)
                p.mana_spend += _card_cost(cid)
                chain.append(m)
            chains[n] = chain

        positions_before = {n: game.players[n].current_position or 0 for n in names}

        # --- trip chain resolution (real engine code) ---
        actions = _replay_trip_chain(game, turn_order, chains, ctxs, t)
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

    ge._on_home_biome = _saved_on_home_biome   # restore (the loop above only breaks, never returns early)

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
            warnings.append(f"replayed winner ({ended['winner']}) differs from stored winner ({state_dict.get('winner')})")

    for n in names:
        rp, sp = game.players[n], state_dict["players"][n]
        for zone in ("hand", "deck", "discard", "mana"):
            r_len, s_len = len(getattr(rp, zone) or []), len(sp.get(zone) or [])
            if r_len != s_len:
                warnings.append(f"{n}: {zone} replayed={r_len} stored={s_len}")
        # card identities: final hand / mana must match the stored ones exactly
        for zone in ("hand", "mana"):
            if sorted(getattr(rp, zone) or []) != sorted(sp.get(zone) or []):
                warnings.append(f"{n}: identities of the {zone} zone not exactly reconstructed")

    verified = positions_ok and (state_dict.get("state") != "game over" or ended is not None)
    if not positions_ok:
        warnings.append(
            f"final positions diverge (replayed {replayed_pos}, stored {stored_pos}) "
            "- game probably generated by an older engine version"
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
    n_total = 0
    for gid, sj in rows:
        st = json.loads(sj)
        if len(st.get("players") or {}) < 2:
            # abandoned game (2nd player never connected) -> nothing to replay
            print(f"SKIP {gid}  (1 player, never started)")
            continue
        n_total += 1
        res = analyze_game(st)
        ok = "OK " if res["verified"] else "FAIL"
        n_ok += int(res["verified"])
        end = res["ended"] or {}
        print(
            f"{ok} {gid}  turns={len(res['turns']):2d} final_turn={st.get('turn'):2d} "
            f"end={end.get('type','-')}/{end.get('winner','-')} "
            f"warn={len(res['warnings'])}"
        )
    print(f"\n{n_ok}/{n_total} games verified")
