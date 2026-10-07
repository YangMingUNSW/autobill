"""ABC parser against the anonymised real statements (docs/banks/abc.md): three of the
current template, one of the template until June 2025 (abc_2025/, §11)."""

import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from autobill.fetch.message import RawMessage
from autobill.model import TxnType
from autobill.parse.abc import AbcHtmlParser
from autobill.parse.base import TemplateChanged
from autobill.reconcile import SYNTHETIC_DESCRIPTION

FIXTURES = Path(__file__).parent / "fixtures"
ABC = sorted((FIXTURES / "abc").glob("*.eml"))
OLD = sorted((FIXTURES / "abc_2025").glob("*.eml"))  # the template until June 2025
D = Decimal
parser = AbcHtmlParser()


def load(name: str) -> RawMessage:
    return RawMessage.from_bytes((FIXTURES / "abc" / f"{name}.eml").read_bytes())


def parse_one(msg: RawMessage):
    (bill,) = parser.parse(msg)
    return bill


def with_html(msg: RawMessage, old: str, new: str) -> RawMessage:
    assert old in msg.html
    return dataclasses.replace(msg, html_parts=[msg.html.replace(old, new)])


@pytest.mark.parametrize("path", ABC + OLD, ids=lambda p: p.stem)
def test_snapshot(path, snapshot):
    bill = parse_one(RawMessage.from_bytes(path.read_bytes()))
    snapshot(f"abc/{path.stem}", bill.model_dump(mode="json"))


@pytest.mark.parametrize("path", ABC + OLD, ids=lambda p: p.stem)
def test_reconciles_ok(path):
    """The old sample also checks the statement-day cashback rule of the cross-check: its
    USD Mastercard cashback of 0.14 posted on the statement day is not in the account-info
    balance, and must not raise a warning (docs/banks/abc.md §11)."""
    bill = parse_one(RawMessage.from_bytes(path.read_bytes()))
    assert bill.status == "OK", bill.warnings
    assert bill.warnings == []


def test_matches_only_abc():
    for path in sorted(FIXTURES.rglob("*.eml")):
        msg = RawMessage.from_bytes(path.read_bytes())
        is_abc = path.relative_to(FIXTURES).parts[0].startswith("abc")
        assert parser.matches(msg) == is_abc, path.name


@pytest.mark.parametrize(
    ("name", "currency", "prev", "prev_dep", "charges", "credits", "adjust", "due", "dep"),
    [
        # docs/banks/abc.md §8
        ("abc_mc_2026-09", "CNY", "0.00", "0.00", "371.53", "371.53", "0.00", "0.00", "0.00"),
        ("abc_mc_2026-09", "USD", "55.20", "0.28", "56.89", "55.48", "0.00", "56.33", "0.00"),
        ("abc_visa_2026-09", "CNY", "0.00", "0.00", "19168.54", "19168.54", "0.00", "0.00", "0.00"),
        (
            "abc_visa_2026-09",
            "USD",
            "2836.84",
            "3.29",
            "2285.06",
            "2866.34",
            "0.00",
            "2252.79",
            "0.52",
        ),
        (
            "abc_unionpay_2026-09",
            "CNY",
            "1209.28",
            "0.00",
            "1218.59",
            "1209.28",
            "-0.62",
            "1217.97",
            "0.00",
        ),
    ],
)
def test_summary_block(name, currency, prev, prev_dep, charges, credits, adjust, due, dep):
    bill = parse_one(load(name))
    (b,) = [b for b in bill.balances if b.currency == currency]
    assert (b.previous_balance, b.previous_deposit, b.new_charges, b.payments_credits) == (
        D(prev),
        D(prev_dep),
        D(charges),
        D(credits),
    )
    assert (b.adjustments, b.amount_due, b.deposit) == (D(adjust), D(due), D(dep))


def test_statement_fields():
    bill = parse_one(load("abc_unionpay_2026-09"))
    assert bill.account_id == "ABC:0003" and bill.cards == ["0003"]
    assert str(bill.period_start) == "2026-08-17" and str(bill.period_end) == "2026-09-16"
    assert bill.statement_date == bill.period_end  # ABC prints no separate statement date
    assert str(bill.due_date) == "2026-10-05"
    assert str(bill.email_date) == "2026-09-18"
    assert bill.balances[0].min_payment == D("781.55")


