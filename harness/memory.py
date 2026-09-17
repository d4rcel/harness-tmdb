"""Run memory: one JSON log per run (question, turns, final answer).

Multi-agent v4: logs include supervisor turns with handoff tracing
and subagent_logs with internal agent details.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import config


def new_supervisor_record(question: str, model: str) -> dict:
    """Create a new multi-agent run record."""
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "question": question,
        "supervisor_turns": [],
        "subagent_logs": {},
        "final": None,
        "status": "running",
    }


def append_supervisor_turn(record: dict, turn_record: dict) -> None:
    """Append a supervisor turn with handoff metadata."""
    record["supervisor_turns"].append(turn_record)


def add_subagent_log(record: dict, agent_name: str, agent_result: dict) -> None:
    """Add a subagent's internal log to the record."""
    record["subagent_logs"][agent_name] = agent_result


def save_run(record: dict) -> str:
    """Save the run record to disk."""
    config.ensure_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    path = config.RUNS_DIR / f"{stamp}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


# Backward compatibility
def new_react_record(question: str, model: str) -> dict:
    return new_supervisor_record(question, model)


def append_turn(record: dict, turn_record: dict) -> None:
    record.setdefault("turns", []).append(turn_record)


def new_run_record(question: str, model: str) -> dict:
    return new_supervisor_record(question, model)


def append_step(record: dict, step_record: dict) -> None:
    record.setdefault("steps", []).append(step_record)