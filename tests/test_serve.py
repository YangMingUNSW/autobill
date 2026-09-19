"""`autobill serve` (the Docker default): keeps running, one bad run never stops it."""

import pytest
import typer
from typer.testing import CliRunner

from autobill import cli
from autobill.cli import app

runner = CliRunner()


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(cli, "_sleep", waited.append)
    return waited


def test_runs_every_interval(monkeypatch, sleeps):
    calls = []
    monkeypatch.setattr(cli, "_run_once", lambda: calls.append(1) or 0)
    result = runner.invoke(app, ["serve", "--interval", "15", "--times", "3"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 3 and sleeps == [900, 900]  # no sleep after the last run
    assert result.output.count("开始运行") == 3


def test_a_crashing_run_does_not_stop_the_service(monkeypatch, sleeps):
    results = iter([RuntimeError("boom"), 0])

    def flaky():
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(cli, "_run_once", flaky)
    result = runner.invoke(app, ["serve", "--times", "2"])
    assert result.exit_code == 0  # the second run succeeded
    assert sleeps == [1800]


def test_not_configured_yet_keeps_waiting(monkeypatch, sleeps):
    def not_ready():
        raise typer.Exit(1)  # what _mailbox does when config.yaml has no mailbox yet

    monkeypatch.setattr(cli, "_run_once", not_ready)
    result = runner.invoke(app, ["serve", "--times", "2"])
    assert result.exit_code == 1 and len(sleeps) == 1


def test_real_run_once_without_config_says_what_is_missing(sleeps):
    result = runner.invoke(app, ["serve", "--times", "1"])
    assert result.exit_code == 1 and "mail_fetcher" in result.output
