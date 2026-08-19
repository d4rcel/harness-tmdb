"""Run memory: one JSON log per run (question, plan, per-step results)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import config


def new_run_record(question: str, model: str) -> dict:
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "question": question,
        "plan": None,
        "steps": [],
        "final": None,
        "status": "running",
    }


def append_step(record: dict, step_record: dict) -> None:
    record["steps"].append(step_record)


def save_run(record: dict) -> str:
    config.ensure_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    path = config.RUNS_DIR / f"{stamp}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)