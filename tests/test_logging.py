from __future__ import annotations

import logging

import pytest

from meadow.logging_utils import PACKAGE_LOGGER_NAME, configure_logging


def test_cli_mode_defaults_to_warning() -> None:
    configure_logging(mode="cli")

    pkg_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    assert pkg_logger.level == logging.WARNING


def test_telegram_mode_defaults_to_info() -> None:
    configure_logging(mode="telegram")

    pkg_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    assert pkg_logger.level == logging.INFO


def test_debug_flag_overrides_mode_default() -> None:
    configure_logging(mode="cli", debug=True)

    pkg_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    assert pkg_logger.level == logging.DEBUG


def test_config_level_overrides_mode_default() -> None:
    configure_logging(mode="cli", config_level="error")

    pkg_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    assert pkg_logger.level == logging.ERROR


def test_debug_flag_takes_priority_over_config_level() -> None:
    configure_logging(mode="telegram", debug=True, config_level="error")

    pkg_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    assert pkg_logger.level == logging.DEBUG


def test_invalid_config_level_raises() -> None:
    with pytest.raises(ValueError, match="Invalid log level"):
        configure_logging(config_level="banana")


def test_package_logger_does_not_propagate_to_root() -> None:
    configure_logging(mode="telegram")

    pkg_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    assert pkg_logger.propagate is False
