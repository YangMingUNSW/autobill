"""The AI interface: a model classifies merchants the rules miss.
See docs/notify.md#ai-分类.

Parsing and amounts never use AI. A classifier is given, per merchant, only its name,
where it is (as the statement prints it, e.g. "SAPPORO JP") and the currency it was paid
in, plus the category names: never amounts, dates, card numbers or other statement
content. autobill/classify.py decides which merchants are asked and stores the answers.

Providers register with `register`; config.yaml names one in ai.provider and the key
comes from the AUTOBILL_AI_API_KEY environment variable. The first provider is
autobill/ai_anthropic.py (DeepSeek, or any API speaking the Anthropic Messages format).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from autobill.config import AiConfig

API_KEY_ENV = "AUTOBILL_AI_API_KEY"
CONFIDENCE = ("high", "medium", "low")

# Bank markers around a merchant name that are not part of it. "AUSVISA Apple Pay" means
# "Australia, Visa card, Apple Pay", but a model reads it as an Australian visa office.
_CHANNEL = re.compile(
    r"^(?:[A-Z]{3} )?(?:跨行无卡消费|跨行预授权完成|跨行消费|境外消费|网上消费|跨境消费)\s*"
)
_WALLET = re.compile(r"(?:[A-Z]{3})?(?:VISA|CUP|MC)\s*apple\s*pay.*$", re.IGNORECASE)


def shown_name(name: str) -> str:
    """The merchant name as a model is shown it, without the bank's payment markers:
    "GM SYDNEY PTY LTDAUSVISA Apple Pay" -> "GM SYDNEY PTY LTD"."""
    cleaned = _WALLET.sub("", _CHANNEL.sub("", name)).strip()
    return cleaned or name


@dataclass(frozen=True)
class MerchantInfo:
    """Everything a model learns about one merchant."""

    name: str  # as the reports group it: the parser's merchant name, else the description
    location: str | None = None  # "SAPPORO JP", as printed; None when the bank prints none
    currency: str | None = None  # paid in: the original currency of a converted purchase


@dataclass(frozen=True)
class Verdict:
    category: str | None  # None: the model is not sure; the merchant stays 未分类
    confidence: str  # high / medium / low
    reason: str = ""  # one sentence, shown by `autobill classify`
    searched: bool = False  # the model looked it up on the web


class CategorySuggester(Protocol):
    model: str

    def classify(
        self, merchants: list[MerchantInfo], categories: list[str], *, search: bool
    ) -> dict[str, Verdict]:
        """merchant name -> verdict. `search`: the model may look merchants up on the web.
        Merchants it says nothing about are left out."""
        ...


class SuggesterUnavailable(RuntimeError):
    """No usable provider: none configured, an unknown one, or no key."""


class SuggesterError(RuntimeError):
    """The provider was reached but failed: login, balance, a malformed answer, ..."""


class AnswerCutOff(SuggesterError):
    """The answer hit the output limit: fewer merchants at a time will fit."""


Factory = Callable[[AiConfig], CategorySuggester]
_PROVIDERS: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    _PROVIDERS[name] = factory


def providers() -> list[str]:
    _load_builtin()
    return sorted(_PROVIDERS)


def get_suggester(config: AiConfig) -> CategorySuggester:
    if not config.provider:
        raise SuggesterUnavailable("还没有配置 AI：在 config.yaml 的 ai.provider 里指定")
    _load_builtin()
    factory = _PROVIDERS.get(config.provider)
    if factory is None:
        known = "、".join(providers()) or "暂时还没有"
        raise SuggesterUnavailable(f"不认识的 AI 提供方 {config.provider!r}（可用的：{known}）")
    return factory(config)


def keep_valid(
    answer: dict[str, Verdict], merchants: list[MerchantInfo], categories: list[str]
) -> dict[str, Verdict]:
    """Only verdicts about the merchants asked, with a category that exists: a model may
    invent either. An invented category counts as "not sure"."""
    asked = {m.name for m in merchants}
    out = {}
    for name, verdict in answer.items():
        if name not in asked:
            continue
        if verdict.category is not None and verdict.category not in categories:
            verdict = Verdict(None, "low", verdict.reason, verdict.searched)
        out[name] = verdict
    return out


def _load_builtin() -> None:
    from autobill import ai_anthropic  # noqa: F401 - registers itself
