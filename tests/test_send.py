"""Sending report e-mails: only with a fake SMTP server, never for real."""

import json
from pathlib import Path

import pytest
from fakes import FakeFrankfurter, FakeSMTP
from typer.testing import CliRunner

from autobill import fx
from autobill.categorize import UNCATEGORISED, load_rules
from autobill.cli import app
from autobill.config import FxConfig, SmtpReportConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
from autobill.model import TxnType
from autobill.notify.mail import Mailer
from autobill.pipeline import process, send_pending_reports
from autobill.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109"}
SMTP = SmtpReportConfig(
    enabled=True,
    smtp_server="smtp.example.invalid",
    username="bills@example.invalid",
    to_addr="me@example.invalid",
)
SECRET = "not-a-real-auth-code-7f3a"


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSMTP.instances.clear()


@pytest.fixture
def db(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    for mail in DirectorySource(FIXTURES / "abc").iter_new():
        process(conn, isolated_data_dir, mail)
    return conn, FxRates(conn, FxConfig(), FakeFrankfurter(RATES))


def pending(conn):
    return conn.execute("SELECT COUNT(*) FROM bills WHERE reported_at IS NULL").fetchone()[0]


def test_mailer_logs_in_and_sends():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "x"
    Mailer(SMTP, SECRET, FakeSMTP).send(msg)
    (smtp,) = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("smtp.example.invalid", 465)
    assert smtp.logins == [("bills@example.invalid", SECRET)] and smtp.sent == [msg]


def test_mailer_repr_hides_password():
    assert SECRET not in repr(Mailer(SMTP, SECRET, FakeSMTP))


def test_reported_at_survives_reimport_of_the_same_statement(db, isolated_data_dir):
    conn, rates = db
    send_pending_reports(conn, Mailer(SMTP, SECRET, FakeSMTP), rates)
    import re

    data = (FIXTURES / "abc" / "abc_mc_2026-09.eml").read_bytes()
    resent = re.sub(rb"(?mi)^Message-Id:[^\r\n]*", b"Message-ID: <again@example.invalid>", data)
    from autobill.fetch.source import RawMail

    process(conn, isolated_data_dir, RawMail(resent, "test:again"))
    assert pending(conn) == 0  # the statement was already reported; no second mail


# --- the import-dir command ---------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_env(isolated_data_dir, monkeypatch):
    monkeypatch.setattr(fx, "http_fetch", FakeFrankfurter(RATES))
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("autobill.cli.find_browser", lambda configured=None: None)
    return isolated_data_dir


def write_config(data_dir: Path, enabled: bool = True) -> None:
    (data_dir / "config.yaml").write_text(
        "notifier:\n  smtp_report:\n"
        f"    enabled: {str(enabled).lower()}\n"
        '    smtp_server: "smtp.example.invalid"\n'
        '    username: "bills@example.invalid"\n'
        '    to_addr: "me@example.invalid"\n',
        encoding="utf-8",
    )


def test_import_dir_sends_reports_when_configured(cli_env, monkeypatch):
    write_config(cli_env)
    monkeypatch.setenv("AUTOBILL_SMTP_PASSWORD", SECRET)
    result = runner.invoke(app, ["import-dir", str(FIXTURES / "abc")])
    assert result.exit_code == 0, result.output
    assert "已发送报表邮件 1 封（新账单 3 份）" in result.output  # one statement month
    assert "PDF" not in result.output  # no PDF by default: nothing to say about browsers
    assert SECRET not in result.output
    assert sum(len(s.sent) for s in FakeSMTP.instances) == 1


def test_pdf_attachments_are_opt_in(cli_env, monkeypatch):
    looked = []
    monkeypatch.setattr(
        "autobill.cli.find_browser", lambda configured=None: looked.append(1) or None
    )
    write_config(cli_env)
    monkeypatch.setenv("AUTOBILL_SMTP_PASSWORD", SECRET)
    runner.invoke(app, ["import-dir", "--no-send", str(FIXTURES / "abc")])
    runner.invoke(app, ["import-dir", str(FIXTURES / "abc")])
    assert looked == []  # default: no browser is even looked for
    with (cli_env / "config.yaml").open("a", encoding="utf-8") as f:
        f.write("statement:\n  email_pdf: true\n")
    import sqlite3

    with sqlite3.connect(cli_env / "autobill.db") as db:  # make the bills unreported again
        db.execute("UPDATE bills SET reported_at = NULL")
    result = runner.invoke(app, ["import-dir", str(FIXTURES / "abc")])
    assert looked and "不附标准账单 PDF" in result.output  # opted in, but no browser here


def test_import_dir_without_config_does_not_send(cli_env):
    result = runner.invoke(app, ["import-dir", str(FIXTURES / "abc")])
    assert result.exit_code == 0 and "没有配置报表邮箱" in result.output
    assert FakeSMTP.instances == []


def test_import_dir_disabled_config_does_not_send(cli_env, monkeypatch):
    write_config(cli_env, enabled=False)
    monkeypatch.setenv("AUTOBILL_SMTP_PASSWORD", SECRET)
    result = runner.invoke(app, ["import-dir", str(FIXTURES / "abc")])
    assert "没有配置报表邮箱" in result.output and FakeSMTP.instances == []


def test_import_dir_without_password_does_not_send(cli_env):
    write_config(cli_env)
    result = runner.invoke(app, ["import-dir", str(FIXTURES / "abc")])
    assert "AUTOBILL_SMTP_PASSWORD" in result.output and FakeSMTP.instances == []


def test_import_dir_no_send_flag(cli_env, monkeypatch):
    write_config(cli_env)
    monkeypatch.setenv("AUTOBILL_SMTP_PASSWORD", SECRET)
    result = runner.invoke(app, ["import-dir", "--no-send", str(FIXTURES / "abc")])
    assert "--no-send" in result.output and FakeSMTP.instances == []


def test_send_failure_exits_non_zero_without_leaking_password(cli_env, monkeypatch):
    write_config(cli_env)
    monkeypatch.setenv("AUTOBILL_SMTP_PASSWORD", SECRET)

    def broken(*args, **kwargs):
        return FakeSMTP(*args, **kwargs, fail_on_send=True)

    monkeypatch.setattr("smtplib.SMTP_SSL", broken)
    result = runner.invoke(app, ["import-dir", str(FIXTURES / "abc")])
    assert result.exit_code == 1 and "发送失败" in result.output
    assert SECRET not in result.output


def test_preview_email_command(cli_env):
    runner.invoke(app, ["import-dir", "--no-send", str(FIXTURES / "abc")])
    out = cli_env / "p.html"
    result = runner.invoke(app, ["preview-email", "-o", str(out)])
    assert result.exit_code == 0 and "2026-09 账单月" in result.output, result.output
    html = out.read_text(encoding="utf-8")
    assert "<h1>2026年9月</h1>" in html and "农业银行 0001" in html and "<img" not in html
    assert FakeSMTP.instances == []  # preview never sends


def test_preview_email_unknown_month(cli_env):
    runner.invoke(app, ["import-dir", "--no-send", str(FIXTURES / "abc")])
    assert runner.invoke(app, ["preview-email", "--cycle", "2030-01"]).exit_code != 0
    assert runner.invoke(app, ["preview-email", "--cycle", "2030-13"]).exit_code != 0


def sent_subjects():
    return [m["Subject"] for s in FakeSMTP.instances for m in s.sent]


def test_resend_rebuilds_the_month_from_the_data_as_it_stands(cli_env, monkeypatch):
    """After a fix (reparse, or the AI classifying merchants that were 未分类), the month
    can be sent again: same conversation, current numbers, nothing marked differently."""
    import sqlite3

    write_config(cli_env)
    monkeypatch.setenv("AUTOBILL_SMTP_PASSWORD", SECRET)
    runner.invoke(app, ["import-dir", str(FIXTURES / "abc")])
    first = FakeSMTP.instances[-1].sent[-1]
    assert "未分类" in first.get_body(("html",)).get_content()

    db = sqlite3.connect(cli_env / "autobill.db")
    reported = db.execute("SELECT id, reported_at FROM bills ORDER BY id").fetchall()
    rules = load_rules()  # the rules alone: which merchants are still 未分类
    unknown = {
        r[1] or r[0]
        for r in db.execute("SELECT description_raw, merchant FROM transactions"
                            " WHERE txn_type = 'purchase'")
        if rules.categorize(r[0], TxnType.PURCHASE, r[1]) == UNCATEGORISED
    }  # fmt: skip
    assert unknown
    db.executemany(  # as the AI would have written them after the first e-mail went out
        "INSERT INTO ai_categories VALUES (?, '健身', '健身', 'high', '', 0, NULL, NULL, 'm', 't')",
        [(name,) for name in unknown],
    )
    db.commit()

    result = runner.invoke(app, ["resend", "--cycle", "2026-09"])
    assert result.exit_code == 0, result.output
    assert "已重发 2026-09" in result.output and SECRET not in result.output
    again = FakeSMTP.instances[-1].sent[-1]
    assert again["Subject"] == first["Subject"] == "📊 2026年9月 信用卡账单"
    assert again["In-Reply-To"] == first["Message-ID"]  # same conversation
    html = again.get_body(("html",)).get_content()
    assert "未分类" not in html  # rebuilt with the AI answers stored since the first e-mail
    assert db.execute("SELECT id, reported_at FROM bills ORDER BY id").fetchall() == reported
    ids_ = json.loads(
        db.execute("SELECT message_ids FROM cycle_threads WHERE cycle = '2026-09'").fetchone()[0]
    )
    assert ids_ == [first["Message-ID"], again["Message-ID"]]


def test_resend_needs_a_month_that_has_statements(cli_env, monkeypatch):
    write_config(cli_env)
    monkeypatch.setenv("AUTOBILL_SMTP_PASSWORD", SECRET)
    runner.invoke(app, ["import-dir", "--no-send", str(FIXTURES / "abc")])
    assert runner.invoke(app, ["resend", "--cycle", "2030-01"]).exit_code != 0
    assert runner.invoke(app, ["resend", "--cycle", "2030-13"]).exit_code != 0
    assert FakeSMTP.instances == []


def test_resend_without_a_mailbox_explains_itself(cli_env):
    runner.invoke(app, ["import-dir", "--no-send", str(FIXTURES / "abc")])
    result = runner.invoke(app, ["resend", "--cycle", "2026-09"])
    assert result.exit_code == 1 and "没有配置报表邮箱" in result.output
    assert FakeSMTP.instances == []
