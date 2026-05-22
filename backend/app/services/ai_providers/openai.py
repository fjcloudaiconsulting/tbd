"""OpenAI adapter — validates by GET /v1/models."""
from __future__ import annotations

import httpx

from app.services.ai_providers.base import ValidateResult


VALIDATE_TIMEOUT_S = 10.0
DEFAULT_CAPABILITIES = ["chat", "embed"]
MODELS_URL = "https://api.openai.com/v1/models"


class OpenAIAdapter:
    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    async def validate(self) -> ValidateResult:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=VALIDATE_TIMEOUT_S) as client:
                resp = await client.get(MODELS_URL, headers=headers)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            return ValidateResult(ok=False, error=f"network error: {exc}")
        if resp.status_code != 200:
            return ValidateResult(
                ok=False,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            return ValidateResult(ok=False, error=f"bad JSON: {exc}")
        models = [
            m["id"]
            for m in payload.get("data", [])
            if isinstance(m, dict) and "id" in m
        ]
        return ValidateResult(
            ok=True,
            discovered_models=models,
            discovered_capabilities=list(DEFAULT_CAPABILITIES),
        )
