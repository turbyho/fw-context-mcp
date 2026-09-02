"""``reindex.log`` holds more than fw-context, so the last line is not progress.

``daemon._run_index_async`` sends the stdout AND the stderr of
``fw-context index --background`` to ``reindex.log``.  A build runs inside
that process, so west, cmake, ninja, and the shell that started them all
write to the same file.

Observed defect: ``get_active_build`` reported ``"reindex_progress":
"lean-ctx:"`` and ``fw-context watch status`` printed ``Last index:
lean-ctx:``.  Both read ``lines[-1]``, and the last line came from a shell
wrapper, not from the index run.

The second half of the same defect is fw-context's own error text:
``utils.run_build`` puts 500 characters of the failing tool's stderr into
the message, so a build failure ends the file on a line fw-context printed
but did not write.

These tests hold the repair in place: read backwards for the newest line
that STARTS a fw-context record.
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.mcp.background import (
    _REINDEX_TAIL_BYTES,
    read_reindex_progress,
)

# The exact tail of the run that exposed the defect, from
# ~/.fw-context/index/<id>/reindex.log.
OBSERVED_LOG = """\
Project: demo-fw  path=/home/u/demo-fw  build=zephyr
16:45:15 INFO 13 source file(s) are missing from compile_commands.json
16:45:15 INFO zephyr build_multi[nrf52840-dev]: west build --sysbuild
fw-context: error: Build command failed (exit 126): west build --sysbuild (nrf52840-dev)
stderr: [BLOCKED — DO NOT RETRY] Command uses eval or $()/ backticks at command position.
Command: source /home/u/demo-fw/scripts/setup_nordic_env.sh && west build
lean-ctx:
"""


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / "reindex.log").write_text(text, encoding="utf-8")
    return tmp_path


def test_stray_wrapper_line_is_not_progress(tmp_path: Path) -> None:
    """The observed defect: a bare ``lean-ctx:`` was reported as progress."""
    db_dir = _write(tmp_path, OBSERVED_LOG)

    progress = read_reindex_progress(db_dir)

    assert progress != "lean-ctx:"
    # The failure itself, and not the tool output quoted below it.
    assert progress == (
        "fw-context: error: Build command failed (exit 126): "
        "west build --sysbuild (nrf52840-dev)"
    )


def test_embedded_stderr_is_not_progress(tmp_path: Path) -> None:
    """A build failure ends on quoted stderr — the error line is the answer."""
    db_dir = _write(
        tmp_path,
        "12:00:00 INFO TUs to index: 265\n"
        "fw-context: error: Build command failed (exit 1): ninja\n"
        "stderr: fatal error: 'zephyr/kernel.h' file not found\n"
        "  1 | #include <zephyr/kernel.h>\n"
        "    |          ^~~~~~~~~~~~~~~~~\n",
    )

    assert read_reindex_progress(db_dir) == (
        "fw-context: error: Build command failed (exit 1): ninja"
    )


def test_newest_log_record_wins(tmp_path: Path) -> None:
    """A healthy run ends on a log record, which is returned unchanged."""
    db_dir = _write(
        tmp_path,
        "21:21:10 INFO [265/265] main.c: 41 syms, 88 refs, 1.2s\n"
        "21:21:15 INFO Embeddings: [355/355] 5 symbols (1723 chars) embedded 0.2s\n",
    )

    assert read_reindex_progress(db_dir) == (
        "21:21:15 INFO Embeddings: [355/355] 5 symbols (1723 chars) embedded 0.2s"
    )


def test_build_tool_output_after_a_record_is_skipped(tmp_path: Path) -> None:
    """west and ninja write to the same file, and neither reports progress."""
    db_dir = _write(
        tmp_path,
        "18:10:20 INFO zephyr build_multi[nrf52840-dev]: west build --sysbuild\n"
        "-- Configuring done (2.1s)\n"
        "[142/1783] Building C object zephyr/CMakeFiles/zephyr.dir/main.c.obj\n"
        "ninja: build stopped: subcommand failed.\n",
    )

    assert read_reindex_progress(db_dir) == (
        "18:10:20 INFO zephyr build_multi[nrf52840-dev]: west build --sysbuild"
    )


def test_header_alone_is_progress(tmp_path: Path) -> None:
    """A run that dies before its first log record still has the header."""
    db_dir = _write(
        tmp_path,
        "Project: demo-fw  path=/home/u/demo-fw  build=zephyr\n"
        "Traceback (most recent call last):\n"
        '  File "<string>", line 1, in <module>\n',
    )

    assert read_reindex_progress(db_dir) == (
        "Project: demo-fw  path=/home/u/demo-fw  build=zephyr"
    )


def test_no_fw_context_record_gives_none(tmp_path: Path) -> None:
    """Nothing to report is reported as nothing, not as foreign text."""
    db_dir = _write(tmp_path, "lean-ctx:\nsome other wrapper output\n")

    assert read_reindex_progress(db_dir) is None


def test_empty_file_gives_none(tmp_path: Path) -> None:
    db_dir = _write(tmp_path, "")

    assert read_reindex_progress(db_dir) is None


def test_missing_file_gives_none(tmp_path: Path) -> None:
    assert read_reindex_progress(tmp_path) is None


def test_tail_cut_inside_a_utf8_character(tmp_path: Path) -> None:
    """A tail cut may land mid-character, and that must not raise.

    The previous reader opened the file as text and seeked to
    ``size - 4096``.  An offset inside a UTF-8 sequence makes the read
    raise UnicodeDecodeError, which the reader did not catch — it left
    ``get_active_build``.  Ordinary progress lines carry "→" and "—", so
    the boundary is reachable.
    """
    pad = "—"  # em dash, 3 bytes in UTF-8
    head = "00:00:00 INFO " + pad * 30000 + "\n"
    tail = "01:02:03 INFO the newest record\n"
    data = (head + tail).encode("utf-8")
    assert len(data) > _REINDEX_TAIL_BYTES

    # Prove the cut really lands inside an em dash rather than trusting it.
    cut = len(data) - _REINDEX_TAIL_BYTES
    assert (cut - len("00:00:00 INFO ")) % 3 != 0

    (tmp_path / "reindex.log").write_bytes(data)

    assert read_reindex_progress(tmp_path) == "01:02:03 INFO the newest record"


def test_partial_first_line_of_a_tail_is_dropped(tmp_path: Path) -> None:
    """A cut line has lost its start, so it cannot be reported as a record."""
    # One record, then padding long enough to push that record out of the
    # tail.  What survives the cut is the middle of the padding line, which
    # must not be mistaken for the record that begins the file.
    head = "23:59:59 INFO the record that falls outside the tail\n"
    padding = "x" * (_REINDEX_TAIL_BYTES + 1000) + "\n"
    (tmp_path / "reindex.log").write_text(head + padding, encoding="utf-8")

    assert read_reindex_progress(tmp_path) is None