def test_unlisted_adjustment_becomes_synthetic_row():
    bill = parse_one(load("abc_unionpay_2026-09"))
    synthetic = [t for t in bill.transactions if t.synthetic]
    assert len(synthetic) == 1
    (s,) = synthetic
    assert s.amount == D("-0.62") and s.txn_type == TxnType.ADJUSTMENT
    assert s.description_raw == SYNTHETIC_DESCRIPTION and s.line_no == len(bill.transactions)
    # Sum of all rows now equals the net change of the account.
    b = bill.balances[0]
    net = (b.amount_due - b.deposit) - (b.previous_balance - b.previous_deposit)
    assert sum(t.amount for t in bill.transactions) == net


def test_spending_is_positive_and_foreign_amounts_kept():
    bill = parse_one(load("abc_mc_2026-09"))
    purchases = [t for t in bill.transactions if t.txn_type == TxnType.PURCHASE]
    assert [t.amount for t in purchases] == [D("28.25"), D("28.64")]
    assert all(t.currency == "USD" and t.orig_currency == "AUD" for t in purchases)
    assert purchases[0].orig_amount == D("39.90")
    rebates = [t for t in bill.transactions if t.txn_type == TxnType.REBATE]
    assert [t.amount for t in rebates] == [D("-0.28"), D("-0.28")]


def test_auto_fx_repayment_chain():
    bill = parse_one(load("abc_mc_2026-09"))
    by_type = {(t.txn_type, t.currency): t for t in bill.transactions}
    repayment = by_type[(TxnType.REPAYMENT, "CNY")]
    fx_out = by_type[(TxnType.FX_TRANSFER, "CNY")]
    fx_in = by_type[(TxnType.FX_TRANSFER, "USD")]
    assert repayment.amount == D("-371.53")  # money in from the debit card
    assert fx_out.amount == D("371.53")  # CNY account pays for the USD
    assert fx_in.amount == D("-54.92") and fx_in.fx_rate == D("6.76485")
    assert fx_out.card_last4 is None


def test_installment_rows():
    bill = parse_one(load("abc_unionpay_2026-09"))
    principal = next(t for t in bill.transactions if t.txn_type == TxnType.INSTALLMENT)
    interest = next(t for t in bill.transactions if t.txn_type == TxnType.INTEREST)
    assert (principal.amount, principal.installment) == (D("659.00"), "3/36")
    assert (interest.amount, interest.installment) == (D("99.59"), "3/36")


@pytest.mark.parametrize(
    ("description", "merchant", "location"),
    [
        ("GREATER UNION SYDNEY SYDNEY AU", "GREATER UNION SYDNEY", "SYDNEY AU"),
        # 23-char merchant runs straight into the city: must be cut by position.
        ("Red Chilli Noodle No/88Sydney AU", "Red Chilli Noodle No/88", "Sydney AU"),
    ],
)
def test_visa_fixed_width_merchant(description, merchant, location):
    bill = parse_one(load("abc_visa_2026-09"))
    txn = next(t for t in bill.transactions if t.description_raw == f"境外消费 {description}")
    assert (txn.merchant, txn.merchant_location) == (merchant, location)


def test_mastercard_and_online_merchants():
    mc = parse_one(load("abc_mc_2026-09"))
    txn = next(t for t in mc.transactions if t.txn_type == TxnType.PURCHASE)
    assert (txn.merchant, txn.merchant_location) == ("PlusFitness SYDNEY", "AUS")
    up = parse_one(load("abc_unionpay_2026-09"))
    txn = next(t for t in up.transactions if t.txn_type == TxnType.PURCHASE)
    assert txn.merchant == "财付通，深圳市腾讯计算机系统有限公司"


def test_txn_ids_unique_within_bill():
    bill = parse_one(load("abc_visa_2026-09"))
    ids = [t.txn_id for t in bill.transactions]
    assert len(ids) == len(set(ids)) == 119


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (">账务说明<", "><"),  # summary section title
        (">交易明细<", "><"),  # transactions section title
        ("交易日期", "日期"),  # transactions header
        ("536113******0001", "no card"),  # card number
        ("2026/08/02-2026/09/01", ""),  # statement cycle
        ("本期调整金额", "调整"),  # summary header column
    ],
)
def test_missing_key_parts_raise_template_changed(old, new):
    msg = with_html(load("abc_mc_2026-09"), old, new)
    with pytest.raises(TemplateChanged):
        parser.parse(msg)


def test_no_html_raises_template_changed():
    msg = dataclasses.replace(load("abc_mc_2026-09"), html_parts=[])
    with pytest.raises(TemplateChanged):
        parser.parse(msg)


def test_unknown_transaction_is_flagged():
    msg = with_html(load("abc_mc_2026-09"), "MASTER返现", "MASTER某某")
    bill = parse_one(msg)
    assert bill.status == "WARN"
    assert any("未知的交易类型" in w for w in bill.warnings)


