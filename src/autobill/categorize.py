"""Spending categories from keyword rules. See docs/notify.md#分类规则.

Rules map a category to keywords. A purchase's raw description is matched against them,
case-insensitively, top to bottom; the first match wins. Categories are worked out when a
report is made, not stored, so editing rules.yaml takes effect on the next report.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from autobill.config import data_dir
from autobill.model import TxnType

UNCATEGORISED = "未分类"
# Types that are categorised by their type, not by rules.
TYPE_CATEGORIES = {TxnType.FEE: "手续费", TxnType.INTEREST: "利息", TxnType.CASH: "取现"}


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
            rules.append((str(category), tuple(k.lower() for k in keywords if k.strip())))
        return cls(tuple(rules))

    def categorize(self, description: str, txn_type: TxnType = TxnType.PURCHASE) -> str:
        if txn_type in TYPE_CATEGORIES:
            return TYPE_CATEGORIES[txn_type]
        text = description.lower()
        for category, keywords in self.rules:
            if any(k in text for k in keywords):
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
