"""Where data lives and what the (optional) config.yaml says.

See docs/security.md. Secrets never live in config.yaml; they come from the environment
or the Windows credential store (later milestones).
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

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
    """Where report e-mails go. The password (授权码) is never in config.yaml: it is read
    from the AUTOBILL_SMTP_PASSWORD environment variable at send time."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False  # off until the author has filled in a real mailbox
    smtp_server: str = ""
    smtp_port: int = 465
    username: str = ""  # the sending (central) mailbox
    to_addr: str = ""  # the author's primary mailbox

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.smtp_server and self.username and self.to_addr)


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


class Config(BaseModel):
    """Only the sections used so far; unknown sections in config.yaml are ignored."""

    model_config = ConfigDict(extra="ignore")

    cards: CardsConfig = Field(default_factory=CardsConfig)
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
