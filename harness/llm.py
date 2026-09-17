"""Thin wrapper around the Google GenAI (Gemini) SDK.

Import is lazy: tests that don't touch the LLM keep working without the SDK.
The planner asks for strict JSON via `response_mime_type="application/json"
(Google's structured-output mode; no Anthropic-style tool_use blocks here).

Model selection: tries `GEMINI_MODEL` first, then `GEMINI_FALLBACK_MODELS` in
order, because aliases like `gemini-flash-latest` are frequently saturated
(503 high demand). `resolved_model` records which model actually answered.

ReAct loop uses native Function Calling for reliable tool invocation.
"""

from __future__ import annotations

import time
from typing import Any

from . import config

resolved_model: str | None = None

_CLIENT: Any | None = None


def _client() -> Any:
    """Return a module-level client so the SDK keeps one live connection."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    from google import genai

    _CLIENT = genai.Client(
        api_key=config.GEMINI_API_KEY,
        http_options=genai.types.HttpOptions(timeout=120_000),
    )
    return _CLIENT


def _status_code(error: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value
    return None


def _is_retryable(error: BaseException) -> bool:
    status = _status_code(error)
    if status is not None:
        return status in (429, 500, 502, 503, 504)
    name = type(error).__name__
    return any(tag in name for tag in ("ServerError", "RateLimit", "InternalServer"))


def _is_fatal(error: BaseException) -> bool:
    """Errors that will repeat on any model (auth/quota) vs model-specific 404s."""
    status = _status_code(error)
    if status is not None:
        return status in (400, 401, 403)
    return type(error).__name__ in ("PermissionDeniedError", "PermissionError", "InvalidArgument")


def _candidate_models() -> list[str]:
    models = [config.GEMINI_MODEL]
    for model in config.GEMINI_FALLBACK_MODELS:
        if model not in models:
            models.append(model)
    return models


def _react_tools() -> list[Any]:
    """Return the function declarations for ReAct native function calling (v3 single-agent)."""
    from google import genai

    return [
        genai.types.Tool(function_declarations=[
            genai.types.FunctionDeclaration(
                name="load_data",
                description="Load and enrich the joined TMDB dataset. Must be the first action.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
            genai.types.FunctionDeclaration(
                name="compute",
                description="Run deterministic pandas pipeline: filter -> year_range -> groupby/agg -> sort -> top_k.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={
                        "filters": genai.types.Schema(
                            type=genai.types.Type.ARRAY,
                            items=genai.types.Schema(type=genai.types.Type.STRING),
                            description="Filter expressions like 'budget > 1000', 'year >= 1997'",
                        ),
                        "groupby": genai.types.Schema(
                            type=genai.types.Type.ARRAY,
                            items=genai.types.Schema(type=genai.types.Type.STRING),
                            description="Columns to group by (genres, directors, cast_names, etc.)",
                        ),
                        "agg": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="JSON string of aggregations like '{\"roi\": \"mean\", \"title\": \"count\"}'. Keys are column names, values are aggregation functions (mean, sum, count, median, max, min).",
                        ),
                        "sort_by": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Column to sort by (descending)",
                        ),
                        "top_k": genai.types.Schema(
                            type=genai.types.Type.INTEGER,
                            description="Limit to top K rows",
                        ),
                        "year_range": genai.types.Schema(
                            type=genai.types.Type.ARRAY,
                            items=genai.types.Schema(type=genai.types.Type.INTEGER),
                            min_items=2,
                            max_items=2,
                            description="Year range [min, max], use null for open-ended",
                        ),
                    },
                    required=[],
                ),
            ),
            genai.types.FunctionDeclaration(
                name="chart",
                description="Render a chart from the last compute result.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={
                        "kind": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            enum=["bar", "line"],
                            description="Chart type",
                        ),
                        "x": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="X-axis column",
                        ),
                        "y": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Y-axis column",
                        ),
                        "path": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Output filename (under output/charts/)",
                        ),
                        "title": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Chart title",
                        ),
                    },
                    required=["x", "y"],
                ),
            ),
            genai.types.FunctionDeclaration(
                name="write_file",
                description="Write a text file under output/results/.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={
                        "content": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="File content",
                        ),
                        "path": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Output filename",
                        ),
                    },
                    required=["content", "path"],
                ),
            ),
            genai.types.FunctionDeclaration(
                name="finish",
                description="Signal that you have enough information to answer the question.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
        ])
    ]


def _supervisor_tools() -> list[Any]:
    """Return the function declarations for Supervisor's agents-as-tools (v4 multi-agent)."""
    from google import genai

    return [
        genai.types.Tool(function_declarations=[
            genai.types.FunctionDeclaration(
                name="call_data_agent",
                description="Delegate data loading and computation to Data Agent. Use for: loading data, filtering, grouping, aggregating, sorting, top-k.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={
                        "task": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Specific data task description (e.g., 'top 5 directors by film count since 1997')",
                        ),
                        "context_summary": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Compressed context summary for the Data Agent",
                        ),
                    },
                    required=["task", "context_summary"],
                ),
            ),
            genai.types.FunctionDeclaration(
                name="call_viz_agent",
                description="Delegate chart generation to Viz Agent. Use when question asks for graph/chart/plot/top/classement/comparison. MANDATORY if chart keywords present.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={
                        "task": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Chart specification (e.g., 'bar chart of director vs film_count')",
                        ),
                        "context_summary": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Compressed context summary including data results",
                        ),
                    },
                    required=["task", "context_summary"],
                ),
            ),
            genai.types.FunctionDeclaration(
                name="call_redaction_agent",
                description="Delegate natural language synthesis to Redaction Agent. Use for producing the final French answer. ALWAYS call this at the end.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={
                        "task": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Synthesis task description",
                        ),
                        "context_summary": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Compressed context summary with all findings",
                        ),
                    },
                    required=["task", "context_summary"],
                ),
            ),
            genai.types.FunctionDeclaration(
                name="finish",
                description="Signal that you have enough information to answer the question.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
        ])
    ]


