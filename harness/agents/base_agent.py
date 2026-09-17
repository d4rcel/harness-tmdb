"""Base Agent class with shared ReAct loop logic."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .. import config, llm, verify
from ..tools import TOOLS as BASE_TOOLS


@dataclass
class AgentResult:
    """Result returned by a specialized agent to the supervisor."""
    success: bool
    result_summary: str
    artifacts: dict
    internal_turns: list[dict]
    verification_status: str  # "passed" | "failed" | "error"


class BaseAgent:
    """Abstract base class for all agents (supervisor + specialized)."""

    def __init__(
        self,
        name: str,
        tools: dict[str, Any],
        system_prompt: str,
        max_turns: int = 5,
        verify_fn=None,
        generate_action_fn: Callable[[str, str], dict] | None = None,
    ):
        self.name = name
        self.tools = tools
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.verify_fn = verify_fn
        self.generate_action_fn = generate_action_fn or llm.generate_react_action

    def run(self, task_description: str, context_summary: str, initial_context: dict | None = None) -> AgentResult:
        """Run internal ReAct loop with compressed context."""
        ctx: dict = {"df": None, "last_result": None, "turn_history": []}
        if initial_context:
            ctx.update(initial_context)
        internal_turns = []

        # Build initial context for this agent
        context = self._build_agent_context(task_description, context_summary, 1)

        for turn in range(1, self.max_turns + 1):
            # Call model for action
            action_response = self.generate_action_fn(self.system_prompt, context)
            action_name = action_response.get("name")
            action_args = action_response.get("args", {})

            # Handle finish
            if action_name == "finish":
                final_answer = self._extract_final_answer(ctx["turn_history"])

                # Determine verification status from internal turns
                verification_status = "passed"
                for t in internal_turns:
                    if t["status"] == "failed":
                        verification_status = "failed"
                        break
                    if t["verification"]["deep"]["passed"] is False:
                        verification_status = "failed"
                        break

                return AgentResult(
                    success=verification_status == "passed",
                    result_summary=final_answer,
                    artifacts={},
                    internal_turns=internal_turns,
                    verification_status=verification_status,
                )

            # Execute action
            started = time.time()
            step_result = self._execute_action(action_name, action_args, ctx)
            duration_ms = int((time.time() - started) * 1000)

            # Determine validation clause
            validate_clause = self._get_validate_clause(action_name, action_args)

            # Run verifications
            syntactic = verify.check_output(validate_clause, step_result)
            deep = self._run_deep_verification(action_name, action_args, step_result, ctx)

            turn_status = "success" if (syntactic["passed"] and deep["passed"]) else "failed"

            # Record turn
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
            internal_turns.append(turn_record)

            # Build context for next turn
            context = self._build_agent_context(task_description, context_summary, turn + 1, ctx["turn_history"])

            # If failed, continue (model sees failure in next context)
            # No automatic retry

        # Max turns reached
        return AgentResult(
            success=False,
            result_summary=f"Max turns ({self.max_turns}) reached without completion",
            artifacts={},
            internal_turns=internal_turns,
            verification_status="failed",
        )

    def _build_agent_context(
        self,
        task_description: str,
        context_summary: str,
        turn: int,
        history: list[dict] | None = None,
    ) -> str:
        """Build the context string for the model."""
        lines = [
            f"Task: {task_description}",
            f"Context from supervisor: {context_summary}",
            f"Turn: {turn} of {self.max_turns}",
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
        available_tools = ", ".join(self.tools.keys())
        lines.append(f"Choose ONE action: {available_tools}.")
        lines.append("If previous verification failed, ADAPT your action (change filters, fix args, etc.).")

        return "\n".join(lines)

    def _execute_action(self, action: str, args: dict, ctx: dict) -> dict:
        """Execute a single action and return the result."""
        tool_fn = self.tools[action]
        try:
            output = tool_fn(args, ctx) or {}
            if action == "load_data":
                from .. import tools as tools_module
                ctx["df"] = tools_module._df_cache
            elif action == "compute":
                ctx["last_result"] = output
            return output
        except Exception as exc:
            return {"error": str(exc), "count": 0, "rows": []}

    def _get_validate_clause(self, action: str, args: dict) -> dict:
        """Determine the validation clause for an action."""
        if action == "load_data":
            return {"kind": "nonempty"}
        if action == "compute":
            clause = {"kind": "plausibility"}
            if args.get("top_k"):
                clause["top_k"] = args["top_k"]
            if args.get("sort_by"):
                clause["sort_by"] = args["sort_by"]
            return clause
        if action == "chart":
            return {"kind": "nonempty"}
        if action == "synthesize":
            return {"kind": "type", "field": "answer", "type": "string"}
        return {"kind": "nonempty"}

    def _run_deep_verification(self, action: str, args: dict, output: dict, ctx: dict) -> dict:
        """Run deep verification for an action."""
        if action != "compute":
            return {"criterion": "deep", "passed": True, "details": "skipped (not compute)"}

        df = ctx.get("df")
        if df is None:
            return {"criterion": "deep", "passed": False, "details": "no DataFrame in context"}

        validate = self._get_validate_clause(action, args)

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

    def _extract_final_answer(self, history: list[dict]) -> str:
        """Extract a summary of findings for the result summary."""
        parts = []
        for h in history:
            if h["action"]["tool"] == "compute":
                rows = h["observation"].get("rows", [])
                if rows:
                    intent = h["action"].get("args", {}).get("groupby", ["result"])[0]
                    parts.append(f"{intent}: {rows[:5]}")
            elif h["action"]["tool"] == "chart":
                parts.append(f"chart: {h['observation'].get('path')}")
            elif h["action"]["tool"] == "load_data":
                parts.append(f"data loaded: {h['observation'].get('rows', '?')} rows")
        return "\n".join(parts) if parts else "Aucun résultat calculé."