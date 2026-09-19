"""Shared parsing helpers. Every amount and date format listed in docs/parsing.md#通用工具
has at least one case here; examples are copied from the bank specs in docs/banks/."""

from datetime import date
from decimal import Decimal

import pytest

from autobill.parse.util import (
    AmountError,
    CurrencyError,
    DateError,
    Direction,
    infer_year,
    is_pdf,
    normalize_ws,
    parse_amount,
    parse_amount_currency,
    parse_currency,
    parse_currency_amount,
    parse_date,
    parse_directed_amount,
)

D = Decimal

# --- whitespace -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "手机银行 按卡转账还款\u00a0\u00a0张三",
            "手机银行 按卡转账还款 张三",
        ),  # CCB &nbsp; padding
        ("\u3000账务说明\u3000", "账务说明"),  # full-width space
        ("\ufeff交易明细", "交易明细"),  # ABC body contains BOMs
        ("  a \t\r\n b  ", "a b"),
        ("", ""),
    ],
)
def test_normalize_ws(raw, expected):
    assert normalize_ws(raw) == expected


# --- amounts ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,217.97", D("1217.97")),  # thousands separator (ABC summary block)
        ("15,450.83", D("15450.83")),  # CCB
        ("1,234,567.00", D("1234567.00")),
        ("28.25", D("28.25")),
        ("-781.55", D("-781.55")),  # minus sign (ABC account info: debt is negative)
        ("+371.53", D("371.53")),
        ("\u2212371.53", D("-371.53")),  # U+2212 minus sign
        ("0.00", D("0.00")),
        ("12", D("12")),
        (" 9.31 ", D("9.31")),
        # reserved for other banks
        ("100.00 CR", D("-100.00")),  # CR suffix = credit, reduces debt
        ("100.00CR", D("-100.00")),
        ("100.00 DR", D("100.00")),
        ("１，２３４．５６", D("1234.56")),  # full-width digits and punctuation
        ("－５０．００", D("-50.00")),
        ("¥15,450.83", D("15450.83")),  # yen/yuan sign prefix
        ("￥15,450.83", D("15450.83")),  # full-width yuan sign (CCB greeting)
        ("-¥12.00", D("-12.00")),
        ("RMB 88.00", D("88.00")),  # RMB prefix
    ],
)
def test_parse_amount(raw, expected):
    result = parse_amount(raw)
    assert result == expected
    assert isinstance(result, Decimal)


def test_parse_amount_keeps_exact_decimal_places():
    assert str(parse_amount("0.10")) == "0.10"


@pytest.mark.parametrize("raw", ["-", "--", "", "   ", "\u00a0"])
def test_blank_amount_is_zero_only_when_allowed(raw):
    # CCB writes "-" and BOC leaves the cell empty for zero; parsers must opt in,
    # so a missing key figure is never silently read as 0.
    assert parse_amount(raw, blank_is_zero=True) == D("0")
    with pytest.raises(AmountError):
        parse_amount(raw)


@pytest.mark.parametrize(
    "raw",
    ["abc", "1,23.45", "12,3456.00", "1.2.3", "12.", ".5", "--5", "-100 CR", "12 34", "USD"],
)
def test_parse_amount_rejects_garbage(raw):
    with pytest.raises(AmountError):
        parse_amount(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("-28.25/USD", (D("-28.25"), "USD")),  # ABC settlement column (spending is negative)
        ("39.90/AUD", (D("39.90"), "AUD")),  # ABC original-currency column
        ("371.53/CNY", (D("371.53"), "CNY")),
        (" -1,000.00 / USD ", (D("-1000.00"), "USD")),
    ],
)
def test_parse_amount_currency(raw, expected):
    assert parse_amount_currency(raw) == expected


@pytest.mark.parametrize("raw", ["28.25", "28.25/", "/USD", "28.25/US", "28.25/usd"])
def test_parse_amount_currency_rejects(raw):
    with pytest.raises((AmountError, CurrencyError)):
        parse_amount_currency(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AUD 0.00", (D("0.00"), "AUD")),  # BOC card table
        ("CNY 0.00", (D("0.00"), "CNY")),  # CCB instalment balance
        ("USD 1,234.50", (D("1234.50"), "USD")),
        ("AUD\u00a012.30", (D("12.30"), "AUD")),
    ],
)
def test_parse_currency_amount(raw, expected):
    assert parse_currency_amount(raw) == expected


