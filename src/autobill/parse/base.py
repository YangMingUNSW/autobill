"""Parser interface. See docs/parsing.md#接口."""

from __future__ import annotations

from abc import ABC, abstractmethod

from autobill.fetch.message import RawMessage
from autobill.model import Bill


class TemplateChanged(Exception):
    """A key section or field is missing: the bank probably changed its template.

    Parsers raise this instead of ever returning an empty or partial result silently.
    """


class BaseParser(ABC):
    bank: str  # "ABC" / "CCB" / "BOC"
    name: str  # "abc_html"
    version: int  # bumped when the bank's template changes

    @abstractmethod
    def matches(self, msg: RawMessage) -> bool:
        """Whether this e-mail looks like one of this parser's statements."""

    @abstractmethod
    def parse(self, msg: RawMessage) -> list[Bill]:
        """Parse one e-mail into bills (a BOC combined statement may hold several cards).

        Raises TemplateChanged when a key section or field cannot be found; never returns [].
        """
