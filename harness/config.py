"""Harness configuration: paths, model, execution policy.

Everything here is a constant or env-driven; no secrets in source.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env()

DATA = {
    "movies": ROOT / "tmdb_5000_movies.csv",
    "credits": ROOT / "tmdb_5000_credits.csv",
}

RUNS_DIR = ROOT / "runs"
OUTPUT_DIR = ROOT / "output"
CHARTS_DIR = OUTPUT_DIR / "charts"
RESULTS_DIR = OUTPUT_DIR / "results"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or ""
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash"
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in (
        os.environ.get("GEMINI_FALLBACK_MODELS")
        or "gemini-flash-latest,gemini-flash-lite-latest,gemini-3.1-flash-lite"
    ).split(",")
    if m.strip()
]

# Execution policy.
MAX_RETRIES_PER_STEP = 2
MAX_FAILED_STEPS_BEFORE_ABORT = 1
MAX_TEMPERATURE = 0.0
MAX_OUTPUT_TOKENS = 8192
LLM_ATTEMPTS_PER_MODEL = 3
LLM_RETRY_SLEEP_SECONDS = 5.0

# Deep verification policy.
ROI_MAX_PLAUSIBLE = 10000.0  # 1,000,000% - allows high ROI but catches micro-budget outliers
RECALCULATION_SAMPLE = 3
RECALCULATION_TOLERANCE = 0.01  # 1% relative tolerance

# ReAct policy (v3 single-agent).
MAX_REACT_TURNS = 5

# Multi-Agent policy (v4).
SUPERVISOR_MAX_TURNS = 10
SUBAGENT_MAX_TURNS = {
    "data_agent": 5,
    "viz_agent": 2,
    "redaction_agent": 2,
}

# Dataset metadata (verified from the raw files).
PAIR_COUNT = 4803
DATE_MIN = "1916-09-04"
DATE_MAX = "2017-02-03"
MISSING_BUDGET = 1037
MISSING_REVENUE = 1427


def ensure_dirs() -> None:
    for path in (RUNS_DIR, CHARTS_DIR, RESULTS_DIR):
        path.mkdir(parents=True, exist_ok=True)