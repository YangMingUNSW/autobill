"""Where data lives and what the (optional) config.yaml says.

See docs/security.md. Secrets never live in config.yaml; they come from the environment
or the Windows credential store (later milestones).
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

import platformdirs
import yaml
from pydantic import BaseModel, ConfigDict, Field

APP_NAME = "autobill"


class FxConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: str = "https://api.frankfurter.dev/v1"
    # Used only when the rate service cannot be reached; reports mark these as "配置汇率".
    fallback_to_cny: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "USD": Decimal("7.10"),
            "AUD": Decimal("4.60"),
            "EUR": Decimal("7.80"),
        }
    )


class SmtpReportConfig(BaseModel):
    """Where report e-mails go. The password (授权码 / App 专用密码) is never in config.yaml:
    it comes from AUTOBILL_SMTP_PASSWORD, or AUTOBILL_IMAP_PASSWORD when both use the same
    mailbox (iCloud), at send time."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False  # off until the author has filled in a real mailbox
    smtp_server: str = ""
    smtp_port: int = 465
    security: Literal["ssl", "starttls"] = "ssl"  # 465 = ssl (QQ/163); 587 = starttls (iCloud)
    username: str = ""  # the sending (central) mailbox
    to_addr: str = ""  # the author's primary mailbox

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.smtp_server and self.username and self.to_addr)


class FetcherConfig(BaseModel):
    """The mailbox bank statements are forwarded to (docs/fetcher.md#imap). Read only; the
    password comes from the AUTOBILL_IMAP_PASSWORD environment variable."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    imap_server: str = ""  # iCloud: imap.mail.me.com
    imap_port: int = 993
    username: str = ""  # iCloud: the full @icloud.com address
    # IMAP folder names must be ASCII here: non-ASCII names need modified UTF-7.
    folders: list[str] = Field(default_factory=lambda: ["AutoBill", "Junk"])
    only_to: str | None = None  # keep only mail addressed here (the forwarding alias)

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.imap_server and self.username and self.folders)

    @property
    def bad_folders(self) -> list[str]:
        return [f for f in self.folders if not f.isascii() or '"' in f]


class NotifierConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    smtp_report: SmtpReportConfig = Field(default_factory=SmtpReportConfig)


class StatementConfig(BaseModel):
    """Standard statements (docs/statement.md). Both settings are optional."""

    model_config = ConfigDict(extra="ignore")

    output_dir: str | None = None  # default: <data dir>/statements
    pdf_browser: str | None = None  # Edge/Chrome executable; found automatically if unset


class PortfolioCard(BaseModel):
    """A card whose statement is expected every month (docs/notify.md#账单月进度邮件)."""

    model_config = ConfigDict(extra="ignore")

    account: str  # e.g. "ABC:0003"
    statement_day: int | None = Field(default=None, ge=1, le=31)  # roughly; for "约 X 号出账"


class CardsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Empty: cards with a statement in the last two months or so are expected.
    portfolio: list[PortfolioCard] = Field(default_factory=list)
    # "CCB:0009": "CCB:0004" - another card number of the same account (a replaced card,
    # or the second card of a two-card account) is filed under one account id.
    card_aliases: dict[str, str] = Field(default_factory=dict)


class AiConfig(BaseModel):
    """The reserved AI interface (autobill/suggest.py): category suggestions only, never
    parsing or amounts. Off until a provider is named; the key comes from the environment."""

    model_config = ConfigDict(extra="ignore")

    provider: str | None = None  # e.g. "ollama" once a provider is registered
    model: str | None = None
    base_url: str | None = None


class Config(BaseModel):
    """Only the sections used so far; unknown sections in config.yaml are ignored."""

    model_config = ConfigDict(extra="ignore")

    ai: AiConfig = Field(default_factory=AiConfig)
    cards: CardsConfig = Field(default_factory=CardsConfig)
    mail_fetcher: FetcherConfig = Field(default_factory=FetcherConfig)
    fx: FxConfig = Field(default_factory=FxConfig)
    notifier: NotifierConfig = Field(default_factory=NotifierConfig)
    statement: StatementConfig = Field(default_factory=StatementConfig)


def data_dir() -> Path:
    """AUTOBILL_DATA_DIR if set (tests always set it), else %LOCALAPPDATA%\\autobill."""
    override = os.environ.get("AUTOBILL_DATA_DIR")
    path = (
        Path(override) if override else Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    override = os.environ.get("AUTOBILL_CONFIG")
    return Path(override) if override else data_dir() / "config.yaml"


def load_config() -> Config:
    """Read config.yaml if it exists; otherwise use defaults matching config.example.yaml."""
    path = config_path()
    if not path.exists():
        return Config()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fx = raw.get("fx") or {}
    # YAML gives floats for 7.10; go through str so the Decimal is exact.
    fallback = fx.get("fallback_to_cny")
    if isinstance(fallback, dict):
        fx["fallback_to_cny"] = {k: Decimal(str(v)) for k, v in fallback.items()}
    return Config.model_validate({**raw, "fx": fx})
