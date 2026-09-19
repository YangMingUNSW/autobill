"""Spending categories from keyword rules. See docs/notify.md#分类规则.

Rules map a category to keywords. A purchase's raw description and its merchant name (as
the parser split it off, e.g. "SUKIYA" from "SUKIYAJPN") are matched against them,
case-insensitively, category by category from the top; the first match wins.

A keyword matches anywhere in the text ("Woolworths"). Written as "word:BAR" it matches
only as a whole word, so short generic words ("bar", "market") do not fire inside longer
ones ("BARBER", "MARKETPLACE").

Spaces, punctuation and letter case never matter: statements print one merchant many ways
("MCDONALD'S", "MC DONALD S", "SEVEN-ELEVEN", "7 ELEVEN"), so a keyword is compared with
only the letters and digits kept ("mcdonalds"). A whole-word keyword is compared word by
word instead; there, anything but a letter or digit separates words, and so do Chinese
characters and kana, which have no spaces between words.

The author's rules.yaml comes first and the built-in rules after it, so the author's own
keywords win and every built-in keyword still applies. A merchant no rule matches may
still have an AI answer (autobill/classify.py), used last.

Categories are worked out when a report is made, not stored, so editing rules.yaml takes
effect on the next report.
"""

from __future__ import annotations

import os
import sqlite3
import unicodedata
from dataclasses import dataclass, replace
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


def _fold(text: str) -> str:
    # NFKC turns full-width letters and digits ("ＫＦＣ") into plain ones.
    return unicodedata.normalize("NFKC", text).lower()


def _is_cjk(ch: str) -> bool:
    """Kana and Chinese characters: written without spaces, so never part of a whole word."""
    code = ord(ch)
    return 0x3040 <= code <= 0x30FF or 0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF


def _compact(text: str) -> str:
    """ "mcdonalds" for "MC DONALD'S": letters, digits and CJK only."""
    return "".join(ch for ch in _fold(text) if ch.isalnum())


def _words(text: str) -> str:
    """ " sq bar " for "SQ *BAR*": words of letters and digits, one space around each."""
    kept = "".join(ch if ch.isalnum() and not _is_cjk(ch) else " " for ch in _fold(text))
    return " " + " ".join(kept.split()) + " "


def _needle(keyword: str) -> tuple[bool, str]:
    """(whole word?, what to look for); an empty needle is never used."""
    if keyword.startswith(WORD_PREFIX):
        words = _words(keyword[len(WORD_PREFIX) :])
        return True, words if words.strip() else ""
    return False, _compact(keyword)


@dataclass(frozen=True)
class Rules:
    rules: tuple[tuple[str, tuple[str, ...]], ...]  # (category, keywords)
    learned: tuple[tuple[str, str], ...] = ()  # (merchant, category) from the AI, used last

    @classmethod
    def from_yaml(cls, text: str, source: str = "rules") -> Rules:
        raw = yaml.safe_load(text) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{source}: expected 'category: [keyword, ...]' entries")
        rules = []
        for category, keywords in raw.items():
            if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
                raise ValueError(f"{source}: {category!r} must map to a list of keywords")
            usable = [k.strip() for k in keywords if _needle(k.strip())[1]]
            rules.append((str(category), tuple(usable)))
        return cls(tuple(rules))

    def __add__(self, later: Rules) -> Rules:
        """These rules first, then `later`'s: a category may then appear twice."""
        return Rules(self.rules + later.rules, self.learned or later.learned)

    def with_learned(self, learned: dict[str, str]) -> Rules:
        return replace(self, learned=tuple(sorted(learned.items())))

    @cached_property
    def _learned(self) -> dict[str, str]:
        return dict(self.learned)

    @cached_property
    def _needles(self) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]:
        """(category, whole-word needles, anywhere needles)."""
        out = []
        for category, keywords in self.rules:
            needles = [_needle(k) for k in keywords]
            words = tuple(n for is_word, n in needles if is_word)
            anywhere = tuple(n for is_word, n in needles if not is_word)
            out.append((category, words, anywhere))
        return tuple(out)

    @property
    def categories(self) -> list[str]:
        return list(dict.fromkeys(category for category, _ in self.rules))

    def categorize(
        self,
        description: str,
        txn_type: TxnType = TxnType.PURCHASE,
        merchant: str | None = None,
    ) -> str:
        if txn_type in TYPE_CATEGORIES:
            return TYPE_CATEGORIES[txn_type]
        found = self._match(description, merchant)
        if found is not None:
            return found
        return self._learned.get(merchant or description, UNCATEGORISED)

    def by_ai(
        self, description: str, txn_type: TxnType = TxnType.PURCHASE, merchant: str | None = None
    ) -> bool:
        """The category comes from an AI answer, not a rule."""
        if txn_type in TYPE_CATEGORIES or self._match(description, merchant) is not None:
            return False
        return (merchant or description) in self._learned

    def _match(self, description: str, merchant: str | None) -> str | None:
        text = f"{description} {merchant}" if merchant else description
        words, compact = _words(text), _compact(text)
        for category, word_needles, anywhere in self._needles:
            if any(n in words for n in word_needles) or any(n in compact for n in anywhere):
                return category
        return None


def rules_path() -> Path:
    override = os.environ.get("AUTOBILL_RULES")
    return Path(override) if override else data_dir() / "rules.yaml"


def default_rules_text() -> str:
    return resources.files("autobill").joinpath("default_rules.yaml").read_text(encoding="utf-8")


def load_rules(conn: sqlite3.Connection | None = None) -> Rules:
    """The author's rules.yaml (if present) first, then the built-in rules (identical to
    rules.example.yaml in the repository); with `conn`, then the stored AI answers."""
    rules = Rules.from_yaml(default_rules_text(), "built-in rules")
    path = rules_path()
    if path.exists():
        rules = Rules.from_yaml(path.read_text(encoding="utf-8"), str(path)) + rules
    if conn is not None:
        learned = conn.execute(
            "SELECT merchant, category FROM ai_categories WHERE category IS NOT NULL"
        )
        rules = rules.with_learned({r[0]: r[1] for r in learned})
    return rules
