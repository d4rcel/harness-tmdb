# Documentation v1 — État des lieux du harness TMDB

> Document d'analyse, pas de spécification. Il décrit le système **tel qu'il est**
> au 20 août 2026, avant toute correction. Chaque affirmation a été vérifiée
> contre le code et contre les journaux de runs réels (voir l'annexe).

---

## 1. Ce dont on parle

`harness-tmdb` répond à des questions en français sur un dataset statique de
4803 films TMDB. Une question devient un **plan JSON** produit par Gemini, puis
ce plan est **exécuté pas à pas** par un orchestrateur déterministe qui vérifie
et journalise chaque étape.

Environ 900 lignes de Python, dont **une seule fonction parle à un modèle**.
Tout le reste est de la plomberie. C'est déjà le premier enseignement du projet.

### L'architecture en une phrase

Le modèle décide **quoi faire**, une fois, au début. Le code décide **comment
le faire**, toujours de la même manière. Le modèle ne voit jamais les données
ni les résultats.

C'est un patron reconnu : **plan-then-execute**. À distinguer de la boucle
agentique (style ReAct) où le modèle observe chaque résultat et re-décide.

---

## 2. Grille de lecture : qu'est-ce qui fait un harness ?

Un harness, c'est l'échafaudage autour du modèle. Voici les composants
canoniques, et l'état de chacun dans ce projet.

| Composant | Présent | État |
|---|---|---|
| Registre d'outils | ✅ | Solide — 4 outils, paramétrables, jamais de code généré |
| Boucle d'orchestration | ✅ | Solide — séquentielle, état partagé explicite |
| Validation des entrées | ✅ | Solide — le plan est relu et peut être rejeté |
| Vérification des sorties | ⚠️ | Présente mais **syntaxique uniquement** |
| Politique d'échec | ✅ | Explicite et graduée — mais les retries sont inertes |
| Mémoire / traçabilité | ⚠️ | Excellente sur l'exécution, **muette sur le LLM** |
| Confinement des effets | ✅ | Présent — deux dossiers autorisés, contrôle imparfait |
| Boucle de rétroaction | ❌ | **Absente** — choix d'architecture assumé |
| Synthèse de la réponse | ❌ | **Absente** — manque fonctionnel |

Sept composants sur neuf sont là. Les deux manquants expliquent l'essentiel des
faiblesses décrites plus bas.

---

## 3. Forces

### 3.1 La séparation décision / exécution est nette

C'est la réussite principale. On peut retirer Gemini du projet, écrire un plan à
la main, et tout le reste fonctionne à l'identique. L'infrastructure ne dépend
pas du modèle — signe qu'elle est bien le sujet et non un accessoire.

Bénéfice concret : à température 0 et avec un seul appel, la même question donne
le même plan, donc les mêmes chiffres. Un run coûte **un** appel réseau et 4,4
secondes.

### 3.2 Le prompt système est une couche de connaissance métier versionnée

`planner_prompt.py` n'est pas du texte décoratif. Il encode des faits vérifiés
dans les CSV : 1037 budgets à zéro, 1427 revenus à zéro, fenêtre 1916–2017,
genres multi-labels. Quelqu'un a ouvert les données, s'est cassé les dents, et a
figé les leçons dans un fichier lisible et diffable.

Effet mesurable : sur la question test, le modèle a ajouté **de lui-même** les
filtres `budget > 0` et `revenue > 0` que l'utilisateur n'avait jamais demandés.

C'est ce qui sépare un harness qui marche d'un jouet : le savoir métier est
externalisé, versionné, et injecté à chaque appel.

### 3.3 Aucune confiance accordée à la sortie du modèle

Le plan reçu est **relu et peut être rejeté** : outils inconnus, `step_id` non
numérique, clause de vérification inconnue, première étape qui n'est pas un
chargement. En cas de rejet, une seule nouvelle tentative, avec l'erreur exacte
renvoyée au modèle. Puis échec bruyant.

Conséquence architecturale élégante : puisque la validation garantit que l'outil
existe, la résolution dans l'exécuteur ne peut pas échouer. **On doute aux
frontières pour que le cœur reste bête et sûr.**

### 3.4 Les oublis du modèle sont rattrapés mécaniquement

`_ensure_chart` relit la *question de l'utilisateur*, pas le plan. Si elle
contient un mot de classement ou de visualisation et que le plan n'a pas
d'étape graphique, une étape est fabriquée et insérée après le dernier calcul.

Le principe est plus important que l'implémentation : **sur ce qui compte, on ne
prie pas pour que le modèle fasse bien — on rattrape.**

### 3.5 Le modèle ne peut pas écrire de code

Les outils sont des **formulaires à remplir**, pas des fonctions à composer. Le
modèle choisit des filtres, un regroupement, une agrégation — mais l'ordre des
opérations (filtrer → regrouper → agréger → trier → tronquer) est figé dans le
code, écrit et testé une fois pour toutes.

C'est la décision de sécurité la plus importante du projet, et elle est presque
invisible. Aucun `eval`, aucun code généré, aucune requête arbitraire. La
surface d'attaque se réduit à « quelles valeurs peut-on mettre dans quelles
cases ».

### 3.6 La politique d'échec est explicite et graduée

Deux niveaux, traités différemment :

- **Une étape** échoue → jusqu'à 2 retentatives → marquée `failed`, mais le run
  **continue**. Un résultat partiel documenté vaut mieux que rien.
- **Une deuxième étape** échoue → run `aborted`. Un échec isolé est un accident ;
  deux échecs signalent un plan structurellement mauvais.

Côté LLM, les modes de défaillance sont **discriminés**, ce qui est rare et bien
vu :

| Erreur | Traitement | Pourquoi |
|---|---|---|
| 429, 500, 502, 503, 504 | 3 essais, 5 s d'attente | Transitoire |
| 404 et autres | Modèle suivant, immédiatement | Le modèle n'existe pas |
| 400, 401, 403 | Arrêt total | Le problème est la clé, pas le modèle |

Sans cette distinction, une clé invalide coûterait 12 appels et une minute pour
finir sur la même erreur.

### 3.7 La traçabilité de l'exécution est complète

Un journal JSON par run, horodaté à la microseconde : question, modèle ayant
réellement répondu, plan intégral, et pour chaque étape ses arguments exacts, sa
durée, son statut, et **toutes les tentatives avec leur verdict**.

33 runs archivés à ce jour. On peut rouvrir n'importe lequel et reconstituer
exactement ce qui a été calculé. C'est la différence entre « l'IA m'a sorti ce
chiffre » et « voici le calcul, voici les hypothèses, vérifiez ».

### 3.8 Le projet est testable, et testé

22 tests, tous verts, **aucun n'appelle l'API**. Ils couvrent la jointure, les
colonnes dérivées, l'explosion des listes, les filtres, les replis d'axes, et les
garde-fous du planificateur.

C'est la conséquence directe du choix d'architecture : parce que l'exécution est
déterministe et séparée du modèle, elle est testable comme du code ordinaire.
Un système agentique est bien plus difficile à tester.

---

## 4. Limites par conception

**À ne pas confondre avec des faiblesses.** Ce sont des conséquences assumées de
choix d'architecture. Les corriger signifierait changer d'architecture.

### 4.1 Le modèle planifie à l'aveugle

Gemini ne voit jamais une ligne du dataset. Il ne le connaît qu'à travers la
description qu'on lui en fait. Il ne peut donc pas savoir qu'une valeur qu'il
suppose présente est absente.

**Cas vérifié :** à la question « dans combien de films Tony Stark a joué ? », le
modèle planifie un filtre sur les acteurs avec la valeur « Tony Stark ». Mais
Tony Stark est un *personnage*, pas un acteur — et le chargement **jette** les
personnages pour ne garder que les noms d'acteurs. Zéro occurrence. Le modèle ne
pouvait pas le deviner.

### 4.2 Aucune adaptation à ce qui est découvert

Le plan est écrit avant que la moindre donnée ne soit lue, et ne peut plus
changer. Si le résultat est absurde, personne ne le voit.

C'est le prix exact payé pour le déterminisme, le coût réduit et
l'auditabilité. Le compromis est défendable — mais il faut savoir ce qu'on
achète et ce qu'on vend.

### 4.3 Un seul rôle de modèle, appelé une fois

Le harness est **single-agent**, comme son propre prompt système l'énonce.
Et le mot « agent » est même généreux : un agent boucle ; ce planificateur
traduit une phrase en formulaire, une fois, puis sort de scène.

> ⚠️ Le `README.md` annonce « un harness **multi-agents** », ce que le code
> contredit explicitement. À corriger : la formulation exacte et défendable est
> **« harness single-agent, architecture plan-then-execute »**.

### 4.4 Une seule chaîne de données possible

Le plateau partagé n'a que deux cases : le tableau et *le dernier* résultat.
Deux calculs successifs s'écrasent — le graphique ne verrait que le second.
En pratique, une seule vraie chaîne existe : calcul → graphique.

### 4.5 Les moyennes par genre comptent les films plusieurs fois

L'explosion des genres duplique chaque film autant de fois qu'il a de genres.
Une « moyenne par genre » porte donc sur tous les films *touchant* au genre, pas
sur ceux qui n'en relèvent que. Ni faux ni juste : c'est une définition — mais
elle doit être dite, et elle l'est dans le prompt.

---

## 5. Faiblesses

Défauts réels, corrigeables **sans** changer l'architecture. Classés par gravité.

### 🔴 5.1 La vérification est syntaxique, jamais sémantique

**C'est la faiblesse centrale du système.** Les trois clauses disponibles
(`nonempty`, `bound`, `type`) répondent toutes à la même question : *« le calcul
a-t-il produit quelque chose ? »* Aucune ne répond à *« ce quelque chose a-t-il
du sens ? »*

Deux cas vérifiés, tous deux marqués **verts** :

**Cas A — l'aberration statistique.** Le run du 19/08 affiche un ROI moyen de
**412 428 %** pour le genre Horreur. Ce nombre provient à ~98 % d'un seul film,
*Nurse 3-D*, dont le budget est enregistré à **10 dollars** — donnée corrompue
que le filtre `budget > 0` laisse passer sans broncher. La **médiane** du genre
est de 163 %. Cinq lignes ont été produites, donc `nonempty` passe.

**Cas B — la requête mal formée.** Un filtre cherchant un acteur précis renvoie
**0 film pour Robert Downey Jr.**, présent dans 29 films (raison en 5.3). Le
calcul produit malgré tout une ligne d'agrégat contenant `0`. Une ligne, donc
`nonempty` passe.

Dans les deux cas : toutes les étapes vertes, run `completed`, réponse fausse
présentée avec la même assurance qu'une vraie.

> **Un système qui ne sait pas distinguer « aucun résultat » de « ma requête
> était mal formée » racontera des choses fausses en toute sérénité.**

### 🔴 5.2 Le harness ne répond jamais à la question

Il n'existe aucune étape de synthèse. Le champ `answer_intent`, généré par le
modèle au début, n'est **plus jamais relu** par aucune ligne de code. Le
récapitulatif final ne contient que `steps_ok` et `steps_total`.

Pour connaître la réponse, il faut ouvrir le JSON du run ou le PNG. Un outil qui
répond à des questions en français ne répond jamais en français : c'est le
manque fonctionnel le plus visible, et le chaînon le plus naturel à ajouter.

### 🟠 5.3 Les filtres sur colonnes-listes échouent silencieusement

Les filtres s'appliquent **avant** l'explosion des listes. Filtrer sur les
acteurs revient donc à comparer une *liste entière* à un nom unique : ça ne
matche jamais, et surtout **ça ne lève aucune erreur** — le résultat est
simplement vide.

Conséquence : toute question du type « combien de films pour tel acteur / tel
réalisateur » est **structurellement impossible**, alors que la donnée existe.
On ne peut obtenir que le classement complet et y chercher son nom.

Le prompt système avertit le modèle (« les filtres ne s'appliquent jamais aux
colonnes-listes »), mais rien dans le code ne le fait respecter. Un avertissement
n'est pas un garde-fou.

### 🟠 5.4 Les retries sont inertes

`MAX_RETRIES_PER_STEP = 2` relance le **même outil, avec les mêmes arguments,
sur le même tableau en cache**. L'exécution étant totalement déterministe, une
étape qui échoue échouera trois fois à l'identique.

Le mécanisme triple le coût sans jamais changer l'issue. Un retry n'a de sens que
si quelque chose peut changer entre deux tentatives — et dans une architecture
sans rétroaction, rien ne change.

### 🟠 5.5 Le graphique est écrasé à chaque run

Le modèle fournit rarement un chemin, donc le nom par défaut `chart.png`
s'applique. Chaque run détruit l'image du précédent — alors que les journaux
JSON, eux, sont soigneusement horodatés. Incohérence nette dans la politique
d'archivage.

### 🟡 5.6 Le confinement des chemins repose sur un préfixe de chaîne

Le contrôle vérifie que le chemin résolu *commence par* le dossier autorisé.
Un dossier **frère** au nom voisin passe donc le test : `../charts_evil/x.png`
s'échappe vers `output/charts_evil/` et est accepté (vérifié).

L'évasion reste bornée à `output/` et le vecteur est théorique — le modèle ne lit
jamais de contenu hostile. Mais le contrôle ne fait pas ce qu'il prétend faire.
La comparaison doit porter sur la hiérarchie de chemins, pas sur du texte.

### 🟡 5.7 Détection de mots-clés par sous-chaîne

Le déclenchement du graphique cherche `"plus"` par simple inclusion textuelle.
Toute question contenant « **plus**ieurs » force donc un graphique non désiré
(vérifié). Il faut comparer des mots, pas des fragments.

### 🟡 5.8 Dérive entre le prompt et le code sur la définition du ROI

Le prompt système annonce au modèle `ROI = (revenue - budget) / budget`, soit un
**ratio**. Le code multiplie par 100 et produit donc des **pourcentages**.

Le modèle planifie avec une définition, le système en applique une autre. Les
nombres sont 100 fois plus grands qu'annoncé, sans unité nulle part. C'est ce
qui rend « 412 428 » d'autant plus illisible.

### 🟡 5.9 Aucune observabilité du coût de l'IA

Le journal enregistre la durée de chaque étape d'exécution — mais **rien** sur
l'appel au modèle : ni tokens consommés, ni latence, ni nombre de tentatives
réseau. Or c'est le seul poste de coût monétaire du système.

Le harness observe finement ce qui est gratuit et déterministe, et pas du tout ce
qui est payant et variable.

### 🟡 5.10 Détails de finition

- Le journal affiche `rows=?` pour l'étape de chargement (la clause cherche
  `count`, l'outil renvoie `rows`). Cosmétique, mais 4803 films chargés
  s'affichent comme une inconnue.
- Le cache du tableau n'est jamais invalidé : modifier un CSV en cours de
  process n'a aucun effet.
- La constante `MAX_TEMPERATURE` désigne en réalité *la* température, pas un
  maximum.
- Les personnages du casting sont jetés au chargement, alors qu'ils existent
  dans les données brutes. Choix implicite, jamais documenté, qui ferme toute
  une famille de questions.

---

## 6. Ce que ce projet enseigne sur le harness engineering

Les leçons transférables, indépendantes du dataset.

**1. La plus grande partie d'un système d'IA n'est pas de l'IA.**
Une fonction sur ~900 lignes parle au modèle. Tout le reste — registre, boucle,
vérification, journal, confinement — est du code ordinaire. Et c'est ce code
ordinaire qui décide si le résultat est fiable.

**2. Le prompt système est du code.**
Il encode du savoir métier vérifié, il se versionne, il se diffe, il se teste par
ses effets. Le traiter comme du texte jetable, c'est perdre l'actif le plus
précieux du système.

**3. Donner des formulaires, pas un interpréteur.**
Faire remplir des paramètres à un modèle est radicalement plus sûr que lui faire
écrire du code. La liberté qu'on lui laisse est la surface d'attaque qu'on
accepte.

**4. Valider aux frontières pour que le cœur reste bête.**
Un doute exhaustif à l'entrée permet une exécution sans défiance ensuite. C'est
plus simple à lire, à tester et à raisonner qu'une méfiance diffuse partout.

**5. Ne pas espérer — rattraper.**
Sur les points qui comptent, un garde-fou déterministe dans le code vaut mieux
qu'une instruction dans le prompt. Une instruction est une suggestion.

**6. Discriminer les modes d'échec.**
« Réessayer » n'est pas une politique. Transitoire, spécifique au modèle, et
fatal appellent trois traitements différents. Et un retry qui rejoue exactement
la même opération déterministe ne sert à rien.

**7. La vérification est le maillon faible — toujours.**
C'est le composant le plus facile à implémenter superficiellement et le plus
difficile à faire correctement. Une clause qui vérifie la *forme* donne
l'illusion du contrôle sans en donner la substance.

> **Une étape verte ne signifie pas une réponse juste. Elle signifie que le
> calcul demandé a eu lieu.** Reconnaître cette différence est probablement le
> cœur du sujet.

**8. Journaliser ce qui varie et ce qui coûte.**
Ici, l'exécution déterministe est finement tracée, et l'appel au modèle —
la seule source d'incertitude et le seul poste de dépense — ne l'est pas du tout.
C'est exactement l'inverse de la priorité utile.

---

## 7. Synthèse

Ce harness fait bien ce qu'il prétend faire : montrer les fondamentaux de
l'orchestration. La séparation décision/exécution est propre, le confinement est
réel, la traçabilité de l'exécution est exemplaire, et le tout est testé sans
dépendre du réseau.

Ses deux manques structurants sont **une vérification purement syntaxique** et
**l'absence de réponse en langage naturel**. Le premier laisse passer des
résultats faux en les marquant verts ; le second oblige à lire du JSON pour
connaître une réponse.

Ce ne sont pas des détails d'implémentation : ce sont les deux composants
manquants de la grille du §2. Tout le reste — les huit autres faiblesses — se
corrige en une session de travail.

---

## Annexe — Vérifications effectuées

Tout ce qui est affirmé ci-dessus a été contrôlé le 20 août 2026 :

| Affirmation | Méthode | Résultat |
|---|---|---|
| Run de référence complet | `runs/20260819T153737017240.json` | 3 étapes, toutes vertes, `completed` |
| ROI Horreur = 412 428 % | Rejeu du calcul sur les données | Confirmé — médiane 163 % |
| *Nurse 3-D*, budget 10 $ | Tri du genre Horreur par ROI | Confirmé — ROI 10⁸ % |
| Personnages absents | Recherche « Tony Stark » | 0 occurrence (vs 29 pour R. Downey Jr.) |
| Filtre acteur → 0 ligne | Appel direct de l'outil de calcul | Confirmé, sans erreur levée |
| « plusieurs » force un graphique | Test des mots-clés | Confirmé |
| Évasion `../charts_evil/` | Test du contrôle de chemin | Confirmé — accepté à tort |
| Suite de tests | `python -m unittest discover -s tests` | 22 tests, OK |
| Dérive ROI prompt/code | Lecture croisée | Prompt : ratio · Code : ×100 |
| Runs archivés | `ls runs \| wc -l` | 33 |
