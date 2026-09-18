"""Sending report e-mails over SMTP (SSL, port 465). See docs/notify.md#通道邮件第一版.

The password (邮箱授权码) comes only from the AUTOBILL_SMTP_PASSWORD environment variable.
It is never written to a file, a log line or an error message.
"""

from __future__ import annotations

import os
import smtplib
from collections.abc import Callable
from email.message import EmailMessage

from autobill.config import SmtpReportConfig

PASSWORD_ENV = "AUTOBILL_SMTP_PASSWORD"
SmtpFactory = Callable[..., smtplib.SMTP]  # tests pass a fake instead of smtplib.SMTP_SSL


def smtp_password() -> str | None:
    return os.environ.get(PASSWORD_ENV) or None


class Mailer:
    def __init__(
        self,
        config: SmtpReportConfig,
        password: str,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        self.config = config
        self._password = password
        self._factory = smtp_factory or smtplib.SMTP_SSL  # looked up now: tests replace it

    def __repr__(self) -> str:  # never show the password, even by accident
        return f"Mailer({self.config.username} -> {self.config.to_addr})"

    def send(self, message: EmailMessage) -> None:
        cfg = self.config
        with self._factory(cfg.smtp_server, cfg.smtp_port, timeout=30) as smtp:
            smtp.login(cfg.username, self._password)
            smtp.send_message(message)
