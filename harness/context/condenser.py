"""Result condensation utilities for agent returns.

Each specialized agent condenses its internal reasoning into a brief summary
for the supervisor. This keeps the supervisor's context clean and bounded.
"""

from __future__ import annotations

from typing import Any


def condense_data_agent_result(internal_turns: list[dict]) -> str:
    """Condense Data Agent's internal turns into ~150 word summary."""
    parts = []
    
    for t in internal_turns:
        action = t["action"]["tool"]
        obs = t["observation"]
        status = t["status"]
        
        if action == "load_data":
            rows = obs.get("rows", obs.get("count", "?"))
            cached = obs.get("cached", False)
            parts.append(f"Loaded {rows} films (cached={cached})")
        elif action == "compute":
            rows = obs.get("rows", [])
            count = obs.get("count", 0)
            cols = obs.get("columns", [])
            if rows:
                # Show first 3 rows as sample
                sample = rows[:3]
                parts.append(f"Computed: {count} rows, columns={cols}. Top results: {sample}")
            else:
                parts.append(f"Computed: {count} rows (empty result)")
    
    # Add verification status
    verification_statuses = []
    for t in internal_turns:
        v_deep = t["verification"]["deep"]
        if v_deep.get("criterion") != "deep":
            verification_statuses.append(f"{v_deep['criterion']}={'PASS' if v_deep['passed'] else 'FAIL'}")
    
    if verification_statuses:
        parts.append(f"Verification: {', '.join(verification_statuses)}")
    
    summary = " | ".join(parts)
    # Limit to ~200 words / ~1200 chars
    if len(summary) > 1200:
        summary = summary[:1197] + "..."
    return summary


def condense_viz_agent_result(internal_turns: list[dict]) -> str:
    """Condense Viz Agent result."""
    parts = []
    
    for t in internal_turns:
        action = t["action"]["tool"]
        obs = t["observation"]
        
        if action == "chart":
            path = obs.get("path", "?")
            count = obs.get("count", obs.get("points", "?"))
            parts.append(f"Generated chart: {path} ({count} data points)")
    
    # Add verification status
    verification_statuses = []
    for t in internal_turns:
        v_syn = t["verification"]["syntactic"]
        verification_statuses.append(f"{v_syn['criterion']}={'PASS' if v_syn['passed'] else 'FAIL'}")
    
    if verification_statuses:
        parts.append(f"Verification: {', '.join(verification_statuses)}")
    
    summary = " | ".join(parts)
    if len(summary) > 500:
        summary = summary[:497] + "..."
    return summary


def condense_redaction_agent_result(internal_turns: list[dict]) -> str:
    """Condense Redaction Agent result."""
    parts = []
    
    for t in internal_turns:
        action = t["action"]["tool"]
        obs = t["observation"]
        
        if action == "synthesize":
            answer = obs.get("answer", "")
            if answer:
                parts.append(f"Synthesized answer: {answer}")
    
    # Add verification status
    verification_statuses = []
    for t in internal_turns:
        v_syn = t["verification"]["syntactic"]
        verification_statuses.append(f"{v_syn['criterion']}={'PASS' if v_syn['passed'] else 'FAIL'}")
    
    if verification_statuses:
        parts.append(f"Verification: {', '.join(verification_statuses)}")
    
    summary = " | ".join(parts)
    if len(summary) > 800:
        summary = summary[:797] + "..."
    return summary


def condense_agent_result(agent_name: str, internal_turns: list[dict]) -> str:
    """Dispatch to the appropriate condenser based on agent name."""
    if agent_name == "data_agent":
        return condense_data_agent_result(internal_turns)
    elif agent_name == "viz_agent":
        return condense_viz_agent_result(internal_turns)
    elif agent_name == "redaction_agent":
        return condense_redaction_agent_result(internal_turns)
    else:
        # Generic fallback
        parts = []
        for t in internal_turns:
            action = t["action"]["tool"]
            status = t["status"]
            parts.append(f"{action}: {status}")
        return " | ".join(parts)