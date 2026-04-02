"""Logging helpers for Foundation CLI."""

from __future__ import annotations

import logging


def configure_logging(level: int | str = logging.INFO) -> logging.Logger:
    """Configure a small, process-wide logging baseline for the CLI."""
    normalized_level = level
    if isinstance(level, str):
        normalized_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        level=normalized_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    return logging.getLogger("foundation")
