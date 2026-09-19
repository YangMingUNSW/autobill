"""A merchant classifier over the Anthropic Messages API format (POST {base_url}/v1/messages).
See docs/notify.md#ai-分类.

DeepSeek serves this format at https://api.deepseek.com/anthropic and runs web searches
itself (the "web_search" server tool), so one API key does both: no search service of
our own. Provider "anthropic" is the same client for any other API speaking the format,
with ai.base_url and ai.model from config.yaml.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable

import httpx

from autobill.config import AiConfig
from autobill.suggest import (
    API_KEY_ENV,
    CONFIDENCE,
    MerchantInfo,
    SuggesterError,
    SuggesterUnavailable,
    Verdict,
    register,
)

DEFAULTS: dict[str, tuple[str, str | None]] = {  # provider -> (base_url, default model)
    "deepseek": ("https://api.deepseek.com/anthropic", "deepseek-v4-flash"),
    "anthropic": ("https://api.anthropic.com", None),  # any such API: ai.model is required
}
WEB_SEARCH_TOOL = "web_search_20250305"
MAX_CONTINUES = 3  # a long server-side search may pause the turn; resume at most this often
TIMEOUT = 120.0

# What each built-in category means, so the model does not guess from the name alone.
HINTS = {
    "餐饮": "restaurants, cafes, bars, pubs, takeaway, food delivery, drink vending machines",
    "超市": "supermarkets, convenience stores, grocers, markets",
    "交通": "public transport, taxis, ride-hailing, fuel, parking, tolls",
    "旅行": "travel agencies, flights, tours, visas, travel insurance",
    "住宿": "hotels, hostels, serviced apartments, Airbnb",
    "医药": "pharmacies, hospitals, clinics, dentists",
    "购物": "clothes, department stores, electronics, homeware, duty free, souvenirs",
    "网购": "online marketplaces",
    "娱乐": "cinemas, concerts, museums, attractions, games, karaoke",
    "订阅": "recurring digital subscriptions and software",
    "通讯网络": "phone and internet plans, eSIMs",
    "水电燃气": "electricity, water, gas",
    "健身": "gyms and fitness",
    "烟酒": "liquor stores and tobacconists",
    "手续费": "bank service fees",
}

SYSTEM = """You classify credit-card purchases by merchant for a personal budget.
Merchants are given as printed on bank statements: often upper-case, truncated, with
the city and a country code glued on, sometimes romanised Japanese.

Rules:
- Pick one category from the list, written exactly as given, or null when you are not
  sure. A wrong category is worse than null.
- Use the location and the currency to tell which place is meant: "YURI" paid in JPY in
  "TOKYO JP" is a business in Tokyo, not one elsewhere with the same name.
- In Australia "<name> HOTEL" is usually a pub (food and drink), not accommodation.
- Payment prefixes (SQ *, KPAY*, ZLR*, SMP*, PAYPAL *, 财付通, 支付宝, 微信支付) are not
  the merchant; the name after them is.
- confidence: "high" when you know the merchant or its name says what it is; "medium"
  when a web search found it or it is very likely; "low" otherwise.
- reason: one short sentence in Simplified Chinese, e.g. "札幌的烤肉店（网上查到）".

