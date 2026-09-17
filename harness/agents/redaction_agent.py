"""Redaction Agent: handles natural language synthesis."""

from __future__ import annotations

import time

from .. import config, llm, tools, verify
from .base_agent import BaseAgent, AgentResult


REDACTION_AGENT_SYSTEM_PROMPT = """You are the Redaction Agent for a TMDB film analysis system.

Your job: produce a natural language French answer from the computed findings.

AVAILABLE TOOLS:
1. synthesize: {question: string, final_answer: string, step_results: array} —
   Generate a French natural language response based on all findings.
   You MUST pass the step_results from the context in the synthesize call.
2. finish: {} — Call IMMEDIATELY AFTER synthesize to signal completion.

STRICT BEHAVIOR (follow exactly):
- Turn 1: Call synthesize ONCE with the question, a brief summary of findings as final_answer, and step_results.
- Turn 2: Call finish (with empty args {}) to complete your task.
- NEVER call synthesize more than once.
- NEVER call finish before synthesize.
- Do NOT load data, compute, or generate charts - those are other agents' jobs.

The synthesize tool returns the final French answer. After it returns, you MUST call finish.

EXAMPLE CORRECT SEQUENCE:
Turn 1: synthesize({"question": "...", "final_answer": "Key findings: ...", "step_results": [...]})
Turn 2: finish({})
"""


class RedactionAgent(BaseAgent):
    """Specialized Redaction Agent that ensures synthesize -> finish flow."""

    def run(self, task_description: str, context_summary: str, initial_context: dict | None = None) -> AgentResult:
        """Run the Redaction Agent with enforced synthesize -> finish flow."""
        ctx: dict = {"df": None, "last_result": None, "turn_history": []}
        if initial_context:
            ctx.update(initial_context)
        internal_turns = []

        # Turn 1: Call synthesize
        context = self._build_agent_context(task_description, context_summary, 1)
        action_response = self.generate_action_fn(self.system_prompt, context)
        action_name = action_response.get("name")
        action_args = action_response.get("args", {})

        if action_name != "synthesize":
            return AgentResult(
                success=False,
                result_summary=f"Expected synthesize as first action, got {action_name}",
                artifacts={},
                internal_turns=internal_turns,
                verification_status="failed",
            )

        # Inject step_results from initial_context if available
        if initial_context and "step_results" in initial_context:
            action_args["step_results"] = initial_context["step_results"]

        # Execute synthesize
        started = time.time()
        step_result = self._execute_action(action_name, action_args, ctx)
        duration_ms = int((time.time() - started) * 1000)

        # Verify
        validate_clause = self._get_validate_clause(action_name, action_args)
        syntactic = verify.check_output(validate_clause, step_result)
        deep = self._run_deep_verification(action_name, action_args, step_result, ctx)
        turn_status = "success" if (syntactic["passed"] and deep["passed"]) else "failed"

        turn_record = {
            "turn": 1,
            "action": {"tool": action_name, "args": action_args},
            "observation": step_result,
            "verification": {"syntactic": syntactic, "deep": deep},
            "status": turn_status,
            "duration_ms": duration_ms,
        }
        internal_turns.append(turn_record)
        ctx["turn_history"].append(turn_record)

        # Extract synthesized answer
        synthesized_answer = step_result.get("answer")

        # Turn 2: Call finish (forced)
        finish_result = {"finished": True}
        finish_record = {
            "turn": 2,
            "action": {"tool": "finish", "args": {}},
            "observation": finish_result,
            "verification": {"syntactic": {"criterion": "finish", "passed": True, "details": "forced finish"}, "deep": {"criterion": "deep", "passed": True, "details": "skipped"}},
            "status": "success",
            "duration_ms": 0,
        }
        internal_turns.append(finish_record)

        return AgentResult(
            success=turn_status == "success" and synthesized_answer is not None,
            result_summary=synthesized_answer or "No answer synthesized",
            artifacts={},
            internal_turns=internal_turns,
            verification_status="passed" if turn_status == "success" else "failed",
        )


def create_redaction_agent() -> RedactionAgent:
    """Create and configure the Redaction Agent."""
    redaction_tools = {
        "synthesize": tools.TOOLS["synthesize"],
        "finish": lambda args, ctx: {"finished": True},
    }

    return RedactionAgent(
        name="redaction_agent",
        tools=redaction_tools,
        system_prompt=REDACTION_AGENT_SYSTEM_PROMPT,
        max_turns=2,
        generate_action_fn=llm.generate_redaction_agent_action,
    )