"""Reading the central mailbox (docs/fetcher.md#imap), only against a fake IMAP server."""

import email
import email.policy
from email.message import EmailMessage
from pathlib import Path

import pytest
from fakes import FakeFrankfurter, FakeIMAP, FakeSMTP
from typer.testing import CliRunner

from autobill import fx as fx_module
from autobill.cli import app
from autobill.config import FetcherConfig, SmtpReportConfig
from autobill.fetch.imap import ImapSource, Mailbox, MailboxError, load_cursor
from autobill.fetch.mime import addressed_to, is_own_report, split_forwarded
from autobill.notify.mail import Mailer, smtp_password
from autobill.pipeline import process
from autobill.store.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
ABC = sorted((FIXTURES / "abc").glob("*.eml"))
ALIAS = "bills.alias@icloud.com"
CONFIG = FetcherConfig(enabled=True, imap_server="imap.example.invalid", username="me@icloud.com")
RATES = {("USD", "2026-09-02"): "6.7215", ("USD", "2026-09-04"): "6.7109"}


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeIMAP.instances.clear()
    FakeSMTP.instances.clear()


def forwarded(*originals: bytes, to: str = ALIAS) -> bytes:
    """A wrapper e-mail carrying the originals as message/rfc822 attachments, the way
    "forward as attachment" sends them."""
    wrapper = EmailMessage()
    wrapper["From"] = "me@qq.example"
    wrapper["To"] = to
    wrapper["Subject"] = "转发：账单"
    wrapper.set_content("见附件")
    for data in originals:
        inner = email.message_from_bytes(data, policy=email.policy.default)
        wrapper.add_attachment(inner)
    return wrapper.as_bytes()


def readdress(data: bytes, to: str = ALIAS) -> bytes:
    """An original statement as if auto-forwarded to the alias (only To changes)."""
    msg = email.message_from_bytes(data, policy=email.policy.compat32)
    del msg["To"]
    msg["To"] = to
    return msg.as_bytes()


def mailbox(folders, config=CONFIG, password="app-password"):
    return Mailbox(config, password, FakeIMAP.factory(folders))


# --- unwrapping and filters -----------------------------------------------------------


def test_forwarded_attachments_become_separate_originals():
    data = [p.read_bytes() for p in ABC[:2]]
    parts = split_forwarded(forwarded(*data))
    assert len(parts) == 2
    for original, part in zip(data, parts, strict=True):
        # Re-serialising may fold encoded headers differently; the decoded values match.
        a = email.message_from_bytes(original, policy=email.policy.default)
        b = email.message_from_bytes(part, policy=email.policy.default)
        assert a["Message-ID"] == b["Message-ID"] and str(a["Subject"]) == str(b["Subject"])


def test_plain_mail_is_left_alone():
    data = ABC[0].read_bytes()
    assert split_forwarded(data) == [data]


