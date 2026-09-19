"""The Anthropic-format classifier (DeepSeek), against recorded-style responses: no network."""

import httpx
import pytest

from autobill import ai_anthropic
from autobill.ai_anthropic import AnthropicClassifier, parse_answer
from autobill.config import AiConfig
from autobill.suggest import (
    API_KEY_ENV,
    MerchantInfo,
    SuggesterError,
    SuggesterUnavailable,
    Verdict,
    get_suggester,
)

MERCHANTS = [MerchantInfo("ICHIKAKUYA", "TOKYO JP", "JPY"), MerchantInfo("FAROS BROS PTY LTD")]
ANSWER = (
    '```json\n{"results": [{"merchant": "ICHIKAKUYA", "category": "餐饮", '
    '"confidence": "high", "reason": "东京的拉面店"}, {"merchant": "FAROS BROS PTY LTD", '
    '"category": null, "confidence": "low", "reason": "不确定"}]}\n```'
)


def text(t):
    return {"type": "text", "text": t}


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, headers, body):
        self.requests.append((url, headers, body))
        return self.responses.pop(0)


def classifier(post, **config):
    return AnthropicClassifier(AiConfig(provider="deepseek", **config), "sk-test", post)


def test_request_without_search():
    post = Recorder({"content": [text(ANSWER)], "stop_reason": "end_turn",
                     "usage": {"input_tokens": 900, "output_tokens": 80}})  # fmt: skip
    got = classifier(post).classify(MERCHANTS, ["餐饮", "购物"], search=False)
    assert got["ICHIKAKUYA"] == Verdict("餐饮", "high", "东京的拉面店", False)
    assert got["FAROS BROS PTY LTD"] == Verdict(None, "low", "不确定", False)
    ((url, headers, body),) = post.requests
    assert url == "https://api.deepseek.com/anthropic/v1/messages"
    assert headers["x-api-key"] == "sk-test" and body["model"] == "deepseek-v4-flash"
    assert "tools" not in body and body["temperature"] == 0
    prompt = body["messages"][0]["content"]
    assert '"location": "TOKYO JP"' in prompt and '"currency": "JPY"' in prompt
    assert "- 餐饮: restaurants" in prompt


def test_search_uses_the_server_side_web_search_tool_and_resumes_a_paused_turn():
    searching = {"type": "server_tool_use", "id": "s1", "name": "web_search",
                 "input": {"query": "ICHIKAKUYA Tokyo"}}  # fmt: skip
    post = Recorder(
        {"content": [searching], "stop_reason": "pause_turn",
         "usage": {"input_tokens": 500, "server_tool_use": {"web_search_requests": 1}}},
        {"content": [searching, {"type": "web_search_tool_result", "tool_use_id": "s1",
                                 "content": []}, text(ANSWER)],
         "stop_reason": "end_turn", "usage": {"input_tokens": 3000, "output_tokens": 90}},
    )  # fmt: skip
    model = classifier(post, max_searches=2)
    got = model.classify(MERCHANTS[:1], ["餐饮"], search=True)
    assert got["ICHIKAKUYA"].searched
    first, second = (body for _, _, body in post.requests)
    assert first["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]
    assert second["messages"][-1] == {"role": "assistant", "content": [searching]}
    assert model.usage == {"input_tokens": 3500, "output_tokens": 90, "web_searches": 1}


def test_a_cut_off_answer_is_an_error():
    post = Recorder({"content": [{"type": "thinking", "thinking": "..."}],
                     "stop_reason": "max_tokens"})  # fmt: skip
    with pytest.raises(SuggesterError, match="截断"):
        classifier(post).classify(MERCHANTS, ["餐饮"], search=False)


def test_answer_parsing():
    assert parse_answer([text("好的。\n" + ANSWER)], False)["ICHIKAKUYA"].category == "餐饮"
    odd = '{"results": [{"merchant": "X", "category": "餐饮", "confidence": "sure"}, 5]}'
    assert parse_answer([text(odd)], False) == {"X": Verdict("餐饮", "low", "", False)}
    with pytest.raises(SuggesterError, match="JSON"):
        parse_answer([text("我不知道")], False)


@pytest.mark.parametrize(("status", "message"), [(401, "密钥无效"), (402, "余额不足"),
                                                 (500, "internal")])  # fmt: skip
def test_http_errors_are_explained(monkeypatch, status, message):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(status, text="internal"))
    with pytest.raises(SuggesterError, match=message):
        ai_anthropic.http_post("https://x.invalid/v1/messages", {}, {})


def test_network_failure_is_a_suggester_error(monkeypatch):
    def refuse(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", refuse)
    with pytest.raises(SuggesterError, match="连不上"):
        ai_anthropic.http_post("https://x.invalid/v1/messages", {}, {})


def test_provider_needs_a_key(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    with pytest.raises(SuggesterUnavailable, match=API_KEY_ENV):
        get_suggester(AiConfig(provider="deepseek"))
    monkeypatch.setenv(API_KEY_ENV, "sk-test")
    model = get_suggester(AiConfig(provider="deepseek", model="deepseek-v4-pro"))
    assert model.model == "deepseek-v4-pro" and model.base_url.endswith("/anthropic")
    with pytest.raises(SuggesterUnavailable, match="ai.model"):
        get_suggester(AiConfig(provider="anthropic", base_url="https://llm.example/"))
    other = get_suggester(
        AiConfig(provider="anthropic", base_url="https://llm.example/", model="m")
    )
    assert other.base_url == "https://llm.example" and other.model == "m"
