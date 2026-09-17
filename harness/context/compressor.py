"""Context compression utilities for agent handoffs.

Each handoff compresses the supervisor's full context into a targeted summary
for the specialized agent. This is lossy by design - the tradeoff is documented
in the architecture spec.
"""

from __future__ import annotations

from typing import Any

from .. import config


# Keywords that trigger mandatory chart generation
CHART_KEYWORDS = [
    "graphique", "chart", "plot", "top", "classement", "évolution",
    "comparaison", "compare", "ranking", "distribution", "histogramme"
]


def _contains_chart_keyword(question: str) -> bool:
    """Check if question contains chart-triggering keywords (whole word match)."""
    import re
    question_lower = question.lower()
    for kw in CHART_KEYWORDS:
        # Use word boundaries to avoid false positives like "plusieurs"
        if re.search(rf"\b{re.escape(kw)}\b", question_lower):
            return True
    return False


def _extract_key_findings(supervisor_history: list[dict]) -> list[str]:
    """Extract key findings from supervisor's turn history."""
    findings = []
    for h in supervisor_history:
        action = h["action"]
        obs = h["observation"]
        if action["tool"] in ("call_data_agent", "call_viz_agent", "call_redaction_agent"):
            # These are supervisor's own agent calls - get the result summary
            result_summary = obs.get("result_summary", "")
            if result_summary:
                findings.append(result_summary)
        elif action["tool"] == "compute":
            rows = obs.get("rows", [])
            if rows:
                intent = action.get("args", {}).get("groupby", ["result"])[0]
                findings.append(f"{intent}: {rows[:3]}")
    return findings


def compress_for_data_agent(
    question: str,
    supervisor_history: list[dict],
    max_chars: int = 500,
) -> tuple[str, dict]:
    """Create targeted summary for Data Agent.

    Returns (compressed_context, metadata_dict).
    """
    # Build the compression
    parts = [
        f"Question: {question}",
        "",
        "Instructions for Data Agent:",
        "- Load the TMDB dataset (load_data)",
        "- Perform the necessary computation (compute)",
        "- Apply filters: budget > 1000 AND revenue > 0 for any ROI/profitability analysis",
        "- For 'since 1997' or '20 last years': use year_range [1997, null]",
        "- Genres are multi-label: groupby ['genres'] explodes to 'genre'",
        "- Return key findings for the supervisor",
    ]

    # Add hints from question analysis
    q_lower = question.lower()
    if "roi" in q_lower or "rentab" in q_lower or "profit" in q_lower:
        parts.append("- Compute ROI = (revenue - budget) / budget * 100")
    if "réalisateur" in q_lower or "director" in q_lower:
        parts.append("- Group by directors (explodes to 'director')")
    if "acteur" in q_lower or "actor" in q_lower or "cast" in q_lower:
        parts.append("- Group by cast_names (explodes to 'actor')")
    if "genre" in q_lower:
        parts.append("- Group by genres (explodes to 'genre')")

    # Add any previous findings from supervisor history
    findings = _extract_key_findings(supervisor_history)
    if findings:
        parts.append("")
        parts.append("Previous findings:")
        parts.extend(f"- {f}" for f in findings[:3])

    compressed = "\n".join(parts)
    if len(compressed) > max_chars:
        compressed = compressed[:max_chars - 3] + "..."

    metadata = {
        "original_chars": len("\n".join(parts)),
        "compressed_chars": len(compressed),
        "compression_ratio": len(compressed) / max(len("\n".join(parts)), 1),
        "target_agent": "data_agent",
    }
    return compressed, metadata


def compress_for_viz_agent(
    question: str,
    supervisor_history: list[dict],
    max_chars: int = 400,
) -> tuple[str, dict]:
    """Create targeted summary for Viz Agent."""
    # Find the most recent data agent result
    data_result = None
    for h in reversed(supervisor_history):
        if h["action"]["tool"] == "call_data_agent":
            data_result = h["observation"].get("result_summary", "")
            break

    parts = [
        f"Question: {question}",
        "",
        "Instructions for Viz Agent:",
        "- Create a chart from the data agent's results",
        "- Use chart tool with appropriate x, y, kind (bar/line)",
        "- Save to output/charts/",
    ]

    if data_result:
        parts.append("")
        parts.append("Data to visualize:")
        parts.append(data_result[:300])

    # Suggest chart type from question
    q_lower = question.lower()
    if "évolution" in q_lower or "tendance" in q_lower or "over time" in q_lower:
        parts.append("- Suggested: line chart (x=year, y=metric)")
    else:
        parts.append("- Suggested: bar chart (x=category, y=value)")

    compressed = "\n".join(parts)
    if len(compressed) > max_chars:
        compressed = compressed[:max_chars - 3] + "..."

    metadata = {
        "original_chars": len("\n".join(parts)),
        "compressed_chars": len(compressed),
        "compression_ratio": len(compressed) / max(len("\n".join(parts)), 1),
        "target_agent": "viz_agent",
    }
    return compressed, metadata


def compress_for_redaction_agent(
    question: str,
    supervisor_history: list[dict],
    max_chars: int = 600,
) -> tuple[str, dict]:
    """Create targeted summary for Redaction Agent."""
    findings = _extract_key_findings(supervisor_history)

    parts = [
        f"Question: {question}",
        "",
        "Instructions for Redaction Agent:",
        "- Produce a natural language French answer (2-3 sentences max)",
        "- Include key numbers, names, percentages",
        "- Be concise and direct",
        "",
        "Key findings to synthesize:",
    ]
    parts.extend(f"- {f}" for f in findings[:5])

    compressed = "\n".join(parts)
    if len(compressed) > max_chars:
        compressed = compressed[:max_chars - 3] + "..."

    metadata = {
        "original_chars": len("\n".join(parts)),
        "compressed_chars": len(compressed),
        "compression_ratio": len(compressed) / max(len("\n".join(parts)), 1),
        "target_agent": "redaction_agent",
    }
    return compressed, metadata


def should_generate_chart(question: str) -> bool:
    """Deterministic guardrail: should we generate a chart for this question?"""
    return _contains_chart_keyword(question)