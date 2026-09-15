"""CLI entrypoint: python -m harness.main "<question>".

Reads the question from the first argument or from stdin when no argument is
given (useful for long questions).
"""

from __future__ import annotations

import json
import sys

from . import config, llm, memory, react_loop
from .config import ensure_dirs


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    question = " ".join(argv).strip() if argv else sys.stdin.read().strip()
    if not question:
        print("Usage: python -m harness.main \"<question>\"")
        return 2

    ensure_dirs()
    print(f"[react] primary model={config.GEMINI_MODEL}")

    log_path = react_loop.run_react_loop(question)
    print(f"\n[harness] run logged to {log_path}")

    # Reload the record to show results
    import json as json_mod
    with open(log_path, encoding="utf-8") as f:
        record = json_mod.load(f)

    print(f"[harness] status: {record['status']}")
    print("\n--- per-turn outcome ---")
    for turn in record["turns"]:
        v_syn = turn["verification"]["syntactic"]
        v_deep = turn["verification"]["deep"]
        print(f"  turn {turn['turn']}: {turn['action']['tool']} -> {turn['status']} (syn={v_syn['criterion']}={v_syn['passed']}, deep={v_deep.get('criterion', '?')}={v_deep.get('passed', '?')})")
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