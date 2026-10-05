"""Industrial and Commercial Bank of China (ICBC) HTML e-statement parser.

Format spec: docs/banks/icbc.md. Like CCB's, the statement is nested tables: every table
row is flattened in document order (util.table_rows) and the sections are found by their
Chinese titles. ICBC prints those with a space between the characters ("本 期 交 易 汇 总"),
so titles are compared with the spaces taken out.

One e-mail is one account, which may hold several cards, and each currency is an account
of its own: 人民币, 美元, 港币, 澳大利亚元 (docs/banks/icbc.md §4). ICBC prints balances
with the opposite sign to ours (a debt is negative) and gives each transaction a direction
instead of a sign ("99.20/RMB(支出)"); both are converted here. Purchases, refunds, rebates
and repayments are confirmed by the author's statements; fees, interest, cash advances
and instalments are inferred (§5).
"""

from __future__ import annotations

import re
from decimal import Decimal

from bs4 import BeautifulSoup

from autobill.fetch.message import RawMessage
from autobill.model import ZERO, Bill, BillBalance, Transaction, TxnType, make_txn_id
from autobill.parse.base import BaseParser, TemplateChanged
from autobill.parse.util import (
    FormatError,
    normalize_ws,
    parse_amount_currency,
    parse_currency,
    parse_date,
    table_rows,
)
from autobill.reconcile import reconcile

SENDER = "webmaster@icbc.com.cn"
SUBJECT = "中国工商银行客户对账单"
BODY_MARKERS = ("信用卡对账单", "本期交易汇总", "交易明细")  # compared without spaces

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LAST4_RE = re.compile(r"^\d{4}$")
ACCOUNT_RE = re.compile(r"^(\d{4})(?:\(.*\))?$")  # "0009" or "0009(牡丹贷记卡)"
POSTED_RE = re.compile(r"^(?P<amount>.+)\((?P<direction>支出|存入)\)$")  # "99.20/RMB(支出)"
_CN_DATE = r"(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"
PERIOD_RE = re.compile(r"账单周期\s*" + _CN_DATE + r"\s*[—–-]+\s*" + _CN_DATE)
STATEMENT_DATE_RE = re.compile(r"对账单生成日\s*" + _CN_DATE)
DUE_RE = re.compile(r"贷记卡到期还款日\s*" + _CN_DATE)
# A Visa dining rebate is printed as 境外退货 from "ICBCVisaDiningRebate"; 刷卡金 is a reward.
REBATE_RE = re.compile(r"rebate|cashback|返现|刷卡金", re.IGNORECASE)
UNKNOWN = "unknown"


class IcbcHtmlParser(BaseParser):
    bank = "ICBC"
    name = "icbc_html"
    version = 1

    def matches(self, msg: RawMessage) -> bool:
        from_bank = msg.from_addr == SENDER or SUBJECT in msg.subject
        if not from_bank:
            return False
        body = _squash(msg.html)
        return all(marker in body for marker in BODY_MARKERS)

    def parse(self, msg: RawMessage) -> list[Bill]:
        if not msg.html:
            raise TemplateChanged("ICBC: e-mail has no HTML body")
        return [reconcile(_Statement(msg).build())]


