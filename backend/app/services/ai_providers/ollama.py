"""Ollama adapter — validates by GET {base_url}/api/tags.

Auth on Ollama is rare in the wild but supported via an optional
``Bearer`` token when fronting the server with a reverse proxy.
"""
from __future__ import annotations

from typing import Optional

import httpx

from app.services.ai_providers.base import ValidateResult


VALIDATE_TIMEOUT_S = 10.0
DEFAULT_CAPABILITIES = ["chat", "embed"]


class OllamaAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        bearer_token: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # ``api_key`` is required by the create form but Ollama itself
        # ignores it. Stored anyway so a rotation flow can sit on the
        # same shape as the other adapters.
        self.api_key = api_key
        self.bearer_token = bearer_token

    async def validate(self) -> ValidateResult:
        headers: dict[str, str] = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        url = f"{self.base_url}/api/tags"
        try:
            async with httpx.AsyncClient(timeout=VALIDATE_TIMEOUT_S) as client:
                resp = await client.get(url, headers=headers)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            return ValidateResult(
                ok=False, error=f"Network error: {type(exc).__name__}"
            )
        if resp.status_code != 200:
            if 400 <= resp.status_code < 500:
                return ValidateResult(
                    ok=False,
                    error=f"Provider rejected the request ({resp.status_code})",
                )
            return ValidateResult(
                ok=False,
                error=f"Provider unavailable ({resp.status_code})",
            )
        try:
            payload = resp.json()
        except ValueError:
            return ValidateResult(ok=False, error="Provider returned invalid JSON")
        models = [
            m["name"]
            for m in payload.get("models", [])
            if isinstance(m, dict) and "name" in m
        ]
        return ValidateResult(
            ok=True,
            discovered_models=models,
            discovered_capabilities=list(DEFAULT_CAPABILITIES),
        )
