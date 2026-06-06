#!/usr/bin/env python3
"""openai-codex SDK helper for tmux-inject sidecar decisions."""

from __future__ import annotations

import json
import sys
from typing import Any


CODEX_SDK_REASONING_EFFORT = "low"


def output_schema(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("terminal_assessment"):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "recommended_action", "confidence", "reason"],
            "properties": {
                "summary": {"type": "string"},
                "recommended_action": {"type": "string", "enum": ["wake_codex", "defer", "refuse"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
        }
    if payload.get("allowed_actions"):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "submit_key", "confidence", "reason"],
            "properties": {
                "action": {"type": "string", "enum": ["confirmed", "submit", "defer", "refuse"]},
                "submit_key": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "target_pane", "confidence", "reason"],
        "properties": {
            "decision": {"type": "string", "enum": ["inject", "defer", "refuse"]},
            "target_pane": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
        payload = request["payload"]
    except Exception as exc:
        print(json.dumps({"source": "codex_sidecar_error", "reason": f"invalid request: {exc}"}))
        return 2

    prompt = "\n".join(
        [
            "You are a tmux-inject delivery checker for tmux-skills.",
            "Return only the requested compact JSON object. Do not run tools or shell commands.",
            "Use only the JSON payload below.",
            json.dumps(payload, sort_keys=True),
        ]
    )
    try:
        from openai_codex import Codex, CodexConfig, Sandbox  # type: ignore
        from openai_codex.types import ReasoningEffort  # type: ignore
    except Exception as exc:
        print(json.dumps({"source": "codex_sidecar_error", "reason": f"openai-codex SDK unavailable: {exc}"}))
        return 3

    try:
        config = CodexConfig(config_overrides=(f'model_reasoning_effort="{CODEX_SDK_REASONING_EFFORT}"',))
        with Codex(config) as codex:
            thread = codex.thread_start(sandbox=Sandbox.read_only, ephemeral=True)
            result = thread.run(
                prompt,
                sandbox=Sandbox.read_only,
                effort=ReasoningEffort.low,
                output_schema=output_schema(payload),
            )
    except Exception as exc:
        print(json.dumps({"source": "codex_sidecar_error", "reason": f"openai-codex sidecar failed: {exc}"}))
        return 4

    print(json.dumps({"source": "codex_sidecar", "output": str(getattr(result, "final_response", ""))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
