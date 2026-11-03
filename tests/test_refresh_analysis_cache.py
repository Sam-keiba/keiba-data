"""refresh の最後に流す、競馬分析の事前集計。作れなくても refresh は失敗にしない。"""

from __future__ import annotations

import argparse
import subprocess

import pytest

from keiba_data import cli, config


@pytest.fixture
def app_project(tmp_path, monkeypatch):
    """隣に keiba-app がある状態。"""
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "keiba-data")
    project = tmp_path / "keiba-app"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    return project


def test_runs_build_cache_in_the_sibling_project(app_project, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    cli._build_analysis_cache()
    assert calls == [["uv", "run", "--project", str(app_project), "keiba-app", "build-cache"]]


def test_does_nothing_without_the_sibling_project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "keiba-data")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("流してはいけない"))
    cli._build_analysis_cache()


@pytest.mark.parametrize("failure", [
    FileNotFoundError("uv"), subprocess.TimeoutExpired("uv", 1),
])
def test_errors_do_not_escape(app_project, monkeypatch, failure):
    def boom(*_a, **_k):
        raise failure
    monkeypatch.setattr(cli.subprocess, "run", boom)
    cli._build_analysis_cache()


def test_failed_exit_code_does_not_escape(app_project, monkeypatch, caplog):
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    cli._build_analysis_cache()
    assert "作れませんでした" in caplog.text


class _Conn:
    def execute(self, *_):
        return self

    def fetchone(self):
        return (0,)

    def close(self):
        pass


def _steps(monkeypatch, code: int, *, no_upload: bool = True) -> tuple[int, list]:
    built: list = []
    step = argparse.Namespace(func=lambda _: code)
    monkeypatch.setattr(cli, "build_parser", lambda: argparse.Namespace(parse_args=lambda _argv: step))
    monkeypatch.setattr(cli.db, "connect", lambda *_a, **_k: _Conn())
    monkeypatch.setattr(cli, "_refresh_summary", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_record_refresh", lambda *_a: None)
    monkeypatch.setattr(cli, "_build_analysis_cache", lambda: built.append(True))
    args = argparse.Namespace(interval=1, no_upload=no_upload, raw_args="")
    return cli._refresh_steps(args), built


def test_refresh_builds_after_success_even_without_upload(monkeypatch):
    assert _steps(monkeypatch, cli.EXIT_OK) == (cli.EXIT_OK, [True])


def test_refresh_does_not_build_after_failure(monkeypatch):
    assert _steps(monkeypatch, cli.EXIT_FAILED) == (cli.EXIT_FAILED, [])
