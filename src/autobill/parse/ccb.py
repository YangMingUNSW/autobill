"""China Construction Bank (CCB) HTML e-statement parser.

Format spec: docs/banks/ccb.md. Sections are sub-tables nested in a container table, and
some data rows (the payment information block) sit directly in container tables. So the
parser flattens every table row in document order, skips rows that only hold nested
tables, and locates the data by Chinese anchor text.

CCB transactions already use our sign convention (repayments negative), so amounts are
taken as printed. Purchases, rebates and repayments are confirmed by samples; fees,
interest, cash advances and instalments are still inferred (docs/banks/ccb.md §5).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import Decimal

from bs4 import BeautifulSoup

from autobill.fetch.message import RawMessage
from autobill.model import ZERO, Bill, BillBalance, Transaction, TxnType, make_txn_id
from autobill.parse.base import BaseParser, TemplateChanged
from autobill.parse.util import (
    CurrencyError,
    FormatError,
    normalize_ws,
    parse_amount,
    parse_currency,
    parse_date,
)
from autobill.reconcile import reconcile

SENDER = "service@vip.ccb.com"
SUBJECT = "中国建设银行信用卡电子账单"
BODY_MARKERS = ("龙卡信用卡对账单", "【交易明细】")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SECTION_RE = re.compile(r"^\[(.+?)\]")  # "[人民币账户] RMB Account"
CARD_CELL_RE = re.compile(r"(\d{4})(?:/\d{4})?")  # "0004" or "0004/0007" (Apple Pay)
UNKNOWN_ACCOUNT = "CCB:unknown"
# "Visa 26 Apr-Sep FX RewardCashback", "CCB CXMUSE 1pct Rebate", 返现
REBATE_RE = re.compile(r"返现|cashback|rebate", re.IGNORECASE)


class CcbHtmlParser(BaseParser):
    bank = "CCB"
    name = "ccb_html"
    version = 1

    def matches(self, msg: RawMessage) -> bool:
        from_bank = msg.from_addr == SENDER or SUBJECT in msg.subject
        return from_bank and all(marker in msg.html for marker in BODY_MARKERS)

    def parse(self, msg: RawMessage) -> list[Bill]:
        if not msg.html:
            raise TemplateChanged("CCB: e-mail has no HTML body")
        return [reconcile(_Statement(msg).build())]


class _Statement:
    def __init__(self, msg: RawMessage) -> None:
        self.msg = msg
        self.warnings: list[str] = []
        self.summary_header_seen = False
        self.summary_rows: list[list[str]] = []
        self.statement_date = None
        self.period: tuple | None = None
        self.due_date = None
        self.min_payment: dict[str, Decimal] = {}
        self.txn_title_seen = False
        self.txn_header_seen = False
        self.section_previous: dict[str, Decimal] = {}  # "[人民币账户]" previous balance
        self.txn_rows: list[list[str]] = []

    def build(self) -> Bill:
        self._walk()
        self._require()
        transactions = self._transactions()
        balances = self._balances({t.currency for t in transactions})
        self._cross_check(balances)
        cards = sorted({t.card_last4 for t in transactions if t.card_last4})
        if cards:
            account_id = f"CCB:{cards[0]}"
            if len(cards) > 1:
                self.warnings.append(f"这份账单里有多个卡号 {cards}，账户取 {cards[0]}")
        else:
            # Card numbers only appear on transaction rows (docs/banks/ccb.md §6).
            account_id = UNKNOWN_ACCOUNT
            self.warnings.append("本期没有流水，取不到卡号，账户记为 CCB:unknown")
        statement_date = self.statement_date or self.period[1]
        email_date = self.msg.email_date
        if email_date is None:
            email_date = statement_date
            self.warnings.append("邮件没有 Date 头，汇率日期改用账单日")
        return Bill(
            bank="CCB",
            account_id=account_id,
            cards=cards,
            statement_date=statement_date,
            period_start=self.period[0] if self.period else None,
            period_end=self.period[1] if self.period else None,
            due_date=self.due_date,
            email_date=email_date,
            balances=balances,
            transactions=self._with_account(transactions, account_id),
            status="OK",  # decided by reconcile()
            warnings=self.warnings,
            source_message_id=self.msg.message_id,
            source_sha256=self.msg.sha256,
            parser_name=CcbHtmlParser.name,
            parser_version=CcbHtmlParser.version,
        )

    # --- walking the document ---------------------------------------------

    def _walk(self) -> None:
        mode: str | None = None  # "summary" / "payment" / "txn"
        for cells in _rows(self.msg.html):
            first = next(c for c in cells if c)
            if first.startswith("账户币种") and any("上期全部应还款额" in c for c in cells):
                self.summary_header_seen, mode = True, "summary"
            elif first.startswith("账户币种") and any("最低还款额" in c for c in cells):
                mode = "payment"
            elif first.startswith("本期账单日") and len(cells) >= 2:
                self.statement_date = parse_date(cells[1])
            elif first.startswith("账单周期") and len(cells) >= 2:
                self._cycle_row(cells)
            elif first.startswith("【交易明细】"):
                self.txn_title_seen, mode = True, "txn"
            elif mode == "summary" and len(cells) == 5 and _is_currency(first):
                self.summary_rows.append(cells)
            elif mode == "payment" and len(cells) == 4 and _is_currency(first):
                self.min_payment[parse_currency(first)] = abs(
                    parse_amount(cells[2], blank_is_zero=True)
                )
            elif mode == "txn":
                mode = self._txn_row(cells)
            else:
                mode = None if mode in ("summary", "payment") else mode

    def _cycle_row(self, cells: list[str]) -> None:
        """账单周期 | 2026/06/11-2026/07/10 | 本期到期还款日 | 2026/08/01"""
        start, sep, end = cells[1].partition("-")
        if not sep:
            raise TemplateChanged(f"CCB: unexpected statement cycle {cells[1]!r}")
        self.period = (parse_date(start), parse_date(end))
        if len(cells) >= 4 and "到期还款日" in cells[2]:
            self.due_date = parse_date(cells[3]) if cells[3] not in ("", "-") else None

    def _txn_row(self, cells: list[str]) -> str | None:
        first = next(c for c in cells if c)
        if first.startswith("交易日"):
            self.txn_header_seen = True
        elif first.startswith("T-Date"):
            pass
        elif m := SECTION_RE.match(first):
            if len(cells) >= 3:
                try:
                    currency = parse_currency(first)
                    self.section_previous[currency] = parse_amount(cells[2])
                except FormatError as exc:
                    self.warnings.append(f"账户分节行无法识别（{exc}）：{m.group(0)}")
        elif len(cells) == 8 and DATE_RE.match(first):
            self.txn_rows.append(cells)
        elif "结束" in first or "The End" in first:
            return None
        else:
            self.warnings.append(f"无法识别的明细行：{' | '.join(c for c in cells if c)}")
        return "txn"

    def _require(self) -> None:
        if not self.summary_header_seen or not self.summary_rows:
            raise TemplateChanged("CCB: summary table (上期全部应还款额) not found")
        if self.statement_date is None and self.period is None:
            raise TemplateChanged("CCB: neither statement date nor statement cycle found")
        if not self.txn_title_seen or not self.txn_header_seen:
            raise TemplateChanged("CCB: transaction table (【交易明细】) not found")

    # --- building the model -------------------------------------------------

    def _balances(self, txn_currencies: set[str]) -> list[BillBalance]:
        balances = []
        for cells in self.summary_rows:
            currency = parse_currency(cells[0])
            previous, charges, credits, due = (parse_amount(c) for c in cells[1:])
            if currency != "CNY" and not any((previous, charges, credits, due)):
                if currency not in txn_currencies:
                    continue  # CCB always lists USD and EUR, even when everything is zero
            balances.append(
                BillBalance(
                    currency=currency,
                    # CCB has no overpayment column; a negative balance is read as a deposit.
                    # (Inferred: no sample yet, docs/banks/ccb.md §4.)
                    previous_balance=max(previous, ZERO),
                    previous_deposit=max(-previous, ZERO),
                    new_charges=charges,
                    payments_credits=credits,
                    amount_due=max(due, ZERO),
                    deposit=max(-due, ZERO),
                    min_payment=self.min_payment.get(currency),
                )
            )
        return balances

    def _cross_check(self, balances: list[BillBalance]) -> None:
        """The transaction table repeats each account's previous balance."""
        for b in balances:
            shown = self.section_previous.get(b.currency)
            expected = b.previous_balance - b.previous_deposit
            if shown is not None and shown != expected:
                self.warnings.append(
                    f"{b.currency} 明细表的上期账单余额 {shown} 与汇总表不一致（应为 {expected}）"
                )

    def _transactions(self) -> list[Transaction]:
        transactions = []
        for line_no, cells in enumerate(self.txn_rows, start=1):
            try:
                transactions.append(self._transaction(line_no, cells))
            except FormatError as exc:
                self.warnings.append(f"明细行解析失败（{exc}）：{' | '.join(cells)}")
        return transactions

    def _transaction(self, line_no: int, cells: list[str]) -> Transaction:
        tdate, pdate, last4, description, trans_cur, trans_amt, sett_cur, sett_amt = cells
        amount = parse_amount(sett_amt)  # CCB already uses our sign convention
        currency = parse_currency(sett_cur)
        orig_currency = parse_currency(trans_cur)
        foreign = orig_currency != currency
        txn_type = _classify(description, amount)
        trans_date = parse_date(tdate)
        # "0004/0007" = physical card / Apple Pay device number. The spending belongs to the
        # physical card; the device number is not kept (docs/banks/ccb.md §5).
        m = CARD_CELL_RE.fullmatch(last4)
        if last4 and not m:
            raise FormatError(f"unexpected card number {last4!r}")
        last4 = m.group(1) if m else ""
        return Transaction(
            line_no=line_no,
            txn_id="",  # set in _with_account once the account is known
            trans_date=trans_date,
            post_date=parse_date(pdate) if pdate else None,
            txn_type=txn_type,
            amount=amount,
            currency=currency,
            orig_amount=abs(parse_amount(trans_amt)) if foreign else None,
            orig_currency=orig_currency if foreign else None,
            description_raw=description,
            merchant=description if txn_type == TxnType.PURCHASE else None,
            card_last4=last4 or None,
        )

    @staticmethod
    def _with_account(transactions: list[Transaction], account_id: str) -> list[Transaction]:
        return [
            t.model_copy(
                update={
                    "txn_id": make_txn_id(
                        "CCB", account_id, t.trans_date, t.amount, t.description_raw, t.line_no
                    )
                }
            )
            for t in transactions
        ]


