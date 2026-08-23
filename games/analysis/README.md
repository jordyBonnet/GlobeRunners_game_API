# Analyse des parties — GlobeRunners

Interface web pour lire `games/games.db` et analyser la phase de résolution de la
**trip chain** d'une partie sélectionnée : résumé tour par tour, cartes jouées
(art + nom uniquement, **aucun id de carte n'est affiché**), condition remplie ou
non, effet produit, et position finale de chaque joueur après chaque tour.

Chaque tour est un bloc repliable / dépliable (boutons « Tout déplier » /
« Tout replier »).

## Lancement

Depuis la racine du projet, avec l'environnement virtuel activé :

```powershell
.venv\Scripts\python.exe -m uvicorn games.analysis.app:app --port 8017
```

Puis ouvrir http://127.0.0.1:8017/ dans le navigateur.

## Contenu du dossier

| Fichier | Rôle |
|---|---|
| `replay.py` | Reconstruction de la partie depuis l'état final stocké (segmentation des tours, replay via le vrai moteur `engine/game_engine.py`, vérification finale) |
| `app.py` | Backend FastAPI : `/api/games`, `/api/game/{id}`, `/card/{id}.png` + frontend statique |
| `static/` | Frontend (HTML/CSS/JS, thème sombre, interface en français) |

## Sources de données

- **Parties** : `games/games.db` — table `games(game_id, state_json)` (état final uniquement).
- **Cartes** : `cards/cardpool.parquet` (noms, factions, coûts, effets).
- **Arts des cartes** : dossier externe
  `C:\Users\jordy\Documents\python\projects\GenAI_TCG\lib\artdesign\cards_framed_0.6`
  (`<card_id>.png`). Si un art est manquant, la carte s'affiche estompée.

## Badge de fiabilité du replay

- **✓ replay exact** : les positions et compteurs finaux recalculés correspondent
  exactement à l'état stocké dans `games.db`.
- **⚠ replay approximatif** : au moins une condition non implémentée dans le moteur
  actuel a été supposée remplie (ou la partie est corrompue) ; les positions peuvent
  diverger. Les détails figurent dans la liste d'avertissements sous l'en-tête.

## Auto-test du replay

```powershell
.venv\Scripts\python.exe -m games.analysis.replay
```

Affiche OK/FAIL pour chaque partie de `games.db`.
