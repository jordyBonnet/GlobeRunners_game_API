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
        if not df.is_empty():
            _CARD_ROWS[card_id] = df.row(0, named=True)
        elif card_id in ge.SUPPORT_DB:
            # support card (not in the main pool): a synthesized row. The engine
            # resolves it as a no-op (ge.process_card: rows.is_empty() -> continue);
            # the replay uses the row for identity/cost bookkeeping only.
            s = ge.SUPPORT_DB[card_id]
            _CARD_ROWS[card_id] = {
                'card_id': card_id,
                'name': card_id,
                'faction': s.get('support_faction_name') or '',
                'mana': int(s.get('mana_cost') or 0),
                'advancing': 0,
                'shield': 0,
                'condition': 'no_condition',
                'effect': 'support',   # marker: no-op effect (support mechanics live outside the pool)
                'effect_number': 0,
                'rare': False,
            }
        else:
            _CARD_ROWS[card_id] = None
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
            # discard selection (engine_version 13): the CHOICE message (to
            # 'discard_pile') is not a play - its cards were in the hand and are
            # removed by the discard effect (already counted in discard_capacity).
            if m.get("to") == "discard_pile":
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

    # cards that must pass through the HAND for an upcoming play / mana put -
    # the engine played/put them from the hand, so a ramp could NOT have taken
    # them (a ramped card sits in the mana zone and can never be played)
    needed = {c for (ti, c) in ctx["needs"] if ti >= cur_turn}

    # 1) cards that must end in the mana zone (ramp is the only way in, besides puts)
    for c in sorted(ctx["final_mana"]):
        if len(chosen) >= n:
            break
        if c in in_mana or c in ctx["initial_mana"] or c in needed:
            continue
        take(c)
    # 2) cards that must end in the discard pile (ramped now, taxed later)
    if ctx["discard_capacity"] > 0:
        for c in sorted(ctx["final_discard"]):
            if len(chosen) >= n:
                break
            if c in in_mana or c in ctx["initial_mana"] or ctx["drawn"][c] or c in needed:
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


def _pick_discard(p: PlayerState, n: int, ctx: dict, cur_turn: int, arrange: bool = True) -> list[str]:
    """Choose which n cards of the hand the discard effect will remove.

    arrange (default True, engine_version < 13): move the chosen cards to the
    END of the hand so the engine's auto-discard (hand[-n:]) removes exactly
    those. For v13 (discard selection) the engine PAUSES (pending_discard) and
    the replay applies the choice itself, so arrange=False (no reordering)."""
    hand = list(p.hand or [])
    n = min(n, len(hand))
    if n <= 0:
        return []
    needed_later = {c for (ti, c) in ctx["needs"] if ti >= cur_turn}
    chosen = [c for c in hand if c in ctx["final_discard"]]
    chosen += [c for c in hand if c not in chosen and c not in needed_later]
    chosen += [c for c in hand if c not in chosen]
    chosen = chosen[:n]
    if arrange:
        _arrange_tail(p, chosen, "hand")
    ctx["discard_capacity"] = max(0, ctx["discard_capacity"] - n)
    return chosen


