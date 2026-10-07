"""The Jinja environment both e-mails render with: the templates in report/templates,
HTML escaped by default, and the two filters the templates use."""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup

from autobill.report.style import emoji_for


def yuan(amount: str) -> Markup:
    """ "16,714.84" -> ¥16,714 with smaller .84, as Apple Card and Wallet show money."""
    whole, _, fraction = amount.partition(".")
    return Markup('<span class="cur">¥</span>{}<span class="dec">.{}</span>').format(
        whole, fraction or "00"
    )


env = Environment(
    loader=PackageLoader("autobill.report", "templates"),
    autoescape=select_autoescape(default=True, default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters["yuan"] = yuan
env.filters["emoji"] = emoji_for
