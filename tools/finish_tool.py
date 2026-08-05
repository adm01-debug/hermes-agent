#!/usr/bin/env python3
"""
Finish Tool Module - Turn Completion

Provides the `finish` tool: a universal, side-effect-free signal that the
current turn's work is complete. When the model emits `finish`, the executor
layer (agent/tool_executor.py, truncate-at-finish) executes the batch only up
to the first `finish` call (inclusive) and turns every trailing tool call in
the same batch into a synthetic "Skipped: turn finished by finish tool"
result, preserving the 1:1 tool_call/tool_result pairing strict providers
require.

Design:
- Single `finish` tool with an optional `summary` parameter
- Handler always returns the canonical JSON payload
  {"finish": true, "summary": ...} — the executor only reacts when it parses
  that payload from the EXECUTED result, so a guardrail/approval-blocked
  call (synthetic result without `finish: true`) never ends the turn
- Registered without check_fn/requires_env: core tool, universal availability
- The summary is capped so the payload stays tiny in the transcript
"""

import json

# Upper bound on the summary echoed back to the executor. Keeps the tool
# result tiny (it rides in the canonical transcript and the executor stores
# it on agent._finish_tool_summary), while still fitting a real wrap-up.
MAX_FINISH_SUMMARY_CHARS = 4000


def finish_tool(summary: str = "") -> str:
    """End the turn. The executor layer detects this payload and truncates
    any trailing tool calls in the batch.

    Args:
        summary: optional final message for the user — what was accomplished,
            key results, next steps (if any).

    Returns:
        JSON string {"finish": true, "summary": ...} with the summary
        truncated to MAX_FINISH_SUMMARY_CHARS.
    """
    summary = str(summary or "")
    if len(summary) > MAX_FINISH_SUMMARY_CHARS:
        summary = summary[:MAX_FINISH_SUMMARY_CHARS]
    return json.dumps({"finish": True, "summary": summary}, ensure_ascii=False)


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================
# Behavioral guidance is baked into the description so it's part of the
# static tool schema (cached, never changes mid-conversation).

FINISH_SCHEMA = {
    "name": "finish",
    "description": (
        "Encerra o turno atual com uma resposta final opcional. "
        "Use ao concluir a tarefa — tool calls seguintes do mesmo batch "
        "serão descartadas."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Resumo final opcional da conclusão",
            }
        },
        "additionalProperties": False,
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry

registry.register(
    name="finish",
    toolset="core",
    schema=FINISH_SCHEMA,
    handler=lambda args, **kw: finish_tool(summary=args.get("summary", "")),
    description=(
        "Encerra o turno atual com uma resposta final opcional. "
        "Use ao concluir a tarefa — tool calls seguintes do mesmo batch "
        "serão descartadas."
    ),
    emoji="🏁",
)
