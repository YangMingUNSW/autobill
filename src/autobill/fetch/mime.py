"""Unwrapping forwarded e-mails. See docs/fetcher.md#mime-处理.

Forwarding "as attachment" (history batches) wraps each original statement in an
attachment. Standard mail clients use a message/rfc822 part; QQ Mail instead attaches a
plain file "<subject>.eml" as application/octet-stream. Both are taken out, recursively,
so a RawMessage is always exactly one original e-mail. An .eml file attachment is taken
byte for byte, so the original keeps its exact content (and sha256).
"""

from __future__ import annotations

import email
import email.policy
from email.message import Message

REPORT_HEADER = "X-AutoBill-Report"  # our own report e-mails carry this


def split_forwarded(data: bytes) -> list[bytes]:
    """The original e-mails inside `data`: the attached ones if it wraps any, else itself."""
    # The default policy decodes RFC 2047 file names ("=?UTF-8?B?...?=" for 账单.eml).
    msg = email.message_from_bytes(data, policy=email.policy.default)
    inner = _attached_messages(msg)
    if not inner:
        return [data]
    out: list[bytes] = []
    for part in inner:
        out.extend(split_forwarded(part))
    return out


def _attached_messages(msg: Message) -> list[bytes]:
    found: list[bytes] = []
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list):
                found.extend(p.as_bytes() for p in payload if isinstance(p, Message))
        elif (part.get_filename() or "").lower().endswith(".eml"):
            data = part.get_payload(decode=True)
            if data and _looks_like_mail(data):
                found.append(data)
    return found


def _looks_like_mail(data: bytes) -> bool:
    """An .eml attachment really holding an e-mail: it starts with header lines."""
    head = data[:4096].replace(b"\r\n", b"\n").split(b"\n\n", 1)[0].lower()
    return b"from:" in head and (b"date:" in head or b"received:" in head)


def is_own_report(data: bytes) -> bool:
    """True for AutoBill's own report e-mails, which must never be parsed as statements."""
    head = data.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
    return REPORT_HEADER.lower().encode() in head.lower()


def addressed_to(data: bytes, address: str) -> bool:
    """True when `address` is among the recipients (To, Cc, Delivered-To, X-Original-To)."""
    msg = email.message_from_bytes(data, policy=email.policy.compat32)
    wanted = address.strip().lower()
    for header in ("To", "Cc", "Delivered-To", "X-Original-To"):
        for value in msg.get_all(header, []):
            if wanted in str(value).lower():
                return True
    return False