def _data_agent_tools() -> list[Any]:
    """Return the function declarations for Data Agent (load_data, compute, finish)."""
    from google import genai

    return [
        genai.types.Tool(function_declarations=[
            genai.types.FunctionDeclaration(
                name="load_data",
                description="Load and enrich the joined TMDB dataset. Must be the first action.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
            genai.types.FunctionDeclaration(
                name="compute",
                description="Run deterministic pandas pipeline: filter -> year_range -> groupby/agg -> sort -> top_k.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={
                        "filters": genai.types.Schema(
                            type=genai.types.Type.ARRAY,
                            items=genai.types.Schema(type=genai.types.Type.STRING),
                            description="Filter expressions like 'budget > 1000', 'year >= 1997'",
                        ),
                        "groupby": genai.types.Schema(
                            type=genai.types.Type.ARRAY,
                            items=genai.types.Schema(type=genai.types.Type.STRING),
                            description="Columns to group by (genres, directors, cast_names, etc.)",
                        ),
                        "agg": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="JSON string of aggregations like '{\"roi\": \"mean\", \"title\": \"count\"}'. Keys are column names, values are aggregation functions (mean, sum, count, median, max, min).",
                        ),
                        "sort_by": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Column to sort by (descending)",
                        ),
                        "top_k": genai.types.Schema(
                            type=genai.types.Type.INTEGER,
                            description="Limit to top K rows",
                        ),
                        "year_range": genai.types.Schema(
                            type=genai.types.Type.ARRAY,
                            items=genai.types.Schema(type=genai.types.Type.INTEGER),
                            min_items=2,
                            max_items=2,
                            description="Year range [min, max], use null for open-ended",
                        ),
                    },
                    required=[],
                ),
            ),
            genai.types.FunctionDeclaration(
                name="finish",
                description="Signal that you have completed the data task.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
        ])
    ]


def _viz_agent_tools() -> list[Any]:
    """Return the function declarations for Viz Agent (chart, finish)."""
    from google import genai

    return [
        genai.types.Tool(function_declarations=[
            genai.types.FunctionDeclaration(
                name="chart",
                description="Render a chart from the last compute result.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={
                        "kind": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            enum=["bar", "line"],
                            description="Chart type",
                        ),
                        "x": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="X-axis column",
                        ),
                        "y": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Y-axis column",
                        ),
                        "path": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Output filename (under output/charts/)",
                        ),
                        "title": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Chart title",
                        ),
                    },
                    required=["x", "y"],
                ),
            ),
            genai.types.FunctionDeclaration(
                name="finish",
                description="Signal that you have completed the chart task.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
        ])
    ]


def _redaction_agent_tools() -> list[Any]:
    """Return the function declarations for Redaction Agent (synthesize, finish)."""
    from google import genai

    return [
        genai.types.Tool(function_declarations=[
            genai.types.FunctionDeclaration(
                name="synthesize",
                description="Generate a natural language French answer from the verified step results.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={
                        "question": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Original user question",
                        ),
                        "final_answer": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="Brief summary of key findings to synthesize",
                        ),
                        "step_results": genai.types.Schema(
                            type=genai.types.Type.ARRAY,
                            items=genai.types.Schema(type=genai.types.Type.OBJECT),
                            description="Step results from all agents",
                        ),
                    },
                    required=["question", "final_answer", "step_results"],
                ),
            ),
            genai.types.FunctionDeclaration(
                name="finish",
                description="Signal that you have completed the synthesis task.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
        ])
    ]


