"""What the commands do, apart from reading their arguments: one run (fetch, process,
classify, report), importing a folder, sending the alerts and the reports. See
docs/pipeline.md#运行层.

Each job says what it is doing through `say` - the command line passes typer.echo - so
`run`, `serve` and `import-dir` share the same jobs, and the tests read the lines a user
sees. A job that cannot run says why and returns, rather than exiting: whether that ends
the program is the command's decision.
"""

from __future__ import annotations

import smtplib
import sqlite3
from collections.abc import Callable, Iterable

from autobill.categorize import load_rules
from autobill.classify import classify_merchants
from autobill.config import Config, data_dir, load_config
from autobill.fetch.imap import PASSWORD_ENV as IMAP_PASSWORD_ENV
from autobill.fetch.imap import ImapSource, Mailbox, MailboxError, imap_password
from autobill.fetch.mime import split_forwarded
from autobill.fetch.source import RawMail
from autobill.fx import FxRates
from autobill.notify import alerts
from autobill.notify.mail import PASSWORD_ENV, Mailer, smtp_password
from autobill.notify.reports import send_pending_reports, send_year_review
from autobill.pipeline import Outcome, outdated_emails, process, record_parsers, reparse
from autobill.store.db import connect
from autobill.suggest import SuggesterUnavailable, get_suggester

Say = Callable[[str], None]  # where a job's progress lines go


class NotReady(Exception):
    """config.yaml or the environment lacks something a job needs; the message says what."""


def open_database() -> sqlite3.Connection:
    return connect(data_dir() / "autobill.db")


def open_mailbox(config: Config) -> Mailbox:
    """The configured central mailbox; NotReady says what is missing."""
    fetcher = config.mail_fetcher
    if not fetcher.ready:
        raise NotReady("还没有配置收账单的邮箱（config.yaml 的 mail_fetcher），见 docs/setup.md。")
    if fetcher.bad_folders:
        names = "、".join(fetcher.bad_folders)
        raise NotReady(f"文件夹名只能用英文和数字（{names}），请在邮箱里改名，比如 AutoBill。")
    password = imap_password()
    if password is None:
        raise NotReady(f"没有找到环境变量 {IMAP_PASSWORD_ENV}（App 专用密码或授权码）。")
    return Mailbox(fetcher, password)


def import_mails(conn: sqlite3.Connection, mails: Iterable[RawMail], say: Say = print) -> list:
    """Process each mail (forwarded-as-attachment ones unwrapped first); say one line per
    original e-mail and a summary. Returns the outcomes."""
    aliases = load_config().cards.card_aliases
    counts: dict[str, int] = {}
    outcomes: list[Outcome] = []
    for mail in mails:
        parts = split_forwarded(mail.data)
        for i, data in enumerate(parts):
            source = mail.source if len(parts) == 1 else f"{mail.source}#{i + 1}"
            outcome = process(conn, data_dir(), RawMail(data, source), aliases)
            outcomes.append(outcome)
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            name = source.rsplit("/", 1)[-1]
            accounts = ", ".join(b.account_id for b in outcome.bills)
            detail = accounts or outcome.error or ""
            say(f"{outcome.status:<12} {name}  {detail}")
    if counts:
        summary = "，".join(f"{status} {n}" for status, n in sorted(counts.items()))
        say("")
        say(f"共 {sum(counts.values())} 封：{summary}")
    return outcomes


def run_once(send: bool = True, rescan: bool = False, say: Say = print) -> int:
    """One fetch-process-report cycle, shared by run and serve. Returns the exit code."""
    config = load_config()
    conn = open_database()
    if rescan:
        # Harmless: e-mails already processed are skipped by Message-ID; failed ones retried.
        conn.execute("DELETE FROM folder_cursors")
        say("从头重读文件夹：处理过的邮件会跳过，之前失败或不认识的会重新处理。")
    known = {r[0] for r in conn.execute("SELECT DISTINCT account_id FROM bills")}
    reparse_outdated(conn, config, known, say)
    try:
        with open_mailbox(config) as box:
            alerts.clear(conn, "mailbox", "login")  # logged in: an old login alert is over
            source = ImapSource(box, conn)
            outcomes = import_mails(conn, source.iter_new(), say)
            total = len(outcomes)
            alerts.record(conn, alerts.from_outcomes(outcomes, known))
            if source.stats.missing_folders:
                names = "、".join(source.stats.missing_folders)
                say(f"邮箱里还没有这些文件夹，已跳过：{names}")
            skipped = "，".join(f"{why} {n} 封" for why, n in source.stats.skipped.items())
            line = f"邮箱里的新邮件 {source.stats.seen} 封，处理了 {total} 封"
            say(line + (f"；跳过：{skipped}" if skipped else ""))
    except NotReady as exc:
        say(str(exc))
        return 1
    except MailboxError as exc:
        say(f"收信失败：{exc}")
        alerts.record(conn, [alerts.mailbox(str(exc))])
        if send:
            send_alerts(conn, say)  # may fail too when both use the same password
        return 1
    waiting = auto_classify(conn, config, say)
    if not send:
        say("按 --no-send 的要求，没有发送报表和提醒邮件。")
        return 0
    if waiting:  # the e-mails would show these merchants as 未分类: send them once all are asked
        say(
            f"AI 分类还有 {waiting} 个新商户排着队（每轮问 {config.ai.per_run} 个）："
            "账单邮件等全部分完再发。"
        )
        send_alerts(conn, say)
        return 0
    sent = send_reports(conn, say)  # a report not sent is retried next run
    send_alerts(conn, say)
    return 0 if sent else 1


