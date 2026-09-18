import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets a fresh, empty AUTOBILL_DATA_DIR; never the real database."""
    data_dir = tmp_path / "autobill-data"
    data_dir.mkdir()
    monkeypatch.setenv("AUTOBILL_DATA_DIR", str(data_dir))
    return data_dir


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


@pytest.fixture
def snapshot():
    """Compare data with tests/snapshots/<name>.json.

    Snapshots are only (re)written when AUTOBILL_UPDATE_SNAPSHOTS=1 is set on purpose;
    a new or changed snapshot must be reviewed by hand (docs/development.md §6).
    """

    def check(name: str, data) -> None:
        path = SNAPSHOT_DIR / f"{name}.json"
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if os.environ.get("AUTOBILL_UPDATE_SNAPSHOTS") == "1":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            return
        if not path.exists():
            pytest.fail(f"missing snapshot {path}; set AUTOBILL_UPDATE_SNAPSHOTS=1 and review it")
        assert json.loads(path.read_text(encoding="utf-8")) == data, (
            f"output differs from snapshot {path}"
        )

    return check


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach the real rate service; a test that needs rates passes a fake."""

    def refuse(url: str) -> str:
        raise RuntimeError(f"network access in a test: {url}")

    monkeypatch.setattr("autobill.fx.http_fetch", refuse)
