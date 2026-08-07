"""Shared debug-logging utilities for LLM providers."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _write_debug_log(path: Path, entry: dict) -> None:
    """Append a JSON line to the LLM debug log file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, ValueError) as e:
        log.warning("LLM debug log write failed: %s", e)
