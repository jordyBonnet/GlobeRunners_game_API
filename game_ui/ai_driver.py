"""Pilotage d'un joueur IA (Robot) qui joue contre l'humain dans la web UI.

L'IA agit directement sur le moteur (`ge.handle_websocket_message`), en passant par
dessus le WebSocket que l'humain utilise. La boucle lit l'état de jeu officiel dans
la base, et dès que c'est au tour du robot elle calcule son action avec PlayerAI
(player_ai/playerai.py) et la soumet.

L'humain voit les actions du robot via son polling habituel (/api/state/...) :
aucun changement de protocole n'est nécessaire.
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
    """Deck de démarrage du robot : une faction tirée au hasard, n cartes distinctes (30 par défaut).

    Même logique que le starter deck du front-end (`buildStarterDeck`) : on ne tire que
    dans une seule faction (pas de mélange), et les cartes rares sont moins fréquentes
    (pondération rare x1 / autre x3, tirage sans remise ni doublon)."""
    df = ge.get_cardpool()
    faction = random.choice(df["faction"].unique().to_list())
    sub = df.filter(pl.col("faction") == faction)

    # sac pondéré : rare x1, autre x3
    bag: list[str] = []
    for row in sub.iter_rows(named=True):
        bag.extend([row["card_id"]] * (1 if row["rare"] else 3))

    # tirage pondéré sans remise, sans doublon
    deck: list[str] = []
    while len(deck) < n and bag:
        card = random.choice(bag)
        bag.remove(card)
        if card not in deck:
            deck.append(card)
    return deck


def ai_decide(game, ai_name: str, st: dict, ai: PlayerAI):
    """Calcule l'action suivante du robot à partir de l'état courant.

    Retourne le message {'cards','to','mode','pendings'} à soumettre, ou None si le
    robot ne doit (pas encore) agir. `st` porte les flags de phase de la boucle
    (acted / fallback) pour qu'une seule action soit jouée par phase.
    """
    state = game.state
    # réinitialise les flags de phase quand l'état change
    if st.get("state") != state:
        st.update(state=state, acted=False, fallback=False)

    me = game.players.get(ai_name)
    if me is None:
        return None
    oppo = next((p for n, p in game.players.items() if n != ai_name), None)
    ai.update_player_state(me, oppo, game)

    # 1. Initialisation : poser 3 cartes en mana (sur les 6 de la main)
    init_state = f"waiting for both players to put {ge.start_cards_in_mana} cards in hand"
    if state == init_state:
        need = ge.start_cards_in_mana - len(me.mana or [])
        if need > 0 and (me.hand or []):
            cards = ai.put_mana(need, in_turn=False)
            return {"cards": cards, "to": "mana", "mode": "", "pendings": []}
        return None

    # 2. Phase mana/passe : poser exactement 1 carte en mana, ou passer
    if state == "waiting for both players to mana or pass":
        if st["acted"]:
            return None
        st["acted"] = True
        if not (me.hand or []):
            return {"cards": [], "to": "", "mode": "pass", "pendings": []}
        return ai.put_mana(1, in_turn=True)

    # 3. Phase de jeu : jouer une carte abordable (ou passer)
    m = PLAY_TURN_RE.match(state)
    if m and m.group(2) == ai_name:
        if st["fallback"]:
            st["fallback"] = False
            return {"cards": [], "to": "", "mode": "pass", "pendings": []}
        msg = ai.play_card()
        # règle d'ordre (identique au frontend) : la k-ième carte du tour va sur le
        # stopover k -> colonnes 4, 3, 2, 1, 0 (stopover 1 = colonne la plus à droite)
        if msg.get("mode") == "move" and msg.get("cards"):
            played = sum(
                1 for a in (me.action_chain or [])
                if a and a.get("mode") == "move" and a.get("cards")
            )
            msg["to"] = f"stopover_{4 - min(played, 4)}"
        return msg

    return None


async def run_ai_loop(game_id: str, ai_name: str, interval: float = 0.4):
    """Boucle en arrière-plan : dès que c'est le tour du robot, il joue.

    S'arrête quand la partie est terminée ou si la partie a disparu de la base.
    """
    ai = PlayerAI(player_state=None)
    st: dict = {}
    await asyncio.sleep(interval)   # laisse le temps au frontend humain de s'installer
    while True:
        await asyncio.sleep(interval)
        try:
            conn, _, game = ge.get_current_game(game_id)
            conn.close()
        except Exception:
            break                   # partie supprimée -> on arrête
        if game.state == "game over" or game.winner:
            break

        msg = ai_decide(game, ai_name, st, ai)
        if msg is None:
            continue

        player = game.players[ai_name].model_copy()
        player.message = msg
        try:
            resp_raw = ge.handle_websocket_message(game_id, player)
        except Exception as e:
            print(f"[ai] action failed for {ai_name}: {e}")
            continue

        # le moteur renvoie un JSON (str ou dict) avec {'message': {'success': bool, ...}}
        try:
            resp = json.loads(resp_raw) if isinstance(resp_raw, str) else resp_raw
        except Exception:
            resp = {}
        info = resp.get("message", {}) if isinstance(resp, dict) else {}
        ok = info.get("success", True)
        if not ok:
            print(f"[ai] action rejected for {ai_name}: {info.get('message')}")
            if msg.get("mode") != "pass":
                st["fallback"] = True   # prochaine décision sur la même phase -> passe
