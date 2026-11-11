"""Parser interface. See docs/parsing.md#接口."""

from __future__ import annotations

from abc import ABC, abstractmethod

from autobill.fetch.message import RawMessage
from autobill.model import Bill, Transaction, make_txn_id


class TemplateChanged(Exception):
    """A key section or field is missing: the bank probably changed its template.

    Parsers raise this instead of ever returning an empty or partial result silently.
    """


class BaseParser(ABC):
    bank: str  # "ABC" / "CCB" / "BOC" / "ICBC"
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


def with_txn_ids(bank: str, account_id: str, transactions: list[Transaction]) -> list[Transaction]:
    """The transactions with their ids filled in, for a statement whose account is only
    known once its rows are read (the id is made from the account)."""
    return [
        t.model_copy(
            update={
                "txn_id": make_txn_id(
                    bank, account_id, t.trans_date, t.amount, t.description_raw, t.line_no
                )
            }
        )
        for t in transactions
    ]
