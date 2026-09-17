# Documentation v4 — Harness TMDB Multi-Agent Architecture

> Document d'analyse actualisé au **17 septembre 2026**. Décrit le système **tel qu'il est maintenant** après migration de l'architecture *ReAct single-agent* (v3) vers une **architecture multi-agents** selon le pattern *agents-as-tools* avec contexte compressé au handoff (inspiré du Component 11 de harness-2.txt).

---

## 1. Ce qui a changé : v3 → v4

### Changement architectural majeur

| Aspect | v3 (ReAct Single-Agent) | v4 (Multi-Agent Agents-as-Tools) |
|---|---|---|
| **Orchestration** | Un seul agent ReAct décide de toutes les actions | **Superviseur** orchestre 3 agents spécialisés via Function Calling |
| **Outils** | 5 outils bas niveau (load_data, compute, chart, write_file, synthesize) | **3 outils-agents** : `call_data_agent`, `call_viz_agent`, `call_redaction_agent` + `finish` |
| **Contexte** | Historique complet grossit linéairement | **Contexte compressé** au handoff (~400-600 chars) |
| **Résultat** | L'agent voit tout l'historique | **Résultat condensé** retourné au superviseur (~150-300 chars) |
| **Vérification** | Centralisée après chaque action | **Distribuée** : chaque agent vérifie son propre travail |
| **Logs** | `turns` plats | `supervisor_turns` + `subagent_logs` hiérarchiques avec métriques handoff |
| **Graphiques** | Décision du modèle | **Garde-fou déterministe** : mots-clés → appel obligatoire Viz Agent |

### Nouveaux composants

```
harness/
├── agents/
│   ├── base_agent.py       # Boucle ReAct partagée (générique)
│   ├── supervisor.py       # Superviseur + agents-as-tools
│   ├── data_agent.py       # Agent Data (load_data, compute)
│   ├── viz_agent.py        # Agent Viz (chart)
│   └── redaction_agent.py  # Agent Rédaction (synthesize, flow forcé)
├── context/
│   ├── compressor.py       # Compression contexte pour handoffs
│   └── condenser.py        # Condensation résultats pour retours
└── (memory.py, config.py, llm.py, main.py mis à jour)
```

---

## 2. Architecture v4 — Flux complet

