"""Helpers shared by all bank parsers: whitespace, amounts, currencies, dates, PDF detection.

See docs/parsing.md#通用工具. Every function raises a FormatError subclass on input it
does not recognise; nothing is guessed and no key figure silently becomes zero.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum


class FormatError(ValueError):
    """A value in a statement does not have the expected format."""


class AmountError(FormatError):
    pass


class CurrencyError(FormatError):
    pass


class DateError(FormatError):
    pass


# --- whitespace -------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    """Turn NBSP (U+00A0), ideographic space (U+3000) and BOM (U+FEFF) into plain spaces,
    collapse runs of whitespace and strip both ends.

    ABC fixed-width descriptions must be sliced *before* calling this.
    """
    # \s already covers U+00A0 and U+3000; the BOM is not whitespace to Python.
    return _WS_RE.sub(" ", text.replace("\ufeff", " ")).strip()


# Full-width digits and punctuation, plus the Unicode minus sign, mapped to ASCII.
_FULLWIDTH = str.maketrans(
    {
        **{chr(0xFF10 + i): str(i) for i in range(10)},
        "，": ",",
        "．": ".",
        "－": "-",
        "＋": "+",
        "\u2212": "-",
        "￥": "¥",
        "／": "/",
        "（": "(",
        "）": ")",
        "：": ":",
    }
)


def _clean(text: str) -> str:
    return normalize_ws(text).translate(_FULLWIDTH)


# --- amounts ----------------------------------------------------------------

_BLANK_AMOUNTS = {"", "-", "--"}
_AMOUNT_RE = re.compile(
    r"""
    ^(?P<sign1>[+-])?\s*
    (?:¥|RMB)?\s*                       # currency prefix, reserved for other banks
    (?P<sign2>[+-])?
    (?P<int>\d{1,3}(?:,\d{3})+|\d+)     # thousands groups must be exactly 3 digits
    (?P<frac>\.\d+)?
    \s*(?P<suffix>CR|DR)?$              # credit / debit suffix, reserved for other banks
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_amount(text: str, *, blank_is_zero: bool = False) -> Decimal:
    """Parse "1,234.56", "-28.25", "¥15,450.83", "100.00 CR", full-width digits, ...

    The sign is returned as written; a CR suffix makes the value negative. Converting to
    our cardholder sign convention is the parser's job (docs/data-model.md#符号约定).
    Blank cells and a lone "-" mean zero only when `blank_is_zero` is set.
    """
    s = _clean(text)
    if s in _BLANK_AMOUNTS:
        if blank_is_zero:
            return Decimal("0")
        raise AmountError(f"blank amount: {text!r}")
    m = _AMOUNT_RE.match(s)
    if not m:
        raise AmountError(f"not an amount: {text!r}")
    signs = [x for x in (m["sign1"], m["sign2"]) if x]
    suffix = (m["suffix"] or "").upper()
    if len(signs) > 1 or (signs and suffix):
        raise AmountError(f"conflicting signs: {text!r}")
    value = Decimal(m["int"].replace(",", "") + (m["frac"] or ""))
    if signs == ["-"] or suffix == "CR":
        value = -value
    return value


_CODE_RE = re.compile(r"^[A-Z]{3}$")


def parse_amount_currency(text: str) -> tuple[Decimal, str]:
    """ABC style "amount/currency", e.g. "-28.25/USD" -> (Decimal("-28.25"), "USD")."""
    s = _clean(text)
    amount, sep, code = s.rpartition("/")
    code = code.strip()
    if not sep or not _CODE_RE.match(code):
        raise CurrencyError(f"expected 'amount/CUR': {text!r}")
    return parse_amount(amount), code


def parse_currency_amount(text: str, *, blank_is_zero: bool = False) -> tuple[Decimal, str]:
    """Currency code then amount, e.g. "AUD 0.00" -> (Decimal("0.00"), "AUD")."""
    s = _clean(text)
    code, amount = s[:3], s[3:]
    if not _CODE_RE.match(code) or (amount and not amount.startswith(" ")):
        raise CurrencyError(f"expected 'CUR amount': {text!r}")
    return parse_amount(amount, blank_is_zero=blank_is_zero), code


class Direction(StrEnum):
    DEBIT = "debit"  # 欠款: the cardholder owes the bank
    CREDIT = "credit"  # 存款: the cardholder has an overpayment (溢缴款)


_DIRECTION_LABELS = {
    "存款": Direction.CREDIT,
    "CRED": Direction.CREDIT,
    "CREDIT": Direction.CREDIT,
    "欠款": Direction.DEBIT,
    "DEBT": Direction.DEBIT,
    "DEBIT": Direction.DEBIT,
}
_DIRECTED_RE = re.compile(r"^(?P<zh>\S+?)\s*/\s*(?P<en>[A-Z]+)\s+(?P<amount>.+)$")


