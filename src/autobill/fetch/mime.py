"""Unwrapping forwarded e-mails. See docs/fetcher.md#mime-处理.

Forwarding "as attachment" (history batches, some auto-forward rules) wraps each original
statement in a message/rfc822 part. Every such part is an e-mail of its own: it is taken
out, recursively, so a RawMessage is always exactly one original e-mail.
"""

from __future__ import annotations

import email
import email.policy
from email.message import Message

REPORT_HEADER = "X-AutoBill-Report"  # our own report e-mails carry this


def split_forwarded(data: bytes) -> list[bytes]:
    """The original e-mails inside `data`: the attached ones if it wraps any, else itself."""
    msg = email.message_from_bytes(data, policy=email.policy.compat32)
    inner = _attached_messages(msg)
    if not inner:
        return [data]
    out: list[bytes] = []
    for part in inner:
        out.extend(split_forwarded(part.as_bytes()))
    return out


def _attached_messages(msg: Message) -> list[Message]:
    found: list[Message] = []
    for part in msg.walk():
        if part.get_content_type() != "message/rfc822":
            continue
        payload = part.get_payload()
        if isinstance(payload, list):
            found.extend(p for p in payload if isinstance(p, Message))
    return found


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
