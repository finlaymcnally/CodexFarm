"""Small logging helper for consistent CLI output."""

from __future__ import annotations

import logging


def configure_logging(level: int = logging.INFO) -> None:
    """Set a basic logging format if the app has not configured logging yet."""
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
