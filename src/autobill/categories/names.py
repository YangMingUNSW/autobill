"""Merchant names without the payment markers banks glue to them, so one shop has one name
whether a model is asked about it or the year in review counts its visits."""

from __future__ import annotations

import re

# Bank markers around a merchant name that are not part of it. "AUSVISA Apple Pay" means
# "Australia, Visa card, Apple Pay", but a model reads it as an Australian visa office.
_CHANNEL = re.compile(
    r"^(?:[A-Z]{3} )?(?:跨行无卡消费|跨行预授权完成|跨行消费|境外消费|网上消费|跨境消费)\s*"
)
_WALLET = re.compile(r"(?:[A-Z]{3})?(?:VISA|CUP|MC)\s*apple\s*pay.*$", re.IGNORECASE)


def shown_name(name: str) -> str:
    """The merchant name as a model is shown it, without the bank's payment markers:
    "GM SYDNEY PTY LTDAUSVISA Apple Pay" -> "GM SYDNEY PTY LTD"."""
    cleaned = _WALLET.sub("", _CHANNEL.sub("", name)).strip()
    return cleaned or name