def generate_structured_json(
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Call the model and return the raw JSON text of the answer.

    Per model, retries transient failures (503/429) up to
    LLM_ATTEMPTS_PER_MODEL times with a short backoff, then moves to the next
    candidate model. Raises only when every candidate is exhausted.
    """
    global resolved_model
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Export it or create a .env file "
            "(see .env.example)."
        )
    from google import genai

    models = _candidate_models()
    last_error: BaseException | None = None
    for model in models:
        ok, error = _call_model(model, system_prompt, user_prompt)
        if ok is not None:
            resolved_model = model
            return ok
        last_error = error
        if error is not None and _is_fatal(error):
            break
    raise RuntimeError(
        f"Gemini unavailable after {len(models)} model(s) x "
        f"{config.LLM_ATTEMPTS_PER_MODEL} attempts"
    ) from last_error


def generate_text(
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Call the model for free-form text generation (not JSON).

    Uses a slightly higher temperature for natural language.
    Retries and fallback chain same as generate_structured_json.
    """
    global resolved_model
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Export it or create a .env file "
            "(see .env.example)."
        )
    from google import genai

    models = _candidate_models()
    last_error: BaseException | None = None
    for model in models:
        ok, error = _call_model_text(model, system_prompt, user_prompt)
        if ok is not None:
            resolved_model = model
            return ok
        last_error = error
        if error is not None and _is_fatal(error):
            break
    raise RuntimeError(
        f"Gemini unavailable after {len(models)} model(s) x "
        f"{config.LLM_ATTEMPTS_PER_MODEL} attempts"
    ) from last_error


def generate_react_action(
    system_prompt: str,
    user_prompt: str,
) -> dict:
    """Call the model with native Function Calling for ReAct action selection (v3 single-agent).

    Returns a dict with the function call: {"name": "tool_name", "args": {...}}
    or {"name": "finish", "args": {}}.
    """
    global resolved_model
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Export it or create a .env file "
            "(see .env.example)."
        )
    from google import genai

    models = _candidate_models()
    tools = _react_tools()
    last_error: BaseException | None = None
    for model in models:
        ok, error = _call_model_react(model, system_prompt, user_prompt, tools)
        if ok is not None:
            resolved_model = model
            return ok
        last_error = error
        if error is not None and _is_fatal(error):
            break
    raise RuntimeError(
        f"Gemini unavailable after {len(models)} model(s) x "
        f"{config.LLM_ATTEMPTS_PER_MODEL} attempts"
    ) from last_error


def generate_supervisor_action(
    system_prompt: str,
    user_prompt: str,
) -> dict:
    """Call the model with native Function Calling for Supervisor action selection (v4 multi-agent).

    Returns a dict with the function call: {"name": "tool_name", "args": {...}}
    or {"name": "finish", "args": {}}.
    """
    global resolved_model
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Export it or create a .env file "
            "(see .env.example)."
        )
    from google import genai

    models = _candidate_models()
    tools = _supervisor_tools()
    last_error: BaseException | None = None
    for model in models:
        ok, error = _call_model_react(model, system_prompt, user_prompt, tools)
        if ok is not None:
            resolved_model = model
            return ok
        last_error = error
        if error is not None and _is_fatal(error):
            break
    raise RuntimeError(
        f"Gemini unavailable after {len(models)} model(s) x "
        f"{config.LLM_ATTEMPTS_PER_MODEL} attempts"
    ) from last_error


def generate_data_agent_action(
    system_prompt: str,
    user_prompt: str,
) -> dict:
    """Call the model with native Function Calling for Data Agent action selection."""
    global resolved_model
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    from google import genai

    models = _candidate_models()
    tools = _data_agent_tools()
    last_error: BaseException | None = None
    for model in models:
        ok, error = _call_model_react(model, system_prompt, user_prompt, tools)
        if ok is not None:
            resolved_model = model
            return ok
        last_error = error
        if error is not None and _is_fatal(error):
            break
    raise RuntimeError("Gemini unavailable for Data Agent") from last_error


def generate_viz_agent_action(
    system_prompt: str,
    user_prompt: str,
) -> dict:
    """Call the model with native Function Calling for Viz Agent action selection."""
    global resolved_model
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    from google import genai

    models = _candidate_models()
    tools = _viz_agent_tools()
    last_error: BaseException | None = None
    for model in models:
        ok, error = _call_model_react(model, system_prompt, user_prompt, tools)
        if ok is not None:
            resolved_model = model
            return ok
        last_error = error
        if error is not None and _is_fatal(error):
            break
    raise RuntimeError("Gemini unavailable for Viz Agent") from last_error