def _apply_discard_choice(game: GameState, target: PlayerState, chosen: list[str]) -> None:
    """ discard selection (engine_version 13): the engine PAUSED on
     pending_discard instead of discarding (the real player's choice arrived as a
     to:'discard_pile' message). The replay applies the identity-reconstructed
     choice here: move the chosen cards hand -> discard and clear the pause
     marker so the chain can continue. """
    for c in chosen:
        if c in (target.hand or []):
            target.hand.remove(c)
    target.discard = (target.discard or []) + list(chosen)
    game.pending_discard = None


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
        if p.get("dwelling"):   # dwelling (engine_version 12): the card sits in the dwelling slot, not a zone
            all_cards.add(p["dwelling"])
        all_cards.update(p.get("pendings") or [])   # pending cards (doctors, engine_version 16)
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

    # biomes are static during a game (pre-v26) -> reuse final earth (strip player tokens)
    earth = [[cell[0]] for cell in state_dict.get("earth") or [] if cell]
    # black_hole (Mages, engine_version 26): the stored FINAL earth is the POST-rotation
    # order. Reconstruct the INITIAL earth (turn 1, pre-rotation) by reversing the total
    # rotation (the sum of all black_hole tap directions, in chronological order). The
    # rotation is commutative (rotating by a then b == rotating by a+b), so only the total
    # matters: the engine's rotate_earth(n) gives final[i] = initial[(i - n) mod 24], so
    # the reversal is initial[i] = final[(i + R) mod 24] with R = sum of all tap deltas.
    # Tokens are addressed by cell index, so they are unaffected (re-added below).
    if earth and (state_dict.get("engine_version") or 0) >= 26:
        _R = 0
        for _n in names:
            for _t in (segs.get(_n, {}).get("turns") or []):
                for _m in (_t.get("moves") or []):
                    if _m.get("to") == "dwelling" and _m.get("mode") == "dwelling_activation" \
                            and _m.get("rotation") in ("cw", "ccw"):
                        _R += 3 if _m["rotation"] == "cw" else -3
        _R %= 24
        if _R:
            _n24 = len(earth)
            _final_codes = [c[0] for c in earth]
            for i in range(_n24):
                earth[i][0] = _final_codes[(i + _R) % _n24]
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
        # Mages thermic_flux (engine_version 24): seed the temperature from the INITIAL
        # rolled value (temperature_initial) so temp_* conditions are evaluated against
        # the pre-change value — the stored `temperature` is the FINAL value, which would
        # be wrong for a temp_* condition resolved BEFORE a thermic_flux. Old games (< 24)
        # have temperature_initial = None, so fall back to the stored `temperature` (the
        # two are equal when no thermic_flux was played).
        temperature=state_dict.get("temperature_initial") or state_dict.get("temperature"),
        day_night="day",
        engine_version=state_dict.get("engine_version"),
    )

    # --- cataclysm pile (rule of engine_version 3; reorderable since 25) ---
    # The stored FINAL state holds the pile AFTER all its rotations (and after all
    # Apocalypticritual reorders, engine_version 25).
    #
    # NO RITUAL in the game (the common case): each trigger takes the top card
    # and puts it at the bottom (pure rotation), so the cycle order is identical
    # and only the starting card must be rewound:
    #   initial = final rotated RIGHT by k, where k = number of triggers in the game.
    # A trigger fires ONLY when a 'cataclysm'-condition card actually RESOLVES
    # (move mode, not blocked, chain not cut short by a win). A BLOCKED cataclysm
    # card — or one flushed un-resolved by a mid-chain win — does NOT trigger, so
    # counting all such cards would over-rewind the pile and shift every strike.
    # Count k from the stored game log instead: the engine writes exactly one
    # '⚡ cataclysm — <biome> strikes' note per actual trigger.
    #
    # RITUAL in the game (engine_version 25): Apocalypticritual SETS the pile to
    # the player's chosen order, so the final pile is NOT a rotation of the
    # initial one. But the initial pile only matters for the strikes BEFORE the
    # first ritual: strike i before the first ritual is the i-th element of the
    # initial pile. So: initial = [strike_1, …, strike_min(i,4)] + (the biomes not
    # in that prefix, in BIOMES order). The ritual overwrites the pile, so the
    # tail is unobservable — any completion works; BIOMES order keeps it
    # deterministic. (Verified by the forward simulation: the strikes before the
    # ritual match the log, the ritual sets the pile, the strikes after are fully
    # determined by the chosen order.)
    final_pile = list(state_dict.get("cataclysm_pile") or [])
    if (game.engine_version or 0) >= 3 and final_pile:
        # chronological event list from the log: ('strike', biome) /
        # ('ritual', None), in the order the engine wrote them (turn → stopover →
        # entry → note)
        events = []
        for t in (state_dict.get("log") or []):
            for s in t.get("stopovers") or []:
                for e in s.get("entries") or []:
                    for n in (e.get("notes") or []):
                        n = str(n)
                        if n.startswith("⚡ cataclysm —"):
                            biome = n.split("—", 1)[1].strip().split(" ", 1)[0]
                            events.append(("strike", biome))
                        elif n.startswith("☄️ Apocalypticritual"):
                            events.append(("ritual", None))
        first_ritual = next((i for i, (kind, _) in enumerate(events) if kind == "ritual"), None)
        if first_ritual is None:
            # no ritual: the pile only rotated — rewind it (the original logic)
            k = sum(1 for kind, _ in events if kind == "strike")
            if k == 0 and not (state_dict.get("log") or []):
                # no stored log (game predates the log feature): best effort —
                # count the cataclysm move cards (a blocked one would over-rewind
                # by one, a rare edge case for those old games)
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
        else:
            # ritual played: the initial pile is pinned by the pre-ritual strikes
            pre_strikes = [biome for kind, biome in events[:first_ritual] if kind == "strike"]
            prefix = [b for b in pre_strikes[:len(final_pile)] if b in set(final_pile)]
            final_pile = prefix + [b for b in ge.BIOMES if b not in prefix]
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
    discard_choice = None   # (target, chosen) - discard selection (engine_version 13)
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
                if (game.engine_version or 0) < 13:
                    _pick_discard(target, n, ctx, cur_turn)
                else:
                    # discard selection (v13): the engine PAUSES (pending_discard)
                    # instead of auto-discarding -> choose the identity-consistent
                    # cards now (no hand arrangement) and apply them AFTER
                    # process_card (mirroring the player's to:'discard_pile' choice)
                    discard_choice = (target, _pick_discard(target, n, ctx, cur_turn, arrange=False))
            elif kind == "tax":
                _pick_tax(target, n, ctx)

    grappling_activated = False
    effect_activated = False
    with contextlib.redirect_stdout(buf):
        _, grappling_activated, effect_activated = ge.process_card(msg, p, game)

    # discard selection (v13): the engine PAUSED instead of discarding (a blocked /
    # not-met card never sets pending_discard, so the guard is exact) -> apply the
    # choice (hand -> discard) and clear the pause marker. NOT applied when the
    # card's own advancing won the game (mid-chain win: the real engine clears the
    # pending discard and the cards stay in the hand).
    if discard_choice is not None and game.pending_discard is not None and game.state != "game over":
        _apply_discard_choice(game, discard_choice[0], discard_choice[1])



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
        # cancelled = the card's effect was CANCELED: either by the opponent's valid
        # effect_canceled card (same stopover, basic advancing still applied) or by a
        # landmine (engine_version 12, the player is blocked for the rest of the turn)
        "cancelled": ("CANCELED by" in out),
        "cancel_reason": ("landmine" if "landmine block" in out
                          else ("effect_canceled" if "effect_canceled" in out else None)),
        "effect": row["effect"],
        "effect_number": int(row["effect_number"]) if row["effect_number"] is not None else 0,
        "advancing": int(row["advancing"]),
        "pos_before": pos_before,
        "pos_after": p.current_position or 0,
        # grappling_hook: this card will copy its facing card's advancement (applied
        # in _replay_trip_chain, mirroring ge.process_trip_chain)
        "grappling_activated": bool(grappling_activated),
        # copy_effect (rule of engine_version 7): this card will copy its facing
        # card's effect, applied with this player as the actor (applied in
        # _replay_trip_chain, mirroring ge.process_trip_chain); the copy only happens
        # when the FACING card's effect fired too (no recursion: a facing copy_effect
        # has nothing to copy - enforced by ge.apply_copy_effect)
        "copy_activated": bool(row["effect"] == "copy_effect" and effect_activated
                               and (game.engine_version or 0) >= 7),
        # effect_activated: this card's effect actually fired (condition met, not
        # blocked, not effect_canceled) - the gate for being COPYED by a facing
        # copy_effect card
        "effect_activated": bool(effect_activated),
        # kind: 'play' (a normal move/defend action) or 'rooted' (a rooted card on
        # the board, engine_version 15 - basic advancing only, blockable, no effect)
        "kind": "play",
    }


