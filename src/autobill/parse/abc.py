"""Agricultural Bank of China (ABC) HTML e-statement parser.

Format spec: docs/banks/abc.md. The body is ~37 leaf <table>s in document order with no
ids or classes, so the parser walks their rows as a state machine keyed on the Chinese
section titles, never on positions or English headers.

Sign conventions inside one ABC e-mail (all converted to ours, docs/data-model.md):
  * account information: debt is negative   -> only used as a cross-check
  * summary block ("账务说明"): all positive  -> BillBalance as is, adjustments negated
  * transactions: spending is negative        -> amount = -value
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

from bs4 import BeautifulSoup

from autobill.fetch.message import RawMessage
from autobill.model import Bill, BillBalance, Transaction, TxnType, make_txn_id
from autobill.parse.base import BaseParser, TemplateChanged
from autobill.parse.util import (
    FormatError,
    normalize_ws,
    parse_amount,
    parse_amount_currency,
    parse_currency,
    parse_date,
)
from autobill.reconcile import reconcile

SENDER_DOMAIN = "creditcard.abchina.com.cn"
SUBJECT = "中国农业银行金穗信用卡电子对账单"
BODY_MARKERS = ("您的信用卡账户信息", "账务说明", "交易明细")

SECTION_TITLES = {
    "安全用卡提示",
    "您的信用卡账户信息",
    "额度信息",
    "账务说明",
    "交易明细",
    "分期记录",
    "分期信息提示",
    "积分统计",
    "刷卡金统计",
    "温馨提示",
}
# Keywords that must appear, in this order, in the summary header row.
SUMMARY_HEADER = [
    "币种",
    "本期应还金额",
    "溢缴款",
    "上期账单",
    "溢缴款",
    "本期账单金额",
    "本期还款",
    "本期调整金额",
]

TXN_DATE_RE = re.compile(r"^\d{6}$")
FX_RATE_RE = re.compile(r"汇率[:：]\s*(\d+(?:\.\d+)?)")
INSTALLMENT_RE = re.compile(r"第\s*(\d+/\d+)\s*期")
# ABC overseas purchases: "境外消费" + 2 spaces + 23-char merchant + 13-char city + country.
OVERSEAS_PREFIX = "境外消费"
ONLINE_PREFIX = "网上消费"
MERCHANT_WIDTH, CITY_WIDTH = 23, 13


@dataclass
class _Row:
    cells: list[str]  # raw cell text, internal spacing kept (fixed-width descriptions)

    @property
    def norm(self) -> list[str]:
        return [normalize_ws(c) for c in self.cells]

    @property
    def filled(self) -> list[str]:
        return [c for c in self.norm if c]


class AbcHtmlParser(BaseParser):
    bank = "ABC"
    name = "abc_html"
    version = 1

    def matches(self, msg: RawMessage) -> bool:
        from_bank = msg.from_addr.endswith("@" + SENDER_DOMAIN) or SUBJECT in msg.subject
        return from_bank and all(marker in msg.html for marker in BODY_MARKERS)

    def parse(self, msg: RawMessage) -> list[Bill]:
        if not msg.html:
            raise TemplateChanged("ABC: e-mail has no HTML body")
        return [reconcile(_Statement(msg).build())]


class _Statement:
    """Collects the sections of one ABC e-mail, then builds the Bill."""

    def __init__(self, msg: RawMessage) -> None:
        self.msg = msg
        self.warnings: list[str] = []
        self.card_last4: str | None = None
        self.period: tuple | None = None
        self.due_date = None
        self.info_balance: dict[str, Decimal] = {}  # 本期应还款额 (debt negative)
        self.min_payment: dict[str, Decimal] = {}
        self.summary_header_seen = False
        self.summary_rows: list[list[str]] = []
        self.txn_header_seen = False
        self.txn_rows: list[tuple[str | None, _Row]] = []  # (group, row)
        self.sections_seen: set[str] = set()

    # --- walking the document ---------------------------------------------

    def build(self) -> Bill:
        self._walk()
        self._require()
        balances = self._balances()
        self._cross_check(balances)
        transactions = self._transactions()
        email_date = self.msg.email_date
        if email_date is None:
            email_date = self.period[1]
            self.warnings.append("邮件没有 Date 头，汇率日期改用账单日")
        return Bill(
            bank="ABC",
            account_id=f"ABC:{self.card_last4}",
            cards=[self.card_last4],
            statement_date=self.period[1],  # ABC prints no statement date: period end
            period_start=self.period[0],
            period_end=self.period[1],
            due_date=self.due_date,
            email_date=email_date,
            balances=balances,
            transactions=transactions,
            status="OK",  # decided by reconcile()
            warnings=self.warnings,
            source_message_id=self.msg.message_id,
            source_sha256=self.msg.sha256,
            parser_name=AbcHtmlParser.name,
            parser_version=AbcHtmlParser.version,
        )

    def _walk(self) -> None:
        section: str | None = None
        info_sub: str | None = None
        group: str | None = None
        for row in _rows(self.msg.html):
            filled = row.filled
            if not filled:
                continue
            if filled[0] in SECTION_TITLES and len(filled) <= 2:
                section = filled[0]
                self.sections_seen.add(section)
                continue
            if section == "您的信用卡账户信息":
                info_sub = self._info_row(row, info_sub)
            elif section == "账务说明":
                self._summary_row(row)
            elif section == "交易明细":
                group = self._txn_row(row, group)

    def _info_row(self, row: _Row, sub: str | None) -> str | None:
        label, *rest = row.norm
        value = rest[0] if rest else ""
        if label.startswith("卡号"):
            m = re.search(r"(\d{4})$", value)
            if not m:
                raise TemplateChanged(f"ABC: card number not found in {value!r}")
            self.card_last4 = m.group(1)
        elif label.startswith("账单周期"):
            start, sep, end = value.partition("-")
            if not sep:
                raise TemplateChanged(f"ABC: unexpected statement cycle {value!r}")
            self.period = (parse_date(start), parse_date(end))
        elif label.startswith("到期还款日"):
            self.due_date = parse_date(value) if value else None
        elif label.startswith("本期应还款额"):
            return "balance"
        elif label.startswith("最低还款额"):
            return "min"
        elif sub in ("balance", "min") and value:
            currency = parse_currency(label)
            amount = parse_amount(value)
            if sub == "balance":
                self.info_balance[currency] = amount
            else:
                self.min_payment[currency] = abs(amount)
        return sub

    def _summary_row(self, row: _Row) -> None:
        cells = row.norm
        if cells[0].startswith("币种"):
            header = " ".join(cells)
            pos = 0
            for keyword in SUMMARY_HEADER:
                pos = header.find(keyword, pos)
                if pos < 0:
                    raise TemplateChanged(f"ABC: summary header lacks {keyword!r}")
                pos += len(keyword)
            self.summary_header_seen = True
        elif len(cells) == 8:
            self.summary_rows.append(cells)
        else:
            raise TemplateChanged(f"ABC: unexpected summary row with {len(cells)} cells")

    def _txn_row(self, row: _Row, group: str | None) -> str | None:
        cells = row.norm
        if cells[0].startswith("交易日期"):
            self.txn_header_seen = True
        elif len(cells) == 3 and cells[1] == "●":
            return cells[2]
        elif len(cells) == 6 and TXN_DATE_RE.match(cells[0]):
            self.txn_rows.append((group, row))
        else:
            self.warnings.append(f"无法识别的明细行：{' | '.join(row.filled)}")
        return group

    def _require(self) -> None:
        missing = [
            t for t in ("您的信用卡账户信息", "账务说明", "交易明细") if t not in self.sections_seen
        ]
        if missing:
            raise TemplateChanged(f"ABC: section(s) not found: {missing}")
        if self.card_last4 is None or self.period is None:
            raise TemplateChanged("ABC: card number or statement cycle not found")
        if not self.summary_header_seen or not self.summary_rows:
            raise TemplateChanged("ABC: summary block (账务说明) is empty")
        if not self.txn_header_seen:
            raise TemplateChanged("ABC: transaction table header (交易日期) not found")

    # --- building the model -------------------------------------------------

    def _balances(self) -> list[BillBalance]:
        balances = []
        for cells in self.summary_rows:
            currency = parse_currency(cells[0])
            due, deposit, prev_due, prev_deposit, charges, credits, adjust = (
                parse_amount(c) for c in cells[1:]
            )
            balances.append(
                BillBalance(
                    currency=currency,
                    previous_balance=prev_due,
                    previous_deposit=prev_deposit,
                    new_charges=charges,
                    payments_credits=credits,
                    adjustments=-adjust,  # ABC adjustments reduce debt
                    amount_due=due,
                    deposit=deposit,
                    min_payment=self.min_payment.get(currency),
                )
            )
        return balances

    def _cross_check(self, balances: list[BillBalance]) -> None:
        """The account-info block shows 本期应还款额 with debt negative. Only compare the
        cases the samples confirm: something due, or nothing due and no deposit."""
        for b in balances:
            shown = self.info_balance.get(b.currency)
            if shown is None:
                continue
            if b.amount_due > 0 and shown != -b.amount_due:
                expected = -b.amount_due
            elif b.amount_due == 0 and b.deposit == 0 and shown != 0:
                expected = Decimal("0")
            else:
                continue
            self.warnings.append(
                f"{b.currency} 账户信息区本期应还款额 {shown} 与账务说明不一致（应为 {expected}）"
            )

    def _transactions(self) -> list[Transaction]:
        transactions = []
        for line_no, (group, row) in enumerate(self.txn_rows, start=1):
            try:
                transactions.append(self._transaction(line_no, group, row))
            except FormatError as exc:
                self.warnings.append(f"明细行解析失败（{exc}）：{' | '.join(row.filled)}")
        return transactions

    def _transaction(self, line_no: int, group: str | None, row: _Row) -> Transaction:
        tdate, pdate, last4, _, orig_cell, sett_cell = row.norm
        raw_description = row.cells[3].strip("\r\n")
        description = normalize_ws(raw_description)
        orig_amount, orig_currency = parse_amount_currency(orig_cell)
        value, currency = parse_amount_currency(sett_cell)
        amount = -value  # ABC: spending is negative
        txn_type, fx_rate, installment = self._classify(group, description)
        merchant, location = (
            _merchant(raw_description) if txn_type == TxnType.PURCHASE else (None, None)
        )
        trans_date = parse_date(tdate)
        foreign = orig_currency != currency
        return Transaction(
            line_no=line_no,
            txn_id=make_txn_id(
                "ABC", f"ABC:{self.card_last4}", trans_date, amount, description, line_no
            ),
            trans_date=trans_date,
            post_date=parse_date(pdate) if pdate else None,
            txn_type=txn_type,
            amount=amount,
            currency=currency,
            orig_amount=orig_amount if foreign else None,
            orig_currency=orig_currency if foreign else None,
            fx_rate=fx_rate,
            description_raw=description,
            group_raw=group,
            merchant=merchant,
            merchant_location=location,
            card_last4=last4 or None,
            installment=installment,
        )

    def _classify(self, group: str | None, text: str) -> tuple[TxnType, Decimal | None, str | None]:
        """Map (group, description) to a transaction type; see docs/banks/abc.md §6."""
        m = INSTALLMENT_RE.search(text)
        installment = m.group(1) if m else None
        if group == "还款":
            if "自动购汇" in text:
                rate = FX_RATE_RE.search(text)
                return TxnType.FX_TRANSFER, Decimal(rate.group(1)) if rate else None, None
            return TxnType.REPAYMENT, None, None
        if group == "消费":
            return TxnType.PURCHASE, None, None
        if group == "分期" and "分期利息" in text:
            return TxnType.INTEREST, None, installment
        if group == "分期" and "分期本金" in text:
            return TxnType.INSTALLMENT, None, installment
        if group == "其他" and "返现" in text:
            return TxnType.REBATE, None, None
        if group == "取现/转出" and "取现" in text:
            return TxnType.CASH, None, None
        if group == "利息":
            return TxnType.INTEREST, None, None
        self.warnings.append(f"未知的交易类型（分组 {group}）：{text}，暂记为调整")
        return TxnType.ADJUSTMENT, None, installment


def _rows(html: str) -> Iterator[_Row]:
    """Rows of all leaf tables (tables without nested tables), in document order."""
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        if table.find("table"):
            continue
        for tr in table.find_all("tr"):
            yield _Row([td.get_text() for td in tr.find_all(["td", "th"])])


def _merchant(raw: str) -> tuple[str | None, str | None]:
    """(merchant, location) from a purchase description; see docs/banks/abc.md §6."""
    text = raw.strip("\r\n")
    if text.startswith(OVERSEAS_PREFIX):
        rest = text[len(OVERSEAS_PREFIX) :]
        rest = rest[2:] if rest.startswith("  ") else rest.lstrip()
        if len(rest.rstrip()) > MERCHANT_WIDTH + CITY_WIDTH:
            # VISA: fixed-width merchant + city + country; merchant may run into the city.
            merchant = normalize_ws(rest[:MERCHANT_WIDTH])
            location = normalize_ws(rest[MERCHANT_WIDTH:])
            return merchant or None, location or None
        # Mastercard: "PlusFitness SYDNEY     AUS" — name and place are not separable.
        m = re.match(r"^(.*?)\s*\b([A-Z]{2,3})$", rest.strip())
        if m:
            return normalize_ws(m.group(1)) or None, m.group(2)
        return normalize_ws(rest) or None, None
    if text.startswith(ONLINE_PREFIX):
        return normalize_ws(text[len(ONLINE_PREFIX) :]) or None, None
    return normalize_ws(text) or None, None
