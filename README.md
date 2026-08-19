# harness-tmdb

Un **harness multi-agents Python** qui répond à des questions en langage naturel sur un dataset TMDB statique. Chaque question est transformée en **plan d'étapes JSON** par un modèle de langage (Gemini), puis le plan est **exécuté pas à pas** par un orchestrateur déterministe avec **vérification à chaque étape** et **traçabilité complète**.

Projet pédagogique : il démontre les fondamentaux du harness engineering — boucle d'orchestration, gestion d'outils, vérification/observabilité — et n'est pas un outil de production.

---

## Vue d'ensemble

```
┌──────────────────────────────────────────────────────────────────────┐
│  question en langage naturel                                          │
│  ex. "Quels sont les 5 genres les plus rentables en ROI depuis 1997?" │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼
   ┌──────────────┐    appel Gemini (JSON imposé)     ┌────────────────┐
   │   PLANNER    │ ────────────────────────────────► │ plan d'étapes  │
   │  (LLM)       │                                   │ en JSON validé │
   └──────────────┘                                   └───────┬────────┘
                                                               ▼
   ┌──────────────┐   boucle déterministe (en Python pur)     ┌────────────┐
   │  EXECUTOR    │ ──► 1. résoudre l'outil                      │ vérifie la │
   │              │    2. exécuter l'outil                       │ sortie     │
   │              │    3. vérifier la sortie  ◄──────────────────│ (verify)   │
   │              │    4. journaliser                            └────────────┘
   └──────────────┘     (retry ×2 / échec → step failed)
                                                               ▼
   ┌──────────────┐   runs/<timestamp>.json                    ┌────────────┐
   │   MÉMOIRE    │   question, plan, résultats/étape,          │ output/    │
   │  (log JSON)  │   statuts de vérification                   │ charts PNG │
   └──────────────┘                                             └────────────┘
```

Deux responsabilités bien distinctes :
- **Planner** (seul endroit où un LLM intervient) : décide *quoi faire*.
- **Executor** (100 % déterministe) : décide *comment le faire*, en toute sécurité.

---

## Données

Deux CSV statiques, jointes 1:1 sur `movies.id` = `credits.movie_id` (**4803 films**, jointure parfaite vérifiée) :

| Fichier | Taille | Contenu principal |
|---|---|---|
| `tmdb_5000_movies.csv` | ~5.7 Mo | budget, revenue, genres, release_date, popularity, vote_average/count, runtime, original_language, ... |
| `tmdb_5000_credits.csv` | ~40 Mo | cast, crew (listes JSON) |

