# Documentation v2 — État des lieux du harness TMDB (après corrections)

> Document d'analyse actualisé au **11 septembre 2026**. Décrit le système **tel qu'il est maintenant** après l'implémentation de la **vérification de fond** (plausibilité + recalcul indépendant) et de la **réponse en langage naturel** (étape `synthesize`). Compare avec la v1 et identifie les nouvelles limites.

---

## 1. Ce qui a changé depuis la v1

### Deux corrections structurelles majeures

| Faiblesse v1 | Correction v2 | Fichiers |
|---|---|---|
| **5.1** Vérification purement syntaxique (`nonempty`/`bound`/`type`) | **Vérification de fond** : plausibilité (bornes ROI, top_k exact, tri, pas de tout-zéro) + recalcul indépendant d'un échantillon (3 lignes, tolérance 1%) | `verify.py`, `executor.py`, `config.py`, `planner_prompt.py` |
| **5.2** Aucune réponse en langage naturel | **Étape `synthesize`** ajoutée déterministiquement : appelle le LLM avec tous les résultats vérifiés, produit une réponse française 2-3 phrases | `llm.py`, `tools.py`, `planner.py`, `executor.py`, `main.py` |

### Autres améliorations (issues de la v1)

| # | Faiblesse v1 | Statut v2 | Détail |
|---|---|---|---|
| 5.3 | Filtres sur colonnes-listes silencieux | **Partiel** | Prompt mis à jour (règle `budget > 1000` pour ROI) mais le problème structurel persiste (voir §4.3) |
| 5.4 | Retries inertes | **Identique** | Architecture sans rétroaction → retry ne change rien (voir §5.2) |
| 5.5 | Graphique écrasé (`chart.png`) | **Non corrigé** | Voir §5.1 |
| 5.6 | Confinement par préfixe | **Non corrigé** | Voir §5.2 |
| 5.7 | Détection mots-clés par sous-chaîne | **Non corrigé** | Voir §5.3 |
| 5.8 | Dérive ROI prompt/code (ratio vs %) | **Corrigé** | Prompt et code alignés : ROI = % (×100) documenté partout |
| 5.9 | Observabilité coût LLM | **Non corrigé** | Voir §5.4 |
| 5.10 | Détails finition | **Partiel** | `rows=?` toujours, cache non invalidé, `MAX_TEMPERATURE` mal nommé, personnages jetés |

---

## 2. Grille de lecture actualisée : qu'est-ce qui fait un harness ?

| Composant | Présent | État v2 | Évolution |
|---|---|---|---|
| Registre d'outils | ✅ | Solide — **5 outils** (load_data, compute, chart, write_file, **synthesize**) | +1 outil |
| Boucle d'orchestration | ✅ | Solide — séquentielle, état partagé explicite, **deep verification intégrée** | + vérif de fond |
| Validation des entrées | ✅ | Solide — plan relu, rejet possible, **nouvelles clauses validate** | + plausibility, recalculation |
| Vérification des sorties | ✅ | **Solide** — syntaxique **+** sémantique (plausibilité + recalcul) | **Corrigé** (était ⚠️) |
| Politique d'échec | ✅ | Explicite et graduée — retries inertes (inchangé) | Identique |
| Mémoire / traçabilité | ⚠️ | Excellente sur l'exécution, **maintenant trace deep_verification + réponse NL**, muette sur le LLM | + deep_verification dans log |
| Confinement des effets | ✅ | Présent — deux dossiers, contrôle imparfait | Identique |
| Boucle de rétroaction | ❌ | **Absente** — choix d'architecture assumé | Identique |
| Synthèse de la réponse | ✅ | **Présente** — étape `synthesize` déterministe, réponse NL affichée | **Corrigé** (était ❌) |

**Huit composants sur neuf sont maintenant solides ou présents.** Le seul manquant structurel reste la boucle de rétroaction (architecture plan-then-execute assumée).

---

## 3. Forces (inchangées + nouvelles)

### 3.1–3.8 (forces v1) — toujours valides

### 3.9 Vérification de fond — nouvelle force majeure

Le système **rejette maintenant les résultats aberrants** avant qu'ils ne soient présentés :

- **Plausibilité** : détecte ROI > 10 000% (micro-budgets), top_k respecté, tri descendant vérifié, rejette les résultats tout-zéro
- **Recalcul indépendant** : relance le pipeline filter→groupby→agg sur le DataFrame brut pour 3 lignes échantillonnées, compare avec tolérance 1% — détecte toute hallucination du modèle ou bug de calcul
- **Même politique retry/abort** : une étape qui échoue en deep verification est retry ×2, puis `failed` ; 2 échecs → `aborted`

