# AGENTS.md

## Repo status

- Working Python harness (packages in `harness/`) over a static TMDB dataset, with tests in `tests/`.
- Not Epitech-related despite the surrounding folder name.

## Data source

- Two static CSVs in the project root, joined 1:1 on `movies.id` = `credits.movie_id` (4803 films):
  - `tmdb_5000_movies.csv` (~5.7 MB): budget, revenue, genres, release_date, popularity, votes, runtime, etc.
  - `tmdb_5000_credits.csv` (~40 MB): cast, crew.
- Gotcha: many text columns are JSON-encoded list strings (`cast`, `crew`, `genres`, `keywords`, ...) — parse with `json.loads`; `harness/tools.py` already does this at load time.
- Data quirks (verified, encoded in the planner prompt): `budget=0` on 1037 films, `revenue=0` on 1427 (must filter for profitability, ROI needs `budget>0`); date window 1916-09-04 → 2017-02-03 (no "20 years" question may exceed 1997+); genres are multi-label (a film belongs to several genres).

## Model access

- The harness calls the Gemini API via the `google-genai` SDK (not OpenCode Zen). Key comes from `GEMINI_API_KEY` (env or `.env`, which is gitignored); model from `GEMINI_MODEL` (default `gemini-3.6-flash`).
- No TMDB API at all. `.env` is gitignored; never commit it.
- Primary `gemini-3.6-flash` is reliable; `gemini-flash-latest` stays a fallback in `GEMINI_FALLBACK_MODELS` because it is frequently 503-saturated. `llm.py` retries each model up to 3× (5 s backoff) then moves on. Verified available on this account; `gemini-2.5-flash` is NOT (404 for new users). The model that answered is recorded in the run log.

## Commands

- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- Run a question: `.venv/bin/python -m harness.main "<question>"` (or pipe the question on stdin)
- Tests: `.venv/bin/python -m unittest discover -s tests`

## Architecture (keep in sync if you change it)

- `planner.py` sends the question + system prompt to Gemini with `response_mime_type=application/json`, validates the returned plan JSON, retries once on schema failure. It also **deterministically appends a chart step** (`_ensure_chart`) for questions with visual or ranking/top/comparison keywords (`graphique, chart, plot, top, classement, plus, évolution, ...`), so a chart is always generated for those questions.
- `executor.py` is a deterministic loop: resolve tool → run → verify → log; retries a step up to 2×, aborts if >1 step fails; all attempts recorded.
- `tools.py` registry: `load_data` (joined/enriched DataFrame, cached per process), `compute` (filter → groupby/agg → sort → top_k; per-film list columns like `directors`/`cast_names` are exploded to singular names `director`/`actor` for groupby; `sort_by` falls back to the agg column when the planner invents an alias), `chart` (matplotlib PNG; returns `count`/`rows` so `verify` clauses work; falls back x/y to real columns and drops None values), `write_file`.
- `verify.py` clauses: `nonempty`, `bound` (count >= min), `type` (field sample).
- One JSON run log per question in `runs/` (gitignored); charts in `output/charts/`, files in `output/results/` (both gitignored).

## Gotchas

- Model availability on this account differs from expectations: `gemini-2.5-flash` 404s for new users while `gemini-3.6-flash` works — always verify a model via `client.models.list()` before hardcoding it.
- The CSV is large; never load it repeatedly or print it wholesale. `load_data` caches the DataFrame; `tools._df_cache` is the single copy.
- `compute` sorts descending when `sort_by` is given; `top_k` truncates rows — keep result payloads small enough for the JSON run log.