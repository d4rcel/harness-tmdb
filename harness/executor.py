"""Executor: the orchestration loop.

For each planned step: resolve the tool from the registry, run it, verify the
output against the step's validate clause, then record the outcome. A step that
fails verification is retried up to MAX_RETRIES_PER_STEP; beyond that it is
marked failed and, if more than MAX_FAILED_STEPS_BEFORE_ABORT steps fail, the
whole run aborts loudly — all visible in the run log.
"""

from __future__ import annotations

import time

from . import config, memory, tools, verify


def execute(plan: dict, record: dict) -> str:
    config.ensure_dirs()
    record["plan"] = plan
    ctx: dict = {"df": None, "last_result": None}
    failed_steps = 0

    for step in plan["steps"]:
        step_record = {
            "step_id": step["step_id"],
            "intent": step.get("intent", ""),
            "tool": step["tool"],
            "args": step.get("args", {}),
            "status": "running",
            "attempts": [],
        }
        started = time.time()
        ok = False

        for attempt in range(1 + config.MAX_RETRIES_PER_STEP):
            output = _run_tool(step, ctx)
            verification = verify.check_output(step.get("validate"), output)
            step_record["attempts"].append(
                {
                    "attempt": attempt,
                    "result": _collect_result(output),
                    "verification": verification,
                }
            )
            if verification["passed"]:
                ok = True
                break

        step_record["status"] = "success" if ok else "failed"
        step_record["duration_ms"] = int((time.time() - started) * 1000)
        step_record["verification"] = next(
            (
                a["verification"]
                for a in step_record["attempts"]
                if a["verification"]["passed"]
            ),
            step_record["attempts"][-1]["verification"],
        )
        memory.append_step(record, step_record)
        print(f"[step {step['step_id']}] {step['tool']} -> {step_record['status']}")

        if not ok:
            failed_steps += 1
            if failed_steps > config.MAX_FAILED_STEPS_BEFORE_ABORT:
                record["status"] = "aborted"
                record["final"] = {
                    "error": "too many failed steps",
                    "failed_steps": failed_steps,
                }
                print(f"[executor] ABORT: {failed_steps} steps failed")
                return memory.save_run(record)

    record["status"] = "completed"
    record["final"] = {
        "steps_ok": sum(1 for s in record["steps"] if s["status"] == "success"),
        "steps_total": len(record["steps"]),
    }
    return memory.save_run(record)


def _run_tool(step: dict, ctx: dict) -> dict:
    tool_fn = tools.TOOLS[step["tool"]]
    try:
        output = tool_fn(step.get("args", {}), ctx) or {}
        if step["tool"] == "load_data":
            ctx["df"] = tools._df_cache
        elif step["tool"] == "compute":
            ctx["last_result"] = output
        return output
    except Exception as exc:
        return {"error": str(exc), "count": 0, "rows": []}


def _collect_result(output: dict) -> dict:
    keys = ("count", "columns", "rows", "path", "points", "error")
    return {k: output[k] for k in keys if k in output}