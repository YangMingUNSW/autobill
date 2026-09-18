"""Sending report e-mails: only with a fake SMTP server, never for real."""

from pathlib import Path

import pytest
from fakes import FakeFrankfurter, FakeSMTP
from typer.testing import CliRunner

from autobill import fx
from autobill.cli import app
from autobill.config import FxConfig, SmtpReportConfig
from autobill.fetch.source import DirectorySource
from autobill.fx import FxRates
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
    assert "不附标准账单 PDF" in result.output  # no browser in this test
    assert SECRET not in result.output
    assert sum(len(s.sent) for s in FakeSMTP.instances) == 1


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
    assert "2026年9月账单" in html and "农业银行 0001" in html and "<img" not in html
    assert FakeSMTP.instances == []  # preview never sends


def test_preview_email_unknown_month(cli_env):
    runner.invoke(app, ["import-dir", "--no-send", str(FIXTURES / "abc")])
    assert runner.invoke(app, ["preview-email", "--cycle", "2030-01"]).exit_code != 0
    assert runner.invoke(app, ["preview-email", "--cycle", "2030-13"]).exit_code != 0
