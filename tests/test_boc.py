"""BOC PDF parser (docs/banks/boc.md): the two real samples, plus hand-built lines for the
cases they do not cover. Building lines instead of PDFs works because the parser's second
layer only ever sees positioned words."""

from dataclasses import replace
from decimal import Decimal
from functools import cache
from pathlib import Path

import pytest

from autobill.fetch.message import RawMessage
from autobill.model import TxnType
from autobill.parse.base import TemplateChanged
from autobill.parse.boc import BocPdfParser, Line, Word, parse_lines

FIXTURES = Path(__file__).parent / "fixtures"
ZERO_SAMPLE = FIXTURES / "boc" / "boc_visa_2026-08.eml"
COMBINED = FIXTURES / "boc" / "boc_combined_2025-06.eml"
D = Decimal
parser = BocPdfParser()


def load(path: Path) -> RawMessage:
    return RawMessage.from_bytes(path.read_bytes())


def by_card(bills):
    return {b.account_id: b for b in bills}


@cache
def parsed(path: Path):
    """Parsing a 12-page PDF takes a moment; the tests only read the result."""
    return tuple(parser.parse(load(path)))


# --- the zero-balance sample (2026-08) -------------------------------------------------


def test_zero_sample_snapshot(snapshot):
    bills = parsed(ZERO_SAMPLE)
    snapshot("boc/boc_visa_2026-08", [b.model_dump(mode="json") for b in bills])


def test_zero_sample_fields():
    (bill,) = parsed(ZERO_SAMPLE)
    assert bill.status == "OK" and bill.warnings == []
    assert bill.account_id == "BOC:0005" and bill.cards == ["0005"]
    assert str(bill.statement_date) == "2026-08-22"
    assert bill.due_date is None  # nothing due: the bank leaves it empty
    (b,) = bill.balances
    assert (b.currency, b.previous_deposit, b.deposit, b.amount_due) == (
        "AUD",
        D("0.07"),
        D("0.07"),
        D("0"),
    )
    assert bill.transactions == []


# --- the combined two-card sample (2025-06) --------------------------------------------


def test_combined_snapshot(snapshot):
    bills = parsed(COMBINED)
    snapshot("boc/boc_combined_2025-06", [b.model_dump(mode="json") for b in bills])


def test_combined_statement_splits_into_one_bill_per_card():
    bills = by_card(parsed(COMBINED))
    assert list(bills) == ["BOC:0005", "BOC:0006"]
    for bill in bills.values():
        assert bill.status == "OK", bill.warnings
        assert (str(bill.statement_date), str(bill.due_date)) == ("2025-06-22", "2025-07-12")


@pytest.mark.parametrize(
    ("account", "currency", "prev", "charges", "credits", "due", "minimum", "count"),
    [
        ("BOC:0005", "AUD", "1627.69", "883.89", "1673.54", "838.04", "83.00", 57),
        ("BOC:0006", "CNY", "7228.74", "9097.75", "12428.56", "3897.93", "389.00", 98),
    ],
)
def test_combined_balances(account, currency, prev, charges, credits, due, minimum, count):
    bill = by_card(parsed(COMBINED))[account]
    (b,) = bill.balances
    assert b.currency == currency
    assert (b.previous_balance, b.new_charges, b.payments_credits, b.amount_due) == (
        D(prev),
        D(charges),
        D(credits),
        D(due),
    )
    assert b.min_payment == D(minimum)
    assert len(bill.transactions) == count
    assert {t.currency for t in bill.transactions} == {currency}


def test_deposits_are_negative_expenditure_positive():
    bill = by_card(parsed(COMBINED))["BOC:0005"]
    repayment = next(t for t in bill.transactions if t.txn_type == TxnType.REPAYMENT)
    assert (repayment.amount, repayment.description_raw) == (D("-1626.90"), "BOCNET")
    refund = next(t for t in bill.transactions if t.txn_type == TxnType.REFUND)
    assert refund.amount == D("-18.80")
    purchases = [t for t in bill.transactions if t.txn_type == TxnType.PURCHASE]
    assert purchases and all(t.amount > 0 for t in purchases)


