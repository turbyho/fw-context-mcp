"""``upsert_build_config`` must not erase a column that the caller omitted.

The UPDATE arm assigns every column from ``excluded``, thus a caller that
omits one writes the default of the parameter over the stored value. Three
columns carried a default of ``''``: ``variant``, ``image`` and ``board``.

``cmd_analyze`` omitted all three, because it only wants to update
``analyze_vendor`` on an index that another run built. It therefore erased
the three, and ``get_active_config`` then matched no row for a build that
names a variant — every tool called with an explicit variant failed on an
index that is fully present.

``description`` and ``row_format`` already carried ``None`` for "keep the
old value". These tests hold the three others to the same contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CONFIG_HASH = "ch"


@pytest.fixture
def conn(tmp_path: Path):
    from fw_context_mcp.indexer.db import (
        open_db,
        transaction,
        upsert_build_config,
        upsert_project,
    )

    connection = open_db(tmp_path / "index.db")
    with transaction(connection):
        upsert_project(connection, "pid", "p", str(tmp_path))
        upsert_build_config(
            connection, CONFIG_HASH, "pid", "cc.json",
            description="a branch",
            variant="the-variant",
            image="the-image",
            board="the-board",
            row_format="fw-context-cc/3",
        )
    try:
        yield connection
    finally:
        connection.close()


def _row(connection) -> dict:
    return connection.execute(
        "SELECT variant, image, board, description, row_format "
        "FROM build_configs WHERE config_hash = ?",
        (CONFIG_HASH,),
    ).fetchone()


class TestAnOmittedColumnSurvives:
    def test_the_analyze_call_keeps_the_build_identity(self, conn) -> None:
        """The shape of the call that `cmd_analyze` made before `set_analyze_vendor`."""
        from fw_context_mcp.indexer.db import transaction, upsert_build_config

        with transaction(conn):
            upsert_build_config(
                conn, CONFIG_HASH, "pid", "cc.json",
                description="a branch",
                manifest_verification="none",
                analyze_vendor=1,
            )

        row = _row(conn)
        assert row["variant"] == "the-variant", (
            "the caller said nothing about the variant, thus the row must "
            "keep the one it holds"
        )
        assert row["image"] == "the-image"
        assert row["board"] == "the-board"

    def test_the_analyze_call_still_writes_what_it_names(self, conn) -> None:
        """Keeping the other columns must not block the real update."""
        from fw_context_mcp.indexer.db import transaction, upsert_build_config

        with transaction(conn):
            upsert_build_config(
                conn, CONFIG_HASH, "pid", "cc.json", analyze_vendor=1,
            )

        assert conn.execute(
            "SELECT analyze_vendor FROM build_configs WHERE config_hash = ?",
            (CONFIG_HASH,),
        ).fetchone()["analyze_vendor"] == 1


class TestAnEmptyStringIsStillAValue:
    """"Keep" is ``None``.  ``''`` stays the value of a single-image build."""

    def test_an_explicit_empty_string_clears_the_column(self, conn) -> None:
        from fw_context_mcp.indexer.db import transaction, upsert_build_config

        with transaction(conn):
            upsert_build_config(
                conn, CONFIG_HASH, "pid", "cc.json",
                variant="", image="", board="",
            )

        row = _row(conn)
        assert row["variant"] == ""
        assert row["image"] == ""
        assert row["board"] == ""

    def test_a_new_row_gets_the_empty_string(self, conn) -> None:
        """An absent row has nothing to keep, thus the column starts empty."""
        from fw_context_mcp.indexer.db import transaction, upsert_build_config

        with transaction(conn):
            upsert_build_config(conn, "other-hash", "pid", "cc.json")

        row = conn.execute(
            "SELECT variant, image, board, row_format FROM build_configs "
            "WHERE config_hash = 'other-hash'"
        ).fetchone()
        assert row["variant"] == ""
        assert row["image"] == ""
        assert row["board"] == ""
        assert row["row_format"] == ""


class TestTheOtherKeepColumnsStillWork:
    """The single read must answer for every column that can be kept."""

    def test_description_and_row_format_survive_together(self, conn) -> None:
        from fw_context_mcp.indexer.db import transaction, upsert_build_config

        with transaction(conn):
            upsert_build_config(conn, CONFIG_HASH, "pid", "cc.json", analyze_vendor=1)

        row = _row(conn)
        assert row["description"] == "a branch"
        assert row["row_format"] == "fw-context-cc/3"
