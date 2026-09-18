"""All bank parsers, newest template version first within a bank.

See docs/banks/README.md#注册表 and docs/parsing.md#接口. Adding a bank = adding its
parser here.
"""

from __future__ import annotations

from autobill.fetch.message import RawMessage
from autobill.parse.abc import AbcHtmlParser
from autobill.parse.base import BaseParser
from autobill.parse.ccb import CcbHtmlParser

PARSERS: list[BaseParser] = [
    AbcHtmlParser(),
    CcbHtmlParser(),
]


def find_parser(msg: RawMessage) -> BaseParser | None:
    """The first parser that recognises the e-mail, or None (-> UNRECOGNIZED)."""
    return next((p for p in PARSERS if p.matches(msg)), None)
