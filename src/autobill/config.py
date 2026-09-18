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


class Config(BaseModel):
    """Only the sections used so far; unknown sections in config.yaml are ignored."""

    model_config = ConfigDict(extra="ignore")

    fx: FxConfig = Field(default_factory=FxConfig)


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
