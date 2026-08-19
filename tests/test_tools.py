import unittest
from pathlib import Path

import pandas as pd

from harness import tools


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


class PlannerEnforcementTest(unittest.TestCase):
    def test_chart_step_added_for_visual_question(self):
        from harness.planner import _ensure_chart
        plan = {
            "steps": [
                {"step_id": 1, "tool": "load_data", "args": {},
                 "validate": {"kind": "nonempty"}},
                {"step_id": 2, "tool": "compute",
                 "args": {"groupby": ["genre"], "agg": {"roi": "mean"}, "top_k": 5},
                 "validate": {"kind": "nonempty"}},
            ]
        }
        out = _ensure_chart("Un graphique s'il vous plaît", plan)
        self.assertEqual(out["steps"][-1]["tool"], "chart")
        self.assertEqual(out["steps"][2]["args"]["x"], "genre")
        self.assertEqual(out["steps"][2]["args"]["y"], "roi")

    def test_no_chart_added_without_visual_keyword(self):
        from harness.planner import _ensure_chart
        plan = {"steps": [{"step_id": 1, "tool": "load_data", "args": {}}]}
        out = _ensure_chart("juste les chiffres", plan)
        self.assertEqual(len(out["steps"]), 1)

    def test_no_duplicate_chart_when_present(self):
        from harness.planner import _ensure_chart
        plan = {"steps": [
            {"step_id": 1, "tool": "load_data", "args": {}},
            {"step_id": 2, "tool": "compute", "args": {"agg": {"roi": "mean"}}},
            {"step_id": 3, "tool": "chart", "args": {"kind": "bar"}},
        ]}
        out = _ensure_chart("avec un graphique", plan)
        self.assertEqual(len(out["steps"]), 3)

    def test_chart_added_for_top_n_question(self):
        from harness.planner import _ensure_chart
        plan = {"steps": [
            {"step_id": 1, "tool": "load_data", "args": {}},
            {"step_id": 2, "tool": "compute",
             "args": {"groupby": ["genre"], "agg": {"roi": "mean"}, "top_k": 5}},
        ]}
        out = _ensure_chart("Quels sont les 5 genres les plus rentables depuis 2007 ?", plan)
        self.assertEqual(out["steps"][-1]["tool"], "chart")
        self.assertEqual(out["steps"][-1]["args"]["x"], "genre")
        self.assertEqual(out["steps"][-1]["args"]["y"], "roi")

    def test_chart_added_for_superlative_question(self):
        from harness.planner import _ensure_chart
        plan = {"steps": [
            {"step_id": 1, "tool": "load_data", "args": {}},
            {"step_id": 2, "tool": "compute",
             "args": {"groupby": ["directors"], "agg": {"title": "count"}, "sort_by": "title"}},
        ]}
        out = _ensure_chart("Quel réalisateur a dirigé le plus de films ?", plan)
        self.assertEqual(out["steps"][-1]["tool"], "chart")
        self.assertEqual(out["steps"][-1]["args"]["x"], "director")

    def test_chart_added_for_explicit_top(self):
        from harness.planner import _ensure_chart
        plan = {"steps": [
            {"step_id": 1, "tool": "load_data", "args": {}},
            {"step_id": 2, "tool": "compute",
             "args": {"groupby": ["cast_names"], "agg": {"movie_id": "count"}, "top_k": 10}},
        ]}
        out = _ensure_chart("Top 10 acteurs les plus présents", plan)
        chart = next(s for s in out["steps"] if s["tool"] == "chart")
        self.assertEqual(chart["args"]["x"], "actor")

    def test_no_chart_for_plain_booleane_question(self):
        from harness.planner import _ensure_chart
        plan = {"steps": [
            {"step_id": 1, "tool": "load_data", "args": {}},
            {"step_id": 2, "tool": "compute", "args": {"agg": {"vote_average": "mean"}}},
        ]}
        out = _ensure_chart("Quelle est la note moyenne des films ?", plan)
        self.assertEqual(len(out["steps"]), 2)


if __name__ == "__main__":
    unittest.main()