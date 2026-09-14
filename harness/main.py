"""CLI entrypoint: python -m harness.main "<question>".

Reads the question from the first argument or from stdin when no argument is
given (useful for long questions).
"""

from __future__ import annotations

import json
import sys

from . import config, executor, llm, memory, planner
from .config import ensure_dirs


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    question = " ".join(argv).strip() if argv else sys.stdin.read().strip()
    if not question:
        print("Usage: python -m harness.main \"<question>\"")
        return 2

    ensure_dirs()
    record = memory.new_run_record(question, config.GEMINI_MODEL)
    print(f"[planner] primary model={config.GEMINI_MODEL}")

    plan = planner.build_plan(question)
    record["model"] = llm.resolved_model or config.GEMINI_MODEL
    print(f"[planner] answered by model={record['model']}")
    print(f"[planner] plan: {len(plan['steps'])} step(s)")

    log_path = executor.execute(plan, record)
    print(f"\n[harness] run logged to {log_path}")
    print(f"[harness] status: {record['status']}")
    print("\n--- per-step outcome ---")
    for step in record["steps"]:
        v = step.get("verification") or "no-clause"
        dv = step.get("deep_verification")
        if isinstance(dv, dict):
            dv_str = f", deep={dv.get('criterion', '?')}({dv.get('passed')})"
        else:
            dv_str = ""
        print(f"  step {step['step_id']}: {step['tool']} -> {step['status']} ({v}{dv_str})")
    if record["final"]:
        print("\n--- final ---")
        final = dict(record["final"])
        answer = final.pop("answer", None)
        print(json.dumps(final, indent=2, ensure_ascii=False))
        if answer:
            print("\n--- réponse ---")
            print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())