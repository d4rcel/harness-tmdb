"""CLI entrypoint: python -m harness.main "<question>".

Reads the question from the first argument or from stdin when no argument is
given (useful for long questions).
"""

from __future__ import annotations

import json
import sys

from . import config, memory
from .agents.supervisor import SupervisorAgent
from .config import ensure_dirs


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    question = " ".join(argv).strip() if argv else sys.stdin.read().strip()
    if not question:
        print("Usage: python -m harness.main \"<question>\"")
        return 2

    ensure_dirs()
    print(f"[supervisor] primary model={config.GEMINI_MODEL}")

    supervisor = SupervisorAgent()
    log_path = supervisor.run(question)
    print(f"\n[harness] run logged to {log_path}")

    # Reload the record to show results
    import json as json_mod
    with open(log_path, encoding="utf-8") as f:
        record = json_mod.load(f)

    print(f"[harness] status: {record['status']}")
    print("\n--- supervisor turns ---")
    for turn in record["supervisor_turns"]:
        ho = turn.get("handoff", {})
        handoff_str = ""
        if ho:
            handoff_str = f" | handoff: agent={ho.get('agent')} sent={ho.get('context_chars_sent')} chars recv={ho.get('result_chars_received')} chars ratio={ho.get('compression_ratio', 0):.1%}"
        print(f"  turn {turn['turn']}: {turn['action']['tool']} -> {turn['status']}{handoff_str}")

    if record.get("subagent_logs"):
        print("\n--- subagent internal turns ---")
        for agent_name, log in record["subagent_logs"].items():
            print(f"  {agent_name} ({log.get('verification_status', '?')}): {len(log.get('internal_turns', []))} turns")
            for t in log.get("internal_turns", []):
                v_syn = t["verification"]["syntactic"]
                v_deep = t["verification"]["deep"]
                deep_str = f", deep={v_deep.get('criterion', '?')}={v_deep.get('passed', '?')}" if v_deep.get('criterion') != 'deep' else ""
                print(f"    turn {t['turn']}: {t['action']['tool']} -> {t['status']} (syn={v_syn['criterion']}={v_syn['passed']}{deep_str})")

    if record["final"]:
        print("\n--- final ---")
        final = dict(record["final"])
        answer = final.pop("answer", None)
        print(json.dumps(final, indent=2, ensure_ascii=False))
        if answer:
            print("\n--- réponse ---")
            print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())