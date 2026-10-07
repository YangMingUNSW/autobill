import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from autobill.fetch.source import DirectorySource
from autobill.pipeline import process
from autobill.store.db import connect


@pytest.fixture(autouse=True, scope="session")
def never_the_real_data_dir():
    """Last line of defence, outside the per-test monkeypatch (so monkeypatch.undo() in a
    test cannot remove it): without AUTOBILL_DATA_DIR, data_dir() falls back to the real
    AutoBill folder under %LOCALAPPDATA%; make that fail loudly instead."""
    guard = pytest.MonkeyPatch()

    def refuse(*args, **kwargs):
        raise RuntimeError("a test tried to use the real AutoBill data directory")

    guard.setattr("platformdirs.user_data_dir", refuse)
    yield
    guard.undo()


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets a fresh, empty AUTOBILL_DATA_DIR; never the real database."""
    data_dir = tmp_path / "autobill-data"
    data_dir.mkdir()
    monkeypatch.setenv("AUTOBILL_DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture(scope="session")
def _imported_samples(tmp_path_factory):
    """Where sample_db keeps each set of folders once it has been imported."""
    return tmp_path_factory.mktemp("samples"), {}


@pytest.fixture
def sample_db(isolated_data_dir, _imported_samples):
    """sample_db(*folders): this test's database holding the sample statements in
    `folders`, imported in that order; returns a connection to it.

    Importing parses every sample, the 12-page BOC PDF too, and takes about a second; a
    hundred tests doing that would take minutes. So each set of folders is imported once
    per test run, and every test gets its own copy of the result (the database and raw/):
    what one test changes, no other test sees."""
    root, built = _imported_samples

    def load(*folders: Path) -> sqlite3.Connection:
        if folders not in built:
            template = root / f"set{len(built)}"
            template.mkdir()
            conn = connect(template / "autobill.db")
            for folder in folders:
                for mail in DirectorySource(folder).iter_new():
                    process(conn, template, mail)
            conn.close()
            built[folders] = template
        template = built[folders]
        source = sqlite3.connect(template / "autobill.db")
        copy = sqlite3.connect(isolated_data_dir / "autobill.db")
        source.backup(copy)
        source.close()
        copy.close()
        shutil.copytree(template / "raw", isolated_data_dir / "raw", dirs_exist_ok=True)
        return connect(isolated_data_dir / "autobill.db")

    return load


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


@pytest.fixture(autouse=True)
def no_real_smtp(monkeypatch):
    """Tests never send real mail; a test that sends passes a fake SMTP class."""

    def refuse(*args, **kwargs):
        raise RuntimeError("real SMTP connection in a test")

    monkeypatch.setattr("smtplib.SMTP_SSL", refuse)
    monkeypatch.setattr("smtplib.SMTP", refuse)  # the STARTTLS path (iCloud, port 587)
    monkeypatch.delenv("AUTOBILL_SMTP_PASSWORD", raising=False)


@pytest.fixture(autouse=True)
def no_real_imap(monkeypatch):
    """Tests never log in to a real mailbox; a test that fetches passes a FakeIMAP."""

    def refuse(*args, **kwargs):
        raise RuntimeError("real IMAP connection in a test")

    monkeypatch.setattr("imaplib.IMAP4_SSL", refuse)
    monkeypatch.delenv("AUTOBILL_IMAP_PASSWORD", raising=False)
