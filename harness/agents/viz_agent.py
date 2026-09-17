"""Viz Agent: handles chart generation."""

from __future__ import annotations

from .. import config, llm, tools
from .base_agent import BaseAgent


VIZ_AGENT_SYSTEM_PROMPT = """You are the Viz Agent for a TMDB film analysis system.

Your job: create charts from computed data results.

AVAILABLE TOOLS:
1. chart: {kind?: "bar"|"line", x: string, y: string, path?: string, title?: string, data?: object} —
   Render a chart from the provided data. Saves PNG under output/charts/.
   The 'data' parameter should contain the compute result with 'columns' and 'rows'.
2. finish: {} — Call when the chart has been generated.

BEHAVIOR:
- You receive a context summary from the supervisor describing the data to visualize.
- The context includes the data columns, rows, and suggested x/y axes.
- You MUST pass the compute result data in the 'data' parameter of the chart tool.
- Choose ONE action per turn.
- If verification fails, ADAPT your next action.
- Maximum 2 turns: chart -> finish.
- Do NOT load data or compute - that's the Data Agent's job.
- Do NOT generate natural language answers - that's the Redaction Agent's job.
"""


def create_viz_agent() -> BaseAgent:
    """Create and configure the Viz Agent."""
    viz_tools = {
        "chart": tools.TOOLS["chart"],
        "finish": lambda args, ctx: {"finished": True},
    }

    return BaseAgent(
        name="viz_agent",
        tools=viz_tools,
        system_prompt=VIZ_AGENT_SYSTEM_PROMPT,
        max_turns=config.SUBAGENT_MAX_TURNS.get("viz_agent", 2),
        generate_action_fn=llm.generate_viz_agent_action,
    )