"""Planner: turns a natural-language question into a validated JSON plan.

Uses Gemini structured output (response_mime_type=application/json) and
re-validates the JSON against the plan schema before handing it to the
executor. A plan that fails validation is retried once with the parse error
echoed back, then rejected loudly.

The model is not the only authority: `_ensure_chart` deterministically appends
a chart step whenever the question asks for a visualization, so a visual is
produced even if the model omitted it.
"""

from __future__ import annotations

import json

from . import llm
from .planner_prompt import SYSTEM_PROMPT
from .tools import PLURAL_TO_SINGULAR

VALID_TOOLS = {"load_data", "compute", "chart", "write_file"}
VALIDATE_KINDS = {"nonempty", "bound", "type"}
VIZ_KEYWORDS = ("graphique", "chart", "plot", "graph", "visual", "diagramme", "graphe")
RANKING_KEYWORDS = (
    "top", "classement", "ranking", "comparaison", "comparer", "comparison",
    "évolution", "evolution", "tendance", "trend", "plus",
)


def build_plan(question: str) -> dict:
    """Generate and schema-check a plan for `question`."""
    raw = llm.generate_structured_json(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=f"Question: {question}\nReturn a strictly valid plan JSON.",
    )
    plan = _parse_and_validate(raw)
    if plan is not None:
        return _ensure_chart(question, plan)

    raw = llm.generate_structured_json(
        system_prompt=SYSTEM_PROMPT + (
            "\nYour previous answer failed validation with: {}"
        ).format(_last_error),
        user_prompt=f"Question: {question}\nReturn a strictly valid plan JSON.",
    )
    plan = _parse_and_validate(raw)
    if plan is not None:
        return _ensure_chart(question, plan)
    raise RuntimeError(f"Planner: plan rejected twice. Last error: {_last_error}")


_last_error: str = ""


def _parse_and_validate(raw: str) -> dict | None:
    global _last_error
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        _last_error = f"invalid JSON: {exc}"
        return None

    if not isinstance(plan, dict) or "steps" not in plan:
        _last_error = "missing top-level 'steps'"
        return None
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        _last_error = "'steps' must be a non-empty list"
        return None
    if steps[0].get("tool") != "load_data":
        _last_error = "step 1 must use the load_data tool"
        return None
    for step in steps:
        err = _check_step(step)
        if err:
            _last_error = err
            return None
    plan["steps"] = steps
    return plan


def _ensure_chart(question: str, plan: dict) -> dict:
    """Append a chart step if the question asks for a visual and none exists.

    Guarantees the visual is generated (not silently dropped by the planner).
    The chart plots the last compute step's rows, so it is appended after it.
    """
    lowered = question.lower()
    asks_visual = (
        any(kw in lowered for kw in VIZ_KEYWORDS)
        or any(kw in lowered for kw in RANKING_KEYWORDS)
    )
    if not asks_visual:
        return plan
    steps = plan["steps"]
    if any(step.get("tool") == "chart" for step in steps):
        return plan

    compute_index = max(
        (i for i, step in enumerate(steps) if step.get("tool") == "compute"),
        default=-1,
    )
    if compute_index == -1:
        return plan  # nothing plottable; leave the plan untouched

    compute_step = steps[compute_index]
    groupby = compute_step.get("args", {}).get("groupby") or []
    agg = compute_step.get("args", {}).get("agg") or {}
    x = PLURAL_TO_SINGULAR.get(groupby[0], groupby[0]) if groupby else "genre"
    y = next(iter(agg), None) if agg else "roi"
    chart_step = {
        "step_id": len(steps) + 1,
        "intent": "plot the previous compute results",
        "tool": "chart",
        "args": {"kind": "bar", "x": x, "y": y, "path": "chart.png"},
        "validate": {"kind": "nonempty"},
    }
    steps.append(chart_step)
    reason = "visual keyword" if any(
        kw in lowered for kw in VIZ_KEYWORDS
    ) else "ranking/comparison question"
    print(f"[planner] {reason}: chart step added deterministically")
    return plan


def _check_step(step: dict) -> str | None:
    if not isinstance(step, dict):
        return "step is not an object"
    if not isinstance(step.get("step_id"), int):
        return f"step '{step.get('intent', '?')}' missing numeric step_id"
    tool = step.get("tool")
    if tool not in VALID_TOOLS:
        return f"step {step['step_id']}: unknown tool '{tool}'"
    if tool in {"chart", "write_file"} and not isinstance(step.get("args"), dict):
        return f"step {step['step_id']}: chart/write_file need args object"
    if "validate" in step:
        v = step.get("validate")
        if not isinstance(v, dict) or v.get("kind") not in VALIDATE_KINDS:
            return f"step {step['step_id']}: bad validate clause {v!r}"
    return None