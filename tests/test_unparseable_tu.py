"""One translation unit libclang cannot read must not stop the index run.

`ops.py` states the policy in a comment and applies it: "Other RuntimeError /
TranslationUnitLoadError: transient parse failures (corrupt PCH, missing
include path, OOM during parse) — log and skip this TU; the indexer continues
with the remaining TUs."

`_unit_processor._check_and_parse_unit` is the newer pre-parse path and
catches `SAFE_EXCEPT`, which is
`(ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error, OSError)`.
`clang.cindex.TranslationUnitLoadError` derives straight from `Exception`, so
that tuple does not hold it and the exception leaves the run.

Measured on zbox-ecb-fw: a branch switch from 4.15.3 to 4.15.1 removed two
generated zcbor sources that compile_commands.json still listed.  Two files
out of 881 ended the whole index run at translation unit 39, and the 842
units behind them were never read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("clang.cindex", reason="libclang not available")

GOOD = """\
int reachable_after_the_bad_unit(void) {
    return 1;
}
"""

FIRST = """\
int reachable_before_the_bad_unit(void) {
    return 0;
}
"""


class TestTheExceptionIsRecoverable:
    """The classification itself, before any indexing."""

    def test_safe_except_does_not_hold_the_parse_error(self):
        from clang.cindex import TranslationUnitLoadError

        from fw_context_mcp.utils import SAFE_EXCEPT

        # Not a complaint about SAFE_EXCEPT — a statement of the fact every
        # caller has to handle.  A caller that means to skip an unreadable
        # unit must name TranslationUnitLoadError as well.
        assert not issubclass(TranslationUnitLoadError, SAFE_EXCEPT)

    def test_it_is_not_fatal(self):
        from clang.cindex import TranslationUnitLoadError

        from fw_context_mcp.utils import is_fatal

        # `is_fatal` decides what must never be swallowed.  A unit that will
        # not parse is not in that class: the index is still correct, it is
        # only missing one file.
        assert not is_fatal(TranslationUnitLoadError("Error parsing"))


@pytest.fixture
def project_with_a_missing_source(tmp_path: Path) -> Path:
    """A compile_commands.json that lists a file which is not on disk.

    The shape a branch switch leaves behind: the database still names the
    generated source, and the tree no longer holds it.
    """
    (tmp_path / "first.c").write_text(FIRST, encoding="utf-8")
    (tmp_path / "good.c").write_text(GOOD, encoding="utf-8")
    entries = [
        {"directory": str(tmp_path), "file": name,
         "arguments": ["cc", "-c", name, "-o", f"{name}.o"]}
        # `gone.c` sits in the middle: a run that stops on it leaves the
        # units behind it unread, which is what the failure looked like.
        for name in ("first.c", "gone.c", "good.c")
    ]
    (tmp_path / "compile_commands.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )
    return tmp_path


def _index(project: Path, db_path: Path) -> str:
    from fw_context_mcp.indexer.runner import run

    return run(
        compile_commands=project / "compile_commands.json",
        db_path=db_path,
        project_root=project,
        project_id="testpid",
        project_name="partial",
        index_refs=False,
        index_embeddings=False,
        analyze_symbols=False,
        analyze_overrides=False,
    )


class TestTheRunSurvives:
    def test_the_run_finishes(self, project_with_a_missing_source, tmp_path):
        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        config_hash = _index(project_with_a_missing_source, db_path)
        assert config_hash, "the run raised instead of skipping one unit"

    def test_the_units_behind_the_bad_one_are_indexed(
        self, project_with_a_missing_source, tmp_path
    ):
        from fw_context_mcp.indexer.db import open_db

        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        config_hash = _index(project_with_a_missing_source, db_path)
        conn = open_db(db_path)
        try:
            names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM symbols WHERE config_hash=?",
                    (config_hash,),
                )
            }
        finally:
            conn.close()
        assert "reachable_before_the_bad_unit" in names
        assert "reachable_after_the_bad_unit" in names, (
            "the unit behind the unreadable one was never read"
        )

    def test_the_missing_file_contributes_no_symbol(
        self, project_with_a_missing_source, tmp_path
    ):
        # Skipping must not invent anything for the file it skipped.
        from fw_context_mcp.indexer.db import open_db

        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        config_hash = _index(project_with_a_missing_source, db_path)
        conn = open_db(db_path)
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? "
                "AND file_path LIKE '%gone.c'",
                (config_hash,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert rows == 0

    def test_the_skip_reaches_the_log(
        self, project_with_a_missing_source, tmp_path, caplog
    ):
        # A silent skip and a file that holds nothing look the same in the
        # index, thus the reason has to be visible.
        import logging

        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        with caplog.at_level(logging.WARNING):
            _index(project_with_a_missing_source, db_path)
        # `getMessage()` applies the args; `record.message` is only set once
        # a formatter has run, and reading `message % args` double-formats.
        messages = [record.getMessage() for record in caplog.records]
        assert any("gone.c" in message for message in messages), (
            f"no log line names the skipped unit: {messages}"
        )
