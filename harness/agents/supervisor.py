"""Supervisor Agent: orchestrates specialized agents via agents-as-tools pattern."""

from __future__ import annotations

import time
from typing import Any

from .. import config, llm, memory
from ..context.compressor import (
    compress_for_data_agent,
    compress_for_viz_agent,
    compress_for_redaction_agent,
    should_generate_chart,
)
from ..context.condenser import condense_agent_result
from .data_agent import create_data_agent
from .viz_agent import create_viz_agent
from .redaction_agent import create_redaction_agent


SUPERVISOR_SYSTEM_PROMPT = """You are the Supervisor Agent for a TMDB film analysis system.

Your job: orchestrate specialized agents to answer user questions about films.

AVAILABLE TOOLS (agents-as-tools):
1. call_data_agent: {task: string, context_summary: string} —
   Delegate data loading and computation to Data Agent.
   Use for: loading data, filtering, grouping, aggregating, sorting, top-k.
   
2. call_viz_agent: {task: string, context_summary: string} —
   Delegate chart generation to Viz Agent.
   Use when: question asks for graph/chart/plot/top/classement/comparison.
   MANDATORY if question contains chart keywords (guardrail enforced).
   
3. call_redaction_agent: {task: string, context_summary: string} —
   Delegate natural language synthesis to Redaction Agent.
   Use for: producing the final French answer to the user.
   ALWAYS call this at the end to generate the final response.

4. finish: {} — Call when all work is done and final answer is ready.

WORKFLOW:
- Turn 1: Analyze question → call_data_agent with appropriate task
- Turn 2+: Based on data results, optionally call_viz_agent (if chart needed)
- Final turn: call_redaction_agent to produce French answer → finish

CHART GUARDRAIL: If the user's question contains words like "graphique", "chart", "plot", 
"top", "classement", "plus", "évolution", "comparaison", you MUST call call_viz_agent
before call_redaction_agent. This is a deterministic rule, not a model decision.

CONTEXT COMPRESSION: When calling an agent, you provide a context_summary (compressed).
The agent returns a result_summary (condensed). Your context stays clean.

ERROR HANDLING: If an agent returns success=false, the error is in result_summary.
You can retry with modified task/context, or proceed if partial results exist.
"""