def test_cross_check_with_account_info():
    msg = with_html(load("abc_mc_2026-09"), "-56.33", "-56.34")
    bill = parse_one(msg)
    assert bill.status == "WARN"
    assert any("账户信息区" in w for w in bill.warnings)


def test_unparseable_amount_is_a_warning_not_a_crash():
    msg = with_html(load("abc_mc_2026-09"), "-28.25/USD", "-28.25/??")
    bill = parse_one(msg)
    assert bill.status == "WARN"
    assert any("明细行解析失败" in w for w in bill.warnings)


@pytest.mark.parametrize(
    ("group", "text", "kind"),
    [
        # both seen on a real 2026-06 VISA statement (not yet a fixture)
        ("取现/转出", "境外取现 MFS5080 VENEZIA IT", TxnType.CASH),
        ("利息", "利息 本期已优惠的利息金额:0.00元", TxnType.INTEREST),
        ("取现/转出", "转出 某某", TxnType.ADJUSTMENT),  # a transfer out is still unknown
        # seen on 29 real history statements
        ("退货", "境外退货 Woolworths OnlineBellaVistaAUS", TxnType.REFUND),
        ("退货", "网上消费退货 财付通退款", TxnType.REFUND),
        ("费用", "跨行ATM取现手续费 SEVEN BANK HOKKAIDO JPN", TxnType.FEE),
        ("分期", "总账分期 办理分期12期", TxnType.INSTALLMENT),  # balance turned into instalments
    ],
)
def test_cash_and_interest_groups(group, text, kind):
    from autobill.parse.abc import _Statement

    statement = _Statement.__new__(_Statement)
    statement.warnings = []
    assert statement._classify(group, text)[0] == kind
    assert bool(statement.warnings) == (kind == TxnType.ADJUSTMENT)  # only unknowns warn


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        # UnionPay's cashback is in the 还款 group too (real statements from 2025-07 on)
        ("银联入账 农行银联信用卡25年3季度境外笔笔返1%", TxnType.REBATE),
        ("银联入账 银联境外25年4季度境外首笔8.8元返现", TxnType.REBATE),
        ("银联入账 农行信用卡银联手机Pay笔笔1%返", TxnType.REBATE),
        ("银联入账 张三/付款尾号:0009/", TxnType.REPAYMENT),
        ("银联入账 张三/付款尾号:0009/财付通信用卡还款", TxnType.REPAYMENT),
        ("卡卡转账 张三", TxnType.REPAYMENT),
        ("财付通信用卡还款", TxnType.REPAYMENT),
    ],
)
def test_repayment_group_holds_unionpay_cashback(text, kind):
    from autobill.parse.abc import _Statement

    statement = _Statement.__new__(_Statement)
    statement.warnings = []
    assert statement._classify("还款", text)[0] == kind
    assert statement.warnings == []


# --- the template until June 2025 (docs/banks/abc.md §11) -----------------------------


def load_old(name: str = "abc_mc_2025-06") -> RawMessage:
    return RawMessage.from_bytes((FIXTURES / "abc_2025" / f"{name}.eml").read_bytes())


def test_old_template_statement_fields():
    bill = parse_one(load_old())
    assert (bill.account_id, bill.cards) == ("ABC:0001", ["0001"])
    assert (bill.period_start, bill.period_end, bill.statement_date) == (
        date(2025, 5, 2), date(2025, 6, 1), date(2025, 6, 1),
    )  # fmt: skip
    assert bill.due_date == date(2025, 6, 20)


@pytest.mark.parametrize(
    ("currency", "prev", "charges", "credits", "due"),
    [("CNY", "0.00", "402.91", "402.91", "0.00"), ("USD", "55.28", "217.75", "55.98", "217.05")],
)
def test_old_template_summary(currency, prev, charges, credits, due):
    """[币种, 上期余额, 本期新增应还款额, 本期已还款额, 本期账户全部余额]: the two balances
    signed with debt negative, the two flows positive, no adjustment column."""
    (b,) = [b for b in parse_one(load_old()).balances if b.currency == currency]
    assert (b.previous_balance, b.new_charges, b.payments_credits, b.amount_due) == (
        D(prev), D(charges), D(credits), D(due),
    )  # fmt: skip
    assert b.previous_deposit == b.deposit == b.adjustments == 0


