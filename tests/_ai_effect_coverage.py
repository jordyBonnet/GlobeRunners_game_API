"""AI effect-coverage audit (manual smoke test, run from the project root).

Question it answers: does the Robot actually USE every mechanic it is supposed to -
every main-faction special effect (avalanche / effect_canceled / rooted / pet_trap /
swap_cards / copy_effect), every support-faction card (Engineers drops + refinery,
Doctors pendings + laboratory, Mages instants + black_hole), including the
*choices* those cards need (cell, day_night, temp_change, cataclysm_order, rotation,
swap_with, pendings attach)? Every attachment is additionally verified ENGINE-side: the
engine's own `pending_card` stamp on the accepted message (so "the pending really left my
pending zone and rode that main card"), and the turn-log note written by
`ge._apply_pending_effect` (so "the attached pending really produced its effect").

How: plays real AI-vs-AI games with the production driver (`ai_driver.ai_decide` +
`player_ai.PlayerAI`, driven by `balance_lab/run_lab._drive`) into TEMP shard DB files
(never games.db / balance_lab.db), then reads each final state:
  * `player.messages_history` = every ACCEPTED message of the game (what the robot did),
  * `game.log` = the per-turn resolution log (condition_met / effect / negatives / notes)
    => whether the effect actually FIRED at resolution time,
  * the final zones => where every support card ended up (played vs burned into the mana
    zone: a card that finishes the game in its owner's mana zone was never played).

Usage:
    python tests/_ai_effect_coverage.py                      # 2 games per pairing
    python tests/_ai_effect_coverage.py --games 6 --workers 8
    python tests/_ai_effect_coverage.py --only Dwa-doc,Mia-doc
"""

from __future__ import annotations