class SupervisorAgent:
    """Supervisor agent that orchestrates specialized sub-agents."""

    def __init__(self):
        self.data_agent = create_data_agent()
        self.viz_agent = create_viz_agent()
        self.redaction_agent = create_redaction_agent()
        self.max_turns = config.SUPERVISOR_MAX_TURNS

    def run(self, question: str) -> str:
        """Run the supervisor ReAct loop for a question.

        Returns the path to the run log JSON.
        """
        config.ensure_dirs()
        record = memory.new_supervisor_record(question, config.GEMINI_MODEL)

        # Track if chart is required by guardrail
        chart_required = should_generate_chart(question)
        chart_generated = False
        data_completed = False
        redaction_completed = False

        print(f"[supervisor] max_turns={self.max_turns}, chart_required={chart_required}")

        for turn in range(1, self.max_turns + 1):
            print(f"[supervisor] turn {turn}/{self.max_turns}")

            # Build context for supervisor
            context = self._build_supervisor_context(
                question, record["supervisor_turns"], turn, chart_required, chart_generated, data_completed, redaction_completed
            )

            # Call model for action
            action_response = llm.generate_supervisor_action(SUPERVISOR_SYSTEM_PROMPT, context)
            action_name = action_response.get("name")
            action_args = action_response.get("args", {})

            print(f"[supervisor] action: {action_name} {action_args}")

            # Handle finish
            if action_name == "finish":
                if not redaction_completed:
                    print("[supervisor] finish called but redaction not done - forcing redaction")
                    # Force redaction before finishing
                    continue
                
                final_answer = self._extract_final_answer(record["supervisor_turns"])
                record["final"] = {
                    "turns_ok": sum(1 for t in record["supervisor_turns"] if t["status"] == "success"),
                    "turns_total": len(record["supervisor_turns"]),
                    "answer": final_answer,
                }
                record["status"] = "completed"
                log_path = memory.save_run(record)
                print(f"[supervisor] completed in {len(record['supervisor_turns'])} turns")
                return log_path

            # Execute supervisor action (delegate to sub-agent)
            started = time.time()
            step_result = self._execute_supervisor_action(
                action_name, action_args, record, chart_required, chart_generated, data_completed
            )
            duration_ms = int((time.time() - started) * 1000)

            # Update state based on action
            if action_name == "call_data_agent":
                data_completed = step_result.get("success", False)
            elif action_name == "call_viz_agent":
                chart_generated = step_result.get("success", False)
            elif action_name == "call_redaction_agent":
                redaction_completed = step_result.get("success", False)

            # Record supervisor turn with handoff metadata
            turn_record = {
                "turn": turn,
                "action": {"tool": action_name, "args": action_args},
                "observation": step_result,
                "handoff": step_result.get("handoff", {}),
                "status": "success" if step_result.get("success", False) else "failed",
                "duration_ms": duration_ms,
            }
            record["supervisor_turns"].append(turn_record)

            print(f"[supervisor] turn {turn}: {action_name} -> {turn_record['status']}")

        # Max turns reached
        record["status"] = "aborted"
        record["final"] = {
            "error": "max turns reached",
            "turns_total": self.max_turns,
        }
        return memory.save_run(record)

    def _build_supervisor_context(
        self,
        question: str,
        history: list[dict],
        turn: int,
        chart_required: bool,
        chart_generated: bool,
        data_completed: bool,
        redaction_completed: bool,
    ) -> str:
        """Build context for supervisor model."""
        lines = [
            f"Question: {question}",
            f"Turn: {turn} of {self.max_turns}",
            f"Chart required by guardrail: {chart_required}",
            f"Chart generated: {chart_generated}",
            f"Data completed: {data_completed}",
            f"Redaction completed: {redaction_completed}",
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
                    if "result_summary" in obs:
                        lines.append(f"    Result: {obs['result_summary'][:200]}")
                    elif "error" in obs:
                        lines.append(f"    Error: {obs['error']}")
                    else:
                        lines.append(f"    Observation: {obs}")
                else:
                    lines.append(f"    Observation: {obs}")
                if "handoff" in h:
                    ho = h["handoff"]
                    lines.append(f"    Handoff: agent={ho.get('agent')}, context_sent={ho.get('context_chars_sent')} chars, result_received={ho.get('result_chars_received')} chars, ratio={ho.get('compression_ratio', 0):.2%}")
                lines.append(f"    Status: {h['status']}")

        lines.append("")
        lines.append("Available actions: call_data_agent, call_viz_agent, call_redaction_agent, finish")
        lines.append("Rules:")
        lines.append("- If chart_required and not chart_generated: MUST call call_viz_agent")
        lines.append("- If not data_completed: MUST call call_data_agent first")
        lines.append("- If data_completed and (not chart_required or chart_generated) and not redaction_completed: call call_redaction_agent")
        lines.append("- If redaction_completed: call finish")
        lines.append("- If previous action failed: adapt your next action (change task, fix context)")

        return "\n".join(lines)

    def _execute_supervisor_action(
        self,
        action: str,
        args: dict,
        record: dict,
        chart_required: bool,
        chart_generated: bool,
        data_completed: bool,
    ) -> dict:
        """Execute a supervisor action by delegating to a sub-agent."""
        if action == "call_data_agent":
            return self._call_data_agent(args, record)
        elif action == "call_viz_agent":
            return self._call_viz_agent(args, record)
        elif action == "call_redaction_agent":
            return self._call_redaction_agent(args, record)
        else:
            return {"success": False, "error": f"Unknown action: {action}", "handoff": {}}

    def _call_data_agent(self, args: dict, record: dict) -> dict:
        """Call the Data Agent with compressed context."""
        task = args.get("task", "Perform the necessary data computation for the question")
        context_summary, compress_meta = compress_for_data_agent(
            record["question"], record["supervisor_turns"]
        )

        print(f"[supervisor] Calling Data Agent with {compress_meta['compressed_chars']} chars context")
        print(f"  Compression ratio: {compress_meta['compression_ratio']:.1%}")

        # Run Data Agent
        agent_result = self.data_agent.run(task, context_summary)

        # Extract compute result from data agent's internal turns for downstream agents
        compute_result = None
        for t in agent_result.internal_turns:
            if t["action"]["tool"] == "compute":
                compute_result = t["observation"]
                break

        # Condense result for supervisor
        result_summary = condense_agent_result("data_agent", agent_result.internal_turns)

        # Log subagent details
        subagent_log = {
            "internal_turns": agent_result.internal_turns,
            "final_summary": result_summary,
            "success": agent_result.success,
            "verification_status": agent_result.verification_status,
        }
        memory.add_subagent_log(record, "data_agent", subagent_log)

        # Prepare handoff metadata
        handoff = {
            "agent": "data_agent",
            "context_chars_sent": compress_meta["compressed_chars"],
            "context_chars_original": compress_meta["original_chars"],
            "compression_ratio": compress_meta["compression_ratio"],
            "result_chars_received": len(result_summary),
            "subagent_turns": len(agent_result.internal_turns),
        }

        artifacts = {"compute_result": compute_result} if compute_result else {}

        return {
            "success": agent_result.success,
            "result_summary": result_summary,
            "artifacts": artifacts,
            "handoff": handoff,
        }

    def _call_viz_agent(self, args: dict, record: dict) -> dict:
        """Call the Viz Agent with compressed context."""
        task = args.get("task", "Generate a chart from the data results")
        context_summary, compress_meta = compress_for_viz_agent(
            record["question"], record["supervisor_turns"]
        )

        # Add compute result from data agent artifacts
        compute_result = None
        for turn in record["supervisor_turns"]:
            if turn["action"]["tool"] == "call_data_agent":
                artifacts = turn["observation"].get("artifacts", {})
                compute_result = artifacts.get("compute_result")
                if compute_result:
                    break

        if compute_result:
            # Append compute result details to context
            rows = compute_result.get("rows", [])
            cols = compute_result.get("columns", [])
            context_summary += f"\n\nCompute result data:\nColumns: {cols}\nRows: {rows[:10]}"

        print(f"[supervisor] Calling Viz Agent with {compress_meta['compressed_chars']} chars context")
        print(f"  Compression ratio: {compress_meta['compression_ratio']:.1%}")

        # Run Viz Agent with compute result in initial context
        initial_context = {"last_result": compute_result} if compute_result else None
        agent_result = self.viz_agent.run(task, context_summary, initial_context)

        # Condense result for supervisor
        result_summary = condense_agent_result("viz_agent", agent_result.internal_turns)

        # Log subagent details
        subagent_log = {
            "internal_turns": agent_result.internal_turns,
            "final_summary": result_summary,
            "success": agent_result.success,
            "verification_status": agent_result.verification_status,
        }
        memory.add_subagent_log(record, "viz_agent", subagent_log)

        # Prepare handoff metadata
        handoff = {
            "agent": "viz_agent",
            "context_chars_sent": compress_meta["compressed_chars"],
            "context_chars_original": compress_meta["original_chars"],
            "compression_ratio": compress_meta["compression_ratio"],
            "result_chars_received": len(result_summary),
            "subagent_turns": len(agent_result.internal_turns),
        }

        return {
            "success": agent_result.success,
            "result_summary": result_summary,
            "artifacts": agent_result.artifacts,
            "handoff": handoff,
        }

    def _call_redaction_agent(self, args: dict, record: dict) -> dict:
        """Call the Redaction Agent with compressed context."""
        task = args.get("task", "Produce a natural language French answer")
        context_summary, compress_meta = compress_for_redaction_agent(
            record["question"], record["supervisor_turns"]
        )

        # Collect step_results from all agents for synthesize
        step_results = []
        for turn in record["supervisor_turns"]:
            if turn["action"]["tool"] in ("call_data_agent", "call_viz_agent"):
                artifacts = turn["observation"].get("artifacts", {})
                if artifacts.get("compute_result"):
                    cr = artifacts["compute_result"]
                    # Simplify: only pass columns and first 5 rows
                    step_results.append({
                        "action": {"tool": "compute"},
                        "observation": {
                            "columns": cr.get("columns", []),
                            "rows": cr.get("rows", [])[:5]
                        }
                    })
                if turn["action"]["tool"] == "call_viz_agent":
                    for sub_turn in record.get("subagent_logs", {}).get("viz_agent", {}).get("internal_turns", []):
                        if sub_turn["action"]["tool"] == "chart":
                            step_results.append({
                                "action": {"tool": "chart"},
                                "observation": {"path": sub_turn["observation"].get("path", "?")}
                            })
                            break

        print(f"[supervisor] Calling Redaction Agent with {compress_meta['compressed_chars']} chars context")
        print(f"  Compression ratio: {compress_meta['compression_ratio']:.1%}")

        # Run Redaction Agent with step_results in initial_context
        initial_context = {"step_results": step_results} if step_results else None
        agent_result = self.redaction_agent.run(task, context_summary, initial_context)

        # Extract synthesized answer from internal turns (even if agent didn't call finish)
        synthesized_answer = None
        for t in agent_result.internal_turns:
            if t["action"]["tool"] == "synthesize":
                synthesized_answer = t["observation"].get("answer")
                break

        # Condense result for supervisor
        result_summary = condense_agent_result("redaction_agent", agent_result.internal_turns)
        if synthesized_answer:
            result_summary = synthesized_answer

        # Log subagent details
        subagent_log = {
            "internal_turns": agent_result.internal_turns,
            "final_summary": result_summary,
            "success": agent_result.success,
            "verification_status": agent_result.verification_status,
        }
        memory.add_subagent_log(record, "redaction_agent", subagent_log)

        # If agent didn't finish but we have an answer, consider it a success
        success = agent_result.success or (synthesized_answer is not None)

        # Prepare handoff metadata
        handoff = {
            "agent": "redaction_agent",
            "context_chars_sent": compress_meta["compressed_chars"],
            "context_chars_original": compress_meta["original_chars"],
            "compression_ratio": compress_meta["compression_ratio"],
            "result_chars_received": len(result_summary),
            "subagent_turns": len(agent_result.internal_turns),
        }

        return {
            "success": agent_result.success,
            "result_summary": result_summary,
            "artifacts": agent_result.artifacts,
            "handoff": handoff,
        }

    def _extract_final_answer(self, history: list[dict]) -> str:
        """Extract the final answer from the redaction agent's result."""
        for h in reversed(history):
            if h["action"]["tool"] == "call_redaction_agent":
                return h["observation"].get("result_summary", "Pas de réponse générée")
        return "Pas de réponse générée"