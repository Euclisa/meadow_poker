from __future__ import annotations

import logging


PACKAGE_LOGGER_NAME = "meadow"

LOG_LEVEL_NAMES = {"debug", "info", "warning", "error", "critical"}

_MODE_DEFAULTS: dict[str, int] = {
    "cli": logging.WARNING,
    "telegram": logging.INFO,
    "web": logging.INFO,
}


class _PytestCaptureBridgeHandler(logging.Handler):
    """Forward records to pytest's capture handler while keeping package propagation disabled."""

    def emit(self, record: logging.LogRecord) -> None:
        root_logger = logging.getLogger()
        for handler in root_logger.handlers:
            if handler is self:
                continue
            if handler.__class__.__name__ != "LogCaptureHandler" and not handler.__class__.__module__.startswith("_pytest."):
                continue
            handler.handle(record)


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
    capture_bridge = _PytestCaptureBridgeHandler()

    pkg_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    pkg_logger.handlers.clear()
    pkg_logger.addHandler(handler)
    pkg_logger.addHandler(capture_bridge)
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
