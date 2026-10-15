from app.services.ai_providers import anthropic as a


def test_chat_timeout_allows_slow_structured_calls():
    assert a.CHAT_TIMEOUT_S >= 60.0


async def test_discovery_requests_limit_and_sees_models_past_the_default_page(monkeypatch):
    # TBD-629: Anthropic pages /v1/models at 20 unless ``limit`` is sent.
    import httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        limit = int(request.url.params.get("limit", 20))
        data = [{"id": f"claude-m{i}"} for i in range(25)][:limit]
        return httpx.Response(200, json={"data": data})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    result = await a.AnthropicAdapter(api_key="sk-ant-test").validate()
    assert seen[0].url.params.get("limit") == "1000"
    assert len(result.discovered_models) == 25