def test_forwarded_statement_still_parses(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    from autobill.fetch.source import RawMail

    (part,) = split_forwarded(forwarded(ABC[0].read_bytes()))
    outcome = process(conn, isolated_data_dir, RawMail(part, "test"))
    assert outcome.status == "OK" and outcome.bills


def test_recipient_filter_and_own_reports():
    assert addressed_to(readdress(ABC[0].read_bytes()), ALIAS)
    assert not addressed_to(ABC[0].read_bytes(), ALIAS)
    report = b"X-AutoBill-Report: true\r\nSubject: x\r\n\r\nbody"
    assert is_own_report(report) and not is_own_report(ABC[0].read_bytes())


# --- the mailbox session --------------------------------------------------------------


def test_wrong_password_is_a_clear_error():
    with pytest.raises(MailboxError, match="登录被拒绝"):
        with mailbox({}, password="wrong"):
            pass


def test_unreachable_server_is_a_clear_error():
    def refuse(*args, **kwargs):
        raise ConnectionRefusedError("refused")

    with pytest.raises(MailboxError, match="连不上"):
        with Mailbox(CONFIG, "app-password", refuse):
            pass


def test_repr_hides_password():
    assert "app-password" not in repr(mailbox({}))


# --- the source: read only, cursors, filters -----------------------------------------


def folders_with(messages, validity=7):
    return {"AutoBill": (validity, dict(messages)), "Junk": (3, {})}


def test_reads_new_messages_once_and_read_only(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    folders = folders_with({5: readdress(ABC[0].read_bytes()), 9: readdress(ABC[1].read_bytes())})
    with mailbox(folders) as box:
        got = list(ImapSource(box, conn).iter_new())
    assert [m.source for m in got] == ["imap:AutoBill:7:5", "imap:AutoBill:7:9"]
    (imap,) = FakeIMAP.instances
    assert all(c[2] is True for c in imap.commands if c[0] == "SELECT")  # EXAMINE only
    assert load_cursor(conn, "AutoBill").last_uid == 9

    with mailbox(folders) as box:
        assert list(ImapSource(box, conn).iter_new()) == []  # nothing new
    folders["AutoBill"][1][12] = readdress(ABC[2].read_bytes())
    with mailbox(folders) as box:
        assert [m.source for m in ImapSource(box, conn).iter_new()] == ["imap:AutoBill:7:12"]


def test_uidvalidity_change_reads_the_folder_again(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    messages = {1: readdress(ABC[0].read_bytes())}
    with mailbox(folders_with(messages, validity=7)) as box:
        assert len(list(ImapSource(box, conn).iter_new())) == 1
    with mailbox(folders_with(messages, validity=8)) as box:
        assert len(list(ImapSource(box, conn).iter_new())) == 1  # renumbered: rescanned


def test_failed_processing_leaves_the_message_for_next_run(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    folders = folders_with({5: readdress(ABC[0].read_bytes())})
    with mailbox(folders) as box:
        for _mail in ImapSource(box, conn).iter_new():
            break  # the consumer stopped (crashed) while handling UID 5
    assert load_cursor(conn, "AutoBill") is None
    with mailbox(folders) as box:
        assert len(list(ImapSource(box, conn).iter_new())) == 1


def test_only_mail_to_the_alias_and_never_our_reports(isolated_data_dir):
    conn = connect(isolated_data_dir / "autobill.db")
    report = b"X-AutoBill-Report: true\r\nTo: " + ALIAS.encode() + b"\r\n\r\nx"
    folders = {
        "AutoBill": (7, {1: readdress(ABC[0].read_bytes()), 2: report}),
        "Junk": (3, {4: ABC[1].read_bytes()}),  # spam not sent to the alias
    }
    config = CONFIG.model_copy(update={"only_to": ALIAS})
    with mailbox(folders, config) as box:
        source = ImapSource(box, conn)
        got = list(source.iter_new())
    assert [m.source for m in got] == ["imap:AutoBill:7:1"]
    assert source.stats.seen == 3 and sum(source.stats.skipped.values()) == 2
    assert load_cursor(conn, "Junk").last_uid == 4  # skipped mail is not read again


# --- sending with STARTTLS (iCloud, port 587) -----------------------------------------


def test_starttls_mailer(monkeypatch):
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    config = SmtpReportConfig(
        enabled=True, smtp_server="smtp.example.invalid", smtp_port=587, security="starttls",
        username="me@icloud.com", to_addr="me@icloud.com",
    )  # fmt: skip
    Mailer(config, "app-password").check_login()
    (smtp,) = FakeSMTP.instances
    assert smtp.port == 587 and smtp.tls_started
    assert smtp.logins == [("me@icloud.com", "app-password")]
    assert smtp.sent == []  # checking never sends


def test_smtp_password_falls_back_to_the_imap_one(monkeypatch):
    monkeypatch.delenv("AUTOBILL_SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("AUTOBILL_IMAP_PASSWORD", "shared")
    assert smtp_password() == "shared"
    monkeypatch.setenv("AUTOBILL_SMTP_PASSWORD", "own")
    assert smtp_password() == "own"


# --- the commands ---------------------------------------------------------------------

runner = CliRunner()


def write_config(data_dir: Path, folders="[AutoBill, Junk]", smtp=True) -> None:
    text = (
        "mail_fetcher:\n  enabled: true\n  imap_server: imap.example.invalid\n"
        f"  username: me@icloud.com\n  folders: {folders}\n  only_to: {ALIAS}\n"
    )
    if smtp:
        text += (
            "notifier:\n  smtp_report:\n    enabled: true\n    smtp_server: smtp.example.invalid\n"
            "    smtp_port: 587\n    security: starttls\n"
            "    username: me@icloud.com\n    to_addr: me@icloud.com\n"
        )
    (data_dir / "config.yaml").write_text(text, encoding="utf-8")


@pytest.fixture
def cli_env(isolated_data_dir, monkeypatch):
    monkeypatch.setattr(fx_module, "http_fetch", FakeFrankfurter(RATES))
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("autobill.cli.find_browser", lambda configured=None: None)
    monkeypatch.setenv("AUTOBILL_IMAP_PASSWORD", "app-password")
    monkeypatch.delenv("AUTOBILL_SMTP_PASSWORD", raising=False)
    return isolated_data_dir


def use_folders(monkeypatch, folders):
    monkeypatch.setattr("imaplib.IMAP4_SSL", FakeIMAP.factory(folders))


def test_check_mailbox_all_good(cli_env, monkeypatch):
    write_config(cli_env)
    use_folders(monkeypatch, folders_with({1: readdress(ABC[0].read_bytes())}))
    result = runner.invoke(app, ["check-mailbox"])
    assert result.exit_code == 0, result.output
    assert "✓ 文件夹 AutoBill：1 封邮件" in result.output and "全部正常" in result.output
    assert "没有发送任何邮件" in result.output and FakeSMTP.instances[0].sent == []
    assert "app-password" not in result.output


def test_check_mailbox_missing_folder_and_bad_login(cli_env, monkeypatch):
    write_config(cli_env)
    use_folders(monkeypatch, {"INBOX": (1, {})})
    result = runner.invoke(app, ["check-mailbox"])
    assert result.exit_code == 1 and "找不到文件夹 AutoBill" in result.output
    monkeypatch.setenv("AUTOBILL_IMAP_PASSWORD", "wrong")
    result = runner.invoke(app, ["check-mailbox"])
    assert result.exit_code == 1 and "登录被拒绝" in result.output and "wrong" not in result.output


def test_non_ascii_folder_names_are_refused(cli_env, monkeypatch):
    write_config(cli_env, folders="[AutoBill 原始账单]")
    use_folders(monkeypatch, {})
    result = runner.invoke(app, ["check-mailbox"])
    assert result.exit_code == 1 and "只能用英文" in result.output


def test_check_mailbox_without_config(cli_env):
    result = runner.invoke(app, ["check-mailbox"])
    assert result.exit_code == 1 and "mail_fetcher" in result.output


def test_run_fetches_processes_and_reports(cli_env, monkeypatch):
    write_config(cli_env)
    statements = [readdress(p.read_bytes()) for p in ABC]
    use_folders(monkeypatch, folders_with(dict(enumerate(statements, start=1))))
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    assert "共 3 封：OK 3" in result.output
    assert "已发送报表邮件 1 封（新账单 3 份）" in result.output
    (smtp,) = [s for s in FakeSMTP.instances if s.sent]
    assert smtp.tls_started and smtp.sent[0]["To"] == "me@icloud.com"

    again = runner.invoke(app, ["run"])  # nothing new: no mail, no report
    assert "处理了 0 封" in again.output and "已发送报表邮件 0 封" in again.output


def test_run_unwraps_mail_forwarded_as_attachment(cli_env, monkeypatch):
    write_config(cli_env, smtp=False)
    batch = forwarded(*(p.read_bytes() for p in ABC))
    use_folders(monkeypatch, folders_with({1: batch}))
    result = runner.invoke(app, ["run", "--no-send"])
    assert result.exit_code == 0, result.output
    assert "共 3 封：OK 3" in result.output


def test_run_reports_login_failure(cli_env, monkeypatch):
    write_config(cli_env)
    use_folders(monkeypatch, {})
    monkeypatch.setenv("AUTOBILL_IMAP_PASSWORD", "wrong")
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1 and "收信失败" in result.output