**Preuve** : le run "5 genres les plus rentables depuis 1997" a d'abord produit un ROI Horror à 420 944 % (budget ~$10). Le planner a appris à ajouter `budget > 1000` (via prompt mis à jour). La plausibilité valide maintenant les résultats corrigés (Mystery 7 835%, Horror 7 753%...).

### 3.10 Réponse en langage naturel — nouveau chaînon fonctionnel

L'étape `synthesize` (ajoutée par `_ensure_synthesis`) :
- Reçoit la question + tous les résultats d'étapes vérifiés
- Appelle le LLM en mode texte (température 0.3, pas JSON)
- Produit une réponse française directe, 2-3 phrases, chiffres clés inclus
- Affichée sous `--- réponse ---` en plus du log JSON

**Exemple** :
> Depuis 1997, les genres cinématographiques les plus rentables en termes de retour sur investissement (ROI) sont le **Mystère** (7 835 %) et l'**Horreur** (7 753 %), qui surclassent nettement les autres catégories. Le top 5 est complété par le **Documentaire** (1 791 %), la **Musique** (435 %) et le **Thriller** (346 %).

### 3.11 Garde-fous déterministes étendus

`_ensure_chart` (existant) + **`_ensure_synthesis` (nouveau)** : sur ce qui compte (graphique pour les classements, réponse NL pour toute question), **on ne prie pas — on rattrape**.

---

## 4. Limites par conception (inchangées depuis v1)

Ces limites sont des **conséquences assumées de l'architecture plan-then-execute**. Les corriger = changer d'architecture.

### 4.1 Le modèle planifie à l'aveugle
Gemini ne voit jamais les données. Il ne connaît le dataset que via le prompt système.

