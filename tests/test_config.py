"""Tests for the core configuration layer (src/config)."""

from __future__ import annotations

from pathlib import Path

from src.config import settings


def test_project_root_points_to_repo_root():
    # pyproject.toml only exists at the actual project root, so this
    # pins PROJECT_ROOT down against silently drifting to the wrong
    # directory if settings.py ever moves.
    assert (settings.PROJECT_ROOT / "pyproject.toml").exists()


def test_data_and_log_dirs_exist_after_import():
    assert settings.DATA_DIR.is_dir()
    assert settings.LOG_DIR.is_dir()


def test_data_and_log_dirs_are_under_project_root():
    assert settings.DATA_DIR == settings.PROJECT_ROOT / "data"
    assert settings.LOG_DIR == settings.PROJECT_ROOT / "logs"


def test_watchlist_contains_expected_symbols():
    assert settings.WATCHLIST == ["SPY", "QQQ"]


def test_default_period_and_interval():
    assert settings.DEFAULT_PERIOD == "2y"
    assert settings.DEFAULT_INTERVAL == "5m"


def test_logger_is_importable_and_callable(capsys):
    from src.config.logging import logger

    logger.info("test log message")

    captured = capsys.readouterr()
    assert "test log message" in captured.out


def test_logger_writes_to_log_file():
    from src.config.logging import log_path, logger

    logger.info("written to file check")

    assert Path(log_path).exists()