class _Statement:
    def __init__(self, msg: RawMessage) -> None:
        self.msg = msg
        self.warnings: list[str] = []
        self.due: dict[str, tuple[Decimal, Decimal]] = {}  # currency -> (应还款额, 最低还款额)
        self.summary_seen = False
        self.summary: dict[str, list[Decimal]] = {}  # currency -> [上期, 收入, 支出, 本期余额]
        self.accounts: list[str] = []  # the account's card number, as each table gives it
        self.txn_rows: list[list[str]] = []

    def build(self) -> Bill:
        self._walk()
        statement_date, period, due_date = self._dates()
        transactions = self._transactions()
        if not self.summary_seen:
            raise TemplateChanged("ICBC: summary table (本期交易汇总) not found")
        if not self.summary and not transactions:
            raise TemplateChanged("ICBC: neither summary rows nor transactions found")
        balances = self._balances(transactions)
        account = self._account(transactions)
        cards = sorted({t.card_last4 for t in transactions if t.card_last4} | {account} - {UNKNOWN})
        account_id = f"ICBC:{account}"
        email_date = self.msg.email_date
        if email_date is None:
            email_date = statement_date
            self.warnings.append("邮件没有 Date 头，汇率日期改用账单日")
        return Bill(
            bank="ICBC",
            account_id=account_id,
            cards=cards,
            statement_date=statement_date,
            period_start=period[0] if period else None,
            period_end=period[1] if period else None,
            due_date=due_date,
            email_date=email_date,
            balances=balances,
            transactions=_with_account(transactions, account_id),
            status="OK",  # decided by reconcile()
            warnings=self.warnings,
            source_message_id=self.msg.message_id,
            source_sha256=self.msg.sha256,
            parser_name=IcbcHtmlParser.name,
            parser_version=IcbcHtmlParser.version,
        )

    # --- walking the document ---------------------------------------------

    def _walk(self) -> None:
        mode: str | None = None  # "due" / "summary" / "txn"
        in_total = False  # inside 需还款明细's 合计 block, which repeats the account's rows
        for cells in table_rows(self.msg.html):
            title = _squash("".join(cells))
            if title.startswith("需还款明细"):
                mode, in_total = "due", False
            elif title.startswith("本期交易汇总"):
                mode, self.summary_seen = "summary", True
            elif len(cells) == 1 and title.endswith("交易明细"):
                mode = "txn"  # "人民币(本位币) 交 易 明 细", "外 币 交 易 明 细"
            elif title.startswith(("工银i豆", "积分信息", "温馨提示")):
                mode = None  # points (积分信息 before mid-2025, then 工银i豆) and the footer
            elif mode == "due":
                in_total = self._due_row(cells, in_total)
            elif mode == "summary":
                self._summary_row(cells)
            elif mode == "txn":
                self._txn_row(cells)

    def _due_row(self, cells: list[str], in_total: bool) -> bool:
        """需还款明细: one row per currency. The first carries the card number
        ("0009(牡丹贷记卡) | 人民币(本位币) | 应还 | 最低 | 额度"); the others only the currency,
        as the card cell spans them. Returns whether the 合计 block has started."""
        if cells[0] == "卡号后四位":
            return in_total
        if in_total or cells[0].startswith("合计"):
            return True
        if len(cells) == 5:
            m = ACCOUNT_RE.match(cells[0])
            if m:
                self.accounts.append(m.group(1))
            else:
                self.warnings.append(f"需还款明细里认不出卡号：{cells[0]}")
            cells = cells[1:]
        if len(cells) != 4:
            self.warnings.append(f"需还款明细里无法识别的行：{' | '.join(cells)}")
            return in_total
        due, currency = _amount(cells[1])
        minimum, _ = _amount(cells[2])
        if parse_currency(cells[0]) != currency:
            self.warnings.append(f"需还款明细的币种 {cells[0]} 与金额 {cells[1]} 不一致")
        self.due[currency] = (due, minimum)
        return in_total

    def _summary_row(self, cells: list[str]) -> None:
        """本期交易汇总: a "---美元---" separator per currency, then the account's row and a
        合计 row: 卡号后四位 | 上期余额 | 本期收入 | 本期支出 | 本期余额."""
        if len(cells) == 1 and cells[0].startswith("---"):
            return
        if cells[0] == "卡号后四位" or cells[0].startswith("合计"):
            return
        m = ACCOUNT_RE.match(cells[0])
        if len(cells) != 5 or not m:
            self.warnings.append(f"本期交易汇总里无法识别的行：{' | '.join(cells)}")
            return
        values = [_amount(c) for c in cells[1:]]
        currencies = {currency for _, currency in values}
        if len(currencies) != 1:
            raise TemplateChanged(f"ICBC: one summary row in several currencies: {cells}")
        currency = currencies.pop()
        amounts = [value for value, _ in values]
        if currency in self.summary:  # a second account in the same currency: add it up
            amounts = [a + b for a, b in zip(self.summary[currency], amounts, strict=True)]
        self.summary[currency] = amounts
        self.accounts.append(m.group(1))

    def _txn_row(self, cells: list[str]) -> None:
        if len(cells) == 1 or cells[0] == "卡号后四位":
            return  # "---主卡明细---", the foreign currencies' names (港币, 美元 …), headers
        if len(cells) == 7 and DATE_RE.match(cells[1]):
            self.txn_rows.append(cells)
        else:
            self.warnings.append(f"无法识别的明细行：{' | '.join(c for c in cells if c)}")

    def _dates(self) -> tuple:
        text = normalize_ws(BeautifulSoup(self.msg.html, "lxml").get_text(" "))
        m = PERIOD_RE.search(text)
        period = (parse_date(m.group(1)), parse_date(m.group(2))) if m else None
        m = STATEMENT_DATE_RE.search(text)
        statement_date = parse_date(m.group(1)) if m else period[1] if period else None
        if statement_date is None:
            raise TemplateChanged("ICBC: neither statement date (对账单生成日) nor cycle found")
        # The due date sits in a container row (its label in a nested table), so it is
        # read from the text rather than from the rows (docs/banks/icbc.md §3).
        m = DUE_RE.search(text)
        return statement_date, period, parse_date(m.group(1)) if m else None

    # --- building the model -------------------------------------------------

    def _account(self, transactions: list[Transaction]) -> str:
        accounts = list(dict.fromkeys(self.accounts))
        if len(accounts) > 1:
            self.warnings.append(f"这份账单里有多个账户 {accounts}，账户取 {accounts[0]}")
        if accounts:
            return accounts[0]
        # A closing statement prints no summary rows: the card on its rows is the account.
        cards = sorted({t.card_last4 for t in transactions if t.card_last4})
        if len(cards) == 1:
            return cards[0]
        self.warnings.append(f"认不出账户（流水里的卡号：{cards or '无'}），账户记为 ICBC:unknown")
        return UNKNOWN

    def _balances(self, transactions: list[Transaction]) -> list[BillBalance]:
        currencies = list(self.summary)
        for t in transactions:
            if t.currency not in currencies:
                currencies.append(t.currency)
        balances = []
        for currency in currencies:
            if currency not in self.summary and any(
                t.amount for t in transactions if t.currency == currency
            ):
                self.warnings.append(f"{currency} 有流水，但本期交易汇总里没有这个币种")
            previous, credits, debits, balance = self.summary.get(currency, [ZERO] * 4)
            # ICBC counts an overpayment moved out (网转) in 本期支出. Here it is an
            # adjustment, so it moves from the debits to `adjustments` (docs/banks/icbc.md §4).
            adjusted = [
                t.amount
                for t in transactions
                if t.currency == currency and t.txn_type == TxnType.ADJUSTMENT
            ]
            moved_out = sum((a for a in adjusted if a > 0), ZERO)
            moved_in = sum((-a for a in adjusted if a < 0), ZERO)
            # ICBC: a debt is negative, an overpayment positive (the opposite of ours).
            owed = max(-balance, ZERO)
            due = self.due.get(currency)
            if due is not None and due[0] != owed:
                self.warnings.append(
                    f"{currency} 需还款明细的应还款额 {due[0]} 与本期交易汇总不一致（应为 {owed}）"
                )
            balances.append(
                BillBalance(
                    currency=currency,
                    previous_balance=max(-previous, ZERO),
                    previous_deposit=max(previous, ZERO),
                    new_charges=debits - moved_out,
                    payments_credits=credits - moved_in,
                    adjustments=moved_out - moved_in,
                    amount_due=owed,
                    deposit=max(balance, ZERO),
                    min_payment=due[1] if due else None,
                )
            )
        for currency in self.due.keys() - set(currencies):
            self.warnings.append(f"{currency} 只出现在需还款明细里，本期交易汇总没有")
        return balances

    def _transactions(self) -> list[Transaction]:
        transactions = []
        for line_no, cells in enumerate(self.txn_rows, start=1):
            try:
                transactions.append(self._transaction(line_no, cells))
            except FormatError as exc:
                self.warnings.append(f"明细行解析失败（{exc}）：{' | '.join(cells)}")
        return transactions

    def _transaction(self, line_no: int, cells: list[str]) -> Transaction:
        card, tdate, pdate, kind, description, original, posted = cells
        m = POSTED_RE.match(posted)
        if not m:
            raise FormatError(f"posted amount without 支出/存入: {posted!r}")
        amount, currency = _amount(m["amount"])
        debit = m["direction"] == "支出"
        orig_amount, orig_currency = _amount(original)
        foreign = orig_currency != currency
        txn_type = self._classify(kind, description, debit)
        if card and not LAST4_RE.match(card):
            raise FormatError(f"unexpected card number {card!r}")
        return Transaction(
            line_no=line_no,
            txn_id="",  # set in _with_account once the account is known
            trans_date=parse_date(tdate),
            post_date=parse_date(pdate) if pdate else None,
            txn_type=txn_type,
            amount=amount if debit or not amount else -amount,  # no "-0.00"
            currency=currency,
            orig_amount=abs(orig_amount) if foreign else None,
            orig_currency=orig_currency if foreign else None,
            description_raw=description,
            group_raw=kind,
            merchant=description if txn_type == TxnType.PURCHASE else None,
            card_last4=card or None,
        )

    def _classify(self, kind: str, description: str, debit: bool) -> TxnType:
        """The bank's own type (交易类型) and the direction decide (docs/banks/icbc.md §5)."""
        if "减免" in kind or "销户" in kind:
            return TxnType.ADJUSTMENT  # 年费减免, 预约销户: notes of 0.00
        if debit:
            if "年费" in kind or "手续费" in kind:
                return TxnType.FEE
            if "利息" in kind:
                return TxnType.INTEREST
            if "取现" in kind:
                return TxnType.CASH
            if "分期" in kind:
                return TxnType.INSTALLMENT
            if "消费" in kind:
                return TxnType.PURCHASE
            if "转" in kind:
                return TxnType.ADJUSTMENT  # an overpayment moved out (网转): not spending
            self.warnings.append(f"不认识的交易类型 {kind}，按消费记：{description}")
            return TxnType.PURCHASE
        if REBATE_RE.search(kind) or REBATE_RE.search(description):
            return TxnType.REBATE
        if "退" in kind:
            return TxnType.REFUND
        if "还款" in kind or "转" in kind or "入账" in kind:
            return TxnType.REPAYMENT  # 转账, 转帐, 网转, 银联入账, 财付通信用卡还款
        self.warnings.append(f"不认识的交易类型 {kind}，按退款记：{description}")
        return TxnType.REFUND


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _amount(text: str) -> tuple[Decimal, str]:
    """ "1,424.08/AUD" -> (Decimal("1424.08"), "AUD"); ICBC writes the yuan as RMB."""
    value, code = parse_amount_currency(text)
    return value, parse_currency(code)


def _with_account(transactions: list[Transaction], account_id: str) -> list[Transaction]:
    return [
        t.model_copy(
            update={
                "txn_id": make_txn_id(
                    "ICBC", account_id, t.trans_date, t.amount, t.description_raw, t.line_no
                )
            }
        )
        for t in transactions
    ]