def test_apple_pay_device_number_is_folded_into_the_card():
    bill = by_card(parsed(COMBINED))["BOC:0006"]
    assert {t.card_last4 for t in bill.transactions} == {"0006"}  # 0008 rows included
    assert bill.cards == ["0006"]


def test_wrapped_descriptions_are_joined():
    bills = by_card(parsed(COMBINED))
    descriptions = {t.description_raw for b in bills.values() for t in b.transactions}
    # "VISA BOC ZJ1PCT REBA" + "TESGP": cut mid-word at the column edge, joined without space
    assert "VISA BOC ZJ1PCT REBATESGP" in descriptions
    # three lines: "支付宝-ALIPAY SINGAP" + "ORE E-COMMERCE PRI" + "CHN"
    assert "支付宝-ALIPAY SINGAPORE E-COMMERCE PRICHN" in descriptions


def test_merchant_and_country_are_split():
    bill = by_card(parsed(COMBINED))["BOC:0005"]
    kelly = next(t for t in bill.transactions if t.description_raw == "Kelly's On KingAUS")
    assert (kelly.merchant, kelly.merchant_location) == ("Kelly's On King", "AUS")


def test_rebates():
    bills = by_card(parsed(COMBINED))
    rebates = [t for b in bills.values() for t in b.transactions if t.txn_type == TxnType.REBATE]
    assert len(rebates) == 29 + 41 and all(t.amount < 0 for t in rebates)
    assert any(t.description_raw == "中行银联境外消费阶梯返活动" for t in rebates)


def test_matches_only_boc():
    for path in sorted(FIXTURES.rglob("*.eml")):
        assert parser.matches(load(path)) == (path.parent.name == "boc"), path.name


def test_no_pdf_is_template_changed():
    msg = replace(load(ZERO_SAMPLE), attachments=[])
    with pytest.raises(TemplateChanged):
        parser.parse(msg)


# --- hand-built lines (layout copied from the samples) ---------------------------------


def line(y: float, *words: tuple[str, float]) -> Line:
    return Line(y, [Word(text, x0, x0 + 6 * len(text), y) for text, x0 in words])


def statement(
    *,
    due_date="",
    rmb_total="",
    fcy_total=("AUD", "0.00"),
    card_row=(("AUD", 280), ("0.00", 300), ("AUD", 504), ("0.00", 524)),
    previous=("存款/CRED", "0.07"),
    charges="0.00",
    credits="0.00",
    new=("存款/CRED", "0.07"),
    with_section=True,
    with_account_row=True,
    detail=(),
) -> list[Line]:
    overview = [("2026-08-22", 205)]
    if due_date:
        overview.append((due_date, 65))
    if rmb_total:
        overview.append((rmb_total, 360))
    overview += [(fcy_total[0], 488), (fcy_total[1], 511)]
    lines = [
        line(
            280,
            ("到期还款日", 68),
            ("账单日", 216),
            ("本期人民币欠款总计", 332),
            ("本期外币欠款总计", 476),
        ),
        line(317, *overview),
        line(392, ("卡号", 66), ("本期应还款额New", 176), ("本期最小还款Minimum", 380)),
        line(408, ("人民币RMB", 165), ("外币FCY", 282), ("人民币RMB", 389), ("外币FCY", 506)),
        line(424, ("4937", 35), ("7600", 55), ("****", 75), ("0005", 89), *card_row),
    ]
    if with_section:
        lines.append(line(467, ("长城卓隽留学威士澳元卡主卡(卡号：0005)", 18)))
    lines += [
        line(520, ("本期支出金额", 227), ("本期存入金额", 320)),
        line(526, ("账单可分期金额", 503)),
        line(532, ("账号类型", 48), ("上期存款/欠款余额", 124), ("本期存款/欠款余额", 404)),
    ]
    if with_account_row:
        lines.append(
            line(592, ("外币/AUD", 46), (previous[0], 122), (previous[1], 168), (charges, 243),
                 (credits, 336), (new[0], 404), (new[1], 450), ("2000.00", 516))
        )  # fmt: skip
    if detail:
        lines += [
            line(700, ("(AUD)外币交易明细/FCY", 18)),
            line(710, ("卡号后四位", 231)),
            line(720, ("交易日", 52), ("交易描述", 328), ("存入", 430), ("支出", 523)),
            line(730, ("of", 220), ("Card", 230), ("Number", 250)),
            *detail,
        ]
    return lines


