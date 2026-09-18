"""CCB parser (docs/banks/ccb.md): the real sample, plus synthetic statements built with the
same layout for cases the single sample does not cover (spending, FX, deposits, no rows)."""

import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest

from autobill.fetch.message import RawMessage
from autobill.model import TxnType
from autobill.parse.base import TemplateChanged
from autobill.parse.ccb import CcbHtmlParser

FIXTURE = Path(__file__).parent / "fixtures" / "ccb" / "ccb_visa_2026-07.eml"
D = Decimal
parser = CcbHtmlParser()


def load() -> RawMessage:
    return RawMessage.from_bytes(FIXTURE.read_bytes())


def parse_one(msg: RawMessage):
    (bill,) = parser.parse(msg)
    return bill


def with_html(msg: RawMessage, html: str) -> RawMessage:
    return dataclasses.replace(msg, html_parts=[html])


def replaced(old: str, new: str) -> RawMessage:
    msg = load()
    assert old in msg.html
    return with_html(msg, msg.html.replace(old, new))


def row(*cells: str) -> str:
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def synthetic(summary, sections, txns, min_payment="-") -> RawMessage:
    """A CCB-shaped statement: nested container tables, like the real e-mail."""
    summary_rows = "".join(row(*r) for r in summary)
    txn_rows = "".join(row(*r) for r in sections + txns)
    html = f"""<table><tr><td>
      <table>{row("龙卡信用卡对账单 Credit Card Statement")}</table>
      <table><tr><td><table>
        {
        row(
            "账户币种Currency",
            "上期全部应还款额",
            "+",
            "消费/取现/其它费用",
            "-",
            "还款/退货/费用返还",
            "=",
            "本期全部应还款额",
        )
    }
        {summary_rows}
      </table></td></tr></table>
      <table>{row("信用信息 Credit Information")}{
        row("本期账单日 Statement Date", "2026-08-10")
    }</table>
      <table>
        {row("应还款信息 Payment Information")}
        {row("账单周期Statement Cycle", "2026/07/11-2026/08/10", "本期到期还款日", "2026/09/01")}
        {row("账户币种Currency", "本期全部应还款额", "最低还款额Min.Payment", "争议款/笔数")}
        {row("人民币（CNY）", "-", min_payment, "-")}
        <tr><td><table>{row("◇ 提示文字")}</table></td></tr>
      </table>
      <table>
        {row("【交易明细】")}
        {row("交易日", "银行记账日", "卡号后四位", "交易描述", "交易币/金额", "结算币/金额")}
        {row("T-Date", "P-Date", "Card Number", "Description", "Trans.Curr/Amt", "Sett.Curr/Amt")}
        {txn_rows}
        {row("*** 结束 The End ***")}
      </table>
    </td></tr></table>"""
    return with_html(load(), html)


# --- the real sample ----------------------------------------------------------


def test_snapshot(snapshot):
    snapshot("ccb/ccb_visa_2026-07", parse_one(load()).model_dump(mode="json"))


def test_real_sample_fields():
    bill = parse_one(load())
    assert bill.status == "OK" and bill.warnings == []
    assert bill.account_id == "CCB:0004" and bill.cards == ["0004"]
    assert str(bill.statement_date) == "2026-07-10"
    assert (str(bill.period_start), str(bill.period_end)) == ("2026-06-11", "2026-07-10")
    assert str(bill.due_date) == "2026-08-01" and str(bill.email_date) == "2026-07-11"
    (b,) = bill.balances  # all-zero USD and EUR rows are skipped
    assert (b.currency, b.previous_balance, b.new_charges, b.payments_credits, b.amount_due) == (
        "CNY",
        D("15450.83"),
        D("0.00"),
        D("15450.83"),
        D("0.00"),
    )
    assert b.min_payment == D("0")  # printed as "-"


def test_repayment_sign_is_kept():
    (txn,) = parse_one(load()).transactions
    assert txn.txn_type == TxnType.REPAYMENT
    assert txn.amount == D("-15450.83")  # CCB already prints repayments as negative
    assert txn.description_raw == "手机银行 按卡转账还款 张三"  # &nbsp; padding normalised


def test_matches_only_ccb():
    for path in sorted(FIXTURE.parent.parent.rglob("*.eml")):
        msg = RawMessage.from_bytes(path.read_bytes())
        assert parser.matches(msg) == (path.parent.name == "ccb"), path.name


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("【交易明细】", "交易"),
        ("上期全部应还款额", "上期"),
        (">交易日<", "><"),
    ],
)
def test_missing_key_parts_raise_template_changed(old, new):
    with pytest.raises(TemplateChanged):
        parser.parse(replaced(old, new))


def test_missing_dates_raise_template_changed():
    msg = load()
    assert "本期账单日" in msg.html and "账单周期" in msg.html
    html = msg.html.replace("本期账单日", "账单日").replace("账单周期", "周期")
    with pytest.raises(TemplateChanged):
        parser.parse(with_html(msg, html))


# --- synthetic statements (inferred formats, docs/banks/ccb.md §7) -------------

CNY_SECTION = ("[人民币账户] RMB Account", "上期账单余额(Previous Balance)", "1,000.00")