Reply with JSON only, no other text:
{"results": [{"merchant": "<name exactly as given>", "category": "<category or null>",
  "confidence": "high|medium|low", "reason": "..."}]}"""

Post = Callable[[str, dict, dict], dict]  # (url, headers, body) -> response JSON


def http_post(url: str, headers: dict, body: dict) -> dict:
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise SuggesterError(f"连不上 AI 接口：{type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise SuggesterError(_status_message(response.status_code, response.text))
    try:
        return response.json()
    except ValueError as exc:
        raise SuggesterError("AI 接口返回的不是 JSON") from exc


def _status_message(status: int, text: str) -> str:
    known = {
        401: "密钥无效（检查 AUTOBILL_AI_API_KEY）",
        402: "账户余额不足，去平台充值",
        429: "请求太频繁或额度用完，稍后再试",
    }
    detail = known.get(status) or text.strip()[:200]
    return f"AI 接口返回 {status}：{detail}"


class AnthropicClassifier:
    def __init__(self, config: AiConfig, api_key: str, post: Post | None = None):
        base_url, model = DEFAULTS.get(config.provider or "", DEFAULTS["anthropic"])
        self.base_url = (config.base_url or base_url).rstrip("/")
        self.model = config.model or model or ""
        self.max_searches = config.max_searches
        self.api_key = api_key
        self.post = post or http_post
        self.usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}

    def classify(
        self, merchants: list[MerchantInfo], categories: list[str], *, search: bool
    ) -> dict[str, Verdict]:
        body: dict = {
            "model": self.model,
            # DeepSeek thinks before answering (better answers; ~1,000 tokens a merchant).
            "max_tokens": 4000 + 500 * len(merchants),
            "temperature": 0,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": _prompt(merchants, categories, search)}],
        }
        if search:
            body["tools"] = [
                {"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": self.max_searches}
            ]
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        content: list[dict] = []
        for _ in range(MAX_CONTINUES + 1):
            response = self.post(f"{self.base_url}/v1/messages", headers, body)
            self._count(response.get("usage") or {})
            content = response.get("content") or []
            if response.get("stop_reason") != "pause_turn":
                break
            body["messages"] = [*body["messages"], {"role": "assistant", "content": content}]
        if response.get("stop_reason") == "max_tokens":
            raise SuggesterError(f"AI 的回答太长被截断（{len(merchants)} 个商户一批）")
        searched = any(b.get("type") == "server_tool_use" for b in content)
        return parse_answer(content, searched)

    def _count(self, usage: dict) -> None:
        self.usage["input_tokens"] += int(usage.get("input_tokens") or 0)
        self.usage["output_tokens"] += int(usage.get("output_tokens") or 0)
        server = usage.get("server_tool_use") or {}
        self.usage["web_searches"] += int(server.get("web_search_requests") or 0)


def _prompt(merchants: list[MerchantInfo], categories: list[str], search: bool) -> str:
    lines = ["Categories:"]
    lines += [f"- {c}" + (f": {HINTS[c]}" if c in HINTS else "") for c in categories]
    listed = [
        {
            k: v
            for k, v in (("merchant", m.name), ("location", m.location), ("currency", m.currency))
            if v
        }
        for m in merchants
    ]
    lines += ["", "Merchants:", json.dumps(listed, ensure_ascii=False, indent=1), ""]
    if search:
        lines.append(
            "Look each merchant up with the web_search tool (its name with the city and "
            "country) unless you are already sure."
        )
    else:
        lines.append("Answer from what you know; do not search.")
    return "\n".join(lines)


def parse_answer(content: list[dict], searched: bool) -> dict[str, Verdict]:
    """The JSON object in the model's last text; an answer without one is an error."""
    texts = [b.get("text") or "" for b in content if b.get("type") == "text"]
    for text in reversed(texts):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            continue
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return _verdicts(data["results"], searched)
    raise SuggesterError("AI 的回答里没有要求的 JSON 结果")


def _verdicts(results: list, searched: bool) -> dict[str, Verdict]:
    out = {}
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("merchant"), str):
            continue
        category = item.get("category")
        category = category if isinstance(category, str) and category.strip() else None
        confidence = item.get("confidence") if item.get("confidence") in CONFIDENCE else "low"
        reason = str(item.get("reason") or "")[:200]
        out[item["merchant"]] = Verdict(category, confidence, reason, searched)
    return out


def _factory(config: AiConfig) -> AnthropicClassifier:
    if not (config.model or DEFAULTS[config.provider or "anthropic"][1]):
        raise SuggesterUnavailable("config.yaml 的 ai.model 要写模型名")
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise SuggesterUnavailable(f"没有找到环境变量 {API_KEY_ENV}（AI 接口的密钥）")
    return AnthropicClassifier(config, key)


for _name in DEFAULTS:
    register(_name, _factory)