def _classify(description: str, amount: Decimal) -> TxnType:
    """CCB prints no groups, so the type comes from sign and keywords. Repayments, purchases
    and rebates are confirmed by samples; the rest is inferred (docs/banks/ccb.md §5)."""
    if amount < 0:
        if "还款" in description:
            return TxnType.REPAYMENT
        if REBATE_RE.search(description):
            return TxnType.REBATE
        return TxnType.REFUND
    if "年费" in description or "手续费" in description:
        return TxnType.FEE
    if "利息" in description:
        return TxnType.INTEREST
    if "取现" in description:
        return TxnType.CASH
    if "分期" in description:
        return TxnType.INSTALLMENT
    return TxnType.PURCHASE


def _is_currency(text: str) -> bool:
    try:
        parse_currency(text)
    except CurrencyError:
        return False
    return True


def _rows(html: str) -> Iterator[list[str]]:
    """Every table row in document order, as normalised cell texts. Rows whose cells hold
    nested tables are containers and are skipped (their inner rows come separately)."""
    soup = BeautifulSoup(html, "lxml")
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells or any(c.find("table") for c in cells):
            continue
        texts = [normalize_ws(c.get_text()) for c in cells]
        if any(texts):
            yield texts  # empty cells are kept: positions matter (e.g. a blank card number)
