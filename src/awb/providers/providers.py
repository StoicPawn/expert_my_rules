from __future__ import annotations

import json
import os
import httpx

from .base import ModelProvider
from .ollama_stream import OllamaProvider


class MockProvider(ModelProvider):
    """Deterministic provider for smoke tests and offline development."""
    def generate(self, system: str, user: str) -> str:
        if "GATE_JSON" in system:
            return json.dumps({"passed": False, "detail": "Mock verifier keeps semantic completion gates open."})
        if "DIRECTOR_JSON" in system or "Director of an autonomous" in system:
            return json.dumps({
                "title": "Inspect current highest-priority gap",
                "description": "Examine the current project state, identify one concrete unresolved gap, and produce evidence that closes or sharpens it.",
                "priority": 1.0,
            })
        if "REVIEW_JSON" in system or "adversarial Reviewer" in system:
            return json.dumps({"approved": True, "critical_objections": [], "recommendations": ["Persist evidence and continue to the next unresolved gate."]})
        return "Mock work result: analyzed the assigned task and produced a candidate result for review."


class OpenAIProvider(ModelProvider):
    """Responses API provider with observable token usage.

    Budget enforcement lives in the orchestrator. This provider exposes the exact
    usage object returned by the API so the durable ledger can meter paid calls.
    """
    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        self.model = model
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.max_output_tokens: int | None = None
        self.reasoning_effort: str | None = None
        self.last_usage: dict = {}
        self.last_response_id: str | None = None
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")

    def generate(self, system: str, user: str) -> str:
        payload: dict = {"model": self.model, "instructions": system, "input": user}
        if self.max_output_tokens:
            payload["max_output_tokens"] = int(self.max_output_tokens)
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        r = httpx.post(
            f"{self.base_url}/responses",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=600,
        )
        r.raise_for_status()
        data = r.json()
        self.last_usage = dict(data.get("usage") or {})
        self.last_response_id = data.get("id")
        if data.get("output_text"):
            return data["output_text"]
        chunks = []
        for item in data.get("output", []):
            for c in item.get("content", []):
                if c.get("type") in {"output_text", "text"}:
                    chunks.append(c.get("text", ""))
        return "\n".join(chunks)


def make_provider(kind: str, model: str | None = None, base_url: str | None = None) -> ModelProvider:
    if kind == "mock":
        return MockProvider()
    if kind == "ollama":
        return OllamaProvider(model or "qwen3:4b", base_url=base_url)
    if kind == "openai":
        return OpenAIProvider(model or "gpt-5.6-sol", base_url=base_url)
    raise ValueError(f"Unknown provider: {kind}")
