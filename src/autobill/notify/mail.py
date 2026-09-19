"""Sending report e-mails over SMTP. See docs/notify.md#通道邮件第一版.

SSL on port 465 (QQ, 163) or STARTTLS on port 587 (iCloud), as config says. The password
(授权码 / App 专用密码) comes only from the environment: AUTOBILL_SMTP_PASSWORD, or
AUTOBILL_IMAP_PASSWORD when the same mailbox receives and sends (iCloud). It is never
written to a file, a log line or an error message.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage

from autobill.config import SmtpReportConfig

PASSWORD_ENV = "AUTOBILL_SMTP_PASSWORD"
SHARED_PASSWORD_ENV = "AUTOBILL_IMAP_PASSWORD"  # one iCloud app password for both
SmtpFactory = Callable[..., smtplib.SMTP]  # tests pass a fake instead of smtplib.SMTP(_SSL)


def smtp_password() -> str | None:
    return os.environ.get(PASSWORD_ENV) or os.environ.get(SHARED_PASSWORD_ENV) or None


class Mailer:
    def __init__(
        self,
        config: SmtpReportConfig,
        password: str,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        self.config = config
        self._password = password
        # Looked up now, not at import: tests replace smtplib.SMTP_SSL / smtplib.SMTP.
        default = smtplib.SMTP if config.security == "starttls" else smtplib.SMTP_SSL
        self._factory = smtp_factory or default

    def __repr__(self) -> str:  # never show the password, even by accident
        return f"Mailer({self.config.username} -> {self.config.to_addr})"

    def send(self, message: EmailMessage) -> None:
        cfg = self.config
        with self._factory(cfg.smtp_server, cfg.smtp_port, timeout=30) as smtp:
            if cfg.security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(cfg.username, self._password)
            smtp.send_message(message)

    def check_login(self) -> None:
        """Log in and out without sending anything (autobill check-mailbox)."""
        cfg = self.config
        with self._factory(cfg.smtp_server, cfg.smtp_port, timeout=30) as smtp:
            if cfg.security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(cfg.username, self._password)
