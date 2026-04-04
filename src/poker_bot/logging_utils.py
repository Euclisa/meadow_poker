from __future__ import annotations

import logging


PACKAGE_LOGGER_NAME = "poker_bot"

LOG_LEVEL_NAMES = {"debug", "info", "warning", "error", "critical"}

_MODE_DEFAULTS: dict[str, int] = {
    "cli": logging.WARNING,
    "telegram": logging.INFO,
}


def configure_logging(
    *,
    mode: str | None = None,
    debug: bool = False,
    config_level: str | None = None,
) -> None:
    if debug:
        level = logging.DEBUG
    elif config_level is not None:
        level = _parse_level(config_level)
    elif mode is not None:
        level = _MODE_DEFAULTS.get(mode, logging.INFO)
    else:
        level = logging.INFO

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))

    pkg_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    pkg_logger.handlers.clear()
    pkg_logger.addHandler(handler)
    pkg_logger.setLevel(level)
    pkg_logger.propagate = False


def _parse_level(raw: str) -> int:
    normalized = raw.strip().upper()
    level = getattr(logging, normalized, None)
    if not isinstance(level, int):
        raise ValueError(
            f"Invalid log level: {raw!r}. "
            f"Choose from: {', '.join(sorted(LOG_LEVEL_NAMES))}"
        )
    return level