def test_parse_currency_amount_blank():
    assert parse_currency_amount("AUD", blank_is_zero=True) == (D("0"), "AUD")
    with pytest.raises(AmountError):
        parse_currency_amount("AUD")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("存款/CRED 0.07", (D("0.07"), Direction.CREDIT)),  # BOC: deposit (overpayment)
        ("欠款/DEBT 1,234.56", (D("1234.56"), Direction.DEBIT)),  # inferred label, see boc.md
        ("存款 / CRED\u00a00.07", (D("0.07"), Direction.CREDIT)),
    ],
)
def test_parse_directed_amount(raw, expected):
    assert parse_directed_amount(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "0.07",  # no label
        "存款/DEBT 0.07",  # Chinese and English labels disagree
        "余额/CRED 0.07",  # unknown label
        "存款/CRED -0.07",  # direction is given by the label, never by a sign
    ],
)
def test_parse_directed_amount_rejects(raw):
    with pytest.raises(AmountError):
        parse_directed_amount(raw)


def test_parse_directed_amount_blank():
    assert parse_directed_amount("", blank_is_zero=True) == (D("0"), Direction.DEBIT)


@pytest.mark.parametrize("raw", ["0.00", "0", "0.0"])
def test_parse_directed_amount_bare_zero(raw):
    """BOC prints an exactly-zero balance without a direction label (author's statements
    2025-12 to 2026-02): zero has no direction. "0.07" without a label is still rejected."""
    assert parse_directed_amount(raw) == (D("0"), Direction.DEBIT)


# --- currencies -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("人民币(CNY)", "CNY"),  # ABC
        ("人民币 （CNY）", "CNY"),  # CCB: full-width parentheses and space
        ("美元(USD)", "USD"),
        ("外币/AUD", "AUD"),  # BOC account type
        ("[人民币账户] RMB Account", "CNY"),  # CCB section row
        ("RMB", "CNY"),
        ("人民币", "CNY"),
        ("美元", "USD"),
        ("澳元", "AUD"),
        ("欧元", "EUR"),
        ("USD", "USD"),
        ("CHF", "CHF"),  # CCB 2026-06: spending in Switzerland
        ("瑞士法郎", "CHF"),
        ("澳门元", "MOP"),
    ],
)
def test_parse_currency(raw, expected):
    assert parse_currency(raw) == expected


@pytest.mark.parametrize("raw", ["", "账户", "XYZ", "人民币(USD)"])
def test_parse_currency_rejects(raw):
    with pytest.raises(CurrencyError):
        parse_currency(raw)


# --- dates ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("260803", date(2026, 8, 3)),  # YYMMDD (ABC transactions)
        ("20260803", date(2026, 8, 3)),  # YYYYMMDD (ABC instalment records)
        ("2026-08-22", date(2026, 8, 22)),  # YYYY-MM-DD (CCB transactions, BOC)
        ("2026/09/01", date(2026, 9, 1)),  # YYYY/MM/DD (periods, due dates)
        ("2026年06月11日", date(2026, 6, 11)),  # YYYY年MM月DD日 (CCB greeting)
        ("2026年6月1日", date(2026, 6, 1)),
        ("２０２６－０８－２２", date(2026, 8, 22)),
        (" 2026/9/1 ", date(2026, 9, 1)),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "2026-13-01", "2026-02-30", "260230", "2026.08.03", "08/03/2026", "2026-08", "1234567"],
)
def test_parse_date_rejects(raw):
    with pytest.raises(DateError):
        parse_date(raw)


@pytest.mark.parametrize(
    ("month", "day", "expected"),
    [
        (12, 20, date(2025, 12, 20)),  # inside a period that crosses the new year
        (1, 5, date(2026, 1, 5)),
        (12, 10, date(2025, 12, 10)),  # before the period start (posted late)
        (1, 16, date(2026, 1, 16)),  # just after the period end
    ],
)
def test_infer_year_across_new_year(month, day, expected):
    assert infer_year(month, day, date(2025, 12, 15), date(2026, 1, 14)) == expected


def test_infer_year_within_one_year():
    assert infer_year(8, 20, date(2026, 8, 2), date(2026, 9, 1)) == date(2026, 8, 20)


def test_infer_year_leap_day():
    assert infer_year(2, 29, date(2028, 2, 10), date(2028, 3, 9)) == date(2028, 2, 29)


def test_infer_year_rejects_impossible_date():
    with pytest.raises(DateError):
        infer_year(2, 30, date(2026, 2, 1), date(2026, 3, 1))


def test_infer_year_rejects_reversed_period():
    with pytest.raises(DateError):
        infer_year(8, 1, date(2026, 9, 1), date(2026, 8, 1))


# --- PDF detection ----------------------------------------------------------


def test_is_pdf_uses_content_not_name():
    assert is_pdf(b"%PDF-1.4\n...")
    assert is_pdf(b"\r\n%PDF-1.7")  # tolerate leading whitespace
    assert not is_pdf(b"<html>%PDF-</html>")
    assert not is_pdf(b"")
