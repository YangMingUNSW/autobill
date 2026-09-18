"""One raw e-mail, decoded just enough for bank detection and parsing.

See docs/fetcher.md#mime-处理. Splitting forwarded message/rfc822 attachments into
separate messages happens before this (fetcher); a RawMessage is always one e-mail.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

from autobill.parse.util import is_pdf

# Statement dates are read in Beijing time. China has no DST, so a fixed offset is exact
# and avoids needing a time-zone database on Windows.
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


@dataclass(frozen=True)
class Attachment:
    filename: str | None
    content_type: str
    data: bytes

    @property
    def is_pdf(self) -> bool:
        return is_pdf(self.data)


@dataclass(frozen=True)
class RawMessage:
    raw: bytes
    sha256: str
    message_id: str  # falls back to "sha256:<hash>" when the header is missing
    subject: str
    from_addr: str  # bare address, lower-cased
    sent_at: datetime | None  # the Date header, converted to Beijing time
    html_parts: list[str] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def email_date(self) -> date | None:
        """The day (Beijing time) the e-mail was sent; decides which day's FX rate is used."""
        return self.sent_at.date() if self.sent_at else None

    @property
    def html(self) -> str:
        return "\n".join(self.html_parts)

    @classmethod
    def from_bytes(cls, data: bytes) -> RawMessage:
        msg = email.message_from_bytes(data, policy=email.policy.default)
        assert isinstance(msg, EmailMessage)
        sha = hashlib.sha256(data).hexdigest()
        html_parts: list[str] = []
        text_parts: list[str] = []
        attachments: list[Attachment] = []
        for part in msg.walk():
            if part.is_multipart():
                continue
            payload = part.get_payload(decode=True) or b""
            ctype = part.get_content_type()
            is_attachment = part.get_content_disposition() == "attachment" or is_pdf(payload)
            if ctype in ("text/html", "text/plain") and not is_attachment:
                text = _decode_text(payload, part.get_content_charset())
                (html_parts if ctype == "text/html" else text_parts).append(text)
            else:
                attachments.append(Attachment(part.get_filename(), ctype, payload))
        return cls(
            raw=data,
            sha256=sha,
            message_id=str(msg.get("Message-ID", "")).strip() or f"sha256:{sha}",
            subject=str(msg.get("Subject", "")).strip(),
            from_addr=parseaddr(str(msg.get("From", "")))[1].lower(),
            sent_at=_parse_sent_at(msg.get("Date")),
            html_parts=html_parts,
            text_parts=text_parts,
            attachments=attachments,
        )


def _decode_text(payload: bytes, charset: str | None) -> str:
    """Decode with the declared charset; banks sometimes mislabel GBK, so fall back to GB18030."""
    try:
        return payload.decode(charset or "utf-8")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("gb18030", errors="replace")


def _parse_sent_at(value: object) -> datetime | None:
    if not value:
        return None
    try:
        sent = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if sent.tzinfo is None:  # no zone in the header: assume Beijing time
        sent = sent.replace(tzinfo=BEIJING)
    return sent.astimezone(BEIJING)
