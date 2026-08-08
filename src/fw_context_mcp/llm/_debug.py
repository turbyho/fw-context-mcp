"""Shared debug-logging utilities for LLM providers.

WHY exists: LLM prompts and responses are opaque — when a model produces
unexpected output, debugging requires seeing exactly what was sent and
received.  This module provides a fire-and-forget JSON-lines append that
all chat and embedding callers use.  The debug log is opt-in (only when
``cfg.debug_log`` is a non-None path).

WHY fdopen with O_APPEND (not ``open(..., "a")`` loop): Multiple threads
and processes within the same MCP worker may write to the debug log
concurrently.  O_APPEND guarantees atomic appends at the kernel level
for writes up to PIPE_BUF (4KB on Linux) — no interleaved lines even
without an explicit lock.  Python ``open("a")`` uses buffered I/O which
can interleave chunks.

WHY 0o600 permissions: Debug logs contain raw prompts and model responses
which may include sensitive source code.  Restricting to owner-only read
prevents accidental exposure.
"""

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
