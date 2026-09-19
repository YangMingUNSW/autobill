"""Alert e-mails (docs/notify.md#提醒邮件): only against fake IMAP and SMTP servers."""

import email
import email.policy
from email.message import EmailMessage
from pathlib import Path

import pytest
from fakes import FakeFrankfurter, FakeIMAP, FakeSMTP
from typer.testing import CliRunner

from autobill import fx as fx_module
from autobill.cli import app
from autobill.fetch.mime import is_own_report
from autobill.notify import alerts
from autobill.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
ABC = sorted((FIXTURES / "abc").glob("*.eml"))
ALIAS = "bills.alias@icloud.com"
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109"}
runner = CliRunner()


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeIMAP.instances.clear()
    FakeSMTP.instances.clear()


@pytest.fixture
def env(isolated_data_dir, monkeypatch):
    monkeypatch.setattr(fx_module, "http_fetch", FakeFrankfurter(RATES))
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("autobill.cli.find_browser", lambda configured=None: None)
    monkeypatch.setenv("AUTOBILL_IMAP_PASSWORD", "app-password")
    (isolated_data_dir / "config.yaml").write_text(
        "mail_fetcher:\n  enabled: true\n  imap_server: imap.example.invalid\n"
        f"  username: me@icloud.com\n  folders: [AutoBill]\n  only_to: {ALIAS}\n"
        "notifier:\n  smtp_report:\n    enabled: true\n    smtp_server: smtp.example.invalid\n"
        "    smtp_port: 587\n    security: starttls\n"
        "    username: me@icloud.com\n    to_addr: me@icloud.com\n",
        encoding="utf-8",
    )
    return isolated_data_dir


def to_alias(data: bytes) -> bytes:
    msg = email.message_from_bytes(data, policy=email.policy.compat32)
    del msg["To"]
    msg["To"] = ALIAS
    return msg.as_bytes()


def mail(subject: str, sender: str, body: str, message_id: str) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ALIAS, subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Wed, 02 Sep 2026 18:01:38 +0800"
    msg.set_content(body, subtype="html")
    return msg.as_bytes()


def mailbox_with(monkeypatch, messages: dict[int, bytes]):
    folders = {"AutoBill": (7, messages)}
    monkeypatch.setattr("imaplib.IMAP4_SSL", FakeIMAP.factory(folders))
    return folders


def sent(kind: str = "提醒"):
    return [m for s in FakeSMTP.instances for m in s.sent if kind in str(m["Subject"])]


def test_unrecognized_mail_is_alerted_once(env, monkeypatch):
    notice = mail("您的账单邮箱已变更", "notice@bank.example", "<p>验证码 123456</p>", "<n1@x>")
    folders = mailbox_with(monkeypatch, {1: notice})
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    (alert,) = sent()
    assert "收到一封不认识的邮件" in str(alert["Subject"])
    text = alert.get_body(("plain",)).get_content()
    assert "您的账单邮箱已变更" in text and "notice@bank.example" in text
    assert is_own_report(alert.as_bytes())  # never read back as a statement

    runner.invoke(app, ["run", "--rescan"])  # the same mail met again: retried, not re-alerted
    assert len(sent()) == 1
    folders["AutoBill"][1][2] = mail("again", "x@y.example", "<p>x</p>", "<n2@x>")
    runner.invoke(app, ["run"])
    assert len(sent()) == 2  # a different mail is a new alert


def test_failed_statement_is_alerted(env, monkeypatch):
    broken = mail(
        "中国农业银行金穗信用卡电子对账单", "e-statement@creditcard.abchina.com.cn",
        "<p>您的信用卡账户信息</p><p>账务说明</p><p>交易明细</p>", "<broken@x>",
    )  # fmt: skip
    mailbox_with(monkeypatch, {1: broken})
    result = runner.invoke(app, ["run"])
    assert "FAILED" in result.output
    (alert,) = sent()
    assert "账单解析失败（ABC）" in str(alert["Subject"])
    assert "TemplateChanged" in alert.get_body(("plain",)).get_content()


def test_new_card_is_alerted_but_not_on_the_first_import(env, monkeypatch):
    folders = mailbox_with(monkeypatch, {1: to_alias(ABC[0].read_bytes())})
    runner.invoke(app, ["run"])
    assert sent() == []  # first statement ever: every card is new, that is not news
    folders["AutoBill"][1][2] = to_alias(ABC[1].read_bytes())  # another ABC card
    runner.invoke(app, ["run"])
    (alert,) = sent()
    assert "发现新卡：农业银行" in str(alert["Subject"])
    assert "card_aliases" in alert.get_body(("plain",)).get_content()


def test_login_failure_alerts_once_and_again_after_recovery(env, monkeypatch):
    mailbox_with(monkeypatch, {})
    monkeypatch.setenv("AUTOBILL_IMAP_PASSWORD", "revoked")
    monkeypatch.setenv("AUTOBILL_SMTP_PASSWORD", "smtp-still-works")
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1 and "收信失败" in result.output
    assert len(sent("登录邮箱失败")) == 1
    runner.invoke(app, ["run"])
    assert len(sent("登录邮箱失败")) == 1  # still broken: no repeat every half hour
    monkeypatch.setenv("AUTOBILL_IMAP_PASSWORD", "app-password")
    assert runner.invoke(app, ["run"]).exit_code == 0  # fixed: the alert is cleared
    monkeypatch.setenv("AUTOBILL_IMAP_PASSWORD", "revoked-again")
    runner.invoke(app, ["run"])
    assert len(sent("登录邮箱失败")) == 2  # a new failure is news again


def test_no_send_keeps_alerts_for_the_next_run(env, monkeypatch):
    mailbox_with(monkeypatch, {1: mail("x", "a@b.example", "<p>x</p>", "<n@x>")})
    result = runner.invoke(app, ["run", "--no-send"])
    assert "没有发送报表和提醒邮件" in result.output and sent() == []
    runner.invoke(app, ["run"])
    assert len(sent()) == 1


def test_alerts_table_dedupes_by_kind_and_key(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    a = alerts.new_card("ABC:0001")
    assert alerts.record(conn, [a, a]) == 1
    assert alerts.record(conn, [a]) == 0
    assert [x.key for _, x in alerts.pending(conn)] == ["ABC:0001"]


def test_alert_email_is_clean(isolated_data_dir):
    msg = alerts.build_email(
        [alerts.new_card("ABC:0001"), alerts.mailbox("登录被拒绝")], "me@x.example", "me@x.example"
    )
    assert str(msg["Subject"]) == "⚠ AutoBill 提醒：发现新卡：农业银行 0001 等 2 条"
    html = msg.get_body(("html",)).get_content()
    assert "format-detection" in html and "prefers-color-scheme: dark" in html
    assert "<script" not in html and "http" not in html
    assert chr(0xFE0F) not in str(msg["Subject"])  # no invisible variation selector
