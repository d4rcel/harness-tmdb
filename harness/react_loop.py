"""ReAct Loop: the orchestration loop for the ReAct agent.

Each turn: build context -> model chooses action -> execute -> verify -> record.
If verification fails, the failure is recorded and the model sees it next turn
and can adapt (change filters, fix args, etc.). No automatic retry of same action.

Deep verification (plausibility + recalculation) runs after EACH action.
"""

from __future__ import annotations

import time

from . import config, llm, memory, tools, verify
from .react_prompt import REACT_SYSTEM_PROMPT


def run_react_loop(question: str, max_turns: int | None = None) -> str:
    """Run the ReAct loop for a question.

    Returns the path to the run log JSON.
    """
    if max_turns is None:
        max_turns = config.MAX_REACT_TURNS

    config.ensure_dirs()
    record = memory.new_react_record(question, config.GEMINI_MODEL)
    ctx: dict = {"df": None, "last_result": None, "turn_history": []}

    print(f"[react] max_turns={max_turns}")

    for turn in range(1, max_turns + 1):
        print(f"[react] turn {turn}/{max_turns}")

        # 1. Build context for the model
        context = _build_react_context(question, ctx["turn_history"], turn, max_turns)

        # 2. Call model for action
        action_response = llm.generate_react_action(REACT_SYSTEM_PROMPT, context)
        action_name = action_response.get("name")
        action_args = action_response.get("args", {})

        print(f"[react] action: {action_name} {action_args}")

        # 3. If finish -> synthesize and complete
        if action_name == "finish":
            # Extract the final answer from the last compute result or synthesize
            final_answer = _extract_final_answer(ctx["turn_history"])
            print(f"[react] finish -> synthesizing answer")

            # Call synthesize tool with all turn results
            synth_result = tools.synthesize({
                "question": question,
                "final_answer": final_answer,
                "step_results": ctx["turn_history"],
            }, ctx)

            record["final"] = {
                "turns_ok": sum(1 for t in record["turns"] if t["status"] == "success"),
                "turns_total": len(record["turns"]),
                "answer": synth_result.get("answer", final_answer),
            }
            record["status"] = "completed"
            log_path = memory.save_run(record)
            print(f"[react] completed in {len(record['turns'])} turns")
            return log_path

        # 4. Execute the action
        started = time.time()
        step_result = _execute_action(action_name, action_args, ctx)
        duration_ms = int((time.time() - started) * 1000)

        # 5. Determine validation clause based on action
        validate_clause = _get_validate_clause(action_name, action_args)

        # 6. Run verifications
        syntactic = verify.check_output(validate_clause, step_result)
        deep = _run_deep_verification_react(action_name, action_args, step_result, ctx)

        turn_status = "success" if (syntactic["passed"] and deep["passed"]) else "failed"

        # 7. Record the turn
        turn_record = {
            "turn": turn,
            "action": {"tool": action_name, "args": action_args},
            "observation": step_result,
            "verification": {
                "syntactic": syntactic,
                "deep": deep,
            },
            "status": turn_status,
            "duration_ms": duration_ms,
        }
        ctx["turn_history"].append(turn_record)
        memory.append_turn(record, turn_record)

        print(f"[react] turn {turn}: {action_name} -> {turn_status}")
        print(f"  syntactic: {syntactic['criterion']}={syntactic['passed']} ({syntactic['details']})")
        if deep.get("criterion") != "deep":
            print(f"  deep: {deep.get('criterion')}={deep.get('passed')} ({deep.get('details')})")

        # 8. If failed, continue (model will see failure in next turn context)
        # No automatic retry!

    # Max turns reached
    record["status"] = "aborted"
    record["final"] = {
        "error": "max turns reached",
        "turns_total": max_turns,
    }
    return memory.save_run(record)


