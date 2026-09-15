"""Verification: checks a step's output against its validate clause.

Returns a record with one pass/fail entry per criterion so the run log
shows exactly what was checked.
"""

from __future__ import annotations

from typing import Any

from . import tools
from . import config


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
    if kind == "plausibility":
        # Delegate to deep check (will be run separately)
        return {"criterion": "plausibility", "passed": True, "details": "checked in deep verification"}
    if kind == "recalculation":
        # Delegate to deep check (will be run separately)
        return {"criterion": "recalculation", "passed": True, "details": "checked in deep verification"}
    return {"criterion": f"unknown-kind:{kind}", "passed": True, "details": "ignored"}


def check_plausibility(validate: dict | None, output: dict, step_args: dict | None = None) -> dict:
    """Deep plausibility checks for compute results.

    Returns {'criterion': str, 'passed': bool, 'details': str}.
    """
    if not validate:
        return {"criterion": "plausibility", "passed": True, "details": "no clause"}

    rows = output.get("rows") or []
    if not rows:
        return {"criterion": "plausibility", "passed": False, "details": "no rows to check"}

    # ROI bound check
    roi_max = validate.get("roi_max", config.ROI_MAX_PLAUSIBLE)
    if roi_max is not None:
        for i, row in enumerate(rows):
            for key, val in row.items():
                if "roi" in key.lower() and isinstance(val, (int, float)):
                    if val > roi_max:
                        return {
                            "criterion": f"roi <= {roi_max}",
                            "passed": False,
                            "details": f"row {i}: {key}={val} exceeds max {roi_max}",
                        }
                    if val < -100:
                        return {
                            "criterion": "roi >= -100",
                            "passed": False,
                            "details": f"row {i}: {key}={val} below -100% (total loss)",
                        }

    # top_k check: if step asked for top_k, verify exact count
    if step_args and "top_k" in step_args:
        expected = int(step_args["top_k"])
        actual = output.get("count", 0)
        if actual != expected:
            return {
                "criterion": f"top_k == {expected}",
                "passed": False,
                "details": f"count={actual}, expected {expected}",
            }

    # sort_desc check: verify descending order on sort_by column
    if step_args and step_args.get("sort_by"):
        sort_col = step_args["sort_by"]
        if sort_col in output.get("columns", []):
            values = [r.get(sort_col) for r in rows if r.get(sort_col) is not None]
            if len(values) >= 2:
                for i in range(len(values) - 1):
                    if values[i] < values[i + 1]:
                        return {
                            "criterion": f"sort_desc on {sort_col}",
                            "passed": False,
                            "details": f"not descending at index {i}: {values[i]} < {values[i+1]}",
                        }

    # no_all_zero check: reject if all agg values are 0/None
    agg_keys = [k for k in output.get("columns", []) if k not in (step_args or {}).get("groupby", [])]
    if agg_keys:
        all_zero = True
        for row in rows:
            for k in agg_keys:
                v = row.get(k)
                if v is not None and v != 0:
                    all_zero = False
                    break
            if not all_zero:
                break
        if all_zero and rows:
            return {
                "criterion": "not all_zero",
                "passed": False,
                "details": "all aggregation values are zero or None",
            }

    return {"criterion": "plausibility", "passed": True, "details": "all checks passed"}


def check_recalculation(validate: dict | None, output: dict, step_args: dict | None, df) -> dict:
    """Independent recalculation of a sample of compute results.

    Re-runs the filter->groupby->agg pipeline on the raw DataFrame for the
    first `sample` rows of the output and compares values within tolerance.
    """
    if not validate:
        return {"criterion": "recalculation", "passed": True, "details": "no clause"}

    rows = output.get("rows") or []
    if not rows:
        return {"criterion": "recalculation", "passed": False, "details": "no rows to recalc"}

    sample_size = validate.get("sample", config.RECALCULATION_SAMPLE)
    tolerance = validate.get("tolerance", config.RECALCULATION_TOLERANCE)
    if sample_size <= 0:
        return {"criterion": "recalculation", "passed": True, "details": "sample=0 skipped"}

    # Extract the compute parameters
    filters = (step_args or {}).get("filters") or []
    groupby = (step_args or {}).get("groupby") or []
    agg = (step_args or {}).get("agg") or {}
    year_range = (step_args or {}).get("year_range")

    # Re-run the same pipeline on the raw DataFrame
    try:
        recomputed = tools.compute(
            {"filters": filters, "year_range": year_range, "groupby": groupby, "agg": agg, "sort_by": None, "top_k": None},
            df,
        )
    except Exception as exc:
        return {"criterion": "recalculation", "passed": False, "details": f"recompute failed: {exc}"}

    reco_rows = recomputed.get("rows") or []
    if not reco_rows:
        return {"criterion": "recalculation", "passed": False, "details": "recompute returned empty"}

    # Match by groupby keys and compare agg values
    # The compute step converts plural groupby keys to singular in output
    from .tools import PLURAL_TO_SINGULAR
    group_keys = [PLURAL_TO_SINGULAR.get(k, k) for k in groupby] if groupby else []
    if not group_keys and agg:
        # No groupby: single row aggregation
        group_keys = []

    checked = 0
    for out_row in rows[:sample_size]:
        # Find matching row in recomputed
        match = None
        for rec_row in reco_rows:
            if all(out_row.get(k) == rec_row.get(k) for k in group_keys):
                match = rec_row
                break
        if not match:
            return {
                "criterion": "recalculation",
                "passed": False,
                "details": f"no matching group in recomputed data for {out_row}",
            }

        # Compare aggregation values
        for key, expected in out_row.items():
            if key in group_keys:
                continue
            actual = match.get(key)
            if expected is None and actual is None:
                continue
            if expected is None or actual is None:
                return {
                    "criterion": "recalculation",
                    "passed": False,
                    "details": f"mismatch {key}: expected {expected}, got {actual}",
                }
            if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                if expected == 0:
                    if actual != 0:
                        return {
                            "criterion": "recalculation",
                            "passed": False,
                            "details": f"mismatch {key}: expected 0, got {actual}",
                        }
                else:
                    rel_diff = abs(expected - actual) / abs(expected)
                    if rel_diff > tolerance:
                        return {
                            "criterion": "recalculation",
                            "passed": False,
                            "details": f"mismatch {key}: expected {expected}, got {actual} (diff {rel_diff:.2%} > {tolerance:.0%})",
                        }

        checked += 1

    return {"criterion": "recalculation", "passed": True, "details": f"{checked} rows recalculated within {tolerance:.0%}"}