"""Logging configuration. Uses loguru if available; falls back to stdlib."""

from __future__ import annotations

import logging
import sys

from loguru import logger as _loguru

from app.core.config import settings


class InterceptHandler(logging.Handler):
    """Redirect stdlib logging to loguru so SQLAlchemy/uvicorn/etc. get formatted."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = _loguru.level(record.levelname).no
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        _loguru.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging() -> None:
    """Idempotent setup. Call once from `main.py` at startup."""
    _loguru.remove()
    _loguru.add(
        sys.stdout,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <7}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        level=settings.log_level,
    )

    logging.root.handlers = [InterceptHandler()]
    logging.root.setLevel(settings.log_level)

    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).handlers = [InterceptHandler()]
        logging.getLogger(noisy).propagate = False


def get_logger(name: str):
    """Convenience wrapper — equivalent to `logging.getLogger(name)`."""
    return _loguru.bind(component=name)