def _build_react_context(
    question: str,
    history: list[dict],
    turn: int,
    max_turns: int,
) -> str:
    """Build the context string for the model."""
    lines = [
        f"Question: {question}",
        f"Turn: {turn} of {max_turns}",
        "",
        "History (previous turns):",
    ]

    if not history:
        lines.append("  (no previous turns)")
    else:
        for h in history:
            lines.append(f"  Turn {h['turn']}:")
            action = h["action"]
            lines.append(f"    Action: {action['tool']} {action.get('args', {})}")
            obs = h["observation"]
            if isinstance(obs, dict):
                if "rows" in obs:
                    lines.append(f"    Observation: {obs.get('count', 0)} rows, cols={obs.get('columns', [])}")
                    rows = obs.get("rows")
                    if isinstance(rows, list) and rows:
                        lines.append(f"    Sample: {rows[:3]}")
                elif "cached" in obs:
                    lines.append(f"    Observation: loaded {obs.get('rows', '?')} rows (cached={obs.get('cached')})")
                elif "path" in obs:
                    lines.append(f"    Observation: chart saved to {obs.get('path')}")
                elif "error" in obs:
                    lines.append(f"    Observation: ERROR - {obs.get('error')}")
                else:
                    lines.append(f"    Observation: {obs}")
            else:
                lines.append(f"    Observation: {obs}")

            v_syn = h["verification"]["syntactic"]
            v_deep = h["verification"]["deep"]
            lines.append(f"    Syntactic verification: {v_syn['criterion']}={'PASS' if v_syn['passed'] else 'FAIL'} ({v_syn['details']})")
            if v_deep.get("criterion") != "deep":
                lines.append(f"    Deep verification: {v_deep['criterion']}={'PASS' if v_deep['passed'] else 'FAIL'} ({v_deep['details']})")
            lines.append(f"    Status: {h['status']}")

    lines.append("")
    lines.append("Choose ONE action: load_data, compute, chart, write_file, or finish.")
    lines.append("If previous verification failed, ADAPT your action (change filters, fix args, etc.).")

    return "\n".join(lines)


def _execute_action(action: str, args: dict, ctx: dict) -> dict:
    """Execute a single action and return the result."""
    tool_fn = tools.TOOLS[action]
    try:
        if action == "synthesize":
            # Synthesize is handled separately in the loop
            return {"error": "synthesize called directly in loop", "count": 0, "rows": []}
        output = tool_fn(args, ctx) or {}
        if action == "load_data":
            ctx["df"] = tools._df_cache
        elif action == "compute":
            ctx["last_result"] = output
        return output
    except Exception as exc:
        return {"error": str(exc), "count": 0, "rows": []}


def _get_validate_clause(action: str, args: dict) -> dict:
    """Determine the validation clause for an action."""
    if action == "load_data":
        return {"kind": "nonempty"}
    if action == "compute":
        # Use plausibility for compute, with recalculation if groupby present
        clause = {"kind": "plausibility"}
        if args.get("top_k"):
            clause["top_k"] = args["top_k"]
        if args.get("sort_by"):
            clause["sort_by"] = args["sort_by"]
        return clause
    if action == "chart":
        return {"kind": "nonempty"}
    if action == "write_file":
        return {"kind": "nonempty"}
    return {"kind": "nonempty"}


def _run_deep_verification_react(action: str, args: dict, output: dict, ctx: dict) -> dict:
    """Run deep verification for a ReAct action."""
    if action != "compute":
        return {"criterion": "deep", "passed": True, "details": "skipped (not compute)"}

    df = ctx.get("df")
    if df is None:
        return {"criterion": "deep", "passed": False, "details": "no DataFrame in context"}

    validate = _get_validate_clause(action, args)

    # Plausibility
    plausibility = verify.check_plausibility(validate, output, args)
    if not plausibility["passed"]:
        return {"criterion": "deep", "passed": False, "details": f"plausibility: {plausibility['details']}"}

    # Recalculation (only if groupby present)
    if args.get("groupby"):
        recalc = verify.check_recalculation(
            {"kind": "recalculation", "sample": config.RECALCULATION_SAMPLE},
            output, args, df
        )
        if not recalc["passed"]:
            return {"criterion": "deep", "passed": False, "details": f"recalculation: {recalc['details']}"}
        return {"criterion": "deep", "passed": True, "details": f"{plausibility['details']}; {recalc['details']}"}

    return {"criterion": "deep", "passed": True, "details": plausibility["details"]}


def _extract_final_answer(history: list[dict]) -> str:
    """Extract a summary of findings for the synthesize step."""
    parts = []
    for h in history:
        if h["action"]["tool"] == "compute":
            rows = h["observation"].get("rows", [])
            if rows:
                intent = h["action"].get("args", {}).get("groupby", ["result"])[0]
                parts.append(f"{intent}: {rows[:5]}")
        elif h["action"]["tool"] == "chart":
            parts.append(f"chart: {h['observation'].get('path')}")
    return "\n".join(parts) if parts else "Aucun résultat calculé."