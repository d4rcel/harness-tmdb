# Documentation v3 — Harness TMDB ReAct Architecture

> Document d'analyse actualisé au **14 septembre 2026**. Décrit le système **tel qu'il est maintenant** après migration de l'architecture *plan-then-execute* (v2) vers une **boucle ReAct** (Reasoning and Acting) avec Function Calling natif Gemini.

---

## 1. Ce qui a changé : v2 → v3

### Changement architectural majeur

| Aspect | v2 (Plan-then-execute) | v3 (ReAct) |
|---|---|---|
| **Planification** | Plan JSON complet généré *une fois* au début | **Aucun plan préalable** — l'agent décide une action à la fois |
| **Exécution** | Exécuteur déroule le plan linéairement | **Boucle** : à chaque tour, le modèle voit l'historique complet et choisit la prochaine action |
| **Réaction à l'échec** | Retry aveugle ×2 (mêmes args, même DataFrame) → abort | **Échec visible dans l'historique** — le modèle *voit* l'échec et *adapte* sa prochaine action (change filtres, outil, args) |
| **Nombre de tours** | Fixé par le plan (3-4 étapes) | **Max 10 tours** (configurable), l'agent décide quand `finish` |
| **Synthèse NL** | Étape `synthesize` ajoutée déterministiquement en fin de plan | Étape `synthesize` appelée **après `finish`** (décision de l'agent) |

### Suppressions
- `planner.py` — plus de planificateur séparé
- `planner_prompt.py` — remplacé par `react_prompt.py`
- Concept de `plan` / `steps` → remplacé par `turns` dans les logs

---

## 2. Architecture v3 — Flux complet

```
question
  │
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ REACT LOOP (max 10 tours)                                            │
│                                                                     │
│  TOUR 1:                                                            │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Contexte: question + "Turn 1 of 10" + "no previous turns"    │   │
│  │ LLM (Function Calling) → action: load_data {}               │   │
│  │ Execute → load_data() → observation: 4803 rows              │   │
│  │ Verify: syntactic (nonempty=PASS) + deep (skipped)          │   │
│  │ Record turn 1 → history                                     │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                            │                                        │
│  TOUR 2:                                                            │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Contexte: question + Turn 2 + history[turn1]                │   │
│  │ LLM → action: compute {filters: ["budget>1000",...],       │   │
│  │                     groupby: ["genres"], agg: {"roi":"mean"}|   │
│  │                     sort_by: "roi", top_k: 5}               │   │
│  │ Execute → compute() → observation: 5 rows (Mystery 7835%...)│   │
│  │ Verify: syntactic (plausibility=PASS) + deep (recalc=PASS)  │   │
│  │ Record turn 2 → history                                     │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                            │                                        │
│  TOUR 3: (optionnel chart)                                          │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ LLM → action: chart {x: "genre", y: "roi", kind: "bar"}    │   │
│  │ Execute → chart() → observation: path=chart.png             │   │
│  │ Verify: syntactic (nonempty=PASS)                           │   │
│  │ Record turn 3 → history                                     │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                            │                                        │
│  TOUR 4:                                                            │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ LLM → action: finish {}                                     │   │
│  │ Extract final_answer from history → synthesize()            │   │
│  │ Record final answer → DONE                                   │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Composants v3

### 3.1 `react_prompt.py` — Prompt système ReAct
Encode la connaissance métier **vérifiée** (budget>1000 pour ROI, explosion genres, fenêtre 1916-2017) et dicte le comportement ReAct :
- **5 outils déclarés** pour Function Calling natif : `load_data`, `compute`, `chart`, `write_file`, `finish`
- Règle : *"Si une vérification échoue, ADAPTE ta prochaine action. Ne réessaie pas la même chose."*
- Format de réponse : **Function Calling natif** (pas de JSON textuel)

### 3.2 `llm.py` — Function Calling natif
- `generate_react_action()` : utilise `tools=` + `tool_config=function_calling_config(mode="ANY")`
- Schémas JSON Schema stricts pour chaque outil (validation côté API)
- Fallback multi-modèles (`gemini-3.6-flash` → `gemini-flash-latest` → ...) avec retry 503/429
- `generate_text()` pour la synthèse NL (température 0.3)

### 3.3 `react_loop.py` — Boucle principale
```python
for turn in 1..MAX_REACT_TURNS:
    context = build_context(question, history, turn, max_turns)
    action = llm.generate_react_action(REACT_SYSTEM_PROMPT, context)
    
    if action.name == "finish":
        final_answer = extract_final_answer(history)
        synth = tools.synthesize({question, final_answer, step_results: history})
        record.final = {turns_ok, turns_total, answer: synth.answer}
        return save_run(record)
    
    result = execute_action(action.name, action.args, ctx)
    syntactic = verify.check_output(validate_clause, result)
    deep = run_deep_verification(action.name, action.args, result, ctx)
    
    history.append({turn, action, observation: result, 
                    verification: {syntactic, deep}, status})
```
**Point clé** : **pas de retry automatique**. Si `deep` échoue → `status=failed` enregistré → le modèle verra l'échec au tour suivant et pourra corriger (ex: ajouter `budget > 1000` après un ROI absurde).

### 3.4 `verify.py` — Vérification profonde (inchangée depuis v2)
- `check_plausibility()` : ROI ≤ 10 000%, top_k exact, sort descending, pas de tout-zéro
- `check_recalculation()` : recalcul indépendant sur échantillon 3, tolérance 1%

### 3.5 `memory.py` — Logs basés sur `turns`
```json
{
  "turns": [
    {"turn": 1, "action": {"tool": "load_data", "args": {}},
     "observation": {"cached": false, "rows": 4803},
     "verification": {"syntactic": {"criterion": "nonempty", "passed": true},
                      "deep": {"criterion": "deep", "passed": true}},
     "status": "success", "duration_ms": 3050},
    ...
  ],
  "final": {"turns_ok": 3, "turns_total": 3, "answer": "..."}
}
```

---

## 4. Preuve de concept : Adaptation après échec (ce qui distingue ReAct)

**Scénario** : Question "5 genres les plus rentables en ROI depuis 1997"

| Architecture | Comportement |
|---|---|
| **v2 (plan-then-execute)** | Planner génère `filters: ["budget > 0", "revenue > 0"]` → compute → ROI Horror 420 944% (budget $10) → vérification `nonempty` passe → réponse fausse présentée comme vraie |
| **v3 (ReAct)** | Tour 1: `load_data` ✓<br>Tour 2: `compute` **sans** `budget > 1000` → deep verification **échoue** (plausibilité: ROI 420k% > 10k%)<br>Tour 3: LLM **voit l'échec dans l'historique** → génère `compute` **avec** `budget > 1000` → deep verification **passe** → continue vers chart → finish |

C'est la **différence fondamentale** : le modèle apprend de ses erreurs *dans la même session*.

---

## 5. Résultats de validation (3 questions testées)

| Question | Tours | Statut | Réponse NL |
|---|---|---|---|
| "5 genres les plus rentables en ROI depuis 1997" | 3 | ✅ completed | "Mystère (7 835%), Horreur (7 753%), Documentaire (1 791%), Musique (435%), Thriller (346%)" |
| "Quel réalisateur a dirigé le plus de films" | 3 | ✅ completed | "Steven Spielberg (27), Woody Allen (22), Martin Scorsese (21)..." |
| "Top 10 acteurs les plus présents" | 3 | ✅ completed | "Samuel L. Jackson (67), Robert De Niro (57), Bruce Willis (51)..." |

Tous les runs : 3 tours (load_data → compute → chart → finish), deep verification PASS à chaque compute.

---

## 6. Tests

```
$ python -m unittest discover -s tests
Ran 29 tests in 23s
OK
```

Nouveaux tests `ReactLoopTest` (8 tests) :
- `test_build_react_context` — construction du contexte texte
- `test_get_validate_clause` — clauses selon l'outil
- `test_run_deep_verification_react_skips_non_compute` — load_data/chart skip deep
- `test_run_deep_verification_react_compute_plausibility` — rejette ROI absurde
- `test_extract_final_answer` — extraction pour synthesize

Anciens tests `PlannerEnforcementTest` / `PlannerSynthesisTest` supprimés (dépendaient de `planner.py`).

---

## 7. Limites actuelles (v3)

### 🔴 7.1 Filtres sur colonnes-listes — problème structurel non résolu
**Hérité de v1/v2.** Les filtres s'appliquent **avant** l'explosion des listes (`genres`, `cast_names`, `directors`...).
- Question : *"Combien de films pour Robert Downey Jr. ?"*
- Modèle peut générer : `filters: ["cast_names == 'Robert Downey Jr.'"]`
- Résultat : 0 (la liste `["Robert Downey Jr.", "Autre"] != "Robert Downey Jr."`)
- **Aucune erreur levée**, résultat vide → `nonempty` échoue mais trop tard.
- *Contournement* : prompt dit "n'utilise jamais de filtre sur colonnes-listes ; fais groupby+top_k complet". Pas de garde-fou code.

### 🟠 7.2 Retries LLM inertes sur erreurs 503
Même mécanisme qu'avant : 3 essais × 5s par modèle, puis suivant. Si tous saturés → échec. Pas de backoff exponentiel.

### 🟠 7.3 Graphique écrasé (`chart.png` par défaut)
Le modèle fournit souvent un chemin, mais si absent → `chart.png` écrase le précédent. Les logs JSON sont horodatés, pas les PNG.

### 🟠 7.4 Confinement chemins par préfixe string
`str(path).startswith(str(CHARTS_DIR.resolve()))` accepte `../charts_evil/x.png` → `output/charts_evil/`.

### 🟡 7.5 Détection mots-clés par sous-chaîne
`"plus"` dans `RANKING_KEYWORDS` (dans `planner_prompt.py` résiduel ?) → "plusieurs" force un graphique. À nettoyer.

### 🟡 7.6 Observabilité coût LLM nulle
Log : durée exécution outils (ms), **rien** sur appel modèle : tokens, latence, tentatives, modèle utilisé. C'est le **seul poste monétaire** et la seule source d'incertitude.

### 🟡 7.7 `synthesize` sans vérification profonde
L'étape `synthesize` n'a que vérification syntaxique (`type string`). Pas de plausibilité/recalcul sur la réponse NL.

### 🟡 7.8 Détail finition
- `load_data` affiche `rows=?` (clause cherche `count`, outil renvoie `rows`)
- Cache DataFrame jamais invalidé
- `MAX_TEMPERATURE` = température fixe, pas un maximum
- Personnages casting jetés au chargement (ferme questions "Tony Stark")

---

## 8. Limites par conception (assumées, architecture ReAct)

| Limite | Pourquoi |
|---|---|
| **Modèle planifie à l'aveugle** | Ne voit jamais les données, seulement le prompt système |
| **Pas de rétroaction sur données** | Le modèle voit les *résultats* (observations) mais pas le DataFrame brut |
| **Max 10 tours** | Garde-fou contre boucles infinies ; peut couper une analyse complexe |
| **Single-agent, pas de délégation** | Un seul modèle, pas de spécialistes (ex: agent SQL, agent viz) |
| **Contexte linéaire** | L'historique grossit ; pas de résumé / compression pour longs runs |

---

## 9. Ce que ce projet enseigne (v3 — leçons actualisées)

**1–11 (v1/v2) — toujours valides**

### 12. ReAct > Plan-then-execute pour la robustesse
La capacité du modèle à **voir l'échec et s'adapter** (changer `budget > 0` → `budget > 1000`) est qualitativement supérieure au retry aveugle. C'est la définition même de *Reasoning and Acting*.

### 13. Function Calling natif > JSON structuré pour les outils
- Schéma imposé par l'API → pas de "JSON mal formé"
- Validation côté serveur → `tool` toujours valide, `args` toujours conformes
- Moins de code de parsing/validation côté client

### 14. Vérification profonde à CHAQUE action, pas en fin de plan
En v2, la deep verification ne tournait qu'après le `compute` final. En v3, elle tourne après **chaque** `compute` (et pourrait tourner après chaque action). Détecte l'erreur immédiatement, avant qu'elle ne se propage.

### 15. L'historique textuel comme "mémoire de travail"
Le contexte passé au modèle est du **texte structuré** (pas du JSON brut). Le modèle raisonne en langage naturel sur ses propres traces : *"Au tour 2, j'ai eu un échec plausibilité sur ROI → j'ajoute budget>1000"*. C'est plus lisible pour le modèle qu'un objet JSON d'erreur.

---

## 10. Synthèse comparative v1 → v2 → v3

| Composant harness | v1 | v2 | v3 |
|---|---|---|---|
| Planification | LLM (plan JSON) | LLM (plan JSON) | **ReAct (action par action)** |
| Vérification sortie | Syntaxique seulement | **Syntaxique + Sémantique** | **Syntaxique + Sémantique (chaque tour)** |
| Réponse NL | ❌ | ✅ (étape forcée) | ✅ (après `finish` choisi par LLM) |
| Gestion échec | Retry aveugle ×2 | Retry aveugle ×2 | **Modèle voit échec → s'adapte** |
| Function Calling | ❌ (JSON textuel) | ❌ (JSON textuel) | ✅ **Natif Gemini** |
| Composants harness | 7/9 | 8/9 | **8/9** (manque: boucle rétroaction sur données) |
| Tests | 22 | 34 | **29** (planner tests supprimés) |
| Logs | `plan` + `steps` | `plan` + `steps` | **`turns`** |

---

## 11. Prochaines étapes recommandées

| Priorité | Action |
|---|---|
| **Haute** | Garde-fou code : interdire filtres sur colonnes-listes dans `compute` (lever `ValueError`) |
| **Haute** | Noms de graphiques uniques (`chart_<run_id>_<turn>.png`) |
| **Moyenne** | Confinement chemins avec `path.is_relative_to()` (Py 3.9+) |
| **Moyenne** | Observabilité LLM : loguer tokens, latence, modèle, tentatives par tour |
| **Moyenne** | Mots-clés entiers (regex `\bplus\b`) pour `_ensure_chart` résiduel |
| **Basse** | Compression d'historique pour runs > 10 tours |
| **Basse** | Cache DataFrame invalidable (optionnel) |

---

## Annexe — Vérifications v3 effectuées (14/09/2026)

| Affirmation | Méthode | Résultat |
|---|---|---|
| Function Calling natif opérationnel | 3 runs complets sans erreur 400 | Confirmé (après fix `agg` string) |
| Boucle ReAct 3 tours typiques | 3 questions testées | Confirmé (load_data → compute → chart → finish) |
| Deep verification par tour | Logs `turns[n].verification.deep` | Confirmé (plausibility + recalculation PASS) |
| Adaptation après échec plausibilité | Simulation manuelle (ROI 420k% → budget>1000) | Confirmé (modèle corrige au tour suivant) |
| Synthèse NL après `finish` | 3 runs | Confirmé (réponses françaises cohérentes) |
| Suite de tests | `python -m unittest discover -s tests` | **29 tests, OK** |
| Logs `turns` au lieu de `steps` | `runs/*.json` inspectés | Confirmé (structure plate, pas de `plan`) |

---

*Documentation v3 — harness-tmdb ReAct — 14 septembre 2026*