```
question: "Quel réalisateur a dirigé le plus de films ?"
  │
  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ SUPERVISEUR (ReAct Loop, max 10 tours)                                       │
│                                                                             │
│ TOUR 1:                                                                     │
│ ┌─────────────────────────────────────────────────────────────────────┐     │
│ │ Contexte: question + "Turn 1 of 10" + "chart_required=true"          │     │
│ │ LLM (Function Calling) → action: call_data_agent {                   │     │
│ │     task: "Find top directors by film count",                        │     │
│ │     context_summary: "User asks for director with most films..."     │     │
│ │ }                                                                    │     │
│ │ Handoff: compresse contexte (465 chars) → Data Agent                 │     │
│ │ Data Agent (interne 2 tours): load_data → compute → finish          │     │
│ │ Retourne: result_summary="Loaded 4803 films | Computed: Spielberg   │     │
│ │  (27), Allen (22)..." + artifacts={compute_result}                  │     │
│ │ Log: handoff {agent="data_agent", sent=465, recv=233, ratio=100%}    │     │
│ └─────────────────────────────────────────────────────────────────────┘     │
│                                    │                                        │
│ TOUR 2: (chart_required=true ∧ ¬chart_generated)                          │
│ ┌─────────────────────────────────────────────────────────────────────┐     │
│ │ LLM → call_viz_agent {                                               │     │
│ │     task: "Create bar chart of top 10 directors",                    │     │
│ │     context_summary: "Top 10 directors: Spielberg (27), Allen (22)..."│    │
│ │ } + initial_context={last_result: compute_result}                    │     │
│ │ Viz Agent (interne 1 tour): chart → finish                          │     │
│ │ Retourne: result_summary="Generated chart: top_10_directors.png"    │     │
│ │ Log: handoff {agent="viz_agent", sent=400, recv=150, ratio=82%}      │     │
│ └─────────────────────────────────────────────────────────────────────┘     │
│                                    │                                        │
│ TOUR 3:                                                                     │
│ ┌─────────────────────────────────────────────────────────────────────┐     │
│ │ LLM → call_redaction_agent {                                         │     │
│ │     task: "Synthesize French answer",                                │     │
│ │     context_summary: "Question + data results + chart generated"     │     │
│ │ } + initial_context={step_results: [...]}                            │     │
│ │ Redaction Agent (interne 2 tours): synthesize → finish (forcé)      │     │
│ │ Retourne: result_summary="Steven Spielberg est le réalisateur..."   │     │
│ │ Log: handoff {agent="redaction_agent", sent=600, recv=291, ratio=86%}│    │
│ └─────────────────────────────────────────────────────────────────────┘     │
│                                    │                                        │
│ TOUR 4:                                                                     │
│ ┌─────────────────────────────────────────────────────────────────────┐     │
│ │ LLM → finish {}                                                      │     │
│ │ Superviseur termine, sauvegarde log enrichi                         │     │
│ └─────────────────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Composants v4

### 3.1 `agents/base_agent.py` — Boucle ReAct générique
Classe de base paramétrable :
- `name`, `tools`, `system_prompt`, `max_turns`
- `generate_action_fn` : fonction d'appel LLM spécifique à l'agent
- `_execute_action()`, `_get_validate_clause()`, `_run_deep_verification()` partagés
- Retourne `AgentResult(success, result_summary, artifacts, internal_turns, verification_status)`

### 3.2 `agents/supervisor.py` — Orchestrateur
- **Outils exposés au LLM** (Function Calling natif) :
  - `call_data_agent(task, context_summary)`
  - `call_viz_agent(task, context_summary)`
  - `call_redaction_agent(task, context_summary)`
  - `finish()`
- **Logique de garde-fou** : si `chart_required` (mots-clés) → force `call_viz_agent` avant `call_redaction_agent`
- **Gestion d'erreur** : si agent échoue → `success=false` dans observation → superviseur décide (retry/abort)
- **État** : `chart_required`, `chart_generated`, `data_completed`, `redaction_completed`

### 3.3 `agents/data_agent.py` — Agent Données
- **Outils** : `load_data`, `compute`, `finish`
- **Prompt** : règles métier critiques (budget>1000, ROI, genres multi-label, year_range)
- **Max turns** : 5
- **Artifacts** : retourne `compute_result` (columns, rows) pour agents aval

### 3.4 `agents/viz_agent.py` — Agent Visualisation
- **Outils** : `chart(data?, ...)`, `finish`
- **Prompt** : doit passer `data` (compute_result) au tool chart
- **Max turns** : 2
- Reçoit `initial_context={"last_result": compute_result}`

### 3.5 `agents/redaction_agent.py` — Agent Rédaction
- **Outils** : `synthesize(question, final_answer, step_results)`, `finish`
- **Classe dédiée `RedactionAgent`** (hérite de `BaseAgent`) qui **force le flow** :
  - Tour 1 : appelle obligatoirement `synthesize` avec `step_results` injectés
  - Tour 2 : appelle obligatoirement `finish`
- **Max turns** : 3 (marge de sécurité)

### 3.6 `context/compressor.py` — Compression au handoff
Trois fonctions spécialisées :

| Fonction | Cible | Taille max | Stratégie |
|---|---|---|---|
| `compress_for_data_agent` | Data Agent | 500 chars | Question + instructions métier + indices (ROI, budget>1000, groupby hints) |
| `compress_for_viz_agent` | Viz Agent | 400 chars | Question + résultat Data Agent (colonnes, top rows) + suggestion x/y/kind |
| `compress_for_redaction_agent` | Redaction Agent | 600 chars | Question + tous les key findings extraits + step_results structurés |

**Garde-fou chart** : `should_generate_chart(question)` — regex word-boundary sur mots-clés (`graphique`, `chart`, `plot`, `top`, `classement`, `évolution`, `comparaison`, `ranking`, `distribution`, `histogramme`). Exclut "plusieurs", "plus de".

### 3.7 `context/condenser.py` — Condensation au retour
Trois fonctions qui transforment les `internal_turns` d'un agent en résumé court :

| Fonction | Sortie typique |
|---|---|
| `condense_data_agent_result` | "Loaded 4803 films | Computed: 10 rows, columns=['director','title']. Top: Spielberg (27), Allen (22)... | Verification: plausibility=PASS; recalculation=PASS" |
| `condense_viz_agent_result` | "Generated chart: top_directors.png (10 data points) | Verification: nonempty=PASS" |
| `condense_redaction_agent_result` | "Synthesized answer: Steven Spielberg... | Verification: type string=PASS; finish=PASS" |

### 3.8 `memory.py` — Logs enrichis
Structure `supervisor_turns` + `subagent_logs` :

```json
{
  "created_at": "...",
  "model": "gemini-3.6-flash",
  "question": "...",
  "supervisor_turns": [
    {
      "turn": 1,
      "action": {"tool": "call_data_agent", "args": {...}},
      "observation": {"success": true, "result_summary": "...", "artifacts": {...}},
      "handoff": {
        "agent": "data_agent",
        "context_chars_sent": 465,
        "context_chars_original": 465,
        "compression_ratio": 1.0,
        "result_chars_received": 233,
        "subagent_turns": 2
      },
      "status": "success",
      "duration_ms": 8677
    }
  ],
  "subagent_logs": {
    "data_agent": {
      "internal_turns": [...],
      "final_summary": "...",
      "success": true,
      "verification_status": "passed"
    },
    "viz_agent": {...},
    "redaction_agent": {...}
  },
  "final": {"turns_ok": 3, "turns_total": 3, "answer": "..."},
  "status": "completed"
}
```

### 3.9 `llm.py` — Function Calling par agent
Quatre jeux de tools déclarés :
- `_react_tools()` : v3 single-agent (load_data, compute, chart, write_file, finish)
- `_supervisor_tools()` : v4 multi-agent (call_data_agent, call_viz_agent, call_redaction_agent, finish)
- `_data_agent_tools()` : load_data, compute, finish
- `_viz_agent_tools()` : chart, finish
- `_redaction_agent_tools()` : synthesize, finish

Quatre fonctions d'appel :
- `generate_react_action()` — v3
- `generate_supervisor_action()` — v4 superviseur
- `generate_data_agent_action()` — Data Agent
- `generate_viz_agent_action()` — Viz Agent
- `generate_redaction_agent_action()` — Redaction Agent

---

## 4. Preuve de concept : Compression visible dans les logs

**Exemple concret** (run "Top 10 acteurs les plus présents") :

| Handoff | Contexte original | Compressé | Ratio | Résultat reçu |
|---|---|---|---|---|
| Superviseur → Data Agent | 453 chars | 453 chars | **100%** (premier tour, pas d'historique) | 222 chars |
| Superviseur → Viz Agent | 487 chars | 400 chars | **82%** | 137 chars |
| Superviseur → Redaction Agent | 600 chars | 600 chars | **95%** (déjà condensé) | 259 chars |

Le contexte du Superviseur reste **borné** (~2-3 KB max) alors qu'en v3 il grossissait à chaque tour.

---

## 5. Résultats de validation (3 questions testées)

| Question | Tours Superviseur | Tours Data | Tours Viz | Tours Redaction | Statut | Réponse NL |
|---|---|---|---|---|---|---|
| "Quel réalisateur a dirigé le plus de films" | 4 | 2 | 1 | 2 | ✅ | "Steven Spielberg est le réalisateur ayant dirigé le plus de films avec un total de 27 réalisations..." |
| "5 genres les plus rentables en ROI depuis 1997" | 4 | 2 | 1 | 2 | ✅ | "Depuis 1997, les genres cinématographiques générant le meilleur retour sur investissement (ROI) sont le Mystère (7 835 %) et l'Horreur (7 753 %). Le top 5..." |
| "Top 10 acteurs les plus présents" | 4 | 2 | 1 | 2 | ✅ | "Samuel L. Jackson occupe la première place des acteurs les plus présents dans le jeu de données TMDB avec un total de 67 films..." |

Tous les runs : 4 tours superviseur (data → viz → redaction → finish), deep verification PASS à chaque compute.

---

## 6. Tests

```
$ python -m unittest discover -s tests
Ran 36 tests in 8s
OK
```

Nouveaux tests `MultiAgentTest` (7 tests) :
- `test_compressor_data_agent` / `viz_agent` / `redaction_agent` — tailles et contenu
- `test_condenser_data_agent` / `viz_agent` — format de condensation
- `test_chart_keyword_detection` — garde-fou déterministe (word boundaries)
- `test_supervisor_handoff_logging` — artifacts propagés

Anciens tests v3 conservés (29 tests).

---

## 7. Limites actuelles (v4)

### 🔴 7.1 Filtres sur colonnes-listes — **inchangé depuis v1**
**Problème structurel non résolu.** Les filtres s'appliquent **avant** l'explosion des listes (`genres`, `cast_names`, `directors`...).
- Question : *"Combien de films pour Robert Downey Jr. ?"*
- Modèle peut générer : `filters: ["cast_names == 'Robert Downey Jr.'"]`
- Résultat : 0 (la liste `["Robert Downey Jr.", "Autre"] != "Robert Downey Jr."`)
- **Aucune erreur levée**, résultat vide → `nonempty` échoue trop tard.
- *Contournement v4* : prompt Data Agent dit "n'utilise jamais de filtre sur colonnes-listes ; fais groupby+top_k complet". Pas de garde-fou code.

### 🟠 7.2 Retries LLM inertes sur erreurs 503
Même mécanisme qu'avant : 3 essais × 5s par modèle, puis suivant. Si tous saturés → échec. Pas de backoff exponentiel.

### 🟠 7.3 Graphiques écrasés (`chart.png` par défaut)
Le modèle fournit souvent un chemin, mais si absent → `chart.png` écrase le précédent. Les logs JSON sont horodatés, pas les PNG.

### 🟠 7.4 Confinement chemins par préfixe string
`str(path).startswith(str(CHARTS_DIR.resolve()))` accepte `../charts_evil/x.png` → `output/charts_evil/`.

### 🟡 7.5 Détection mots-clés chart perfectible
Regex word-boundary évite "plusieurs" → "plus", mais faux positifs possibles sur "classement" dans "reclassement". Pas de vraie NLP.

### 🟡 7.6 Observabilité coût LLM nulle
Log : durée exécution outils (ms), **rien** sur appel modèle : tokens, latence, tentatives, modèle utilisé. C'est le **seul poste monétaire** et la seule source d'incertitude.

### 🟡 7.7 `synthesize` sans vérification profonde
L'étape `synthesize` n'a que vérification syntaxique (`type string`). Pas de plausibilité/recalcul sur la réponse NL.

### 🟡 7.8 RedactionAgent flow forcé — coupling fort
La classe `RedactionAgent` surcharge `run()` pour forcer `synthesize → finish`. Cela casse l'uniformité de `BaseAgent` et rend le code moins générique.

### 🟡 7.9 Compression lossy — information perdue
**Chaque handoff est une compression avec perte.** Exemple : le Data Agent a 2 tours d'historique (load_data + compute) mais le Superviseur ne reçoit que 233 chars de résumé. Si le Superviseur doit déboguer *pourquoi* le compute a choisi tel groupby, l'information n'est plus là.

### 🟡 7.10 Pas de parallélisme
Exécution séquentielle seulement. Le pattern *Teammate* (parallèle) du Component 11 n'est pas implémenté.

### 🟡 7.11 Cache DataFrame jamais invalidé
`tools._df_cache` global, partagé en lecture. Pas d'invalidation possible si on changeait de dataset.

### 🟡 7.12 Limites de tours arbitraires
- Superviseur : 10 tours
- Data Agent : 5 tours
- Viz Agent : 2 tours
- Redaction Agent : 3 tours
Pas de dynamique adaptative.

---

## 8. Limites par conception (assumées, architecture multi-agent)

| Limite | Pourquoi |
|---|---|
| **Compression lossy à chaque handoff** | Tradeoff explicite : contexte borné vs information complète. Component 11 : *"Every handoff is a lossy compression operation. That information loss is the real cost of multi-agent systems."* |
| **Pas de rétroaction sur données brutes** | Les agents ne voient jamais le DataFrame, seulement les résultats condensés. Le superviseur ne peut pas "regarder les données". |
| **Single-agent pas de délégation réelle** | Un seul modèle (Gemini) joue tous les rôles via prompts différents. Pas de spécialisation modèle (ex: modèle code pour Data, modèle créatif pour Redaction). |
| **Max 10 tours superviseur** | Garde-fou contre boucles infinies ; peut couper une analyse complexe. |
| **Contexte linéaire non compressé côté superviseur** | L'historique `supervisor_turns` grossit ; pas de résumé/compression pour longs runs. |
| **Pas de time-travel debugging** | Pas de checkpointing comme LangGraph (Component 7). |

---

## 9. Ce que ce projet enseigne (v4 — leçons actualisées)

**1–15 (v1/v2/v3) — toujours valides**

### 16. Agents-as-tools > Plan-then-execute pour la modularité
Exposer des agents comme des tools Function Calling natifs permet au superviseur de **décider dynamiquement** quel agent appeler, dans quel ordre, avec quel contexte. Plus flexible qu'un plan figé.

### 17. Compression de contexte = necessity, pas optimisation
En v3 single-agent, le contexte grossit linéairement. En v4 multi-agent, la compression **est obligatoire** pour que le pattern scale. Component 11 : *"The parent agent's context stays clean."*

### 18. Vérification distribuée > Vérification centralisée
Chaque agent vérifie son propre output (plausibilité, recalcul) avant de le retourner. Le superviseur **fait confiance** au statut `success`. Cela évite de re-vérifier et garde les responsabilités claires.

### 19. Garde-fous déterministes > Décisions du modèle
Le chart guardrail (`should_generate_chart`) est une **règle code**, pas une décision LLM. Cela garantit la cohérence (toutes les questions "top/classement" ont un graphique) sans dépendre de l'attention du modèle.

### 20. RedactionAgent flow forcé = pattern "scaffolding"
Forcer `synthesize → finish` via une classe dédiée est du **scaffolding** (Component 11/12). Quand les modèles s'amélioreront, ce scaffolding deviendra inutile — le modèle appellera `finish` naturellement après `synthesize`.

### 21. Logs hiérarchiques = observabilité multi-niveau
`supervisor_turns` (vue métier) + `subagent_logs` (vue technique) + `handoff` (métriques compression) permettent de **tracer la compression** concrètement : `context_chars_sent`, `compression_ratio`, `result_chars_received`.

### 22. Dataset partagé en lecture = simplicité opérationnelle
`tools._df_cache` global évite de recharger 4803 films × 3 agents. Acceptable car lecture seule. En production avec écriture, il faudrait une architecture différente.

---

## 10. Synthèse comparative v1 → v2 → v3 → v4

| Composant harness | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| Planification | LLM (plan JSON) | LLM (plan JSON) | **ReAct (action par action)** | **ReAct Superviseur + Agents-as-tools** |
| Vérification sortie | Syntaxique seulement | **Syntaxique + Sémantique** | **Syntaxique + Sémantique (chaque tour)** | **Distribuée par agent (chaque tour)** |
| Réponse NL | ❌ | ✅ (étape forcée) | ✅ (après `finish` choisi par LLM) | ✅ (Agent Rédaction dédié) |
| Gestion échec | Retry aveugle ×2 | Retry aveugle ×2 | **Modèle voit échec → s'adapte** | **Agent voit échec → s'adapte ; Superviseur décide** |
| Function Calling | ❌ (JSON textuel) | ❌ (JSON textuel) | ✅ **Natif Gemini** | ✅ **Natif Gemini (4 schémas distincts)** |
| Contexte | Complet, grossit | Complet, grossit | Complet, grossit | **Compressé au handoff, condensé au retour** |
| Graphiques | Optionnel | Forcé par planner | Forcé par planner (mots-clés) | **Garde-fou déterministe + Agent Viz dédié** |
| Composants harness | 7/9 | 8/9 | 8/9 | **11/12** (manque: parallélisme, time-travel, observabilité LLM) |
| Tests | 22 | 34 | 29 | **36** (7 multi-agent + 29 v3) |
| Logs | `plan` + `steps` | `plan` + `steps` | `turns` | **`supervisor_turns` + `subagent_logs` + `handoff`** |

---

## 11. Prochaines étapes recommandées

| Priorité | Action |
|---|---|
| **Haute** | Garde-fou code : interdire filtres sur colonnes-listes dans `compute` (lever `ValueError`) |
| **Haute** | Noms de graphiques uniques (`chart_<run_id>_<turn>.png`) |
| **Moyenne** | Confinement chemins avec `path.is_relative_to()` (Py 3.9+) |
| **Moyenne** | Observabilité LLM : loguer tokens, latence, modèle, tentatives par tour |
| **Moyenne** | Backoff exponentiel + jitter pour retries LLM (Component 8) |
| **Basse** | Parallélisme *Teammate* : `call_data_agent` + `call_viz_agent` simultanés quand indépendants |
| **Basse** | Compression d'historique superviseur pour runs > 10 tours |
| **Basse** | Cache DataFrame invalidable (optionnel) |
| **Basse** | Time-travel debugging (checkpoints LangGraph-style) |
| **Basse** | Modèles spécialisés par agent (ex: `gemini-pro` pour Data, `gemini-flash` pour Redaction) |

---

## 12. Vérifications v4 effectuées (17/09/2026)

| Affirmation | Méthode | Résultat |
|---|---|---|
| Function Calling natif agents-as-tools opérationnel | 3 runs complets sans erreur 400 | Confirmé |
| Boucle Superviseur 4 tours typiques | 3 questions testées | Confirmé (data → viz → redaction → finish) |
| Compression contexte mesurée | Logs `handoff.compression_ratio` | Confirmé (82-100% selon tour) |
| Condensation résultats mesurée | Logs `handoff.result_chars_received` | Confirmé (137-291 chars) |
| Deep verification par agent | Logs `subagent_logs.*.verification_status` | Confirmé (tous "passed") |
| Garde-fou chart déterministe | Test `test_chart_keyword_detection` + 3 runs | Confirmé (tous chart générés) |
| Flow RedactionAgent forcé | Logs `internal_turns: synthesize → finish` | Confirmé |
| Suite de tests | `python -m unittest discover -s tests` | **36 tests, OK** |
| Logs hiérarchiques | `runs/*.json` inspectés | Confirmé (`supervisor_turns` + `subagent_logs`) |

---

## Annexe — Note pédagogique : Pourquoi ce projet n'a pas l'échelle pour justifier le multi-agent

Le Component 11 le dit explicitement :
> *"If your task is 30 turns and fits in one context, multi-agent only adds latency and handoff loss."*

Ce projet : **3-4 tours superviseur**, contexte < 10 KB, 5 outils bas niveau. L'architecture v3 ReAct single-agent était **parfaitement adaptée**.

La v4 est un **exercice pédagogique** pour :
1. Comprendre le pattern *agents-as-tools* (Function Calling natif)
2. Implémenter la compression/condensation de contexte
3. Tracer la perte d'information (handoff metrics)
4. Expérimenter la vérification distribuée
5. Comparer concrètement v3 vs v4 sur le même dataset

**En production** : restez en v3 pour ce volume. Passez en v4 quand :
- Contexte > 75% de la fenêtre du modèle
- Outils > 10 (décision quality degrades)
- Sous-tâches genuinely indépendantes (différents domaines, permissions)

---

*Documentation v4 — harness-tmdb Multi-Agent — 17 septembre 2026*