def reparse_outdated(
    conn: sqlite3.Connection, config: Config, known: set[str], say: Say = print
) -> None:
    """After an update that changed a parser, read again the stored e-mails it would read
    differently, so no `reparse` by hand (docs/pipeline.md#运行层). Bills keep reported_at,
    so nothing is sent twice; a statement read for the first time is reported as new."""
    outdated = outdated_emails(conn)
    if outdated.email_ids:
        aliases = config.cards.card_aliases
        outcomes = [reparse(conn, data_dir(), i, aliases) for i in outdated.email_ids]
        counts: dict[str, int] = {}
        for o in outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        why = "、".join(outdated.changed)
        why = f"解析器更新了（{why}）" if why else "有账单是旧版解析器读的"
        summary = "，".join(f"{status} {n}" for status, n in sorted(counts.items()))
        say(f"{why}：重新解析了 {len(outcomes)} 封：{summary}。")
        alerts.record(conn, alerts.from_outcomes(outcomes, known))
    if outdated.changed:
        record_parsers(conn)


def auto_classify(conn: sqlite3.Connection, config: Config, say: Say = print) -> int:
    """Let the AI classify new merchants before the reports are built (ai.auto_classify).
    A failure is alerted once and never stops the run: those merchants stay 未分类.

    Returns how many new merchants are still waiting for a later run (ai.per_run) when
    this run asked some and went well; the reports wait for them, so a batch of history
    never goes out full of 未分类 (docs/notify.md#ai-分类). 0 when the AI is off, failed
    or asked nothing: the reports are never held by an AI that is not getting anywhere."""
    if not (config.ai.provider and config.ai.auto_classify):
        return 0
    try:
        suggester = get_suggester(config.ai)
    except SuggesterUnavailable as exc:
        say(f"AI 分类没有完成：{exc}")
        alerts.record(conn, [alerts.ai(str(exc))])
        return 0
    result = classify_merchants(conn, suggester, load_rules(conn), config.ai)
    if result.verdicts:
        line = f"AI 分类：问了 {len(result.verdicts)} 个新商户，分好 {len(result.used)} 个"
        say(line + (f"，还有 {result.left} 个下次再问" if result.left else ""))
    if result.error is not None:
        say(f"AI 分类没有完成：{result.error}")
        alerts.record(conn, [alerts.ai(str(result.error))])
        return 0
    alerts.clear(conn, "ai", "error")
    return result.left if result.verdicts else 0


def send_alerts(conn: sqlite3.Connection, say: Say = print) -> None:
    """E-mail pending alerts, if a mailbox is configured; failures are said, not raised."""
    config = load_config()
    smtp = config.notifier.smtp_report
    password = smtp_password()
    waiting = len(alerts.pending(conn))
    if not waiting:
        return
    if not smtp.ready or password is None:
        say(f"有 {waiting} 条提醒，但没有配置发信，这次没发出去。")
        return
    try:
        n = alerts.send_pending(conn, Mailer(smtp, password))
        say(f"已发送提醒邮件（{n} 条提醒），收件人 {smtp.to_addr}。")
    except (smtplib.SMTPException, OSError) as exc:
        say(f"提醒邮件发送失败：{type(exc).__name__}: {exc}。下次运行会再发。")


def report_mailer(config: Config, say: Say = print) -> Mailer | None:
    """The mailer for reports, or None with the reason said."""
    smtp = config.notifier.smtp_report
    if not smtp.ready:
        say("没有配置报表邮箱（config.yaml 的 notifier.smtp_report），这次不发送报表。")
        return None
    password = smtp_password()
    if password is None:
        missing = f"{PASSWORD_ENV} 或 {IMAP_PASSWORD_ENV}"
        say(f"没有找到环境变量 {missing}（邮箱密码），这次不发送报表。")
        return None
    return Mailer(smtp, password)


def send_reports(conn: sqlite3.Connection, say: Say = print) -> bool:
    """E-mail every statement month that is complete and not reported yet, then last
    year's review once its January has gone out complete. False when a report could not
    be sent; it is tried again next run."""
    config = load_config()
    mailer = report_mailer(config, say)
    if mailer is None:
        return True
    fx, rules = FxRates(conn, config.fx), load_rules(conn)
    result = send_pending_reports(
        conn,
        mailer,
        fx,
        rules,
        portfolio=config.cards.portfolio,
        backup=config.backup,
    )
    sent = f"已发送报表邮件 {result.emails} 封（新账单 {len(result.sent)} 份）"
    say(f"{sent}，收件人 {mailer.config.to_addr}。")
    if result.quiet:
        quiet = len(result.quiet)
        say(f"另有 {quiet} 份迟到的账单没有消费、也不用还钱，已记下，不单独发邮件。")
    for cycle, error in result.backup_errors:
        say(f"数据库备份失败（{cycle} 账单月）：{error}。报表照常发出，下个月会再备份。")
    if result.failed:
        cycle, error = result.failed
        say(f"发送失败（{cycle} 账单月）：{error}。没发出去的下次运行会再发。")
        return False
    year, error = send_year_review(conn, mailer, fx, rules)
    if error is not None:
        say(f"{year} 年度回顾发送失败：{error}。下次运行会再发。")
        return False
    if year is not None:
        say(f"已发送 {year} 年度回顾，收件人 {mailer.config.to_addr}。")
    return True
