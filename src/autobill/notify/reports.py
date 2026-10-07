"""Sending the reports: the month's e-mail once a statement month is complete, and last
year's review once its January has gone out (docs/notify.md#账单月邮件, #年度回顾).
What goes into them is built in report/; this module decides when, sends, and records
what was sent, so nothing goes out twice.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from autobill import backup as backup_module
from autobill.ledger import REDUCING_TYPES, SPENDING_TYPES, today_in_china
from autobill.model import Bill
from autobill.report.cycle import cycle_complete
from autobill.report.cycle_mail import build_cycle_email, record_sent, thread_ids
from autobill.report.year import due_year
from autobill.report.year_mail import build_year_email
from autobill.store.db import load_bill, now


@dataclass
class SendResult:
    sent: list[int] = field(default_factory=list)  # bill ids now reported
    emails: int = 0
    failed: tuple[str, str] | None = None  # (statement month, error); the first failure stops
    backup_errors: list[tuple[str, str]] = field(default_factory=list)  # (month, error)
    quiet: list[int] = field(default_factory=list)  # late and empty: reported, no e-mail


def nothing_to_tell(bill: Bill) -> bool:
    """No spending, refund or rebate on it and nothing to pay: all an e-mail of its own
    would add to the month is a "无需还款" row."""
    counted = {*SPENDING_TYPES, *REDUCING_TYPES}
    return all(b.amount_due == 0 for b in bill.balances) and not any(
        t.txn_type in counted for t in bill.transactions
    )


def _attach_backup(conn: sqlite3.Connection, message, cycle: str, config) -> None:
    """A gzipped copy of the database rides along with the month's e-mail. A failure here
    must never cost the report itself: send_pending_reports catches it and the caller
    prints it."""
    if not config.enabled:
        return
    data = backup_module.dump(conn)
    message.add_attachment(
        data, maintype="application", subtype="gzip", filename=backup_module.name_for(cycle)
    )
    folder = backup_module.database_path(conn)
    if folder is not None:
        backup_module.write(data, folder.parent / "backups", cycle, config.keep)


def send_pending_reports(
    conn: sqlite3.Connection,
    mailer,
    fx,
    rules=None,
    *,
    portfolio=(),
    today=None,
    backup=None,
) -> SendResult:
    """One e-mail per statement month, sent once the month is complete: every expected card
    has issued its statement or run out of time (docs/notify.md#账单月邮件). Oldest month
    first. Until then the month's statements wait, so the one e-mail covers them all.

    reported_at is written only after the mail server accepted the message, so a failed
    send is retried on the next run and a successful one is never repeated. The first
    failure stops the run: when the login or server is broken, every later send would
    fail the same way.

    A statement arriving after its month's e-mail went out gets an e-mail of its own, but
    not when there is nothing to tell (nothing_to_tell): a card on a later statement day
    that was not used, its empty statement coming ten days after the others. It counts
    as reported, in `quiet`, and the month's next e-mail shows it anyway.

    `backup` (config.backup) carries a copy of the database out with the e-mail, so the
    mailbox holds one per month: see autobill/backup.py.
    """
    result = SendResult()
    months: dict[str, list[int]] = {}
    for bill_id, statement_date in conn.execute(
        "SELECT id, statement_date FROM bills WHERE reported_at IS NULL ORDER BY statement_date, id"
    ):
        months.setdefault(statement_date[:7], []).append(bill_id)

    for cycle, bill_ids in sorted(months.items()):
        # Asked before the e-mail is built: building it loads and renders every card of the
        # month, and a month still waiting for a card would do that every run for weeks.
        if not cycle_complete(conn, cycle, portfolio, today):
            continue
        if thread_ids(conn, cycle) and all(nothing_to_tell(load_bill(conn, i)) for i in bill_ids):
            stamp = now()
            conn.execute("BEGIN")
            try:
                conn.executemany(
                    "UPDATE bills SET reported_at = ? WHERE id = ?", [(stamp, i) for i in bill_ids]
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            result.quiet += bill_ids
            continue
        try:
            message, report = build_cycle_email(
                conn,
                cycle,
                fx,
                mailer.config.username,
                mailer.config.to_addr,
                portfolio=portfolio,
                rules=rules,
                today=today,
            )
            if backup is not None:
                try:
                    _attach_backup(conn, message, cycle, backup)
                except Exception as exc:  # noqa: BLE001 - the report matters more than its copy
                    result.backup_errors.append((cycle, f"{type(exc).__name__}: {exc}"))
            mailer.send(message)
        except Exception as exc:  # noqa: BLE001 - report the error, keep the bills pending
            result.failed = (cycle, f"{type(exc).__name__}: {exc}")
            break
        conn.execute("BEGIN")
        try:
            stamp = now()
            conn.executemany(
                "UPDATE bills SET reported_at = ? WHERE id = ?", [(stamp, i) for i in bill_ids]
            )
            record_sent(conn, cycle, message["Message-ID"], report.complete)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        result.sent += bill_ids
        result.emails += 1
    return result


def send_year_review(
    conn: sqlite3.Connection, mailer, fx, rules=None, *, today=None
) -> tuple[int | None, str | None]:
    """Last year's review, once, in the run whose January e-mail went out complete
    (docs/notify.md#年度回顾, report.year.due_year). Recorded only after the mail server
    accepted it, so a failed send is tried again next run and a sent one is never repeated.

    Returns (year, None) when it went out, (year, error) when sending failed, and
    (None, None) when no review is due.
    """
    year = due_year(conn, today or today_in_china())
    if year is None:
        return None, None
    try:
        message, _ = build_year_email(
            conn, year, fx, mailer.config.username, mailer.config.to_addr, rules=rules
        )
        mailer.send(message)
    except Exception as exc:  # noqa: BLE001 - reported to the caller; the next run tries again
        return year, f"{type(exc).__name__}: {exc}"
    conn.execute(
        "INSERT INTO year_reviews (year, message_id, sent_at) VALUES (?, ?, ?)",
        (year, message["Message-ID"], now()),
    )
    return year, None
