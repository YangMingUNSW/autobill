"""AutoBill: rule-based credit-card statement e-mail summariser."""

import os
from importlib.metadata import version

__version__ = version("autobill")


def build_label() -> str:
    """The version, with the commit when there is one: "0.2.0 (15fe0d7)" in the published
    Docker image, whose build sets AUTOBILL_REVISION (see the Dockerfile), plain "0.2.0"
    anywhere else. `autobill --version` and the month's e-mail show it, so you can
    tell which version the server runs (docs/deploy.md)."""
    revision = os.environ.get("AUTOBILL_REVISION", "").strip()
    return f"{__version__} ({revision[:7]})" if revision else __version__
