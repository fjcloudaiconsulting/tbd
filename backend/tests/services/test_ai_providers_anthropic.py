from app.services.ai_providers import anthropic as a


def test_chat_timeout_allows_slow_structured_calls():
    assert a.CHAT_TIMEOUT_S >= 60.0