import argparse
import collections
import multiprocessing as mp
import os
import random
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LAB = os.path.join(ROOT, "balance_lab")
for _p in (ROOT, os.path.join(ROOT, "game_ui"), LAB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import engine.game_engine as ge                        # noqa: E402
import decks as lab_decks                              # noqa: E402
import run_lab                                         # noqa: E402
from deck_rules import load_support_cards              # noqa: E402

POOL = {r["card_id"]: r for r in ge.get_cardpool().iter_rows(named=True)}
_SUP = load_support_cards()
SUPPORT_KIND = {r["card_name"]: r["support_faction_name"]
                for r in ([] if _SUP.is_empty() else _SUP.to_dicts())}

# the special main-faction effect of each faction (mirror of game_ui/ai_deck.SPECIAL_EFFECT)
SPECIAL_EFFECT = {
    "Dwarves": "avalanche", "Demons": "effect_canceled", "Twigs": "rooted",
    "Miaous": "pet_trap", "Orcs": "swap_cards", "Mummies": "copy_effect",
}
ALL_EFFECTS = sorted({r["effect"] for r in POOL.values()})
ALL_CONDS = sorted({r["condition"] for r in POOL.values()})


def classify(msg: dict) -> tuple[str, str]:
    """(bucket, card-name) of one accepted message."""
    mode = msg.get("mode") or ""
    to = msg.get("to") or ""
    cards = msg.get("cards") or []
    if mode == "pass":
        return "pass", ""
    if to == "mana":
        return "mana_place", ""
    if mode == "dwelling_activation":
        return "dwelling_tap", ""
    if to == "dwelling":
        return "dwelling_place", cards[0] if cards else "?"
    if to == "pending_zone":
        return "pending_place", cards[0] if cards else "?"
    if to == "discard_pile":
        return "discard_choice", ""
    if mode == "defend":
        return "defend", ",".join(cards)
    if mode == "move" and len(cards) == 1:
        c = cards[0]
        if c in SUPPORT_KIND:
            if c in ("boost", "trampoline", "gluetrap", "landmine"):
                return "drop_play", c
            return f"support_{SUPPORT_KIND[c]}", c
        row = POOL.get(c)
        return (f"eff:{row['effect']}" if row else "unknown"), c
    return "other", ",".join(cards)


# --------------------------------------------------------------------------- worker
def worker_init(tmp_dir: str) -> None:
    """One PRIVATE SQLite shard per worker process (the engine writes one game per tick)."""
    import engine.game_engine as wge
    shard = os.path.join(tmp_dir, f"cov_{os.getpid()}.db")
    wge.DB_PATH = shard
    conn = sqlite3.connect(shard)
    conn.execute("CREATE TABLE IF NOT EXISTS games (game_id TEXT PRIMARY KEY, state_json TEXT)")
    conn.commit()
    conn.close()


def _cleanup(wge, gid: str) -> None:
    """Delete the game this worker created (the audit never leaves a game in its shard)."""
    try:
        conn = sqlite3.connect(wge.DB_PATH)
        conn.execute("DELETE FROM games WHERE game_id = ?", (gid,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def play_one(task):
    """One AI-vs-AI game -> the counters this audit cares about (worker side)."""
    import engine.game_engine as wge
    from models import PlayerState
    label_a, label_b, seed = task
    out = {"plays": collections.Counter(), "support": collections.Counter(),
           "attach": collections.Counter(), "attach_eng": collections.Counter(),
           "pending_eff": collections.Counter(), "epo_trace": collections.Counter(),
           "swap": collections.Counter(),
           "fired": collections.Counter(), "cond": collections.Counter(),
           "cond_met": collections.Counter(), "support_end": collections.Counter(),
           "statuses": collections.Counter(), "seats": 0, "sup_seats": collections.Counter(),
           "bug": collections.Counter(), "defend": collections.Counter()}
    random.seed(seed)
    try:
        with run_lab._quiet():
            deck_a = run_lab.build_valid_deck(label_a)
            deck_b = run_lab.build_valid_deck(label_b)
            gid = wge.create_new_game(PlayerState(name="P1", deck=deck_a).model_dump())
            wge.p2_connect_to_game(PlayerState(name="P2", deck=deck_b).model_dump(), gid)
            try:
                status, _t, game = run_lab._drive(wge, gid, 4000, 150)
            finally:
                _cleanup(wge, gid)          # the audit leaves no game behind
    except Exception as e:                              # noqa: BLE001 - the audit must not die
        out["statuses"][f"error {type(e).__name__}"] += 1
        return out
    out["statuses"][status] += 1
    # `epo` is the one pending whose engine-side effect ALWAYS leaves a trace in the turn log
    # (a "+1 advancing" note when it fires, a "suppressed" note under nobodymoves), so its whole
    # lifecycle can be split without guessing: attached -> {fired, suppressed, resolved-but-no
    # effect, never resolved/logged}. Keys are (player, stopover, card) - unique per play.
    epo_attach, epo_note, epo_logged = set(), set(), set()
    for pname, p in game.players.items():
        out["seats"] += 1
        # where did the 10 support cards of the deck end up? (mana = burned as a token)
        owned = collections.Counter(
            list(p.deck or []) + list(p.hand or []) + list(p.discard or [])
            + list(p.mana or []) + list(p.pendings or []) + ([p.dwelling] if p.dwelling else []))
        facs = {SUPPORT_KIND[c] for c in owned if c in SUPPORT_KIND}
        if len(facs) == 1:
            out["sup_seats"][facs.pop()] += 1
        for c in owned:
            if c in SUPPORT_KIND:
                for z in ("mana", "hand", "discard", "deck", "pendings"):
                    out["support_end"][(c, z)] += list(getattr(p, z) or []).count(c)
                if p.dwelling == c:
                    out["support_end"][(c, "on_board")] += 1
        for msg in (p.messages_history or []):
            bucket, card = classify(msg)
            out["plays"][bucket] += 1
            if bucket.startswith("support_") or bucket in ("drop_play", "dwelling_place",
                                                           "dwelling_tap", "pending_place"):
                out["support"][card or bucket] += 1
            if bucket == "eff:swap_cards":
                out["swap"]["with swap_with" if msg.get("swap_with") else "NO swap_with"] += 1
            if msg.get("pendings"):
                out["attach"][msg["pendings"][0]] += 1
                if (msg.get("mode") or "") != "move":
                    out["bug"]["pending attached to a NON-move play"] += 1
                # ENGINE side of the same fact. `player_play` removes the card from the pending
                # zone and stamps `pending_card` on the message (game_engine.py:1078-1091), and
                # `messages_history` is appended ONLY when the play was accepted (same function,
                # `if success:`) - so a stamp here means "the engine consumed this pending out of
                # my zone and put it on this main card". No stamp = the request never landed.
                stamp = msg.get("pending_card")
                if stamp:
                    out["attach_eng"][stamp] += 1
                    if stamp != msg["pendings"][0]:
                        out["bug"]["engine consumed a DIFFERENT pending than requested"] += 1
                    if stamp == "epo":
                        epo_attach.add((p.name, msg.get("to"), (msg.get("cards") or [""])[0]))
                else:
                    out["bug"]["pendings requested, engine consumed nothing"] += 1
            if bucket == "defend":
                for cid in (msg.get("cards") or []):
                    if cid in SUPPORT_KIND:
                        out["bug"]["defended WITH a support card (no shield) - never valid"] += 1
                    elif (POOL.get(cid) or {}).get("condition") == "block":
                        out["defend"]["block-condition card used in a defend (#30)"] += 1
            if bucket in ("drop_play", "support_mages") and (msg.get("pendings") or []):
                out["bug"]["pending attached to a support play - never valid"] += 1
        if p.dwelling:
            out["support"][p.dwelling] += 0     # keep the key present
    for turn in (game.log or []):
        for sv in (turn.get("stopovers") or []):
            for e in (sv.get("entries") or []):
                if (e.get("mode") or "") != "move":
                    continue
                row = POOL.get((e.get("cards") or [""])[0])
                if row is None:
                    continue        # support card: no pool row (no-op on the trip chain)
                out["cond"][row["condition"]] += 1
                if e.get("condition_met"):
                    out["cond_met"][row["condition"]] += 1
                    negs = e.get("negatives") or []
                    state = ("canceled" if any("canceled" in n for n in negs)
                             else "blocked" if any("blocked" in n for n in negs) else "FIRED")
                else:
                    state = "cond NOT met"
                out["fired"][(row["effect"], state)] += 1
                key = (e.get("player"), e.get("to"), (e.get("cards") or [""])[0])
                epo_logged.add(key)
                # Did the ATTACHED pending actually DO something? `_apply_pending_effect`
                # (game_engine.py:2311) writes its own note on the entry - this is the only
                # evidence that the consumed card produced its +1 / -1 / discard, as opposed to
                # being eaten by a not-met condition, a block or a cancel after the AI decided.
                for note in (e.get("notes") or []):
                    if "pending epo" in note:
                        out["pending_eff"]["epo suppressed" if "suppress" in note
                                           else "epo applied"] += 1
                        if "suppress" not in note:
                            epo_note.add(key)
                    elif "pending virus" in note:
                        out["pending_eff"]["virus suppressed" if "suppress" in note
                                           else "virus applied"] += 1
                    elif "pending bloodtest" in note:
                        out["pending_eff"]["bloodtest: opponent had no card"] += 1
                if e.get("_pending_discard"):
                    # bloodtest armed: the chain PAUSED for the opponent's choice. The engine
                    # pops this key when the choice arrives, so it is rarely in the FINAL state.
                    out["pending_eff"]["bloodtest: opponent discard armed"] += 1
    out["epo_trace"]["attached"] += len(epo_attach)
    out["epo_trace"]["fired the +1"] += len(epo_attach & epo_note)
    out["epo_trace"]["resolved, produced nothing"] += len((epo_attach & epo_logged) - epo_note)
    out["epo_trace"]["never resolved/logged (mid-chain win)"] += len(epo_attach - epo_logged)
    return {"seats": out["seats"],
            "plays": dict(out["plays"]), "support": dict(out["support"]),
            "attach": dict(out["attach"]), "attach_eng": dict(out["attach_eng"]),
            "pending_eff": dict(out["pending_eff"]), "epo_trace": dict(out["epo_trace"]),
            "swap": dict(out["swap"]),
            "statuses": dict(out["statuses"]), "sup_seats": dict(out["sup_seats"]),
            "bug": dict(out["bug"]), "defend": dict(out["defend"]),
            "cond": dict(out["cond"]), "cond_met": dict(out["cond_met"]),
            "fired": {f"{a}|{b}": n for (a, b), n in out["fired"].items()},
            "support_end": {f"{a}|{b}": n for (a, b), n in out["support_end"].items()}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=2, help="games per pairing")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--only", default="")
    ap.add_argument("--max-ram-gb", type=float, default=2.0,
                    help="RAM budget for the worker Pool (default 2.0) - see balance_lab/README.md")
    args = ap.parse_args()

    labels = [x.strip() for x in args.only.split(",") if x.strip()] or lab_decks.DECK_LABELS
    pairings = [(labels[i], labels[(i + 5) % len(labels)]) for i in range(len(labels))]
    tasks = [(la, lb, 1000 + 7919 * (k * 100 + g))
             for k, (la, lb) in enumerate(pairings) for g in range(args.games)]

    tmp = tempfile.mkdtemp(prefix="ai_cov_")
    shard = os.path.join(tmp, f"cov_{os.getpid()}.db")
    # RAM guard, same sizing as balance_lab/run_lab.py: a Pool worker keeps ~12 MB of engine state
    # per FINISHED game (it is never given back), so a long-lived worker on a big --games count
    # grows without bound. Recycle each worker and cap how many run at once with the live free-RAM
    # reading - asking for 12 workers on a machine that cannot hold them really runs fewer.
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "balance_lab"))
        import run_lab
        workers, ram_note = run_lab.plan_workers(args.workers, 20, args.max_ram_gb)
    except Exception as exc:                                    # pragma: no cover - diagnostics only
        workers, ram_note = max(1, args.workers), f"run_lab guard unavailable ({exc})"
    print(f"[ram] {ram_note}")
    if workers > 1:
        with mp.Pool(workers, initializer=worker_init, initargs=(tmp,), maxtasksperchild=20) as pool:
            results = pool.map(play_one, tasks, chunksize=1)
    else:
        worker_init(shard)
        results = [play_one(t) for t in tasks]
    shutil.rmtree(tmp, ignore_errors=True)

    (plays, support, attach, attach_eng, pending_eff, epo_trace, swap, fired, cond, cond_met,
     support_end) = (collections.Counter() for _ in range(11))
    statuses, sup_seats, bugs, defends = (collections.Counter() for _ in range(4))
    seats = 0
    for r in results:
        seats += r.get("seats", 0)
        for name, ctr in (("plays", plays), ("support", support), ("attach", attach),
                          ("swap", swap), ("statuses", statuses), ("sup_seats", sup_seats),
                          ("fired", fired), ("cond", cond), ("cond_met", cond_met),
                          ("support_end", support_end), ("bug", bugs), ("defend", defends),
                          ("attach_eng", attach_eng), ("pending_eff", pending_eff),
                          ("epo_trace", epo_trace)):
            ctr.update({k: v for k, v in r.get(name, {}).items()})

    W = print
    W("=" * 78)
    W(f"AI EFFECT COVERAGE — {sum(statuses.values())} games ({dict(statuses)}), {seats} seat-games")
    W(f"support-faction seats: {dict(sup_seats)}")
    W("=" * 78)

    W("\n--- SUPPORT faction cards: plays (accepted) / where their copies ENDED ---")
    W(f"  {'card':22s} {'played':>7s} {'burned(mana)':>13s} {'in hand':>8s} {'left in zone':>13s} {'on board':>9s}")
    for name in sorted(SUPPORT_KIND):
        end = {z: support_end.get(f"{name}|{z}", 0) for z in ("mana", "hand", "deck", "pendings", "on_board")}
        played = support[name] + end["on_board"] + end["pendings"]
        W(f"  {name:22s} {played:7d} {end['mana']:13d} {end['hand']:8d} {end['pendings']:13d} {end['on_board']:9d}")
    W(f"  dwelling placements: {plays['dwelling_place']}   dwelling taps: {plays['dwelling_tap']}")
    W(f"  pending placements (to pending_zone): {plays['pending_place']}")
    W(f"  PENDING ATTACHMENTS: {sum(attach.values())} {dict(attach)}")
    W(f"    -> engine CONSUMED them (pending_card stamp): {sum(attach_eng.values())} "
      f"{dict(attach_eng)}")
    W(f"    -> effect APPLIED at resolution: {dict(pending_eff)}")
    # Wasted = consumed but produced nothing (the main card's condition failed / it got blocked
    # or canceled AFTER the AI attached). Aggregated over all games, so it is a lower bound on
    # per-game waste; a high number would mean the AI's `_choose_attachment` gate misjudges the
    # resolution (the gate itself refuses on not-met condition and facing cancel).
    for name in ("epo", "virus"):
        spent = attach.get(name, 0)
        done = sum(v for k, v in pending_eff.items() if k.startswith(name))
        W(f"    -> {name}: {spent} attached, {done} accounted in the log, "
          f"{max(0, spent - done)} produced nothing (blocked/canceled/not-met after the attach)")
    # exact per-play trace of epo (see play_one): the only pending whose effect always leaves a
    # note, so "wasted" here is measured, not a difference of global sums.
    W(f"    -> epo trace (per play): {dict(epo_trace)}")

    W("\n--- MAIN-FACTION special effects (plays at resolution) ---")
    for fac, eff in SPECIAL_EFFECT.items():
        by = {s: n for k, n in fired.items() if f"{eff}|" in k and k.split("|")[0] == eff
              for s in [k.split("|")[1]]}
        W(f"  {fac:9s} {eff:16s} plays={sum(by.values()):5d}  {by}")

    W("\n--- EVERY pool effect: plays at resolution, fired? ---")
    for eff in ALL_EFFECTS:
        by = {k.split("|")[1]: n for k, n in fired.items() if k.split("|")[0] == eff}
        W(f"  {eff:18s} {sum(by.values()):5d}  {by}")

    W("\n--- EVERY pool condition: move-mode plays / met at resolution ---")
    for c in ALL_CONDS:
        if cond[c] or c not in ("face_point_left", "face_point_right"):
            W(f"  {c:26s} plays={cond[c]:5d} met={cond_met[c]:5d}")

    W("\n--- swap_cards choice field / defend usage / illegal-message self-checks ---")
    for k, v in sorted(swap.items()):
        W(f"  {k}: {v}")
    for k, v in sorted(defends.items()):
        W(f"  {k}: {v}  (defend plays total: {plays['defend']})")
    W(f"  illegal messages emitted by the robot: {sum(bugs.values())} {dict(bugs)}")
    W("\n--- all accepted plays by category ---")
    for k, v in sorted(plays.items(), key=lambda kv: -kv[1]):
        W(f"  {v:6d}  {k}")
    W("")


if __name__ == "__main__":
    main()
