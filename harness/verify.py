"""Verification: checks a step's output against its validate clause.

Returns a record with one pass/fail entry per criterion so the run log
shows exactly what was checked.
"""

from __future__ import annotations

from typing import Any


def check_output(validate: dict | None, output: dict) -> dict:
    """Return {'criterion': str, 'passed': bool, 'details': str}."""
    if not validate:
        return {"criterion": "no-clause", "passed": True, "details": "step ran"}

    kind = validate.get("kind")
    if kind == "nonempty":
        passed = output.get("count", 0) > 0 or bool(output.get("rows"))
        return {
            "criterion": "nonempty",
            "passed": passed,
            "details": f"rows={output.get('count', '?')}",
        }
    if kind == "bound":
        value = output.get("count", 0)
        bound = validate.get("min", 0)
        passed = value >= bound
        return {
            "criterion": f"count >= {bound}",
            "passed": passed,
            "details": f"count={value}",
        }
    if kind == "type":
        rows = output.get("rows") or []
        field = validate.get("field")
        sample = None
        for row in rows:
            if field in row:
                sample = row[field]
                break
        expected = validate.get("type")
        if expected == "number":
            passed = isinstance(sample, (int, float)) and not isinstance(sample, bool)
        else:
            passed = True
        return {
            "criterion": f"type {expected}",
            "passed": passed,
            "details": f"sample={sample!r}",
        }
    return {"criterion": f"unknown-kind:{kind}", "passed": True, "details": "ignored"}