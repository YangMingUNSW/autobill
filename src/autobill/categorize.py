"""Spending categories from keyword rules. See docs/notify.md#分类规则.

Rules map a category to keywords. A purchase's raw description and its merchant name (as
the parser split it off, e.g. "SUKIYA" from "SUKIYAJPN") are matched against them,
case-insensitively, category by category from the top; the first match wins.

A keyword matches anywhere in the text ("Woolworths"). Written as "word:BAR" it matches
only as a whole word, so short generic words ("bar", "market") do not fire inside longer
ones ("BARBER", "MARKETPLACE").

Categories are worked out when a report is made, not stored, so editing rules.yaml takes
effect on the next report.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import cached_property
from importlib import resources
from pathlib import Path

import yaml

from autobill.config import data_dir
from autobill.model import TxnType

UNCATEGORISED = "未分类"
# Types that are categorised by their type, not by rules.
TYPE_CATEGORIES = {TxnType.FEE: "手续费", TxnType.INTEREST: "利息", TxnType.CASH: "取现"}
WORD_PREFIX = "word:"
# Letters and digits make a word; anything else (space, "*", "_", ".") separates words.
_NOT_WORD_BEFORE, _NOT_WORD_AFTER = "(?<![a-z0-9])", "(?![a-z0-9])"


def _pattern(keyword: str) -> str:
    if keyword.startswith(WORD_PREFIX):
        word = keyword[len(WORD_PREFIX) :].strip()
        return _NOT_WORD_BEFORE + re.escape(word) + _NOT_WORD_AFTER
    return re.escape(keyword)


@dataclass(frozen=True)
class Rules:
    rules: tuple[tuple[str, tuple[str, ...]], ...]  # (category, lower-cased keywords)

    @classmethod
    def from_yaml(cls, text: str, source: str = "rules") -> Rules:
        raw = yaml.safe_load(text) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{source}: expected 'category: [keyword, ...]' entries")
        rules = []
        for category, keywords in raw.items():
            if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
                raise ValueError(f"{source}: {category!r} must map to a list of keywords")
            usable = [k.strip().lower() for k in keywords if k.strip()]
            usable = [k for k in usable if k.removeprefix(WORD_PREFIX).strip()]
            rules.append((str(category), tuple(usable)))
        return cls(tuple(rules))

    @cached_property
    def _compiled(self) -> tuple[tuple[str, re.Pattern | None], ...]:
        return tuple(
            (category, re.compile("|".join(map(_pattern, keywords))) if keywords else None)
            for category, keywords in self.rules
        )

    @property
    def categories(self) -> list[str]:
        return [category for category, _ in self.rules]

    def categorize(
        self,
        description: str,
        txn_type: TxnType = TxnType.PURCHASE,
        merchant: str | None = None,
    ) -> str:
        if txn_type in TYPE_CATEGORIES:
            return TYPE_CATEGORIES[txn_type]
        text = f"{description} {merchant}" if merchant else description
        text = text.lower()
        for category, pattern in self._compiled:
            if pattern is not None and pattern.search(text):
                return category
        return UNCATEGORISED


def rules_path() -> Path:
    override = os.environ.get("AUTOBILL_RULES")
    return Path(override) if override else data_dir() / "rules.yaml"


def default_rules_text() -> str:
    return resources.files("autobill").joinpath("default_rules.yaml").read_text(encoding="utf-8")


def load_rules() -> Rules:
    """rules.yaml from the data directory if present, else the built-in defaults
    (identical to rules.example.yaml in the repository)."""
    path = rules_path()
    if path.exists():
        return Rules.from_yaml(path.read_text(encoding="utf-8"), str(path))
    return Rules.from_yaml(default_rules_text(), "built-in rules")