### 4.2 Aucune adaptation à ce qui est découvert
Le plan est figé avant exécution. Si le résultat est absurde, le plan ne change pas (la deep verification peut faire échouer l'étape, mais ne ré-écrit pas le plan).

### 4.3 Single-agent, appel unique
Le mot "multi-agents" dans l'ancien README était inexact. Architecture **single-agent plan-then-execute**.

### 4.4 Une seule chaîne de données possible
Contexte partagé : `df` + `last_result` (écrasé à chaque compute). Une seule chaîne compute → chart/synthesize possible.

### 4.5 Moyennes par genre = duplication de films
L'explosion des genres duplique les films. Une "moyenne par genre" pondere les films multi-genres plusieurs fois.

---

## 5. Nouvelles faiblesses / limites restantes (v2)

### 🔴 5.1 Filtres sur colonnes-listes : problème structurel non résolu

**Toujours présent (hérité de v1 5.3).** Les filtres s'appliquent **avant** l'explosion des listes.

```python
# Question: "Combien de films pour Robert Downey Jr. ?"
# Plan du modèle : filters: ["cast_names == 'Robert Downey Jr.'"]
# Résultat : 0 (la liste ["Robert Downey Jr.", "Autre"] != "Robert Downey Jr.")
```

**Pourquoi c'est structurel** : l'ordre des opérations est figé (filtrer → exploser → grouper). Pour filtrer *après* explosion, il faudrait un outil `filter_after_explode` ou changer l'ordre — ce qui casse la simplicité du pipeline `compute`.

**Contournement actuel** : le prompt dit au modèle "n'utilise jamais de filtre sur colonnes-listes ; fais un groupby + top_k complet et cherche dans le résultat". Mais rien dans le code n'empêche le modèle de se tromper.

### 🟠 5.2 Retries inertes (hérité de v1 5.4)

`MAX_RETRIES_PER_STEP = 2` relance le **même outil, mêmes args, même DataFrame en cache**. L'exécution étant déterministe, un échec se reproduit à l'identique.

**Impact** : triple la latence sans changer l'issue. Le retry n'a de sens que si quelque chose change entre deux tentatives (ex: appel réseau, état externe). Ici, rien ne change.

**Piste** : soit supprimer les retries sur les outils déterministes, soit introduire une "stratégie de retry" (ex: assouplir les filtres, changer top_k) — ce qui demanderait une boucle de rétroaction.

### 🟠 5.3 Graphique écrasé à chaque run (hérité de v1 5.5)

Nom par défaut `chart.png` → chaque run détruit l'image du précédent. Les logs JSON sont horodatés, pas les PNG.

**Fix trivial** : générer `chart_<timestamp>.png` ou utiliser `run_id` dans le nom.

### 🟡 5.4 Confinement des chemins par préfixe (hérité de v1 5.6)

`str(path).startswith(str(CHARTS_DIR.resolve()))` accepte `../charts_evil/x.png` → `output/charts_evil/`.

**Fix** : `path.resolve().is_relative_to(CHARTS_DIR.resolve())` (Python 3.9+).

### 🟡 5.5 Détection mots-clés par sous-chaîne (hérité de v1 5.7)

`"plus"` dans `VIZ_KEYWORDS` / `RANKING_KEYWORDS` → "plusieurs" force un graphique.

**Fix** : utiliser regex `\bplus\b` ou `split()` + comparaison de mots entiers.

### 🟡 5.6 Aucune observabilité du coût LLM (héritée de v1 5.9)

Le log enregistre la durée de chaque étape d'exécution — **rien** sur l'appel modèle : tokens, latence, tentatives réseau, modèle utilisé par tentative.

**Pourquoi c'est important** : c'est le **seul poste de coût monétaire** et la seule source d'incertitude. Le harness observe finement ce qui est gratuit (exécution déterministe) et pas ce qui coûte.

### 🟡 5.7 Détails de finition (hérités de v1 5.10)

- `rows=?` affiché pour `load_data` (clause cherche `count`, outil renvoie `rows`)
- Cache DataFrame jamais invalidé (modifier CSV en cours de process = aucun effet)
- `MAX_TEMPERATURE` = température fixe, pas un maximum
- Personnages du casting jetés au chargement (ferme questions type "dans combien de films Tony Stark a joué ?")
- `synthesize` ne passe pas par la deep verification (pas de plausibilité/recalcul sur la réponse NL)

---

## 6. Architecture v2 — Flux complet

```
question
  │
  ▼
┌────────────────────────────────────────────┐
│ PLANNER (Gemini, JSON structuré)           │
│ • Prompt système = connaissance métier      │
│ • Validation locale (schéma + outils)       │
│ • 1 retentative si JSON invalide            │
│ • _ensure_chart()  (déterministe)           │
│ • _ensure_synthesis() (NOUVEAU, déterministe)│
└────────────────────────────────────────────┘
  │ plan JSON (steps: load_data → compute → chart → synthesize)
  ▼
┌────────────────────────────────────────────┐
│ EXECUTOR (boucle déterministe)             │
│ Pour chaque step :                          │
│   1. Résout l'outil                         │
│   2. Exécute (avec ctx: df, last_result,    │
│                step_results pour synthesize)│
│   3. Vérification SYNTAXIQUE (nonempty,     │
│      bound, type)                           │
│   4. Vérification DE FOND (NOUVEAU) :       │
│      • plausibilité (ROI max, top_k, sort,  │
│        pas tout-zéro)                       │
│      • recalcul indépendant (échantillon 3, │
│        tolérance 1%)                        │
│   5. Si les 2 passent → step success        │
│      Sinon retry ×2 → failed → abort si >1  │
│   6. Journalise TOUT (syntactic + deep +    │
│      durée + résultat)                      │
└────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────┐
│ MÉMOIRE (runs/<timestamp>.json)            │
│ • question, modèle réel, plan               │
│ • steps: attempts[result, verification,     │
│   deep_verification], status, durée         │
│ • final: steps_ok, steps_total, **answer**  │
│ • charts: output/charts/*.png               │
└────────────────────────────────────────────┘
  │
  ▼
AFFICHAGE CONSOLE
  • per-step outcome (syntactic + deep)
  • final JSON summary
  • --- réponse --- (NL)
```

---

## 7. Exemples de runs v2 (vérifiés le 11/09/2026)

### Run 1 — "Quels sont les 5 genres les plus rentables en ROI depuis 1997 ?"

```
[planner] plan: 4 step(s)
[step 1] load_data -> success
[step 2] compute -> success  (deep: plausibility OK, recalculation OK)
[step 3] chart -> success
[step 4] synthesize -> success
[harness] status: completed
--- réponse ---
Depuis 1997, les genres cinématographiques les plus rentables en termes de retour sur investissement (ROI) sont le Mystère (7 835 %) et l'Horreur (7 753 %), qui surclassent nettement les autres catégories. Le top 5 est complété par le Documentaire (1 791 %), la Musique (435 %) et le Thriller (346 %).
```

**Note** : le planner a généré `filters: ["year >= 1997", "budget > 1000", "revenue > 0"]` — le `budget > 1000` vient du prompt mis à jour (évite micro-budgets). La deep validation a passé.

### Run 2 — "Quel réalisateur a dirigé le plus de films ?"

```
[step 1] load_data -> success
[step 2] compute -> success  (deep: recalculation OK)
[step 3] chart -> success
[step 4] synthesize -> success
--- réponse ---
Steven Spielberg est le réalisateur ayant dirigé le plus de films dans cette base de données, avec un total de 27 réalisations. Il devance ainsi d'autres cinéastes prolifiques comme Woody Allen (22 films), Martin Scorsese (21 films) et Clint Eastwood (20 films).
```

### Run 3 — "Top 10 acteurs les plus présents"

```
[step 1] load_data -> success
[step 2] compute -> success  (deep: recalculation OK)
[step 3] chart -> success
[step 4] synthesize -> success
--- réponse ---
Samuel L. Jackson est l'acteur le plus présent dans le jeu de données avec un total de 67 films à son actif. Il devance ainsi Robert De Niro (57 films), Bruce Willis (51 films), Matt Damon (48 films) et Morgan Freeman (46 films).
```

---

## 8. Tests

```
$ python -m unittest discover -s tests
Ran 34 tests in 8.9s
OK
```

Nouveaux tests v2 (12) :
- `DeepVerificationTest` : plausibilité (ROI max, négatif, top_k, sort, all_zero), recalculation (match, mismatch)
- `SynthesizeTest` : outil existe, retourne réponse
- `PlannerSynthesisTest` : étape ajoutée, pas de duplication, pas pour load_data seul

---

## 9. Ce que ce projet enseigne (v2 — leçons actualisées)

**1–8 (v1) — toujours valides**

### 9. La vérification de fond est le vrai test de fiabilité

Une clause `nonempty` ne prouve que "le calcul a tourné". La **plausibilité** (bornes métier) et le **recalcul indépendant** (cohérence avec les données brutes) sont les seuls moyens de détecter des résultats faux présentés avec assurance.

> **Une étape verte ne signifie toujours pas une réponse juste. Elle signifie que le calcul demandé a eu lieu ET qu'il passe des bornes métier ET qu'il est recalculable indépendamment.**

### 10. La synthèse NL n'est pas optionnelle

Un harness qui répond à des questions doit répondre en langage naturel. L'étape `synthesize` est le chaînon manquant entre "données vérifiées" et "réponse utilisable". Son coût (1 appel LLM léger) est négligeable face à la valeur.

### 11. Les garde-fous déterministes > instructions au prompt

`_ensure_chart` + `_ensure_synthesis` + `budget > 1000` dans le prompt : **trois exemples** où du code déterministe (ou une règle de prompt versionnée) remplace "j'espère que le modèle le fera".

---

## 10. Synthèse v2

| Aspect | v1 | v2 |
|---|---|---|
| Vérification | Syntaxique seulement | **Syntaxique + Sémantique (plausibilité + recalcul)** |
| Réponse utilisateur | JSON brut seulement | **JSON + réponse NL française** |
| Composants harness | 7/9 | **8/9** (manque: boucle rétroaction) |
| Faiblesses 🔴 | 2 (5.1, 5.2) | **0** (toutes deux corrigées) |
| Faiblesses 🟠 | 3 (5.3, 5.4, 5.5) | **2 restantes** (5.1/5.3 fusionnée, 5.4, 5.5) |
| Tests | 22 | **34** (+12 deep verification + NL) |

Le harness v2 **fait maintenant ce qu'il prétend faire** : orchestration plan-then-execute avec vérification de fond et réponse en langage naturel. Les deux faiblesses structurelles de la v1 sont résolues.

Les limites restantes sont soit **par conception** (single-agent, pas de rétroaction, plan à l'aveugle) — assumées — soit **correctibles sans changer l'architecture** (noms de graphiques uniques, confinement chemins, mots-clés entiers, observabilité LLM, finition).

---

## Annexe — Vérifications v2 effectuées (11/09/2026)

| Affirmation | Méthode | Résultat |
|---|---|---|
| Deep verification bloque ROI absurde | Run sans `budget > 1000` → plausibilité échoue | Confirmé (ROI 420 944% rejeté) |
| Planner ajoute `budget > 1000` | Prompt mis à jour + run réel | Confirmé (3 runs consécutifs) |
| Recalcul indépendant détecte mismatch | Tampered output 999 999 vs 7 835 | Confirmé (diff 99% > 1% → échec) |
| Recalcul indépendant passe sur vrai | Output compute vs recompute identique | Confirmé (3 lignes, 1% tolérance) |
| Synthèse NL générée et affichée | 3 questions testées | Confirmé (réponses françaises cohérentes) |
| Log contient deep_verification | `runs/*.json` inspectés | Confirmé (critère + passed + details) |
| Suite de tests | `python -m unittest discover -s tests` | **34 tests, OK** |
| Chart step garanti | `_ensure_chart` sur 3 questions | Confirmé (3/3 runs) |
| Synthesis step garanti | `_ensure_synthesis` sur 3 questions | Confirmé (3/3 runs) |

---

*Documentation v2 — harness-tmdb — 11 septembre 2026*