def test_old_template_rows_are_kind_and_place():
    """No groups: the type comes from 交易摘要, the place is a column of its own."""
    bill = parse_one(load_old())
    kinds = {t.group_raw for t in bill.transactions}
    assert {"境外消费", "MASTER返现", "境外取现", "境外取现手续费", "利息"} <= kinds
    purchase = next(t for t in bill.transactions if t.group_raw == "境外消费")
    assert purchase.txn_type == TxnType.PURCHASE and purchase.amount > 0
    assert purchase.description_raw.startswith("境外消费 ") and purchase.merchant
    assert all(len(t.card_last4 or "0000") == 4 for t in bill.transactions)


@pytest.mark.parametrize(
    ("kind", "place", "expected"),
    [  # every 交易摘要 on 13 real statements of this template (2024-12 to 2025-06)
        ("网上消费", "财付通，深圳市腾讯计算机系统有限公司", TxnType.PURCHASE),
        ("境外消费", "UBER *EATSSydneyAUS", TxnType.PURCHASE),
        ("跨行消费", "HUANCHEN PTY LTD HAYMARKET AUS", TxnType.PURCHASE),
        ("跨行预授权完成", "NetEase UU Game Booster Hongkong H", TxnType.PURCHASE),
        ("跨行无卡消费", "(特约)龙腾出行", TxnType.PURCHASE),
        ("跨行有卡消费", "上海公共交通卡股份有限公司", TxnType.PURCHASE),
        ("跨行二维码支付", "财付通(银联云闪付)", TxnType.PURCHASE),
        ("MASTER返现", "ABCMC Merchant RebateRebateCHN", TxnType.REBATE),
        ("刷卡金转入", "蓝色宝箱刷卡金奖励,消费时间10/18,20:", TxnType.REBATE),
        ("刷卡金撤销", "天天返现,交易时间01/15,23:21", TxnType.REBATE),  # a rebate taken back
        ("银联入账", "农行银联信用卡25年1季度境外笔笔返", TxnType.REBATE),
        ("银联入账", "张三/付款尾号0009/", TxnType.REPAYMENT),
        ("卡卡转账", "", TxnType.REPAYMENT),
        ("卡卡转账", "10元还款金-3月", TxnType.REPAYMENT),
        ("人民币账户自动购汇转入还款", "汇率:7.1380900", TxnType.FX_TRANSFER),
        ("自动购汇转入外币账户还款", "", TxnType.FX_TRANSFER),
        ("网上消费退货", "程支付退款", TxnType.REFUND),
        ("跨行消费退货", "(特约)龙腾出行", TxnType.REFUND),
        ("短信服务费", "", TxnType.FEE),
        ("境外取现手续费", "", TxnType.FEE),
        ("已免除年费580.00元", "", TxnType.FEE),
        ("境外取现", "SEVEN BANKYAMANASHIJPN", TxnType.CASH),
        ("利息", "本期已优惠的利息金额:0.00元", TxnType.INTEREST),
        ("某种新摘要", "", TxnType.ADJUSTMENT),  # unknown: an adjustment, and a warning
    ],
)
def test_old_template_kinds(kind, place, expected):
    from autobill.parse.abc import _Statement

    statement = _Statement.__new__(_Statement)
    statement.warnings = []
    txn_type, fx_rate, _ = statement._classify_old(kind, place)
    assert txn_type == expected
    assert bool(statement.warnings) == (expected == TxnType.ADJUSTMENT)  # only unknowns warn
    if kind.startswith("人民币账户自动购汇"):
        assert fx_rate == D("7.1380900")


def test_old_template_supplementary_card():
    """A supplementary card reads "1234附" in the card column."""
    from autobill.parse.abc import _Row, _Statement

    statement = _Statement(load_old())
    statement.card_last4 = "0001"
    row = _Row(
        ["", "20250518", "20250518", "0009附", "网上消费", "某商户", "10.00/CNY", "-10.00/CNY"]
    )
    txn = statement._transaction_old(1, row)
    assert (txn.card_last4, txn.amount, txn.txn_type) == ("0009", D("10.00"), TxnType.PURCHASE)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("【交易明细】", "【交易】"),  # a section gone
        ("本期新增应还款额", "本期新增"),  # a summary column renamed
        ("交易摘要", "摘要"),  # the transaction header changed
    ],
)
def test_old_template_changes_raise(old, new):
    """Never a silent empty result: anything the old template must have, it must have."""
    with pytest.raises(TemplateChanged):
        parser.parse(with_html(load_old(), old, new))


def test_old_template_cross_check_catches_a_real_difference():
    """The statement-day cashback allowance does not swallow real differences."""
    bill = parse_one(with_html(load_old(), "-217.19", "-218.19"))
    assert bill.status == "WARN" and any("账户信息区" in w for w in bill.warnings)