def parse_directed_amount(text: str, *, blank_is_zero: bool = False) -> tuple[Decimal, Direction]:
    """BOC style "存款/CRED 0.07" -> (Decimal("0.07"), Direction.CREDIT).

    The label gives the direction, so the amount itself must not carry a sign.
    A blank cell (when allowed) is zero and reported as DEBIT.
    """
    s = _clean(text)
    if s in _BLANK_AMOUNTS and blank_is_zero:
        return Decimal("0"), Direction.DEBIT
    m = _DIRECTED_RE.match(s)
    if not m:
        raise AmountError(f"expected '存款/CRED amount' style: {text!r}")
    zh, en = _DIRECTION_LABELS.get(m["zh"]), _DIRECTION_LABELS.get(m["en"])
    if zh is None or en is None or zh != en:
        raise AmountError(f"unknown or conflicting direction label: {text!r}")
    value = parse_amount(m["amount"])
    if value < 0 or m["amount"].lstrip().startswith(("+", "-")):
        raise AmountError(f"signed amount after a direction label: {text!r}")
    return value, zh


# --- currencies -------------------------------------------------------------

_ISO_CODES = {"CNY", "USD", "AUD", "EUR", "HKD", "JPY", "GBP", "SGD", "NZD", "CAD"}
_CODE_ALIASES = {"RMB": "CNY"}
_CHINESE_NAMES = {
    "人民币": "CNY",
    "美元": "USD",
    "澳元": "AUD",
    "欧元": "EUR",
    "港币": "HKD",
    "日元": "JPY",
    "英镑": "GBP",
    "新加坡元": "SGD",
    "新西兰元": "NZD",
    "加元": "CAD",
}
_ASCII_CODE_RE = re.compile(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])")


def parse_currency(label: str) -> str:
    """Normalise a currency label to an ISO code.

    "人民币(CNY)", "人民币 （CNY）", "外币/AUD", "[人民币账户] RMB Account", "美元" ...
    Every currency mentioned in the label must agree, otherwise CurrencyError.
    """
    s = _clean(label)
    found: set[str] = set()
    for code in _ASCII_CODE_RE.findall(s):
        code = _CODE_ALIASES.get(code, code)
        if code not in _ISO_CODES:
            raise CurrencyError(f"unknown currency code {code!r} in {label!r}")
        found.add(code)
    found |= {code for name, code in _CHINESE_NAMES.items() if name in s}
    if len(found) != 1:
        raise CurrencyError(f"expected exactly one currency in {label!r}, found {sorted(found)}")
    return found.pop()


# --- dates ------------------------------------------------------------------

_DATE_PATTERNS = [
    re.compile(r"^(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})$"),  # 2026-08-22
    re.compile(r"^(?P<y>\d{4})/(?P<m>\d{1,2})/(?P<d>\d{1,2})$"),  # 2026/09/01
    re.compile(
        r"^(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日$"
    ),  # 2026年06月11日
    re.compile(r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})$"),  # 20260803
    re.compile(r"^(?P<yy>\d{2})(?P<m>\d{2})(?P<d>\d{2})$"),  # 260803
]


def _make_date(year: int, month: int, day: int, text: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise DateError(f"invalid date {text!r}: {exc}") from None


def parse_date(text: str) -> date:
    """Parse the full-date formats used by the banks (all include a year)."""
    s = _clean(text)
    for pattern in _DATE_PATTERNS:
        m = pattern.match(s)
        if m:
            year = int(m["y"]) if m.groupdict().get("y") else 2000 + int(m["yy"])
            return _make_date(year, int(m["m"]), int(m["d"]), text)
    raise DateError(f"unrecognised date format: {text!r}")


def infer_year(month: int, day: int, period_start: date, period_end: date) -> date:
    """Give a month/day without a year the year that puts it nearest the statement period.

    Reserved for banks that print only MM-DD. Handles periods that cross the new year and
    transactions dated slightly outside the period (e.g. posted late).
    """
    if period_start > period_end:
        raise DateError(f"period start {period_start} is after end {period_end}")

    def distance(d: date) -> timedelta:
        if d < period_start:
            return period_start - d
        if d > period_end:
            return d - period_end
        return timedelta(0)

    candidates = []
    for year in (period_end.year - 1, period_end.year, period_end.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise DateError(f"no valid date for month={month} day={day}")
    return min(candidates, key=distance)


# --- PDF detection ----------------------------------------------------------


def is_pdf(data: bytes) -> bool:
    """Detect a PDF by its content, never by file name or MIME type (BOC sends octet-stream)."""
    return data.lstrip(b" \t\r\n\x00").startswith(b"%PDF-")
