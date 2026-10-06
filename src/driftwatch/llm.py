"""Thin wrapper around the Anthropic API that always returns structured output.

Uses structured JSON outputs (output_config.format): the response is constrained
to match a JSON schema, so we get valid, parseable data instead of free text.
Range limits (minimum/maximum) aren't enforced by the API, so callers validate them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ToolResult:
    data: dict
    input_tokens: int
    output_tokens: int


class LLM(Protocol):
    def call_json(self, model: str, system: str, user: str, schema: dict,
                  max_tokens: int = 1024) -> ToolResult: ...


class AnthropicLLM:
    def __init__(self, api_key: str):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=90)
        self._transform = anthropic.transform_schema

    def call_json(self, model, system, user, schema, max_tokens=1024) -> ToolResult:
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema",
                                      "schema": self._transform(schema)}},
        )
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise ValueError(f"unusable response (stop_reason={resp.stop_reason})")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise ValueError("response contained no text block")
        return ToolResult(json.loads(text), resp.usage.input_tokens, resp.usage.output_tokens)