def parse(lines):
    return parse_lines(lines, load(ZERO_SAMPLE))


def test_built_lines_match_the_real_zero_sample():
    (bill,) = parse(statement())
    assert bill.status == "OK" and bill.balances[0].deposit == D("0.07")


def test_debt_label_and_detail_rows():
    lines = statement(
        due_date="2026-09-12",
        fcy_total=("AUD", "30.00"),
        card_row=(("AUD", 280), ("30.00", 300), ("AUD", 504), ("3.00", 524)),
        previous=("欠款/DEBT", "50.00"),
        charges="30.00",
        credits="50.00",
        new=("欠款/DEBT", "30.00"),
        detail=[
            line(760, ("2026-07-25", 42), ("2026-07-26", 135), ("0005", 242), ("BOCNET", 328),
                 ("50.00", 423)),
            line(790, ("Coffee", 300), ("ShopAUS", 340)),
            line(800, ("2026-08-01", 42), ("2026-08-02", 135), ("0009", 242), ("30.00", 521)),
        ],
    )  # fmt: skip
    (bill,) = parse(lines)
    assert bill.status == "OK", bill.warnings
    (b,) = bill.balances
    assert (b.previous_balance, b.amount_due, b.min_payment) == (D("50.00"), D("30.00"), D("3.00"))
    repayment, purchase = bill.transactions
    assert (repayment.txn_type, repayment.amount) == (TxnType.REPAYMENT, D("-50.00"))
    assert (purchase.txn_type, purchase.amount, purchase.merchant) == (
        TxnType.PURCHASE,
        D("30.00"),
        "Coffee Shop",
    )
    assert purchase.card_last4 == "0005"  # device number 0009 folded into the section card


def test_debt_without_due_date_is_flagged():
    lines = statement(
        fcy_total=("AUD", "30.00"),
        previous=("欠款/DEBT", "0.00"),
        charges="30.00",
        new=("欠款/DEBT", "30.00"),
        card_row=(("AUD", 280), ("30.00", 300), ("AUD", 504), ("3.00", 524)),
        detail=[line(760, ("2026-08-01", 42), ("2026-08-02", 135), ("0005", 242), ("X", 328),
                     ("30.00", 521))],
    )  # fmt: skip
    (bill,) = parse(lines)
    assert bill.status == "WARN"
    assert any("到期还款日为空" in w for w in bill.warnings)


def test_summary_total_mismatch_is_flagged():
    (bill,) = parse(statement(fcy_total=("AUD", "9.99")))
    assert bill.status == "WARN"
    assert any("欠款总计" in w for w in bill.warnings)


def test_card_table_mismatch_is_flagged():
    (bill,) = parse(statement(card_row=(("AUD", 280), ("5.00", 300), ("AUD", 504), ("0.00", 524))))
    assert bill.status == "WARN"
    assert any("卡表" in w for w in bill.warnings)


def test_stray_detail_line_is_flagged():
    lines = statement(
        detail=[line(900, ("小计", 300), ("0.00", 523))],
    )
    (bill,) = parse(lines)
    assert bill.status == "WARN"
    assert any("没法归到任何交易" in w for w in bill.warnings)


@pytest.mark.parametrize(
    "broken",
    [
        {"with_section": False},
        {"with_account_row": False},
    ],
)
def test_missing_blocks_raise_template_changed(broken):
    with pytest.raises(TemplateChanged):
        parse(statement(**broken))


def test_missing_overview_raises_template_changed():
    lines = [ln for ln in statement() if "账单日" not in ln.text]
    with pytest.raises(TemplateChanged):
        parse(lines)
