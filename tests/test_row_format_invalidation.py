"""An index whose stored text has an older meaning must ask for a reindex.

``CURRENT_SCHEMA_VERSION`` is a hash of the COLUMN SET, thus it moves only
when a column appears or goes.  The meaning of a column can change while
every name stays: the text of ``files.content`` and ``symbols.source``
became ifdef-filtered, and no check noticed.  Every index went on answering
with dead code until something else forced a reindex.

``build_configs.row_format`` records which meaning the stored text carries.
These tests pin the two ends of it — the writer stamps the current format,
and the staleness check reports a mismatch.
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer.db import (
    CURRENT_ROW_FORMAT,
    open_db,
    transaction,
    upsert_build_config,
    upsert_project,
)
from fw_context_mcp.mcp.shared.stale import check_structural_staleness


def _build(tmp_path: Path, **kwargs):
    """Give ``(conn, cfg_row, root)`` for one build config."""
    root = tmp_path / "proj"
    root.mkdir()
    cc = root / "compile_commands.json"
    cc.write_text("[]", encoding="utf-8")

    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(root))
        upsert_build_config(conn, "ch", "pid", str(cc), **kwargs)
    row = conn.execute(
        "SELECT * FROM build_configs WHERE config_hash='ch'"
    ).fetchone()
    return conn, dict(row), root


def _stored_format(conn) -> str:
    return str(
        conn.execute(
            "SELECT row_format FROM build_configs WHERE config_hash='ch'"
        ).fetchone()["row_format"]
    )


class TestOnlyTheContentWriterStampsTheFormat:
    """Stamping is opt-in, because a wrong stamp is silent.

    Only the step that wrote the text may say what the text means.
    ``_run_postprocess`` is that step and passes ``CURRENT_ROW_FORMAT``.
    ``runner.run`` writes the row before it reads any translation unit, and
    ``cmd_analyze`` only updates ``analyze_vendor`` on an index that another
    version built — neither may stamp.
    """

    def test_the_default_does_not_claim_a_format(self, tmp_path: Path):
        conn, cfg, _ = _build(tmp_path)
        try:
            assert cfg["row_format"] == "", (
                "a write that did not produce the text must claim nothing"
            )
        finally:
            conn.close()

    def test_an_explicit_format_is_recorded(self, tmp_path: Path):
        conn, cfg, _ = _build(tmp_path, row_format=CURRENT_ROW_FORMAT)
        try:
            assert cfg["row_format"] == CURRENT_ROW_FORMAT
        finally:
            conn.close()

    def test_a_later_write_without_a_format_keeps_the_stamp(self, tmp_path: Path):
        """The case that made this parameter necessary.

        ``fw-context analyze`` updates ``analyze_vendor`` on an existing
        index.  It must neither clear a good stamp nor mint one it did not
        earn.
        """
        conn, cfg, _ = _build(tmp_path, row_format=CURRENT_ROW_FORMAT)
        try:
            with transaction(conn):
                upsert_build_config(
                    conn, "ch", "pid", cfg["compile_commands_path"],
                    analyze_vendor=1,
                )
            assert _stored_format(conn) == CURRENT_ROW_FORMAT
        finally:
            conn.close()

    def test_a_later_write_does_not_mint_a_stamp(self, tmp_path: Path):
        """An older index must stay reported as older."""
        conn, cfg, _ = _build(tmp_path)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE build_configs SET row_format='fw-context-rows/0' "
                    "WHERE config_hash='ch'"
                )
                upsert_build_config(
                    conn, "ch", "pid", cfg["compile_commands_path"],
                    analyze_vendor=1,
                )
            assert _stored_format(conn) == "fw-context-rows/0"
        finally:
            conn.close()


class TestTheCheckReportsAMismatch:
    def test_the_current_format_gives_no_reason(self, tmp_path: Path):
        conn, cfg, root = _build(tmp_path, row_format=CURRENT_ROW_FORMAT)
        try:
            reasons = check_structural_staleness(conn, "ch", cfg, root)
            assert not [r for r in reasons if "row format" in r]
        finally:
            conn.close()

    def test_an_older_format_asks_for_a_reindex(self, tmp_path: Path):
        conn, cfg, root = _build(tmp_path)
        try:
            cfg["row_format"] = "fw-context-rows/0"
            reasons = check_structural_staleness(conn, "ch", cfg, root)
            assert any("row format" in r for r in reasons), reasons
        finally:
            conn.close()

    def test_a_build_from_before_the_column_asks_for_a_reindex(self, tmp_path: Path):
        """An empty value is what the migration leaves on an old database.

        Such an index holds every ``#ifdef`` branch, thus it must not pass
        as current.
        """
        conn, cfg, root = _build(tmp_path)
        try:
            cfg["row_format"] = ""
            reasons = check_structural_staleness(conn, "ch", cfg, root)
            assert any("row format" in r and "(none)" in r for r in reasons), reasons
        finally:
            conn.close()

    def test_an_absent_key_asks_for_a_reindex(self, tmp_path: Path):
        """The safe direction: no answer must never read as "current"."""
        conn, cfg, root = _build(tmp_path)
        try:
            cfg.pop("row_format")
            reasons = check_structural_staleness(conn, "ch", cfg, root)
            assert any("row format" in r for r in reasons), reasons
        finally:
            conn.close()


class TestANewerFormatIsNotAReindex:
    """A reindex cannot repair an index that is NEWER than its reader.

    The check used a plain ``!=``, thus it asked for a reindex in both
    directions.  In this one the index is correct and the READER is old: the
    indexer writes the same new format again, and the reason comes straight
    back.  A daemon acts on what ``check_structural_staleness`` returns, thus
    the plain inequality put it into a loop — reindex, mismatch, reindex —
    over an index nothing was wrong with.

    Measured: a session started before a row-format bump reported
    ``reindex_needed`` right after a finished reindex, and every reindex
    after it reported the same.
    """

    def test_a_newer_format_gives_no_reindex_reason(self, tmp_path: Path):
        conn, cfg, root = _build(tmp_path)
        try:
            cfg["row_format"] = "fw-context-rows/999"
            reasons = check_structural_staleness(conn, "ch", cfg, root)
            assert not [r for r in reasons if "row format" in r], (
                "a newer format must not ask for a reindex: the run writes "
                "the same format again and the reason never clears"
            )
        finally:
            conn.close()

    def test_the_two_directions_are_told_apart(self) -> None:
        from fw_context_mcp.indexer.db import row_format_is_newer, row_format_is_older

        assert row_format_is_older("fw-context-rows/0")
        assert not row_format_is_newer("fw-context-rows/0")

        assert row_format_is_newer("fw-context-rows/999")
        assert not row_format_is_older("fw-context-rows/999")

        assert not row_format_is_older(CURRENT_ROW_FORMAT)
        assert not row_format_is_newer(CURRENT_ROW_FORMAT)

    def test_an_unreadable_format_counts_as_older(self) -> None:
        """The safe direction, for a value no ordinal can be read from."""
        from fw_context_mcp.indexer.db import row_format_is_newer, row_format_is_older

        for value in ("", "(none)", "fw-context-rows/", "something-else/2", "2"):
            assert row_format_is_older(value), value
            assert not row_format_is_newer(value), value


class TestTheTwoVersionsMoveTogether:
    """A bump of the row format alone rewrites no row.

    The staleness check asks every index for a reindex when the stored
    format differs.  That run mints no new build config, because the config
    hash did not move, and the content pass skips every file that already
    holds text.  `_step_finalize_manifest` then stamps the NEW format over
    rows that still hold the OLD text, and the check goes quiet for good
    over an index that answers with dead code.

    A new config hash is what really rewrites the rows: no row exists for
    the build, thus a plain `fw-context index` writes every file and every
    body again.  Nothing at the stamp site can see the difference, thus this
    test is the guard.
    """

    def test_a_row_format_bump_needs_a_config_hash_bump(self) -> None:
        import inspect

        from fw_context_mcp.indexer.db._schema import ROW_FORMAT_PAIRED_WITH
        from fw_context_mcp.indexer.manifest import compute_config_hash

        source = inspect.getsource(compute_config_hash)
        assert f'"_format": "{ROW_FORMAT_PAIRED_WITH}"' in source, (
            "CURRENT_ROW_FORMAT moved without the config hash. A reindex "
            "that mints no new build config keeps the old text and stamps "
            "the new format over it, thus the staleness check goes quiet "
            "over an index that answers with dead code. Bump `_format` in "
            "compute_config_hash, then set ROW_FORMAT_PAIRED_WITH to the "
            "new value."
        )


class TestTheReasonNamesTheRightFault:
    """The reason must describe the gap it reports, not a fixed one.

    The wording used to be one sentence about inactive ``#ifdef`` branches.
    That describes the ``/0`` text alone.  From ``/1`` on the stored text is
    filtered, thus the same sentence over a ``/2`` index told the reader to
    expect dead code where the real fault is the opposite — the rows answer
    LESS than they should.
    """

    def test_the_oldest_text_is_told_it_holds_dead_code(self) -> None:
        from fw_context_mcp.indexer.db import row_format_effect

        assert "#ifdef" in row_format_effect("fw-context-rows/0")

    def test_the_newest_stale_text_is_told_it_answers_less(self) -> None:
        from fw_context_mcp.indexer.db import row_format_effect

        effect = row_format_effect("fw-context-rows/2")
        assert "answer less" in effect
        assert "#ifdef" not in effect, (
            "a /2 index already filters #ifdef branches — naming them sends "
            "the reader after a fault that is not there"
        )

    def test_each_generation_gets_its_own_wording(self) -> None:
        from fw_context_mcp.indexer.db import row_format_effect

        said = {row_format_effect(f"fw-context-rows/{n}") for n in (0, 1, 2)}
        assert len(said) == 3

    def test_an_unreadable_format_gets_the_oldest_wording(self) -> None:
        # The safe direction: nothing says such text is free of any fault.
        from fw_context_mcp.indexer.db import row_format_effect

        for value in ("", "(none)", "fw-context-rows/", "something-else/2"):
            assert row_format_effect(value) == row_format_effect("fw-context-rows/0"), value

    def test_every_stale_generation_has_a_wording(self) -> None:
        # A bump of CURRENT_ROW_FORMAT without a new entry in the fault table
        # leaves the newest stale generation with the fallback text, which
        # names no fault at all.
        from fw_context_mcp.indexer.db import row_format_effect
        from fw_context_mcp.indexer.db._schema import _row_format_number

        current = _row_format_number(CURRENT_ROW_FORMAT)
        assert current is not None
        for number in range(current):
            effect = row_format_effect(f"fw-context-rows/{number}")
            assert "does not describe" not in effect, (
                f"row format /{number} is stale but _ROW_FORMAT_FAULTS says "
                f"nothing about it — add an entry when you bump "
                f"CURRENT_ROW_FORMAT"
            )
