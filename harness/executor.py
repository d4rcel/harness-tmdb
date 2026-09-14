"""Executor: the orchestration loop.

For each planned step: resolve the tool from the registry, run it, verify the
output against the step's validate clause, then record the outcome. A step that
fails verification is retried up to MAX_RETRIES_PER_STEP; beyond that it is
marked failed and, if more than MAX_FAILED_STEPS_BEFORE_ABORT steps fail, the
whole run aborts loudly — all visible in the run log.

Deep verification (plausibility + recalculation) runs after syntactic checks
pass. If deep checks fail, the same retry/abort policy applies.
"""

from __future__ import annotations

import time

from . import config, memory, tools, verify


def execute(plan: dict, record: dict) -> str:
    config.ensure_dirs()
    record["plan"] = plan
    ctx: dict = {"df": None, "last_result": None, "step_results": []}
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
            # For synthesize step, inject the collected step results
            if step["tool"] == "synthesize":
                step_args = dict(step.get("args", {}))
                step_args["step_results"] = ctx["step_results"]
                output = _run_tool_with_args(step, step_args, ctx)
            else:
                output = _run_tool(step, ctx)
            
            syntactic = verify.check_output(step.get("validate"), output)
            deep = _run_deep_verification(step, output, ctx)
            
            step_record["attempts"].append(
                {
                    "attempt": attempt,
                    "result": _collect_result(output),
                    "verification": syntactic,
                    "deep_verification": deep,
                }
            )
            if syntactic["passed"] and deep["passed"]:
                ok = True
                break

        step_record["status"] = "success" if ok else "failed"
        step_record["duration_ms"] = int((time.time() - started) * 1000)
        # Find the first passing verification for the summary
        step_record["verification"] = next(
            (
                a["verification"]
                for a in step_record["attempts"]
                if a["verification"]["passed"] and a["deep_verification"]["passed"]
            ),
            step_record["attempts"][-1]["verification"],
        )
        memory.append_step(record, step_record)
        print(f"[step {step['step_id']}] {step['tool']} -> {step_record['status']}")

        # Store step result for synthesis
        ctx["step_results"].append({
            "step_id": step["step_id"],
            "tool": step["tool"],
            "intent": step.get("intent", ""),
            "result": _collect_result(step_record["attempts"][-1]["result"]),
        })

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

    # Extract the natural language answer from the synthesize step
    answer = None
    for step in record["steps"]:
        if step["tool"] == "synthesize":
            last_attempt = step["attempts"][-1]
            answer = last_attempt["result"].get("answer")
            break
    
    record["status"] = "completed"
    record["final"] = {
        "steps_ok": sum(1 for s in record["steps"] if s["status"] == "success"),
        "steps_total": len(record["steps"]),
    }
    if answer:
        record["final"]["answer"] = answer
    return memory.save_run(record)


def _run_tool_with_args(step: dict, args: dict, ctx: dict) -> dict:
    """Run a tool with overridden args (used for synthesize)."""
    tool_fn = tools.TOOLS[step["tool"]]
    try:
        output = tool_fn(args, ctx) or {}
        return output
    except Exception as exc:
        return {"error": str(exc), "count": 0, "rows": []}


def _run_deep_verification(step: dict, output: dict, ctx: dict) -> dict:
    """Run plausibility and recalculation checks for compute steps."""
    tool = step["tool"]
    validate = step.get("validate") or {}
    step_args = step.get("args") or {}
    
    # Only run deep verification on compute steps with a validate clause
    if tool != "compute" or not validate:
        return {"criterion": "deep", "passed": True, "details": "skipped (not compute or no validate)"}
    
    df = ctx.get("df")
    if df is None:
        return {"criterion": "deep", "passed": False, "details": "no DataFrame in context"}
    
    # Run plausibility check
    plausibility = verify.check_plausibility(validate, output, step_args)
    if not plausibility["passed"]:
        return {"criterion": "deep", "passed": False, "details": f"plausibility: {plausibility['details']}"}
    
    # Run recalculation check
    recalc = verify.check_recalculation(validate, output, step_args, df)
    if not recalc["passed"]:
        return {"criterion": "deep", "passed": False, "details": f"recalculation: {recalc['details']}"}
    
    return {"criterion": "deep", "passed": True, "details": f"{plausibility['details']}; {recalc['details']}"}


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
    keys = ("count", "columns", "rows", "path", "points", "error", "answer", "question")
    return {k: output[k] for k in keys if k in output}