def _process_rooted_action(game: GameState, name: str, card_id: str) -> dict | None:
    """Resolve a ROOTED card on the trip chain (engine_version 15): basic advancing
    only (no condition, no effect), blockable by the opponent's defend at the same
    stopover. Mirrors ge.process_rooted_card. No pinning needed (rooted cards never
    fire a zone-moving effect - they only advance by their base value)."""
    p = game.players[name]
    row = _card_row(card_id)
    if row is None:
        return {"card_id": card_id, "error": "unknown_card", "kind": "rooted"}
    pos_before = p.current_position or 0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        game, _adv, _blocked = ge.process_rooted_card(card_id, p, game)
    out = buf.getvalue()
    return {
        "card_id": card_id,
        "name": row["name"],
        "faction": row["faction"],
        "mode": "rooted",
        "mana_cost": int(row["mana"]),
        "condition": "no_condition",
        "condition_met": True,   # rooted cards don't check a condition
        "blocked": ("BLOCKED by" in out),
        "cancelled": False,
        "cancel_reason": None,
        "effect": row["effect"],
        "effect_number": int(row["effect_number"]) if row["effect_number"] is not None else 0,
        "advancing": int(row["advancing"]),
        "pos_before": pos_before,
        "pos_after": p.current_position or 0,
        "grappling_activated": False,   # rooted cards never grapple
        "copy_activated": False,        # rooted cards never copy
        "effect_activated": False,      # rooted cards never fire an effect
        "kind": "rooted",
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

    # pet_trap (rule of engine_version 8): the INSTANT effect fired at PLAY TIME -
    # every drop token was placed on its owner's cell BEFORE the trip chain
    # resolved (while both players were still at their pre-chain positions), so
    # the mirror places them all here, before any card has moved.
    if (game.engine_version or 0) >= 8:
        for nm, chain in ((first_name, chain_f), (second_name, chain_s)):
            for m in chain:
                if (m.get('mode') or '') != 'move':
                    continue
                for cid in (m.get('cards') or [])[:1]:
                    row = _card_row(cid)
                    if row and row.get('effect') == 'pet_trap':
                        cell = game.players[nm].current_position or 0
                        game.drop_tokens[cell] = game.drop_tokens.get(cell, 0) + 1

    # engineers' drops (rule of engine_version 12): the INSTANT effect fired at
    # PLAY TIME - every drop token was placed on the cell CHOSEN by the player
    # (message 'cell'), before the trip chain resolved. The mirror places them
    # here, before any card has moved.
    if (game.engine_version or 0) >= 12:
        for nm, chain in ((first_name, chain_f), (second_name, chain_s)):
            for m in chain:
                if (m.get('mode') or '') != 'move':
                    continue
                for cid in (m.get('cards') or [])[:1]:
                    if cid in ge.ENGINEER_DROPS and m.get('cell') is not None:
                        game.board_drops.append({'cell': int(m['cell']), 'kind': cid, 'owner': nm})

    # wrecking_ball (rule of engine_version 11): the INSTANT effect fired at PLAY
    # TIME - it removed the opponent's dwelling card (if any), sending it to the
    # opponent's discard and clearing the dwelling slot. A no-op today (no
    # implemented effect places a dwelling card yet), but mirrored here so the
    # replay stays faithful when dwelling cards are added later.
    if (game.engine_version or 0) >= 11:
        for nm, chain in ((first_name, chain_f), (second_name, chain_s)):
            for m in chain:
                if (m.get('mode') or '') != 'move':
                    continue
                for cid in (m.get('cards') or [])[:1]:
                    row = _card_row(cid)
                    if row and row.get('effect') == 'wrecking_ball':
                        oppo = game.players[second_name] if nm == first_name else game.players[first_name]
                        if oppo.dwelling:
                            dwelling_card = oppo.dwelling
                            oppo.discard = (oppo.discard or []) + [dwelling_card]
                            oppo.dwelling = None

    try:
        # Build the per-player chains: v15 = that player's OWN rooted cards (from
        # the previous turn, basic advancing + blockable) at the leading positions
        # + this turn's plays; legacy (v<15) = plays only (rooted cards are inert
        # and not part of the chain). The trip chain resolves by POSITION, and the
        # facing (block / grappling / copy) is between the two players' entries at
        # the SAME position.
        if (game.engine_version or 0) >= 15:
            chain_f = ge._player_chain(game, first_name)
            chain_s = ge._player_chain(game, second_name)
        else:
            def _legacy_chain(nm):
                out = []
                for idx, a in enumerate(chains[nm]):
                    if a and a.get('mode') in ('move', 'defend') and a.get('cards'):
                        out.append({'kind': 'play', 'action': a, 'stopover': a.get('to'), 'position': idx + 1})
                return out
            chain_f = _legacy_chain(first_name)
            chain_s = _legacy_chain(second_name)
        # max POSITION (not len): since engine_version 17 the chain can have GAPS -
        # a pending placeholder attached to a play is removed from the chain, leaving
        # its position empty (mirror of the engine's max_pos)
        _positions = [e.get('position') or 0 for e in (chain_f or [])] + [e.get('position') or 0 for e in (chain_s or [])]
        max_pos = max(_positions) if _positions else 0

        def _entry_at(chain, pos):
            for e in chain:
                if e.get('position') == pos:
                    return e
            return None

        def _resolve(nm, entry):
            if entry is None:
                return None
            if entry['kind'] == 'rooted':
                return _process_rooted_action(game, nm, entry['card_id'])
            if entry['kind'] == 'placeholder':
                # board-furniture placeholder (v17): an EMPTY position - no card,
                # no effect, nothing to copy / block (the live engine skips it)
                return None
            return _process_action(game, nm, entry['action'], ctxs, cur_turn)

        actions: list[dict] = []
        p = 1
        over = False
        while not over and p <= max_pos:
            # the engine checks the game state BEFORE processing every entry
            # (ge.process_trip_chain returns immediately on a mid-chain win) - a
            # win during the previous position's grappling/copy must stop the
            # chain here, not let the next position resolve
            if game.state == "game over":
                over = True
                break
            row = {}
            for nm, chain in ((first_name, chain_f), (second_name, chain_s)):
                e = _entry_at(chain, p)
                if e is not None:
                    row[nm] = _resolve(nm, e)
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
            f_entry = _entry_at(chain_f, p) if f else None
            s_entry = _entry_at(chain_s, p) if s else None
            # the engine returns IMMEDIATELY on a mid-chain win (before any
            # grappling copy) - so a copy must not fire when the game ended on the
            # facing card (mirrors the game-over checks around apply_grappling_copy
            # in ge.process_trip_chain).
            # nobodymoves (engine_version 23): a movement-locked copier does NOT
            # copy its facing advancement (the copy is movement); an UNSTOPPABLE
            # copier (condition met) still copies. (For landmine the card was
            # canceled, so grappling_activated is False and this is never reached.)
            if game.state != "game over" and f and f.get("grappling_activated") and \
                    f_entry and not ge._is_movement_locked(game, game.players[first_name], f_entry.get("action") or {}):
                s_adv = (s.get("pos_after", 0) - s.get("pos_before", 0)) if s else 0
                ge.apply_grappling_copy(game, game.players[first_name],
                                       ge.grappling_copy_amount(game, s_adv))
            if game.state != "game over" and s and s.get("grappling_activated") and \
                    s_entry and not ge._is_movement_locked(game, game.players[second_name], s_entry.get("action") or {}):
                f_adv = (f.get("pos_after", 0) - f.get("pos_before", 0)) if f else 0
                ge.apply_grappling_copy(game, game.players[second_name],
                                       ge.grappling_copy_amount(game, f_adv))

            # copy_effect copies (mirror of ge.process_trip_chain): a validated
            # copy_effect card copies the facing card's effect, applied with the
            # copier as the actor. Only when the facing card's effect fired (known
            # here: both entries of this position are resolved) and the facing
            # entry is a PLAY (rooted cards never fire an effect - their
            # effect_activated is False) and it is not a copy_effect itself (no
            # recursion - ge.apply_copy_effect enforces it).
            for copier_nm, facing_nm in ((first_name, second_name), (second_name, first_name)):
                if game.state == "game over":
                    break
                c_info, o_info = row.get(copier_nm), row.get(facing_nm)
                if not (c_info and o_info and c_info.get("copy_activated") and o_info.get("effect_activated")):
                    continue
                # both entries at this position must be PLAYS (rooted entries
                # never fire an effect - a copy never fires when facing a rooted card)
                c_entry = _entry_at(chain_f if copier_nm == first_name else chain_s, p)
                o_entry = _entry_at(chain_f if facing_nm == first_name else chain_s, p)
                if not (c_entry and o_entry and c_entry['kind'] == 'play' and o_entry['kind'] == 'play'):
                    continue
                # landmine (engine_version 12): a blocked player does not copy (for
                # landmine the card was canceled, so copy_activated is False and this
                # is never reached - kept for clarity).
                # nobodymoves (engine_version 23): a movement-locked copier copies ONLY
                # a non-movement (zone) effect; a movement-effect copy is suppressed.
                # An UNSTOPPABLE copier (condition met) copies regardless (it still
                # moves, so its copy fires).
                if ge._is_movement_locked(game, game.players[copier_nm], c_entry.get("action") or {}) \
                        and o_info.get("effect") in ge.MOVEMENT_EFFECTS:
                    continue
                # pin the cards the copy will move (the facing card's zone-moving
                # effect, applied with the copier as the actor) before the engine pops them
                ev = _effect_events(o_info.get("effect"), o_info.get("effect_number"))
                copy_discard = None
                if ev:
                    kind, n, own = ev
                    target = game.players[copier_nm] if own else _other_player(game, game.players[copier_nm])
                    if target is not None:
                        ctx = ctxs[target.name]
                        if kind == "draw":
                            _pick_draw(target, n, ctx, cur_turn)
                        elif kind == "ramp":
                            _pick_ramp(target, n, ctx, cur_turn)
                        elif kind == "discard":
                            if (game.engine_version or 0) < 13:
                                _pick_discard(target, n, ctx, cur_turn)
                            else:
                                # discard selection (v13): the copied discard PAUSES
                                # the engine (pending_discard) -> apply the choice
                                # after apply_copy_effect (mirrors the real game)
                                copy_discard = (target, _pick_discard(target, n, ctx, cur_turn, arrange=False))
                        elif kind == "tax":
                            _pick_tax(target, n, ctx)
                ge.apply_copy_effect(game, game.players[copier_nm], c_entry['action'],
                                     game.players[facing_nm], o_entry['action'])
                if copy_discard is not None and game.pending_discard is not None and game.state != "game over":
                    _apply_discard_choice(game, copy_discard[0], copy_discard[1])

            if over:
                # mid-chain win: mirror the engine's flush_unresolved - push the
                # UNPROCESSED PLAY cards to their owners' discard (rooted cards are
                # NOT flushed - they are in rooted_on_board and are discarded by
                # _process_rooted_cards at the end of the turn)
                for nm, chain in ((first_name, chain_f), (second_name, chain_s)):
                    pl = game.players[nm]
                    # count the resolved plays for this player (in the actions list)
                    resolved_plays = sum(1 for a in actions
                                         if a.get(nm) is not None and a[nm].get("kind") == "play")
                    # flush the unprocessed plays (beyond the resolved count)
                    play_idx = 0
                    for entry in chain:
                        if entry['kind'] != 'play':
                            continue
                        play_idx += 1
                        if play_idx > resolved_plays:
                            cards = (entry['action'] or {}).get('cards') or []
                            if cards:
                                if pl.discard is None:
                                    pl.discard = []
                                pl.discard.extend(cards)
                            # doctors (engine_version 16): flush the attached pending card too
                            _pc = (entry['action'] or {}).get('pending_card')
                            if _pc and (game.engine_version or 0) >= 16:
                                pl.discard.append(_pc)
                return actions
            if p >= max_pos:
                return actions
            p += 1
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

    # One-off historical artifact: a few OLD games (created before the mid-chain-win
    # flush fix in ge.process_trip_chain) lost their unprocessed play cards - the
    # cards are missing from ALL final zones. The current engine conserves every
    # card (flushed to discard on a mid-chain win), so for those old games we
    # reconstruct the missing cards in the discard to complete the identity pool.
    state_dict = dict(state_dict)
    fixed_players: dict[str, dict] = {}
    for n, p in state_dict["players"].items():
        final_set: set[str] = set()
        for z in ("hand", "deck", "discard", "mana"):
            final_set.update(p.get(z) or [])
        if p.get("dwelling"):   # dwelling (engine_version 12): a card outside the four zones
            final_set.add(p["dwelling"])
        final_set.update(p.get("pendings") or [])   # pending cards (doctors, engine_version 16)
        hist = {c for m in (p.get("messages_history") or []) for c in (m.get("cards") or [])}
        missing = sorted(hist - final_set)
        p = dict(p)
        if missing:
            p["discard"] = list(p.get("discard") or []) + missing
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
    day_night_fixed = False   # Mages Celestial_reversal (engine_version 22): once a
    # Celestial_reversal card is played, the day/night is FIXED (set to the player's
    # choice) and no longer flips each turn - the flip below is then skipped.
    nobodymoves_active = False  # Mages nobodymoves (engine_version 23): once a
    # nobodymoves card is played, ALL players' MOVEMENT is LOCKED for the rest of the turn
    # (their MOVE cards are canceled, only unstoppable may advance). Reset every turn
    # (the block lasts until the end of the turn, like landmine).
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
        events: list[dict] = []   # board / dwelling actions (NOT part of the trip chain)
        chains: dict[str, list[dict]] = {}
        for n in names:
            chain = []
            p = game.players[n]
            for m in seg_t[n]["moves"]:
                if m.get("mode") == "pass":
                    continue
                # discard selection (engine_version 13): the CHOICE message (to
                # 'discard_pile') is NOT part of the trip chain - it was made DURING
                # resolution (pause/resume) and its cards were already removed from
                # the hand by the discard effect (applied in _replay_trip_chain).
                if m.get("to") == "discard_pile":
                    continue
                # dwelling actions (engine_version 12): PLACE (1 card -> the dwelling
                # slot) or TAP (no cards -> draw 1). They are NOT part of the trip
                # chain - they resolved immediately at play time (mirror of ge.player_play).
                if m.get("to") == "dwelling" and (game.engine_version or 0) >= 12:
                    if m.get("mode") == "dwelling_activation":
                        if p.dwelling == ge.MAGE_BLACK_HOLE and (game.engine_version or 0) >= 26 \
                                and m.get("rotation") in ("cw", "ccw"):
                            # black_hole tap (Mages, engine_version 26): ROTATES THE EARTH 3
                            # CELLS in the player's chosen direction. A QUICK action (no trip
                            # chain) processed in the play-phase message loop, BEFORE the trip
                            # chain resolves — mirroring the engine (the tap is a quick action,
                            # so the rotation is applied before the chain's biome conditions /
                            # biome bonus / cataclysm knockback are evaluated). The 4 biomes
                            # shift position in game.earth; every token stays on its cell index.
                            _buf_bh = io.StringIO()
                            with contextlib.redirect_stdout(_buf_bh):
                                ge.rotate_earth(game, +3 if m["rotation"] == "cw" else -3)
                            if p.dwelling:
                                p.dwelling_tapped = True
                                events.append({"player": n, "type": "black_hole_tap",
                                              "card": "black_hole", "rotation": m["rotation"]})
                        elif p.dwelling == "laboratory" and (game.engine_version or 0) >= 16:
                            # laboratory tap (doctors, v16): adds an 'epo' pending card (NOT a draw)
                            if p.pendings is None: p.pendings = []
                            if p.pending_slots is None: p.pending_slots = []
                            p.pendings.append("epo")
                            if (game.engine_version or 0) >= 19:
                                # v19: the tap is a QUICK action — the 'epo' gets NO
                                # placeholder entry (pending_slots is the per-turn list)
                                pass
                            elif (game.engine_version or 0) == 18:
                                # v18: the tap is a QUICK action — the 'epo' occupies NO
                                # trip-chain position (pending_slots stays parallel: None)
                                p.pending_slots.append(None)
                            else:
                                p.pending_slots.append(int(ge._player_stopover(game, n, len(chain)).rsplit('_', 1)[-1]) if (game.engine_version or 0) >= 15 else 4 - min(len(p.pending_slots) - 1, 4))
                            p.dwelling_tapped = True
                            events.append({"player": n, "type": "laboratory_tap", "card": "epo"})
                        else:
                            # refinery tap: draw 1 card (pin the identity, then the real engine draw)
                            _pick_draw(p, 1, ctxs[n], t)
                            _buf_tap = io.StringIO()
                            with contextlib.redirect_stdout(_buf_tap):
                                ge._draw_cards(p, 1)
                            if p.dwelling:
                                p.dwelling_tapped = True
                                events.append({"player": n, "type": "dwelling_tap", "card": p.dwelling})
                    else:
                        cids_d = m.get("cards") or []
                        cid_d = cids_d[0] if cids_d else None
                        if cid_d and cid_d in (p.hand or []):
                            p.hand.remove(cid_d)
                            p.dwelling = cid_d
                            # placeholder slot (stopover-skip rule, engine_version 14;
                            # per-player positions, engine_version 15): must match the
                            # engine's. v15: the dwelling placeholder takes the NEXT
                            # position in this player's OWN chain — ge._player_stopover
                            # (rooted count + plays so far + 1). v14: the k-th FREE stopover
                            # (shared skip) — ge._next_free_stopover_v14. len(chain) = the
                            # play messages this player sent BEFORE this dwelling message
                            # this turn (= the engine's played_count at placement time).
                            # Needed so the _process_rooted_cards mirror skips this column
                            # exactly like the live engine did (rooted_on_board is compared
                            # WITH stopovers at the end). For games with engine_version < 14
                            # the skip rule did not exist (legacy plain convention), so the
                            # slot is left unset and the placement stays plain.
                            if (game.engine_version or 0) >= 15:
                                p.dwelling_slot = int(ge._player_stopover(game, n, len(chain)).rsplit('_', 1)[-1])
                            elif (game.engine_version or 0) >= 14:
                                p.dwelling_slot = int(ge._next_free_stopover_v14(game, len(chain), n).rsplit('_', 1)[-1])
                            events.append({"player": n, "type": "dwelling_place", "card": cid_d})
                        elif cid_d:
                            warnings.append(f"turn {t} - {n}: dwelling card {cid_d} not found in hand (reconstruction)")
                        p.mana_spend += _card_cost(cid_d or "")
                    continue
                # pending zone (doctors, engine_version 16): PLACE a pending card in the
                # pending zone (NOT part of the trip chain — resolved at play time).
                if m.get("to") == "pending_zone" and (game.engine_version or 0) >= 16:
                    cids_p = m.get("cards") or []
                    cid_p = cids_p[0] if cids_p else None
                    if cid_p:
                        if cid_p in (p.hand or []):
                            p.hand.remove(cid_p)
                        if p.pendings is None:
                            p.pendings = []
                        if p.pending_slots is None:
                            p.pending_slots = []
                        p.pendings.append(cid_p)
                        if (game.engine_version or 0) >= 15:
                            _slot = int(ge._player_stopover(game, n, len(chain)).rsplit('_', 1)[-1])
                            # v19: placeholder is a [card, slot] pair (per-turn list, NOT
                            # parallel to the persistent pendings zone); v16-18: bare int
                            if (game.engine_version or 0) >= 19:
                                p.pending_slots.append([cid_p, _slot])
                            else:
                                p.pending_slots.append(_slot)
                        else:
                            p.pending_slots.append(4 - min(len(p.pending_slots), 4))
                        events.append({"player": n, "type": "pending_place", "card": cid_p})
                    p.mana_spend += _card_cost(cid_p or "")
                    continue
                cids = m.get("cards") or []
                if len(cids) != 1:
                    warnings.append(f"turn {t} - {n}: message with {len(cids)} cards (ignored)")
                    continue
                cid = cids[0]
                if _card_row(cid) is None:
                    warnings.append(f"turn {t} - {n}: card unknown to the cardpool (ignored)")
                    continue
                # engineer drop (engine_version 12): the drop token is placed INSTANTLY
                # at play time on the message's 'cell' - record the event for the UI
                if (game.engine_version or 0) >= 12 and cid in ge.ENGINEER_DROPS and m.get("cell") is not None:
                    events.append({"player": n, "type": "drop_place", "card": cid, "cell": int(m["cell"])})
                # Mages Celestial_reversal (engine_version 22): the INSTANT effect fired
                # at play time (BEFORE the trip chain) - the day/night is FIXED to the
                # player's choice (message 'day_night') for the rest of the game. The
                # card itself is a no-op on the chain (support card: advancing 0).
                if ((game.engine_version or 0) >= 22 and cid == ge.MAGE_CELASTIAL_REVERSAL
                        and m.get("day_night") in ("day", "night")):
                    day_night = m["day_night"]
                    day_night_fixed = True
                    game.day_night = day_night   # keep the ENGINE state in sync (is_condition_met reads it)
                    events.append({"player": n, "type": "celestial_reversal", "card": cid, "day_night": day_night})
                # Mages thermic_flux (engine_version 24): the INSTANT effect fired at
                # play time (BEFORE the trip chain) - the planet temperature changes by
                # ±4 °C to the player's choice (message 'temp_change': 'up'/'down'),
                # CLAMPED to 1..20, PERMANENTLY. The card itself is a no-op on the chain
                # (support card: advancing 0). Mutate game.temperature so the temp_*
                # conditions read the new value at resolution time.
                if ((game.engine_version or 0) >= 24 and cid == ge.MAGE_THERMIC_FLUX
                        and m.get("temp_change") in ("up", "down")):
                    _old_temp = game.temperature
                    _delta = 4 if m["temp_change"] == "up" else -4
                    _new_temp = max(1, min(20, (_old_temp or 0) + _delta))
                    game.temperature = _new_temp   # keep the ENGINE state in sync (is_condition_met reads it)
                    events.append({"player": n, "type": "thermic_flux", "card": cid,
                                   "from": _old_temp, "to": _new_temp})
                # Mages nobodymoves (engine_version 23): the INSTANT effect fired at play
                # time (BEFORE the trip chain) - ALL players are BLOCKED for the rest of
                # the turn (MOVE cards canceled, only unstoppable may advance). The card
                # itself is a no-op on the chain (support card: advancing 0). The block
                # is game-level and applies to the trip chain resolution below.
                if (game.engine_version or 0) >= 23 and cid == ge.MAGE_NOBODYMOVES:
                    nobodymoves_active = True
                    game.nobodymoves_active = True   # keep the ENGINE state in sync (process_card reads it)
                    events.append({"player": n, "type": "nobodymoves", "card": cid})
                # Mages Apocalypticritual (engine_version 25): the INSTANT effect fired
                # at play time (BEFORE the trip chain) - the ORDER OF ALL 4 CATACLYSM
                # CARDS is SET to the player's choice (message 'cataclysm_order': a
                # permutation of the 4 biomes, index 0 strikes next), PERMANENTLY
                # (until the next ritual). The card itself is a no-op on the chain
                # (support card: advancing 0). Mutate game.cataclysm_pile so the
                # real trigger_cataclysm (called by process_card) reads the new order.
                if ((game.engine_version or 0) >= 25 and cid == ge.MAGE_APOCALYPTICRITUAL
                        and isinstance(m.get("cataclysm_order"), list)):
                    game.cataclysm_pile = list(m["cataclysm_order"])   # keep the ENGINE state in sync (trigger_cataclysm reads it)
                    events.append({"player": n, "type": "apocalypticritual", "card": cid,
                                   "order": list(m["cataclysm_order"])})
                if cid in (p.hand or []):
                    p.hand.remove(cid)
                p.mana_spend += _card_cost(cid)
                # doctors (engine_version 16): the pending card was consumed from the
                # zone at play time (the engine removes it from player.pendings in
                # player_play). Mirror that here so the replay's pending zone matches.
                _pcard = m.get("pending_card")
                if _pcard and p.pendings and _pcard in p.pendings and (game.engine_version or 0) >= 16:
                    _pidx = p.pendings.index(_pcard)
                    p.pendings = p.pendings[:_pidx] + p.pendings[_pidx+1:]
                    if (game.engine_version or 0) >= 20:
                        # v20: the attached pending card's placeholder STAYS IN PLACE
                        # (it marks the consumed trip-chain position) — pending_slots
                        # is untouched (the engine keeps the [card, slot] pair)
                        pass
                    elif (game.engine_version or 0) >= 19:
                        # v19: remove the attached card's placeholder BY CARD NAME
                        # (the first matching [card, slot] pair) — the index in
                        # `pendings` does NOT index the per-turn placeholder list
                        _new_slots, _removed = [], False
                        for _e in (p.pending_slots or []):
                            if (not _removed and isinstance(_e, (list, tuple)) and len(_e) == 2
                                    and _e[0] == _pcard):
                                _removed = True
                                continue
                            _new_slots.append(_e)
                        p.pending_slots = _new_slots
                    elif p.pending_slots and len(p.pending_slots) > _pidx:
                        p.pending_slots = p.pending_slots[:_pidx] + p.pending_slots[_pidx+1:]
                chain.append(m)
            chains[n] = chain

        positions_before = {n: game.players[n].current_position or 0 for n in names}

        # --- trip chain resolution (real engine code) ---
        actions = _replay_trip_chain(game, turn_order, chains, ctxs, t)
        positions_after = {n: game.players[n].current_position or 0 for n in names}

        # rooted (engine_version 10): settle the rooted cards NOW (mirror of the
        # engine's ge._process_rooted_cards, called in handle_websocket_message right
        # after the trip chain, in ALL end-of-turn cases). The engine's process_card
        # already granted the tokens (rooted_this_turn); this pulls the survivors out
        # of the discard onto free stopovers (rooted_on_board) and discards last
        # turn's rooted cards. A no-op for games with engine_version < 10.
        _buf_rooted = io.StringIO()
        with contextlib.redirect_stdout(_buf_rooted):
            ge._process_rooted_cards(game)

        turns_out.append({
            "turn": t,
            "day_night": day_night,
            "order": list(turn_order),
            "mana_puts": mana_puts,
            "hands_start": hands_start,
            "actions": actions,
            "events": events,
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
        # flip day/night each new turn - SKIPPED once a Mages Celestial_reversal card
        # (engine_version 22) fixed it (day_night_fixed set above; the engine skips the
        # same flip in _end_turn when game.day_night_fixed is True).
        if not day_night_fixed:
            day_night = "night" if day_night == "day" else "day"
            game.day_night = day_night   # keep the ENGINE state in sync (is_condition_met reads it)
        # nobodymoves (engine_version 23): the movement lock lasts until the end of
        # the turn - reset the game-level flag (the engine clears it in _end_turn).
        # A no-op for games < 23 (the field defaults to False and is never set).
        nobodymoves_active = False
        game.nobodymoves_active = False
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

    # pet_trap (engine_version 8): the unconsumed drop tokens left on the board
    # must match the stored ones (tokens consumed by triggers are gone from both)
    stored_drops = {int(k): int(v) for k, v in (state_dict.get('drop_tokens') or {}).items()}
    replayed_drops = {int(k): int(v) for k, v in (game.drop_tokens or {}).items()}
    if stored_drops != replayed_drops:
        warnings.append(f"drop tokens diverge (replayed {replayed_drops}, stored {stored_drops})")

    # rooted (engine_version 10): the rooted cards sitting on the board must match
    # the stored ones (same cards, same owners, same stopovers). Empty for games
    # with engine_version < 10 (the rooted effect is a no-op there).
    stored_rooted = sorted(
        (e.get('card_id'), e.get('owner'), e.get('stopover'))
        for e in (state_dict.get('rooted_on_board') or [])
    )
    replayed_rooted = sorted(
        (e.get('card_id'), e.get('owner'), e.get('stopover'))
        for e in (game.rooted_on_board or [])
    )
    if stored_rooted != replayed_rooted:
        warnings.append(f"rooted on board diverge (replayed {replayed_rooted}, stored {stored_rooted})")

    # engineers' drops (engine_version 12): the unconsumed drop tokens left on the
    # board must match the stored ones (tokens consumed by triggers are gone from both)
    stored_bdrops = sorted(
        (e.get('cell'), e.get('kind'), e.get('owner'))
        for e in (state_dict.get('board_drops') or [])
    )
    replayed_bdrops = sorted(
        (e.get('cell'), e.get('kind'), e.get('owner'))
        for e in (game.board_drops or [])
    )
    if stored_bdrops != replayed_bdrops:
        warnings.append(f"engineer drops diverge (replayed {replayed_bdrops}, stored {stored_bdrops})")

    # dwelling (engine_version 12): the dwelling card on the board (outside the
    # four zones) must match the stored one for both players
    for n in names:
        rp_d = game.players[n].dwelling or None
        sp_d = state_dict["players"][n].get("dwelling") or None
        if rp_d != sp_d:
            warnings.append(f"{n}: dwelling diverges (replayed {rp_d}, stored {sp_d})")

    # thermic_flux (engine_version 24): the replay tracks the temperature from the
    # initial rolled value + the thermic_flux changes; the final tracked value must
    # match the stored `temperature`. (For games < 24 the replay uses the stored value
    # as-is, so this is always equal.)
    stored_temp = state_dict.get("temperature")
    replayed_temp = game.temperature
    if stored_temp != replayed_temp:
        warnings.append(f"temperature diverges (replayed {replayed_temp}, stored {stored_temp})")

    # Apocalypticritual (engine_version 25) + cataclysm triggers: the replay tracks
    # the pile from the reconstructed initial order + the ritual reorders + the
    # trigger rotations; the final tracked pile must match the stored one.
    stored_pile = list(state_dict.get("cataclysm_pile") or [])
    replayed_pile = list(game.cataclysm_pile or [])
    if stored_pile != replayed_pile:
        warnings.append(f"cataclysm pile diverges (replayed {replayed_pile}, stored {stored_pile})")

    # black_hole (engine_version 26): the replay reconstructs the INITIAL earth (the
    # stored final earth reversed by the total rotation) and applies each black_hole tap
    # in order; the final biome codes must match the stored final earth. (Tokens are
    # addressed by cell index and are checked separately via the final positions.)
    if (game.engine_version or 0) >= 26:
        stored_earth_codes = [c[0] for c in (state_dict.get("earth") or []) if c]
        replayed_earth_codes = [c[0] for c in (game.earth or []) if c]
        if stored_earth_codes != replayed_earth_codes:
            warnings.append(f"earth biomes diverge (replayed {replayed_earth_codes}, stored {stored_earth_codes})")

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
        # in-progress game with a pending trip chain: the stored state holds a card
        # that was PLAYED but not yet RESOLVED (the opponent never passed), while a
        # full-history replay always resolves everything -> the stored state can
        # never verify. Skip it (analyze_game still works for the analysis UI).
        _state = st.get("state") or ""
        if _state.startswith("turn") and _state.endswith("to play"):
            print(f"SKIP {gid}  (in progress - unresolved trip chain)")
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