def generate_redaction_agent_action(
    system_prompt: str,
    user_prompt: str,
) -> dict:
    """Call the model with native Function Calling for Redaction Agent action selection."""
    global resolved_model
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    from google import genai

    models = _candidate_models()
    tools = _redaction_agent_tools()
    last_error: BaseException | None = None
    for model in models:
        ok, error = _call_model_react(model, system_prompt, user_prompt, tools)
        if ok is not None:
            resolved_model = model
            return ok
        last_error = error
        if error is not None and _is_fatal(error):
            break
    raise RuntimeError("Gemini unavailable for Redaction Agent") from last_error


def _call_model(
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str | None, BaseException | None]:
    """Try `model` up to LLM_ATTEMPTS_PER_MODEL times. Returns (text, None) on
    success, (None, last_error) after exhausting - or immediately for a
    non-retryable error."""
    from google import genai

    for attempt in range(1, config.LLM_ATTEMPTS_PER_MODEL + 1):
        try:
            response = _client().models.generate_content(
                model=model,
                contents=user_prompt,
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=config.MAX_TEMPERATURE,
                    max_output_tokens=config.MAX_OUTPUT_TOKENS,
                ),
            )
            text: str = response.text
            if not text.strip():
                raise RuntimeError("Gemini returned an empty response.")
            return text.strip(), None
        except Exception as exc:  # noqa: BLE001 - surface after retries
            if not _is_retryable(exc):
                print(
                    f"[llm] model '{model}' error ({type(exc).__name__}), "
                    "trying next candidate"
                )
                return None, exc
            if attempt < config.LLM_ATTEMPTS_PER_MODEL:
                print(
                    f"[llm] model '{model}' busy ({type(exc).__name__}), "
                    f"retry {attempt}/{config.LLM_ATTEMPTS_PER_MODEL}"
                )
                time.sleep(config.LLM_RETRY_SLEEP_SECONDS)
            else:
                print(f"[llm] model '{model}' busy, giving up")
            last_error = exc
    return None, last_error


def _call_model_text(
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str | None, BaseException | None]:
    """Same as _call_model but for free-form text (no JSON mime type)."""
    from google import genai

    for attempt in range(1, config.LLM_ATTEMPTS_PER_MODEL + 1):
        try:
            response = _client().models.generate_content(
                model=model,
                contents=user_prompt,
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.3,
                    max_output_tokens=config.MAX_OUTPUT_TOKENS,
                ),
            )
            text: str = response.text
            if not text.strip():
                raise RuntimeError("Gemini returned an empty response.")
            return text.strip(), None
        except Exception as exc:  # noqa: BLE001 - surface after retries
            if not _is_retryable(exc):
                print(
                    f"[llm] model '{model}' error ({type(exc).__name__}), "
                    "trying next candidate"
                )
                return None, exc
            if attempt < config.LLM_ATTEMPTS_PER_MODEL:
                print(
                    f"[llm] model '{model}' busy ({type(exc).__name__}), "
                    f"retry {attempt}/{config.LLM_ATTEMPTS_PER_MODEL}"
                )
                time.sleep(config.LLM_RETRY_SLEEP_SECONDS)
            else:
                print(f"[llm] model '{model}' busy, giving up")
            last_error = exc
    return None, last_error


def _call_model_react(
    model: str,
    system_prompt: str,
    user_prompt: str,
    tools: list[Any],
) -> tuple[dict | None, BaseException | None]:
    """Try `model` with native Function Calling for ReAct action selection."""
    from google import genai

    for attempt in range(1, config.LLM_ATTEMPTS_PER_MODEL + 1):
        try:
            response = _client().models.generate_content(
                model=model,
                contents=user_prompt,
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=tools,
                    tool_config=genai.types.ToolConfig(
                        function_calling_config=genai.types.FunctionCallingConfig(mode="ANY")
                    ),
                    temperature=config.MAX_TEMPERATURE,
                    max_output_tokens=config.MAX_OUTPUT_TOKENS,
                ),
            )
            # Extract function call from response
            candidates = response.candidates
            if not candidates:
                raise RuntimeError("No candidates in response")
            parts = candidates[0].content.parts
            for part in parts:
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    return {"name": fc.name, "args": dict(fc.args)}, None
            raise RuntimeError("No function call in response")
        except Exception as exc:  # noqa: BLE001 - surface after retries
            if not _is_retryable(exc):
                print(
                    f"[llm] model '{model}' error ({type(exc).__name__}), "
                    "trying next candidate"
                )
                return None, exc
            if attempt < config.LLM_ATTEMPTS_PER_MODEL:
                print(
                    f"[llm] model '{model}' busy ({type(exc).__name__}), "
                    f"retry {attempt}/{config.LLM_ATTEMPTS_PER_MODEL}"
                )
                time.sleep(config.LLM_RETRY_SLEEP_SECONDS)
            else:
                print(f"[llm] model '{model}' busy, giving up")
            last_error = exc
    return None, last_error