def test_spending_statement_reconciles():
    msg = synthetic(
        summary=[("人民币（CNY）", "1,000.00", "350.00", "1,000.00", "350.00")],
        sections=[CNY_SECTION],
        txns=[
            ("2026-07-12", "2026-07-12", "0004", "手机银行 按卡转账还款",
             "CNY", "-1,000.00", "CNY", "-1,000.00"),
            ("2026-07-20", "2026-07-21", "0004", "某某超市", "CNY", "300.00", "CNY", "300.00"),
            ("2026-08-01", "2026-08-02", "0004", "年费", "CNY", "50.00", "CNY", "50.00"),
        ],
        min_payment="35.00",
    )  # fmt: skip
    bill = parse_one(msg)
    assert bill.status == "OK", bill.warnings
    assert [t.txn_type for t in bill.transactions] == [
        TxnType.REPAYMENT,
        TxnType.PURCHASE,
        TxnType.FEE,
    ]
    assert bill.transactions[1].merchant == "某某超市"
    assert bill.balances[0].min_payment == D("35.00")


def test_foreign_currency_purchase():
    msg = synthetic(
        summary=[
            ("人民币（CNY）", "0.00", "0.00", "0.00", "0.00"),
            ("美元（USD）", "0.00", "20.00", "0.00", "20.00"),
        ],
        sections=[("[美元账户] USD Account", "上期账单余额(Previous Balance)", "0.00")],
        txns=[("2026-07-20", "2026-07-21", "0004", "AMAZON", "AUD", "30.00", "USD", "20.00")],
    )  # fmt: skip
    bill = parse_one(msg)
    assert bill.status == "OK", bill.warnings
    assert [b.currency for b in bill.balances] == ["CNY", "USD"]  # CNY is always kept
    (txn,) = bill.transactions
    assert (txn.amount, txn.currency, txn.orig_amount, txn.orig_currency) == (
        D("20.00"),
        "USD",
        D("30.00"),
        "AUD",
    )


def test_negative_balance_is_read_as_deposit():
    msg = synthetic(
        summary=[("人民币（CNY）", "100.00", "0.00", "150.00", "-50.00")],
        sections=[("[人民币账户] RMB Account", "上期账单余额(Previous Balance)", "100.00")],
        txns=[("2026-07-12", "2026-07-12", "0004", "按卡转账还款",
               "CNY", "-150.00", "CNY", "-150.00")],
    )  # fmt: skip
    bill = parse_one(msg)
    assert bill.status == "OK", bill.warnings
    (b,) = bill.balances
    assert (b.amount_due, b.deposit) == (D("0"), D("50.00"))


def test_no_transactions_means_unknown_card():
    msg = synthetic(
        summary=[("人民币（CNY）", "0.00", "0.00", "0.00", "0.00")],
        sections=[],
        txns=[],
    )
    bill = parse_one(msg)
    assert bill.account_id == "CCB:unknown" and bill.cards == []
    assert bill.status == "WARN"
    assert any("取不到卡号" in w for w in bill.warnings)
    # The CNY row is always kept, so a zero statement is still reconciled (not UNVERIFIED).
    assert [b.currency for b in bill.balances] == ["CNY"]
    assert not any(w.startswith("对账") for w in bill.warnings)


def test_section_balance_cross_check():
    msg = synthetic(
        summary=[("人民币（CNY）", "1,000.00", "0.00", "1,000.00", "0.00")],
        sections=[("[人民币账户] RMB Account", "上期账单余额(Previous Balance)", "999.00")],
        txns=[("2026-07-12", "2026-07-12", "0004", "还款", "CNY", "-1,000.00", "CNY", "-1,000.00")],
    )  # fmt: skip
    bill = parse_one(msg)
    assert bill.status == "WARN"
    assert any("上期账单余额 999.00" in w for w in bill.warnings)


def test_unrecognised_row_in_transactions_is_flagged():
    msg = synthetic(
        summary=[("人民币（CNY）", "1,000.00", "0.00", "1,000.00", "0.00")],
        sections=[CNY_SECTION],
        txns=[
            ("2026-07-12", "2026-07-12", "0004", "还款", "CNY", "-1,000.00", "CNY", "-1,000.00"),
            ("小计", "1,000.00"),
        ],
    )  # fmt: skip
    bill = parse_one(msg)
    assert bill.status == "WARN"
    assert any("无法识别的明细行" in w for w in bill.warnings)


def test_blank_card_cell_keeps_row_positions():
    # Rows such as instalment principal may have no card number (as with ABC); the empty
    # cell must not shift the other columns.
    msg = synthetic(
        summary=[("人民币（CNY）", "1,000.00", "100.00", "1,000.00", "100.00")],
        sections=[CNY_SECTION],
        txns=[
            ("2026-07-12", "2026-07-12", "0004", "还款", "CNY", "-1,000.00", "CNY", "-1,000.00"),
            ("2026-07-15", "2026-07-15", "", "账单分期本金", "CNY", "100.00", "CNY", "100.00"),
        ],
    )  # fmt: skip
    bill = parse_one(msg)
    assert bill.status == "OK", bill.warnings
    principal = bill.transactions[1]
    assert (principal.card_last4, principal.txn_type, principal.amount) == (
        None,
        TxnType.INSTALLMENT,
        D("100.00"),
    )
