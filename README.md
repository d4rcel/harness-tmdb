# harness-tmdb

A multi-agent Python harness that answers natural-language questions about a static TMDB dataset. Each question is turned into a structured JSON plan by an LLM (Gemini), then executed step by step by a deterministic executor with verification and full observability.

Pedagogical project: it demonstrates the fundamentals of harness engineering — orchestration loop, tool management, verification and run logging — not a production tool.

## Data

Two static CSVs, joined 1:1 on `movies.id` = `credits.movie_id` (4803 films):

- `tmdb_5000_movies.csv` (~5.7 MB): budget, revenue, genres, release_date, popularity, votes, runtime, ...
- `tmdb_5000_credits.csv` (~40 MB): cast, crew.

No live TMDB API. Many text columns are JSON-encoded list strings (`cast`, `crew`, `genres`, ...) and are parsed at load time.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The harness calls the Gemini API. Put your key in `GEMINI_API_KEY` (env or `.env`, gitignored); the model is `GEMINI_MODEL` (default `gemini-3.6-flash`). See `.env.example`.

The default model is reliable; saturated aliases (`gemini-flash-latest`) are kept
in the fallback chain. The harness retries each model up to 3 times with
backoff, then tries the candidates in `GEMINI_FALLBACK_MODELS` (default
`gemini-flash-latest,gemini-flash-lite-latest,gemini-3.1-flash-lite`). The model
that actually answered is recorded in the run log. Set `GEMINI_FALLBACK_MODELS`
to override.

## Usage

```bash
.venv/bin/python -m harness.main "Quels sont les 5 genres les plus rentables en ROI depuis 1997 ?"
```

Long questions can be piped on stdin. Each run is logged as JSON in `runs/<timestamp>.json`; charts go to `output/charts/`, files to `output/results/`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

## Architecture

```
question
  → Planner   : Gemini → validated JSON plan (response_mime_type=application/json)
  → Executor  : for each step → resolve tool → run → verify → log
                (retry ×2 per step, abort if >1 step fails)
  → Memory    : one JSON run log per question
```

- `harness/planner.py` — question → validated JSON plan, retried once on schema failure.
- `harness/executor.py` — deterministic orchestration loop.
- `harness/tools.py` — tool registry: `load_data` (cached joined/enriched DataFrame, `compute` (filter/groupby/agg/sort/top_k), `chart` (matplotlib PNG), `write_file`.
- `harness/verify.py` — check clauses: `nonempty`, `bound`, `type`.
- `harness/llm.py` — Gemini wrapper that auto-retries transient 503s (5×, 6 s backoff).

Data caveats encoded in the planner prompt: `budget`/`revenue` are 0 on many films (filter before profitability; ROI needs `budget>0`), the date window is 1916 → 2017, and genres are multi-label (per-genre averages weight films several times).