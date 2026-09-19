"""Category suggestions for merchants the rules miss: the reserved AI interface.
See docs/notify.md#分类建议与-ai-接口.

Parsing and amounts never use AI. A suggester only proposes a category for a merchant
name; nothing takes effect until the author copies it into rules.yaml, so reports stay
deterministic. A suggester is given merchant names and the category list, nothing else:
no amounts, dates, card numbers or other statement content.

No provider ships yet. A later milestone registers one (a local Ollama model, an
OpenAI-compatible API, ...) with `register`; config.yaml then names it in ai.provider and
the key, if any, comes from the AUTOBILL_AI_API_KEY environment variable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from autobill.config import AiConfig

API_KEY_ENV = "AUTOBILL_AI_API_KEY"


class CategorySuggester(Protocol):
    def suggest(self, merchants: list[str], categories: list[str]) -> dict[str, str]:
        """merchant -> one of `categories`. Merchants it is unsure about are left out."""
        ...


class SuggesterUnavailable(RuntimeError):
    pass


Factory = Callable[[AiConfig], CategorySuggester]
_PROVIDERS: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    _PROVIDERS[name] = factory


def providers() -> list[str]:
    return sorted(_PROVIDERS)


def get_suggester(config: AiConfig) -> CategorySuggester:
    if not config.provider:
        raise SuggesterUnavailable("还没有配置 AI：在 config.yaml 的 ai.provider 里指定")
    factory = _PROVIDERS.get(config.provider)
    if factory is None:
        known = "、".join(providers()) or "暂时还没有"
        raise SuggesterUnavailable(f"不认识的 AI 提供方 {config.provider!r}（可用的：{known}）")
    return factory(config)


def suggest_categories(
    suggester: CategorySuggester, merchants: Iterable[str], categories: Iterable[str]
) -> dict[str, str]:
    """Ask the suggester, keeping only answers about the merchants asked and categories
    that exist: a model may invent either."""
    merchants, categories = list(merchants), list(categories)
    answer = suggester.suggest(list(merchants), list(categories))
    return {m: c for m, c in answer.items() if m in merchants and c in categories}
