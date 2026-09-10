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


class ResponsesProvider(ModelProvider):
    """OpenAI-compatible Responses endpoint provider.

    `require_api_key=False` is used for trusted local servers such as LM Studio.
    Cloud OpenAI calls remain separate so the budget guard can meter them.
    """
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str,
        require_api_key: bool = True,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.max_output_tokens: int | None = None
        self.reasoning_effort: str | None = None
        self.last_usage: dict = {}
        self.last_response_id: str | None = None
        if require_api_key and not self.api_key:
            raise RuntimeError("API key is not set")

    def generate(self, system: str, user: str) -> str:
        payload: dict = {"model": self.model, "instructions": system, "input": user}
        if self.max_output_tokens:
            payload["max_output_tokens"] = int(self.max_output_tokens)
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        r = httpx.post(
            f"{self.base_url}/responses",
            headers=headers,
            json=payload,
            timeout=600,
        )
        r.raise_for_status()
        data = r.json()
        self.last_usage = dict(data.get("usage") or {})
        self.last_response_id = data.get("id")
        if data.get("output_text"):
            return str(data["output_text"])
        chunks = []
        for item in data.get("output", []):
            for c in item.get("content", []):
                if c.get("type") in {"output_text", "text"}:
                    chunks.append(c.get("text", ""))
        return "\n".join(chunks)


class OpenAIProvider(ResponsesProvider):
    """Paid OpenAI provider. Budget enforcement lives in CloudAwareOrchestrator."""
    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        super().__init__(
            model,
            api_key=api_key or os.getenv("OPENAI_API_KEY"),
            base_url=base_url or "https://api.openai.com/v1",
            require_api_key=True,
        )


class LMStudioProvider(ResponsesProvider):
    """Local/remote LM Studio provider using its OpenAI-compatible Responses API."""
    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        base = (base_url or os.getenv('LM_STUDIO_BASE_URL') or 'http://host.docker.internal:1234/v1').rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        super().__init__(
            model,
            api_key=api_key or os.getenv('LM_STUDIO_API_KEY') or None,
            base_url=base,
            require_api_key=False,
        )


def make_provider(kind: str, model: str | None = None, base_url: str | None = None) -> ModelProvider:
    if kind == "mock":
        return MockProvider()
    if kind == "ollama":
        return OllamaProvider(model or "qwen3:4b", base_url=base_url)
    if kind == "lmstudio":
        return LMStudioProvider(model or os.getenv('LM_STUDIO_MODEL', 'local-model'), base_url=base_url)
    if kind == "openai":
        return OpenAIProvider(model or "gpt-5.6-sol", base_url=base_url)
    raise ValueError(f"Unknown provider: {kind}")
