"""Tool registry: the deterministic operations the executor may call.

Every tool returns a JSON-serializable result so it can be stored in the
run log. `load_data` builds the joined, enriched DataFrame once (cache);
later steps share it via the executor context.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from . import config

_FILTER_RE = re.compile(r"^\s*(\w+)\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$")
_AGG_FUNCS = {"mean", "sum", "count", "median", "max", "min"}

# Per-film list columns are exploded into one row per name and renamed to the
# singular at groupby time. Shared here so the planner can derive the exact
# column names a compute step will output (e.g. for a chart's x axis).
PLURAL_TO_SINGULAR = {
    "genres": "genre",
    "cast_names": "actor",
    "directors": "director",
    "writers": "writer",
    "producers": "producer",
}

_df_cache: pd.DataFrame | None = None


def _parse_json_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return json.loads(value)


def _load_raw_movies() -> pd.DataFrame:
    movies = pd.read_csv(config.DATA["movies"], dtype={"id": "int64"})
    credits = pd.read_csv(config.DATA["credits"], dtype={"movie_id": "int64"})
    credits = credits.rename(columns={"movie_id": "id"})
    df = movies.merge(credits, on="id", how="inner")
    df = df.rename(columns={"id": "movie_id", "title_x": "title"})
    return df


def load_data() -> dict:
    """Read both CSVs once, join 1:1, enrich with derived columns."""
    global _df_cache
    if _df_cache is not None:
        return {"cached": True, "rows": len(_df_cache)}

    df = _load_raw_movies()

    cast = df["cast"].apply(_parse_json_list)
    crew = df["crew"].apply(_parse_json_list)
    genres = df["genres"].apply(
        lambda items: [g["name"] for g in _parse_json_list(items)]
    )

    def crew_by_job(job: str) -> list:
        return lambda items: [
            m["name"] for m in _parse_json_list(items) if m.get("job") == job
        ]

    df["genres"] = genres
    df["cast_names"] = cast.apply(lambda items: [m["name"] for m in items])
    df["actor_count"] = df["cast_names"].str.len()
    df["directors"] = df["crew"].apply(crew_by_job("Director"))
    df["writers"] = df["crew"].apply(crew_by_job("Writer"))
    df["producers"] = df["crew"].apply(crew_by_job("Producer"))

    df["year"] = pd.to_numeric(df["release_date"].str[:4], errors="coerce")
    df["budget"] = pd.to_numeric(df["budget"], errors="coerce").fillna(0)
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce").fillna(0)
    valid_budget = df["budget"] > 0
    df["profit"] = df["revenue"] - df["budget"]
    df["roi"] = pd.NA
    df.loc[valid_budget, "roi"] = (
        (df.loc[valid_budget, "revenue"] - df.loc[valid_budget, "budget"])
        / df.loc[valid_budget, "budget"] * 100.0
    )

    keep = [
        "movie_id", "title", "year", "budget", "revenue", "profit", "roi",
        "popularity", "vote_average", "vote_count", "runtime",
        "original_language", "genres", "cast_names", "actor_count",
        "directors", "writers", "producers",
    ]
    df = df[keep]
    df["title"] = df["title"].astype(str)
    _df_cache = df
    return {"cached": False, "rows": len(df)}


def _apply_filters(df: pd.DataFrame, filters: list) -> pd.DataFrame:
    if not filters:
        return df
    for clause in filters:
        match = _FILTER_RE.match(str(clause))
        if not match:
            raise ValueError(f"Unparseable filter: {clause!r}")
        col, op, value = match.groups()
        if col not in df.columns:
            raise ValueError(f"Filter column '{col}' does not exist")
        try:
            literal = float(value)
        except ValueError:
            literal = value.strip("'\"")
        mask = {
            ">": lambda s, v: s > v,
            ">=": lambda s, v: s >= v,
            "<": lambda s, v: s < v,
            "<=": lambda s, v: s <= v,
            "==": lambda s, v: s == v,
            "!=": lambda s, v: s != v,
        }[op](df[col], literal)
        df = df[mask.fillna(False)]
    return df


def _normalize_groupby(df: pd.DataFrame, groupby: list) -> tuple[pd.DataFrame, list]:
    """Explode per-film list columns (genres, directors, ...) into one row per
    name, renamed to the singular, so groupby stays hashable.

    e.g. groupby ["genres"] first explodes genres and renames to "genre".
    Returns the (possibly exploded) frame and the effective key list.
    """
    plural_to_singular = PLURAL_TO_SINGULAR
    singular_to_plural = {v: k for k, v in plural_to_singular.items()}
    renamed = []
    for col in groupby:
        column = singular_to_plural.get(col, col)
        singular = plural_to_singular.get(column, col)
        if column in df.columns and column in plural_to_singular:
            df = df.explode(column).rename(columns={column: singular})
        renamed.append(singular)
    return df, renamed


def compute(args: dict, df: pd.DataFrame) -> dict:
    """Deterministic pandas pipeline: filter -> groupby/agg -> sort -> top_k."""
    frame = _apply_filters(df, args.get("filters") or [])

    year_range = args.get("year_range")
    if year_range:
        lo, hi = year_range
        if lo is not None:
            frame = frame[frame["year"] >= lo]
        if hi is not None:
            frame = frame[frame["year"] <= hi]

    groupby = args.get("groupby") or []
    agg = args.get("agg") or {}
    if groupby:
        frame, groupby = _normalize_groupby(frame, groupby)
    if groupby:
        frame = frame.groupby(groupby, observed=False).agg(agg).reset_index()
    elif agg:
        frame = frame.agg(agg).to_frame().T

    sort_by = args.get("sort_by")
    if sort_by and sort_by not in frame.columns:
        agg_cols = [c for c in frame.columns if c in (agg or {})]
        fallback = agg_cols[-1] if agg_cols else frame.columns[-1]
        print(f"[compute] sort_by '{sort_by}' not a column, using '{fallback}'")
        sort_by = fallback
    if sort_by and sort_by in frame.columns:
        frame = frame.sort_values(sort_by, ascending=False)

    top_k = args.get("top_k")
    if top_k:
        frame = frame.head(int(top_k))

    return _frame_to_json(frame)


def _frame_to_json(frame: pd.DataFrame) -> dict:
    rows = frame.where(frame.notna(), None).to_dict(orient="records")
    for row in rows:
        for key, value in list(row.items()):
            if isinstance(value, (pd.Timestamp,)):
                row[key] = value.isoformat()
    return {
        "columns": [str(c) for c in frame.columns],
        "count": int(len(frame)),
        "rows": rows,
    }


def chart(data: dict, args: dict, df: pd.DataFrame) -> dict:
    """Render a bar/line chart from a compute result and write a PNG."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = data.get("rows") or []
    if not rows:
        raise ValueError("chart: no rows to plot")

    cols = data["columns"]
    x_col = args.get("x")
    if x_col not in cols:
        x_col = cols[0]
        print(f"[chart] x '{args.get('x')}' not a column, using '{x_col}'")
    y_col = args.get("y")
    if y_col not in cols:
        y_col = next((c for c in reversed(cols) if c != x_col), cols[0])
        print(f"[chart] y '{args.get('y')}' not a column, using '{y_col}'")
    kind = args.get("kind", "bar")

    pairs = []
    for row in rows:
        y = row.get(y_col)
        if y is None:
            continue
        pairs.append((str(row.get(x_col)), float(y)))
    if not pairs:
        raise ValueError("chart: no plottable values after filtering")
    x = [p[0] for p in pairs]
    y = [p[1] for p in pairs]

    fig, ax = plt.subplots(figsize=(10, 6))
    if kind == "line":
        ax.plot(x, y, marker="o")
    else:
        xticks = range(len(x))
        ax.bar(xticks, y)
        ax.set_xticks(xticks, labels=[str(v) for v in x], rotation=60)
    ax.set_xlabel(str(x_col))
    ax.set_ylabel(str(y_col))
    ax.set_title(args.get("title") or f"{x_col} vs {y_col}")
    fig.tight_layout()

    name = args.get("path") or "chart.png"
    path = (config.CHARTS_DIR / name).resolve()
    if not str(path).startswith(str(config.CHARTS_DIR.resolve())):
        raise ValueError("chart path must stay under output/charts")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return {
        "path": str(path),
        "count": len(x),
        "rows": rows,
        "points": len(x),
    }


def write_file(args: dict, df: pd.DataFrame) -> dict:
    content = args.get("content", "")
    name = args.get("path", "result.txt")
    path = (config.RESULTS_DIR / name).resolve()
    if not str(path).startswith(str(config.RESULTS_DIR.resolve())):
        raise ValueError("write_file path must stay under output/results")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "bytes": len(content)}


TOOLS = {
    "load_data": lambda args, ctx: load_data(),
    "compute": lambda args, ctx: compute(args, ctx["df"]),
    "chart": lambda args, ctx: chart(ctx["last_result"], args, ctx["df"]),
    "write_file": lambda args, ctx: write_file(args, ctx["df"]),
}