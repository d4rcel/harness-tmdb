import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd

from harness import tools
from harness import config


class LoadDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tools._df_cache = None
        result = tools.load_data()
        cls.df = tools._df_cache

    def test_join_count(self):
        self.assertEqual(len(self.df), 4803)

    def test_derived_columns_present(self):
        for col in (
            "movie_id", "title", "year", "budget", "revenue",
            "profit", "roi", "genres", "directors", "cast_names",
        ):
            self.assertIn(col, self.df.columns)

    def test_cast_is_list_of_names(self):
        avatar = self.df[self.df["title"] == "Avatar"].iloc[0]
        self.assertIsInstance(avatar["cast_names"], list)
        self.assertIn("Sam Worthington", avatar["cast_names"])

    def test_directors_extracted(self):
        avatar = self.df[self.df["title"] == "Avatar"].iloc[0]
        self.assertIn("James Cameron", avatar["directors"])

    def test_genres_explode_compute(self):
        top = tools.compute(
            {"groupby": ["genre"], "agg": {"movie_id": "count"}, "sort_by": "movie_id", "top_k": 3},
            self.df,
        )
        self.assertGreater(top["count"], 0)
        self.assertIn("genre", top["columns"])

    def test_roi_only_on_budget(self):
        sub = tools.compute(
            {"filters": ["budget > 0", "revenue > 0"], "groupby": ["year"], "agg": {"roi": "mean"}, "top_k": 1},
            self.df,
        )
        self.assertGreater(sub["count"], 0)

    def test_revenue_window(self):
        recent = tools.compute(
            {"filters": ["revenue > 0"], "year_range": [1997, None], "groupby": ["year"], "agg": {"revenue": "sum"}, "sort_by": "revenue", "top_k": 5},
            self.df,
        )
        self.assertLessEqual(recent["rows"][0]["year"], 2017)


class ComputeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tools._df_cache = None
        cls.df = pd.DataFrame({
            "movie_id": [1, 2, 3],
            "title": ["a", "b", "c"],
            "budget": [100, 0, 200],
            "revenue": [150, 0, 400],
            "year": [2000, 2001, 2002],
            "genres": [["Action"], ["Drama"], ["Action", "Drama"]],
        })
        tools._df_cache = cls.df

    def test_filter_budget(self):
        out = tools.compute({"filters": ["budget > 0"]}, self.df)
        self.assertEqual(out["count"], 2)

    def test_groupby_with_agg(self):
        out = tools.compute({"groupby": ["year"], "agg": {"revenue": "sum"}}, self.df)
        self.assertEqual(out["count"], 3)

    def test_bad_filter_column(self):
        with self.assertRaises(ValueError):
            tools.compute({"filters": ["nonexistent > 0"]}, self.df)

    def test_sort_by_fallback_to_agg_column(self):
        out = tools.compute(
            {"groupby": ["year"], "agg": {"revenue": "sum"}, "sort_by": "revenue_sum"},
            self.df,
        )
        self.assertEqual(out["count"], 3)
        self.assertGreater(out["rows"][0]["revenue"], out["rows"][-1]["revenue"])

    def test_chart_y_fallback_when_alias_missing(self):
        import tempfile

        from harness import config, tools
        data = {"columns": ["year", "revenue"], "rows": [
            {"year": 2000, "revenue": 10.0},
            {"year": 2001, "revenue": None},
            {"year": 2002, "revenue": 30.0},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            old = config.CHARTS_DIR
            config.CHARTS_DIR = Path(tmp)
            try:
                out = tools.chart(data, {"kind": "bar", "path": "fb.png",
                                         "x": "year", "y": "revenue_sum"}, self.df)
            finally:
                config.CHARTS_DIR = old
        self.assertEqual(out["count"], 2)


class VerifyTest(unittest.TestCase):
    def test_nonempty_passe(self):
        from harness.verify import check_output
        self.assertTrue(check_output({"kind": "nonempty"}, {"count": 5, "rows": [{}]})["passed"])

    def test_bound_fail(self):
        from harness.verify import check_output
        self.assertFalse(check_output({"kind": "bound", "min": 10}, {"count": 3})["passed"])

    def test_chart_result_passes_nonempty(self):
        import tempfile

        from harness import config, tools, verify
        data = {"columns": ["genre", "roi"], "rows": [{"genre": "Horror", "roi": 1.5}]}
        df = pd.DataFrame({"genre": ["Horror"], "roi": [1.5]})
        with tempfile.TemporaryDirectory() as tmp:
            old_charts = config.CHARTS_DIR
            config.CHARTS_DIR = Path(tmp)
            try:
                out = tools.chart(data, {"kind": "bar", "path": "test.png"}, df)
            finally:
                config.CHARTS_DIR = old_charts
        self.assertIn("count", out)
        self.assertIn("rows", out)
        self.assertTrue(verify.check_output({"kind": "nonempty"}, out)["passed"])


class DeepVerificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tools._df_cache = None
        cls.df = pd.DataFrame({
            "movie_id": [1, 2, 3, 4, 5],
            "title": ["a", "b", "c", "d", "e"],
            "budget": [100, 0, 200, 300, 400],
            "revenue": [150, 0, 400, 900, 1200],
            "year": [2000, 2001, 2002, 2000, 2002],
            "genres": [["Action"], ["Drama"], ["Action"], ["Drama"], ["Action"]],
        })
        tools._df_cache = cls.df

    def test_plausibility_roi_max_rejects_absurd(self):
        from harness.verify import check_plausibility
        # ROI of 500000% should be rejected with default max
        out = {"count": 1, "rows": [{"genre": "Horror", "roi": 500000.0}]}
        result = check_plausibility({"kind": "plausibility", "roi_max": 5000}, out, {})
        self.assertFalse(result["passed"])
        self.assertIn("exceeds max", result["details"])

    def test_plausibility_roi_negative_rejects_below_minus_100(self):
        from harness.verify import check_plausibility
        out = {"count": 1, "rows": [{"genre": "Test", "roi": -150.0}]}
        result = check_plausibility({"kind": "plausibility"}, out, {})
        self.assertFalse(result["passed"])
        self.assertIn("below -100%", result["details"])

    def test_plausibility_top_k_exact_count(self):
        from harness.verify import check_plausibility
        out = {"count": 3, "rows": [{"genre": "A", "roi": 1}, {"genre": "B", "roi": 2}, {"genre": "C", "roi": 3}]}
        # top_k=5 but only 3 rows -> fail
        result = check_plausibility({"kind": "plausibility"}, out, {"top_k": 5})
        self.assertFalse(result["passed"])
        self.assertIn("count=3, expected 5", result["details"])
        # top_k=3 -> pass
        result = check_plausibility({"kind": "plausibility"}, out, {"top_k": 3})
        self.assertTrue(result["passed"])

    def test_plausibility_sort_desc(self):
        from harness.verify import check_plausibility
        # Correctly sorted descending
        out = {"count": 3, "rows": [{"roi": 10}, {"roi": 5}, {"roi": 1}], "columns": ["roi"]}
        result = check_plausibility({"kind": "plausibility"}, out, {"sort_by": "roi"})
        self.assertTrue(result["passed"])
        # Not sorted
        out = {"count": 3, "rows": [{"roi": 1}, {"roi": 10}, {"roi": 5}], "columns": ["roi"]}
        result = check_plausibility({"kind": "plausibility"}, out, {"sort_by": "roi"})
        self.assertFalse(result["passed"])
        self.assertIn("not descending", result["details"])

    def test_plausibility_no_all_zero(self):
        from harness.verify import check_plausibility
        out = {"count": 2, "rows": [{"genre": "A", "roi": 0}, {"genre": "B", "roi": 0}], "columns": ["genre", "roi"]}
        result = check_plausibility({"kind": "plausibility"}, out, {"groupby": ["genre"]})
        self.assertFalse(result["passed"])
        self.assertIn("all aggregation values are zero", result["details"])

    def test_recalculation_matches(self):
        from harness.verify import check_recalculation
        # Simple case: filter budget>0, groupby year, sum revenue
        # Our test data: year 2000 has movies 1,4 -> revenue 150+900=1050; year 2002 has movies 3,5 -> 400+1200=1600
        out = {"count": 2, "columns": ["year", "revenue"], "rows": [
            {"year": 2000, "revenue": 1050.0},
            {"year": 2002, "revenue": 1600.0},
        ]}
        step_args = {
            "filters": ["budget > 0"],
            "groupby": ["year"],
            "agg": {"revenue": "sum"},
        }
        result = check_recalculation({"kind": "recalculation", "sample": 2}, out, step_args, self.df)
        self.assertTrue(result["passed"], result["details"])

    def test_recalculation_detects_mismatch(self):
        from harness.verify import check_recalculation
        # Tampered output - wrong revenue value
        out = {"count": 2, "columns": ["year", "revenue"], "rows": [
            {"year": 2000, "revenue": 999999.0},  # wrong
            {"year": 2002, "revenue": 1600.0},
        ]}
        step_args = {
            "filters": ["budget > 0"],
            "groupby": ["year"],
            "agg": {"revenue": "sum"},
        }
        result = check_recalculation({"kind": "recalculation", "sample": 2, "tolerance": 0.01}, out, step_args, self.df)
        self.assertFalse(result["passed"])
        self.assertIn("mismatch", result["details"])


class SynthesizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tools._df_cache = None
        cls.df = pd.DataFrame({
            "movie_id": [1, 2, 3],
            "title": ["a", "b", "c"],
            "budget": [100, 200, 300],
            "revenue": [150, 400, 900],
            "year": [2000, 2001, 2002],
            "genres": [["Action"], ["Drama"], ["Action"]],
        })
        tools._df_cache = cls.df

    def test_synthesize_tool_exists(self):
        self.assertIn("synthesize", tools.TOOLS)

    def test_synthesize_returns_answer(self):
        ctx = {"df": self.df, "step_results": [
            {"step_id": 1, "tool": "load_data", "intent": "load data", "result": {"rows": 3}},
            {"step_id": 2, "tool": "compute", "intent": "top genres", "result": {"rows": [{"genre": "Action", "roi": 150}]}},
        ]}
        out = tools.synthesize({"question": "Quel genre est le plus rentable ?", "step_results": []}, ctx)
        self.assertIn("answer", out)
        self.assertIsInstance(out["answer"], str)
        self.assertGreater(len(out["answer"]), 0)


class ReactLoopTest(unittest.TestCase):
    """Tests for the ReAct loop."""
    
    @classmethod
    def setUpClass(cls):
        tools._df_cache = None
        cls.df = pd.DataFrame({
            "movie_id": [1, 2, 3, 4, 5],
            "title": ["a", "b", "c", "d", "e"],
            "budget": [100, 0, 200, 300, 400],
            "revenue": [150, 0, 400, 900, 1200],
            "year": [2000, 2001, 2002, 2000, 2002],
            "genres": [["Action"], ["Drama"], ["Action"], ["Drama"], ["Action"]],
        })
        tools._df_cache = cls.df

    def test_build_react_context(self):
        from harness.react_loop import _build_react_context
        history = [
            {
                "turn": 1,
                "action": {"tool": "load_data", "args": {}},
                "observation": {"cached": False, "rows": [{"id": 1}]},  # mock rows as list
                "verification": {
                    "syntactic": {"criterion": "nonempty", "passed": True, "details": "rows=4803"},
                    "deep": {"criterion": "deep", "passed": True, "details": "skipped (not compute)"}
                },
                "status": "success"
            }
        ]
        context = _build_react_context("Test question", history, 2, 5)
        self.assertIn("Test question", context)
        self.assertIn("Turn: 2 of 5", context)
        self.assertIn("load_data", context)

    def test_get_validate_clause(self):
        from harness.react_loop import _get_validate_clause
        # load_data
        self.assertEqual(_get_validate_clause("load_data", {}), {"kind": "nonempty"})
        # compute with top_k and sort_by
        clause = _get_validate_clause("compute", {"top_k": 5, "sort_by": "roi"})
        self.assertEqual(clause["kind"], "plausibility")
        self.assertEqual(clause["top_k"], 5)
        self.assertEqual(clause["sort_by"], "roi")
        # chart
        self.assertEqual(_get_validate_clause("chart", {}), {"kind": "nonempty"})

    def test_run_deep_verification_react_skips_non_compute(self):
        from harness.react_loop import _run_deep_verification_react
        result = _run_deep_verification_react("load_data", {}, {}, {"df": self.df})
        self.assertTrue(result["passed"])
        self.assertIn("skipped", result["details"])

    def test_run_deep_verification_react_compute_plausibility(self):
        from harness.react_loop import _run_deep_verification_react
        # ROI too high should fail
        out = {"count": 1, "rows": [{"genre": "Horror", "roi": 500000.0}], "columns": ["genre", "roi"]}
        result = _run_deep_verification_react("compute", {"groupby": ["genres"], "agg": {"roi": "mean"}}, out, {"df": self.df})
        self.assertFalse(result["passed"])
        self.assertIn("plausibility", result["details"])

    def test_extract_final_answer(self):
        from harness.react_loop import _extract_final_answer
        history = [
            {"action": {"tool": "compute", "args": {"groupby": ["genres"]}}, "observation": {"rows": [{"genre": "Action", "roi": 100}]}},
            {"action": {"tool": "chart", "args": {}}, "observation": {"path": "chart.png"}},
        ]
        answer = _extract_final_answer(history)
        self.assertIn("Action", answer)
        self.assertIn("chart", answer)


if __name__ == "__main__":
    unittest.main()