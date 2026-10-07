"""The files rows that the assembly pass creates carry a time and a hash."""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer.asm import _file_state
from fw_context_mcp.utils import compute_source_hash


def test_a_file_gets_its_time_and_hash(tmp_path: Path):
    src = tmp_path / "start.S"
    src.write_text("  .global Reset_Handler\n", encoding="utf-8")

    state = _file_state(str(src))

    assert state == {"mtime": src.stat().st_mtime, "source_hash": compute_source_hash(src)}


def test_a_missing_file_gets_neither(tmp_path: Path):
    """A row with neither says nothing to the staleness checks, and they skip it."""
    assert _file_state(str(tmp_path / "gone.S")) == {"mtime": 0.0, "source_hash": ""}
