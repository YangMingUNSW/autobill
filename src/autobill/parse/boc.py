"""Bank of China (BOC) combined e-statement parser (PDF attachment).

Format spec: docs/banks/boc.md. A PDF has no table structure, only words with
coordinates, so parsing is split in two layers:

1. `pdf_lines()` turns the PDF into lines of positioned words: pdfplumber with
   `dedupe_chars()` (the English headers are drawn twice as fake bold), pages stacked on
   one vertical axis, page footers dropped.
2. `parse_lines()` reads the statement from those lines alone, using the Chinese labels
   and the x position of each column. Tests feed it hand-built lines, so no PDF needs
   to be generated for them.

One e-mail can hold several cards ("合并账单"): each card section (`…(卡号：0005)`) has
its own account table and transaction list and becomes its own Bill. Every row in a
section belongs to that section's card; other card numbers there are Apple Pay device
numbers (confirmed by the card holder) and are not kept.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pdfplumber

from autobill.fetch.message import RawMessage
from autobill.model import ZERO, Bill, BillBalance, Transaction, TxnType, make_txn_id
from autobill.parse.base import BaseParser, TemplateChanged
from autobill.parse.util import (
    Direction,
    FormatError,
    parse_amount,
    parse_currency,
    parse_currency_amount,
    parse_date,
    parse_directed_amount,
)
from autobill.reconcile import reconcile

SENDER = "boczhangdan@bankofchina.com"
SUBJECT = "中国银行信用卡电子账单"
BODY_MARKERS = ("账单合并为pdf模板", "电子合并账单")
PASSWORD_ENV = "AUTOBILL_BOC_PDF_PASSWORD"  # optional; the samples are not encrypted

PAGE_STRIDE = 10_000  # pages are stacked on one y axis: y = page_index * stride + top
LINE_TOLERANCE = 2.0  # words whose tops differ by less than this share a line
MAX_SATELLITE_GAP = 25.0  # a wrapped description line lies this close to its date line

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^-?[\d,]+\.\d{2}$")
CARD_RE = re.compile(r"^\d{4}$")
MASKED_CARD_RE = re.compile(r"\d{4} \d{4} \*{4} (\d{4})")
SECTION_RE = re.compile(r"\(卡号[:：](\d{4})\)")
FOOTER_RE = re.compile(r"^第[\s\d]+页/共[\s\d]+页$")  # "第 10 页/共1 2 页" happens too
ACCOUNT_ROW_RE = re.compile(r"^(人民币|外币)/")
DETAIL_CURRENCY_RE = re.compile(r"\(([A-Z]{3})\)")

OVERVIEW_LABELS = ["到期还款日", "账单日", "本期人民币欠款总计", "本期外币欠款总计"]
ACCOUNT_LABELS = {
    "账号类型": "type",
    "上期存款/欠款余额": "previous",
    "本期支出金额": "charges",
    "本期存入金额": "credits",
    "本期存款/欠款余额": "new",
    "账单可分期金额": "installable",
}
# Merchant names end with an ISO alpha-3 country code glued on ("Kelly's On KingAUS").
COUNTRY_SUFFIXES = set(
    "AUS CHN JPN USA SGP HKG MAC TWN KOR THA MYS NZL GBR FRA DEU ITA ESP CHE NLD CAN IDN "
    "VNM PHL ARE".split()
)
REPAYMENT_RE = re.compile(r"还款|BOCNET|转账|银联入账", re.IGNORECASE)
# "VISA BOC ZJ1PCT REBATESGP", "…返消费金1%活动", "中行银联境外消费阶梯返活动"
REBATE_RE = re.compile(r"返现|返消费金|阶梯返|REBA|CASHBACK", re.IGNORECASE)


# --- layer 1: PDF -> lines ------------------------------------------------------


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    y: float  # global: page_index * PAGE_STRIDE + top

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Line:
    y: float
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def pdf_lines(data: bytes, password: str | None = None) -> list[Line]:
    lines: list[Line] = []
    with pdfplumber.open(io.BytesIO(data), password=password or "") as pdf:
        for index, page in enumerate(pdf.pages):
            raw = page.dedupe_chars().extract_words()
            words = sorted(
                (Word(w["text"], w["x0"], w["x1"], index * PAGE_STRIDE + w["top"]) for w in raw),
                key=lambda w: (w.y, w.x0),
            )
            page_lines: list[Line] = []
            for word in words:
                if page_lines and word.y - page_lines[-1].y < LINE_TOLERANCE:
                    page_lines[-1].words.append(word)
                else:
                    page_lines.append(Line(word.y, [word]))
            for line in page_lines:
                line.words.sort(key=lambda w: w.x0)
                if not FOOTER_RE.match(line.text):
                    lines.append(line)
    return lines


# --- layer 2: lines -> bills ----------------------------------------------------


@dataclass
class _Section:
    card: str
    account_cols: dict[str, float] = field(default_factory=dict)
    account_rows: list[Line] = field(default_factory=list)
    detail_currency: str | None = None
    detail_cols: dict[str, float] = field(default_factory=dict)
    detail_lines: list[Line] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Parsed:
    statement_date: date | None = None
    due_date: date | None = None
    totals: dict[str, Decimal] = field(default_factory=dict)  # 本期人民币/外币欠款总计
    card_due: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    card_min: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    sections: list[_Section] = field(default_factory=list)


class BocPdfParser(BaseParser):
    bank = "BOC"
    name = "boc_pdf"
    version = 1

    def matches(self, msg: RawMessage) -> bool:
        from_bank = msg.from_addr == SENDER or SUBJECT in msg.subject
        return (
            from_bank
            and any(marker in msg.html for marker in BODY_MARKERS)
            and any(a.is_pdf for a in msg.attachments)
        )

    def parse(self, msg: RawMessage) -> list[Bill]:
        pdfs = [a for a in msg.attachments if a.is_pdf]
        if not pdfs:
            raise TemplateChanged("BOC: no PDF attachment")
        lines = [line for a in pdfs for line in pdf_lines(a.data, os.environ.get(PASSWORD_ENV))]
        return parse_lines(lines, msg)


def parse_lines(lines: list[Line], msg: RawMessage) -> list[Bill]:
    parsed = _read(lines)
    if parsed.statement_date is None:
        raise TemplateChanged("BOC: account summary (账单信息总览 / 账单日) not found")
    if not parsed.sections:
        raise TemplateChanged("BOC: no card section (…(卡号：XXXX)) found")
    bills = [reconcile(_bill(section, parsed, msg)) for section in parsed.sections]
    _check_totals(parsed, bills)
    return bills


def _read(lines: list[Line]) -> _Parsed:
    """One pass over the lines, collecting each block by its Chinese label."""
    parsed = _Parsed()
    state: str | None = None
    overview_cols: dict[str, float] = {}
    card_cols: list[float] = []
    section: _Section | None = None
    for line in lines:
        text = line.text
        if m := SECTION_RE.search(text):
            section = _Section(card=m.group(1))
            parsed.sections.append(section)
            state = "section"
            continue
        if "积分奖励计划" in text:
            state = None
            continue
        if all(label in text for label in OVERVIEW_LABELS[:2]):
            overview_cols = {w.text: w.xc for w in line.words if w.text in OVERVIEW_LABELS}
            state = "overview"
            continue
        if state == "overview" and any(
            DATE_RE.match(w.text) or AMOUNT_RE.match(w.text) for w in line.words
        ):
            _read_overview(line, overview_cols, parsed)
            state = None
            continue
        if text.startswith("卡号") and "本期应还款额" in text:
            state = "card_header"
            continue
        if state in ("card_header", "cards"):
            if "人民币RMB" in text:
                card_cols = [w.xc for w in line.words if w.text in ("人民币RMB", "外币FCY")]
                continue
            if m := MASKED_CARD_RE.search(text):
                _read_card_row(line, m.group(1), card_cols, parsed)
                state = "cards"
                continue
        if section is None:
            continue
        for w in line.words:
            if w.text in ACCOUNT_LABELS:
                section.account_cols[ACCOUNT_LABELS[w.text]] = w.xc
        if line.words and ACCOUNT_ROW_RE.match(line.words[0].text):
            section.account_rows.append(line)
            continue
        if "交易明细" in text:
            m = DETAIL_CURRENCY_RE.search(text)
            section.detail_currency = m.group(1) if m else ("CNY" if "人民币" in text else None)
            state = "detail_header"
            continue
        if state == "detail_header":
            for w in line.words:
                if w.text in ("存入", "支出", "卡号后四位", "交易描述"):
                    section.detail_cols[w.text] = w.xc
            if "Number" in text:  # last header line: "of Card Number"
                state = "detail"
            continue
        if state == "detail" and not text.startswith("参考汇率"):
            section.detail_lines.append(line)
    return parsed


def _nearest(x: float, columns: dict[str, float]) -> str:
    return min(columns, key=lambda name: abs(columns[name] - x))


def _read_overview(line: Line, cols: dict[str, float], parsed: _Parsed) -> None:
    """Values under 到期还款日 | 账单日 | 本期人民币欠款总计 | 本期外币欠款总计."""
    if "账单日" not in cols:
        raise TemplateChanged("BOC: statement date column not found")
    cells: dict[str, list[str]] = {}
    for w in line.words:
        cells.setdefault(_nearest(w.xc, cols), []).append(w.text)
    parsed.statement_date = parse_date(" ".join(cells.get("账单日", [])))
    due = " ".join(cells.get("到期还款日", []))
    parsed.due_date = parse_date(due) if due else None  # empty when nothing is due
    rmb = " ".join(cells.get("本期人民币欠款总计", []))
    if rmb:
        parsed.totals["CNY"] = parse_amount(rmb)
    fcy = " ".join(cells.get("本期外币欠款总计", []))
    if fcy:
        amount, currency = parse_currency_amount(fcy)
        parsed.totals[currency] = amount


def _read_card_row(line: Line, card: str, cols: list[float], parsed: _Parsed) -> None:
    """卡号 | 本期应还款额 人民币/外币 | 本期最小还款 人民币/外币 (blank cells are zero)."""
    if len(cols) != 4:
        raise TemplateChanged("BOC: card table sub-columns (人民币RMB/外币FCY) not found")
    names = dict(zip(["due_rmb", "due_fcy", "min_rmb", "min_fcy"], cols, strict=True))
    cells: dict[str, list[str]] = {}
    for w in line.words:
        if w.x0 > 150:  # right of the masked card number
            cells.setdefault(_nearest(w.xc, names), []).append(w.text)
    due = parsed.card_due.setdefault(card, {})
    low = parsed.card_min.setdefault(card, {})
    for key, target in (("due", due), ("min", low)):
        if rmb := " ".join(cells.get(f"{key}_rmb", [])):
            target["CNY"] = parse_amount(rmb)
        if fcy := " ".join(cells.get(f"{key}_fcy", [])):
            amount, currency = parse_currency_amount(fcy)
            target[currency] = amount


def _balances(section: _Section, parsed: _Parsed) -> list[BillBalance]:
    needed = {"type", "previous", "charges", "credits", "new"}
    if missing := needed - section.account_cols.keys():
        raise TemplateChanged(f"BOC: account table columns not found: {sorted(missing)}")
    if not section.account_rows:
        raise TemplateChanged(f"BOC: card {section.card} has no account row (账号类型)")
    balances = []
    for row in section.account_rows:
        cells: dict[str, list[str]] = {}
        for w in row.words:
            cells.setdefault(_nearest(w.xc, section.account_cols), []).append(w.text)
        currency = parse_currency(" ".join(cells["type"]))
        previous, prev_dir = parse_directed_amount(
            " ".join(cells.get("previous", [])), blank_is_zero=True
        )
        new, new_dir = parse_directed_amount(" ".join(cells.get("new", [])), blank_is_zero=True)
        credit = Direction.CREDIT
        balances.append(
            BillBalance(
                currency=currency,
                previous_balance=ZERO if prev_dir == credit else previous,
                previous_deposit=previous if prev_dir == credit else ZERO,
                # BOC's 本期支出金额 already includes adjustments (docs/banks/boc.md §5).
                new_charges=parse_amount(" ".join(cells.get("charges", [])), blank_is_zero=True),
                payments_credits=parse_amount(
                    " ".join(cells.get("credits", [])), blank_is_zero=True
                ),
                amount_due=ZERO if new_dir == credit else new,
                deposit=new if new_dir == credit else ZERO,
                min_payment=parsed.card_min.get(section.card, {}).get(currency),
            )
        )
    return balances


def _transactions(section: _Section, currency: str, account_id: str) -> list[Transaction]:
    """Group detail lines into one block per transaction and read each block.

    The date line is the anchor; wrapped description lines sit just above or below it and
    are attached to the nearest anchor.
    """
    lines = section.detail_lines
    if not lines:
        return []
    cols = section.detail_cols
    if not {"存入", "支出", "卡号后四位"} <= cols.keys():
        raise TemplateChanged(f"BOC: transaction header columns not found for card {section.card}")
    anchors = [ln for ln in lines if DATE_RE.match(ln.words[0].text) and ln.words[0].x0 < 100]
    blocks: dict[int, list[Line]] = {id(a): [a] for a in anchors}
    for ln in lines:
        if id(ln) in blocks:
            continue
        nearest = min(anchors, key=lambda a: abs(a.y - ln.y), default=None)
        if nearest is None or abs(nearest.y - ln.y) > MAX_SATELLITE_GAP:
            section.warnings.append(f"明细区有一行没法归到任何交易：{ln.text}")
            continue
        blocks[id(nearest)].append(ln)

    amount_cols = {"deposit": cols["存入"], "expenditure": cols["支出"]}
    amount_left = min(amount_cols.values()) - 40
    desc_right = (cols.get("交易描述", 344) + cols["存入"]) / 2  # ~391 on the samples
    transactions = []
    for line_no, anchor in enumerate(anchors, start=1):
        block = sorted(blocks[id(anchor)], key=lambda ln: ln.y)
        words = [w for ln in block for w in ln.words]
        try:
            dates = sorted((w for w in words if DATE_RE.match(w.text)), key=lambda w: w.x0)
            amounts = [w for w in words if AMOUNT_RE.match(w.text) and w.x0 > amount_left]
            if len(dates) != 2 or len(amounts) != 1:
                raise FormatError(f"{len(dates)} dates and {len(amounts)} amounts")
            (amount_word,) = amounts
            value = parse_amount(amount_word.text)
            is_credit = _nearest(amount_word.xc, amount_cols) == "deposit"
            description = _description(block, cols["卡号后四位"] + 30, desc_right)
            transactions.append(
                _transaction(
                    line_no,
                    parse_date(dates[0].text),
                    parse_date(dates[1].text),
                    -value if is_credit else value,
                    currency,
                    description,
                    section.card,
                    account_id,
                )
            )
        except FormatError as exc:
            texts = " / ".join(ln.text for ln in block)
            section.warnings.append(f"明细行解析失败（{exc}）：{texts}")
    return transactions


def _description(block: list[Line], left: float, right: float) -> str:
    """Join the description words of a block. A line that runs to the column's right edge
    was wrapped mid-word ("REBA" + "TESGP"), so it is joined without a space."""
    parts: list[tuple[str, float]] = []
    for ln in block:
        words = [w for w in ln.words if left <= w.x0 and w.x1 <= right + 4]
        if words:
            parts.append((" ".join(w.text for w in words), words[-1].x1))
    text = ""
    for i, (chunk, _) in enumerate(parts):
        if i and parts[i - 1][1] < right - 8:
            text += " "
        text += chunk
    return text


def _transaction(line_no, trans_date, post_date, amount, currency, description, card, account_id):
    txn_type = _classify(description, amount)
    merchant, location = (None, None)
    if txn_type == TxnType.PURCHASE:
        merchant, location = _merchant(description)
    return Transaction(
        line_no=line_no,
        txn_id=make_txn_id("BOC", account_id, trans_date, amount, description, line_no),
        trans_date=trans_date,
        post_date=post_date,
        txn_type=txn_type,
        amount=amount,
        currency=currency,
        description_raw=description,
        merchant=merchant,
        merchant_location=location,
        card_last4=card,  # Apple Pay device numbers in the row are folded into the card
    )


def _classify(description: str, amount: Decimal) -> TxnType:
    """Deposits (存入) are negative, expenditure (支出) positive; see docs/banks/boc.md §6."""
    if amount < 0:
        if REBATE_RE.search(description):
            return TxnType.REBATE
        if REPAYMENT_RE.search(description):
            return TxnType.REPAYMENT
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


def _merchant(description: str) -> tuple[str | None, str | None]:
    """ "Kelly's On KingAUS" -> ("Kelly's On King", "AUS")."""
    suffix = description[-3:]
    if len(description) > 3 and suffix in COUNTRY_SUFFIXES:
        return description[:-3].strip() or None, suffix
    return description or None, None


def _bill(section: _Section, parsed: _Parsed, msg: RawMessage) -> Bill:
    account_id = f"BOC:{section.card}"
    balances = _balances(section, parsed)
    currency = section.detail_currency
    if currency is None:
        currency = balances[0].currency if len(balances) == 1 else None
    if section.detail_lines and currency is None:
        raise TemplateChanged(f"BOC: currency of card {section.card}'s transactions unknown")
    transactions = _transactions(section, currency, account_id) if currency else []
    warnings = list(section.warnings)
    due_by_currency = parsed.card_due.get(section.card, {})
    for b in balances:
        shown = due_by_currency.get(b.currency)
        if shown is not None and shown != b.amount_due:
            warnings.append(
                f"{b.currency} 卡表的本期应还款额 {shown} 与账户表不一致（应为 {b.amount_due}）"
            )
    if parsed.due_date is None and any(b.amount_due > 0 for b in balances):
        warnings.append("有欠款但到期还款日为空，请以原账单为准")
    email_date = msg.email_date
    if email_date is None:
        email_date = parsed.statement_date
        warnings.append("邮件没有 Date 头，汇率日期改用账单日")
    return Bill(
        bank="BOC",
        account_id=account_id,
        cards=[section.card],
        statement_date=parsed.statement_date,
        due_date=parsed.due_date,
        email_date=email_date,
        balances=balances,
        transactions=transactions,
        status="OK",  # decided by reconcile()
        warnings=warnings,
        source_message_id=msg.message_id,
        source_sha256=msg.sha256,
        parser_name=BocPdfParser.name,
        parser_version=BocPdfParser.version,
    )


def _check_totals(parsed: _Parsed, bills: list[Bill]) -> None:
    """The summary's 本期人民币/外币欠款总计 must equal the sum over all cards."""
    for currency, total in parsed.totals.items():
        summed = sum(
            (b.amount_due for bill in bills for b in bill.balances if b.currency == currency), ZERO
        )
        if summed != total:
            first = bills[0]
            first.warnings.append(
                f"账单总览的{currency}欠款总计 {total} 与各卡合计 {summed} 不一致"
            )
            if first.status == "OK":
                first.status = "WARN"
