"""
app/logging_setup.py — Centralised logging configuration for Presence.

Call configure_logging() once from application.py before anything else.
All other modules obtain their logger via:

    import logging
    log = logging.getLogger(__name__)
"""

import logging
import sys
from pathlib import Path


def configure_logging(debug: bool = False) -> None:
    """
    Configure the root logger for the application.

    In debug mode (--debug flag or PRESENCE_DEBUG env var) all log records
    go to stderr at DEBUG level.  In normal mode only WARNING and above are
    shown.  Critical errors always go to stderr.
    """
    import os
    if os.environ.get("PRESENCE_DEBUG"):
        debug = True

    level = logging.DEBUG if debug else logging.WARNING

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        "%(levelname)s  %(name)s  %(message)s"
    ))

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid adding duplicate handlers on repeated calls (e.g. in tests)
    if not root.handlers:
        root.addHandler(handler)