**Points critiques des données** (encodés dans le prompt du planner) :
- De nombreuses colonnes sont des **chaînes JSON** (`cast`, `crew`, `genres`, ...). Elles sont parsées avec `json.loads` au chargement dans `harness/tools.py`.
- `budget = 0` sur 1037 films et `revenue = 0` sur 1427 → toute analyse de rentabilité **doit** filtrer `budget > 0` (le ROI = (revenue − budget) / budget n'a de sens que si budget > 0).
- Les films s'étalent de **1916-09-04 à 2017-02-03**. « 20 dernières années » ⇒ ≥ 1997 ; « depuis 2012 » ⇒ ≥ 2012.
- Les **genres sont multi-labels** : un film appartient à plusieurs genres. Une moyenne « par genre » (ROI, revenus...) comptera un film plusieurs fois (effet portefeuille).

Aucune API TMDB : pas de réseau vers TMDB, pas de clé TMDB.

---

## Prérequis et installation

Python ≥ 3.12.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Dépendances : `google-genai` (SDK Gemini), `pandas`, `matplotlib`.

### Configuration : la clé API du modèle

Le harness appelle l'**API Gemini** (Google AI Studio) pour le planning. Rien ne fonctionne sans clé.

```bash
cp .env.example .env   # puis éditez .env
```

| Variable | Défaut | Rôle |
|---|---|---|
| `GEMINI_API_KEY` | — (obligatoire) | Clé d'API Google AI Studio |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Modèle primaire (laisser vide = défaut) |
| `GEMINI_FALLBACK_MODELS` | `gemini-flash-latest,gemini-flash-lite-latest,gemini-3.1-flash-lite` | Modèles de secours (liste CSV) |

`.env` est **gitignoré** : ne jamais le committer. On peut aussi exporter les variables (`export GEMINI_API_KEY=...`).

---

## Utilisation

```bash
.venv/bin/python -m harness.main "Quels sont les 5 genres les plus rentables en ROI depuis 1997 ?"
```

Pour les questions longues, on peut les passer sur stdin :

```bash
echo "Une très longue question détaillée..." | .venv/bin/python -m harness.main
```

Sorties :
- **Log de run** : `runs/<horodatage>.json` — question, modèle, plan, résultats par étape, vérifications.
- **Graphiques** : `output/charts/*.png`.
- **Fichiers** : `output/results/*` (outil `write_file`).

---

## Déroulement complet d'un run

1. **Boot** : `config` charge les chemins et lit `.env` (sans écraser l'environnement). Dossiers `runs/` et `output/` créés.
2. **Planner : génération du plan** — prompt système (règles données/outils) + question envoyés à Gemini avec `response_mime_type="application/json"` (sortie structurée, pas de free text). Réponse : un plan JSON `{ "answer_intent": ..., "steps": [...] }`.
3. **Planner : validation locale du plan** — chaque étape est contrôlée (outil connu, `step_id` numérique, `args` objet si nécessaire, clause `validate` valide, première étape = `load_data`). Si le JSON est invalide : **1 retentative** en renvoyant l'erreur de validation au modèle, puis échec global.
4. **Planner : garantie du graphique** — si la question contient des mots-clés visuels/classement/comparaison (`graphique`, `chart`, `plot`, `top`, `classement`, `plus`, `évolution`, ...) et qu'aucune étape `chart` n'existe, une étape `chart` est **ajoutée de façon déterministe** après le dernier `compute`. Le modèle ne peut donc pas « oublier » le graphique.
5. **Executor : boucle** — pour chaque étape du plan (voir section suivante), dans l'ordre.
6. **Finalisation** — le run est marqué `completed` (ou `aborted`), le log JSON est écrit, le récapitulatif des étapes est affiché.

Exemple de plan produit (log du run) :

```json
{
  "steps": [
    { "step_id": 1, "tool": "load_data", "args": {},
      "validate": { "kind": "nonempty" } },
    { "step_id": 2, "tool": "compute",
      "args": { "filters": ["budget > 0", "revenue > 0", "year >= 1997"],
                "groupby": ["genre"], "agg": { "roi": "mean" },
                "sort_by": "roi", "top_k": 5 },
      "validate": { "kind": "nonempty" } },
    { "step_id": 3, "tool": "chart",
      "args": { "kind": "bar", "x": "genre", "y": "roi" },
      "validate": { "kind": "nonempty" } }
  ]
}
```

---

## La boucle d'exécution (Executor)

L'état du run est porté par un **contexte** (`ctx`) partagé entre les étapes :
- `ctx["df"]` : DataFrame joint/enrichi (chargé une seule fois par le process, en cache).
- `ctx["last_result"]` : sortie du dernier `compute` (utilisée par l'étape `chart`).

Pour **chaque étape** :

```
1. résoudre l'outil via le registre (tools.TOOLS["compute"], ...)
2. exécuter l'outil avec step.args et ctx
3. vérifier la sortie via verify.check_output(step.validate, sortie)
4. journaliser la tentative (sortie + résultat de vérification)
   - réussite  → step = "success", passer à l'étape suivante
   - échec     → retry (jusqu'à 2 retentatives) puis step = "failed"
5. compter les échecs, journaliser le statut et la durée de l'étape
```

C'est un vrai **orchestrateur** : la décision de quel outil appeler et dans quel ordre vient du plan du LLM, mais l'exécution elle-même est du Python pur, déterministe et traçable.

### Outils (registre `harness/tools.py`)

| Outil | Rôle | Sortie |
|---|---|---|
| `load_data` | Lit les 2 CSV, joint sur `id`/`movie_id`, enrichit (année, profit, ROI, acteurs, réalisateurs, writers, producteurs). **Résultat mis en cache** dans `tools._df_cache` (le CSV 40 Mo n'est chargé qu'une fois). | stats sur le chargement |
| `compute` | Pipeline pandas déterministe : `filters` → `year_range` → `groupby`+`agg` → `sort_by` → `top_k`. Les colonnes-listes (`genres`, `directors`, `cast_names`, `writers`, `producers`) sont **explosées** en une ligne par nom et renommées au singulier (`genre`, `director`, `actor`, ...) pour le groupby. `sort_by` se replie sur la colonne d'agrégat si le planner invente un alias. | `{columns, count, rows}` |
| `chart` | Rend un PNG matplotlib (bar/line) depuis les `rows` du dernier `compute`. Repli x/y sur les vraies colonnes si besoin, ignore les valeurs None. Ne peut écrire que sous `output/charts/`. | `{path, count, rows}` |
| `write_file` | Écrit un fichier textuel. Ne peut écrire que sous `output/results/`. | `{path, bytes}` |

Exemples d'expressions `compute` :
```json
{"filters": ["budget > 0", "revenue > 0", "year >= 1997"],
 "groupby": ["genre"], "agg": {"roi": "mean"}, "sort_by": "roi", "top_k": 5}
```
```json
{"groupby": ["directors"], "agg": {"title": "count"}, "sort_by": "title", "top_k": 10}
```

### Vérification (`harness/verify.py`)

Chaque étape porte une clause `validate`. Trois types :

| Clause | Signification | Exemple |
|---|---|---|
| `nonempty` | la sortie contient des lignes (`count > 0`) | `{"kind": "nonempty"}` |
| `bound` | `count >= min` | `{"kind": "bound", "min": 5}` |
| `type` | le premier échantillon d'un champ est du bon type | `{"kind": "type", "field": "roi", "type": "number"}` |

Le résultat de la vérification (critère, succès/échec, détail) est enregistré pour **chaque tentative** dans le log de run — c'est le cœur de l'observabilité.

---

## Que se passe-t-il en cas d'échec ?

Il y a deux niveaux d'échec, traités différemment :

### 1. Échec d'une étape d'exécution (erreur outil ou vérification échouée)
- L'executor réessaie jusqu'à **2 retentatives** (policy `MAX_RETRIES_PER_STEP = 2`).
- Chaque tentative est journalisée avec son résultat de vérification.
- Si l'étape échoue encore → `status = "failed"`, on passe à l'étape suivante (le run **continue**, avec un résultat partiel).
- Si **plus d'une étape** a échoué → le run est marqué **`aborted`** (`MAX_FAILED_STEPS_BEFORE_ABORT = 1`) et la boucle s'arrête.
- En toutes circonstances, le run log est écrit avec la liste des échecs.

### 2. Échec des appels au modèle (Gemini)
- `gemini-flash-latest` (alias) est fréquemment saturé (503 « high demand ») — c'est normal et prévu.
- Chaque modèle est tenté jusqu'à **3 fois** (backoff 5 s entre chaque).
- Puis on passe au **modèle suivant** de `GEMINI_FALLBACK_MODELS` (le primaire `gemini-3.6-flash` est fiable, les secours sont là pour les pics).
- Les erreurs 404 (modèle inexistant) passent immédiatement au candidat suivant ; les 400/401/403 sont fatales (le problème ne dépend pas du modèle).
- Le modèle qui a réellement répondu est tracé dans la console (`answered by model=...`) et **enregistré dans le log du run**.

---

## Le Planner en détail

`harness/planner.py` + `harness/planner_prompt.py`.

- Le **prompt système** (injecté à chaque appel) encode tout le savoir non négociable du projet : contraintes des données (zéros, fenêtre 1916–2017, genres multi-labels), vocabulaire des colonnes, format exact du plan JSON, règles de groupby (explosion des listes), règle « clé d'agg = nom de colonne », et l'obligation d'inclure un `chart` pour les questions top/comparaison/évolution.
- La sortie est demandée en **JSON strict** (`response_mime_type="application/json"`), avec `temperature=0` et `max_output_tokens=8192` (limite la troncature des plans longs).
- Le JSON reçu est **re-validé localement** (pas de confiance aveugle) : structure, outils, premières étapes, clauses.
- `_ensure_chart` : garde-fou **déterministe** du graphique (voir « Déroulement »).

---

## La mémoire (logs et fichiers)

- **Un log JSON par run** dans `runs/<horodatage>.json`, contenant :
  - `question`, `model` (celui qui a répondu), horodatage ;
  - `plan` (les étapes telles que validées) ;
  - `steps` : pour chaque étape, `tool`, `args`, `status`, `duration_ms`, et **toutes les tentatives** (sortie + résultat de vérification) ;
  - `final` : récapitulatif (`steps_ok`, `steps_total`) ou erreur d'abandon.
- **Charts** : `output/charts/*.png`. **Fichiers** : `output/results/*`.
- `runs/` et `output/` sont gitignorés (artefacts de run, pas du code).

---

## Appels au modèle (Gemini / `google-genai`)

Implémentation dans `harness/llm.py` :
- SDK **`google-genai`** (pas un autre SDK), client unique réutilisé (`timeout` 120 s).
- Sortie structurée : `response_mime_type="application/json"`. Pas de bloc de type Anthropic `tool_use` ici : le plan est un simple texte JSON que le planner re-valide.
- retries/fallback décrits dans la section « échec ».
- L'import du SDK est **paresseux** : les tests unitaires qui ne touchent pas au LLM tournent sans SDK ni clé.

---

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

- `tests/test_tools.py` : chargement/jointure (4803 films), colonnes dérivées, explosion des genres/acteurs, filtres budget/ROI, verrouillage du contrat `chart` + `verify`, fallbacks `sort_by`/axes, et garde-fous du planner (`_ensure_chart` pour top/comparaison/visuel, pas de doublon, pas de chart hors mots-clés).
- Aucun test n'appelle l'API : ils valident la logique déterministe (outils, vérification, planner local).

---

## Structure du dépôt

```
AGENTS.md                    # consignes pour les agents opencode (à garder à jour)
README.md
requirements.txt             # google-genai, pandas, matplotlib
.env.example                 # modèle de configuration (gitignored: .env)
.gitignore                   # .env, .venv/, runs/, output/
harness/
  __init__.py
  config.py                  # chemins, modèles, politiques d'exécution
  llm.py                     # wrapper Gemini (JSON, retries, fallback modèles)
  planner_prompt.py          # prompt système du planner (règles données/outils)
  planner.py                 # génération + validation du plan + _ensure_chart
  tools.py                   # registre d'outils (load_data/compute/chart/write_file)
  verify.py                  # clauses de vérification (nonempty/bound/type)
  executor.py                # boucle d'orchestration déterministe
  memory.py                  # log JSON du run
  main.py                    # point d'entrée CLI
tests/
  test_tools.py              # tests unitaires (outils, verify, planner)
runs/                        # logs JSON des runs (gitignoré)
output/charts/               # PNG générés (gitignoré)
output/results/              # fichiers écrits (gitignoré)
```

---

## Points d'attention pour développer

- Ne **jamais** charger les CSV en boucle : `load_data` met en cache dans `tools._df_cache` — c'est la seule copie du DataFrame par process.
- Ne pas hardcoder un modèle Gemini : les disponibilités varient par compte (ex. `gemini-2.5-flash` renvoie 404 sur les nouveaux comptes). Vérifier via `client.models.list()` avant de figer un nom.
- `compute` trie en **descendant** quand `sort_by` est donné ; `top_k` tronque les lignes — garder les payloads du log petites.
- Une question sans mot-clé visuel/classement/comparaison ne reçoit **pas** d'étape `chart` — c'est voulu (seulement chiffres).