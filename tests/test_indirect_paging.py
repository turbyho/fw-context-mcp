"""find_indirect_call_sites and find_indirect_targets page their answer.

The two tools cut the answer at ``limit`` and said nothing about the cut.
A common field name such as ``handler`` or ``send`` matches many call
sites, and the name match of ``find_indirect_targets`` takes a substring
of the assigned function too.

The order used to stop at file and line.  Two calls on one line tie
there, and so does one assignment that reaches several call sites.  The
fixture builds both ties; the order now ends at the primary keys.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    count_indirect_call_site_matches,
    count_indirect_target_matches,
    find_indirect_call_sites,
    find_indirect_targets,
    insert_fp_assignments_batch,
    insert_indirect_call_sites_batch,
    insert_symbols_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.utils import compute_source_hash
from tests._paging import assert_whole_and_once, notice, walk_pages

CH = "hash-deadbeef"
PROJECT_ID = "proj-indirect"
FIELD_USR = "c:@S@Driver@FI@on_data"
# Two call sites on each of five lines: ten rows that tie in pairs.
CALL_LINES = 5
CALLS_PER_LINE = 2
# Four functions assigned to the field on one line (an init list).
ASSIGNED = 4


def _fill(conn, root: Path, **file_stamp) -> None:
    """The field, its call sites and the functions assigned to it."""
    with transaction(conn):
        upsert_project(conn, PROJECT_ID, root.name, str(root))
        upsert_build_config(conn, CH, PROJECT_ID, str(root / "compile_commands.json"))
    fid = upsert_file(conn, CH, "src/drv.c", "c", **file_stamp)
    insert_symbols_batch(conn, [(
        CH, fid, "src/drv.c", "on_data", FIELD_USR, "on_data", "Driver::on_data", "field",
        3, 1, 3, 1, "void (*)(int)", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0,
    )])
    insert_indirect_call_sites_batch(conn, [
        (CH, "src/drv.c", 100 + line, None, f"drv.on_data /* call {k} */", FIELD_USR, "on_data", "void (*)(int)")
        for line in range(CALL_LINES)
        for k in range(CALLS_PER_LINE)
    ])
    insert_fp_assignments_batch(conn, [
        (CH, "src/init.c", 7, FIELD_USR, "on_data", f"c:@F@cb{i}", f"cb{i}",
         "void (*)(int)", "init_list", None)
        for i in range(ASSIGNED)
    ])
    conn.commit()


@pytest.fixture
def db():
    tmpdir = Path(tempfile.mkdtemp())
    conn = open_db(tmpdir / "test.db")
    _fill(conn, Path("/tmp/project"))
    yield conn
    conn.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project that the handlers can open, with the same rows as ``db``."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{PROJECT_ID}"\n\n[build]\n\n[index]\ndb_dir = "{tmp_path}"\n',
        encoding="utf-8",
    )
    source = root / "src" / "drv.c"
    source.write_text("/* driver */\n" * 120, encoding="utf-8")
    db_path = tmp_path / PROJECT_ID / "index.db"
    db_path.parent.mkdir(parents=True)
    conn = open_db(db_path)
    try:
        _fill(conn, root, mtime=source.stat().st_mtime, source_hash=compute_source_hash(source))
    finally:
        conn.close()
    return root


def _walk(fetch, key, page: int) -> list:
    seen: list = []
    offset = 0
    while True:
        rows = fetch(page, offset)
        if not rows:
            return seen
        seen += [key(r) for r in rows]
        offset += len(rows)


class TestCallSites:
    def test_the_count_is_the_whole_answer(self, db):
        assert count_indirect_call_site_matches(db, CH, "on_data") == CALL_LINES * CALLS_PER_LINE

    @pytest.mark.parametrize("page", [1, 3, 50])
    def test_the_pages_walk_whole_and_once(self, db, page):
        seen = _walk(
            lambda limit, offset: find_indirect_call_sites(db, CH, "on_data", limit=limit, offset=offset),
            lambda r: r["id"], page,
        )
        assert_whole_and_once(seen, count_indirect_call_site_matches(db, CH, "on_data"))

    def test_the_order_ends_at_the_primary_key(self):
        """Two calls on one line tie on file and line; ``id`` breaks the tie."""
        from fw_context_mcp.indexer.db._refs import _CALL_SITE_ORDER

        assert _CALL_SITE_ORDER.endswith("ics.id"), _CALL_SITE_ORDER


class TestTargets:
    def test_the_count_is_the_whole_answer(self, db):
        """Each assignment meets each call site: the join gives one row per pair."""
        assert count_indirect_target_matches(db, CH, "on_data") == (
            ASSIGNED * CALL_LINES * CALLS_PER_LINE
        )

    @pytest.mark.parametrize("page", [1, 7, 500])
    def test_the_pages_walk_whole_and_once(self, db, page):
        """Two calls on one line give two rows that look the same, thus the walk counts pairs."""
        seen = _walk(
            lambda limit, offset: find_indirect_targets(db, CH, "on_data", limit=limit, offset=offset),
            lambda r: (r["rhs_name"], r["call_line"]),
            page,
        )
        assert len(seen) == count_indirect_target_matches(db, CH, "on_data")
        # Each pair of an assignment and a call line occurs once per call on that line.
        assert len(set(seen)) == ASSIGNED * CALL_LINES
        assert all(seen.count(pair) == CALLS_PER_LINE for pair in set(seen))

    def test_the_order_ends_at_the_two_primary_keys(self):
        from fw_context_mcp.indexer.db._refs import _TARGET_ORDER

        assert _TARGET_ORDER.endswith("fpa.id, ics.id"), _TARGET_ORDER


class TestHandlers:
    def test_the_call_sites_walk_through_the_handler(self, project):
        from fw_context_mcp.mcp.handlers.callgraph import find_indirect_call_sites as tool

        seen, total = walk_pages(
            lambda offset: tool("on_data", project_root=str(project), limit=3, offset=offset),
            lambda r: (r["line"], r["expr_text"]),
        )
        assert total == CALL_LINES * CALLS_PER_LINE
        assert_whole_and_once(seen, total)

    def test_the_targets_walk_through_the_handler(self, project):
        from fw_context_mcp.mcp.handlers.callgraph import find_indirect_targets as tool

        seen, total = walk_pages(
            lambda offset: tool("on_data", project_root=str(project), limit=7, offset=offset),
            lambda r: (r["rhs_name"], r["call_line"], r["call_expr_text"]),
        )
        assert total == ASSIGNED * CALL_LINES * CALLS_PER_LINE
        assert_whole_and_once(seen, total)

    def test_the_hint_names_the_next_call(self, project):
        from fw_context_mcp.mcp.handlers.callgraph import find_indirect_call_sites as tool

        rows = tool("on_data", project_root=str(project), limit=3)
        assert notice(rows)["hint"] == (
            "find_indirect_call_sites('on_data', offset=3) reads the next page."
        )

    @pytest.mark.parametrize(("tool_name", "thing", "total"), [
        ("find_indirect_call_sites", "indirect call site", CALL_LINES * CALLS_PER_LINE),
        ("find_indirect_targets", "assignment", ASSIGNED * CALL_LINES * CALLS_PER_LINE),
    ])
    def test_a_page_after_the_end_names_the_total(self, project, tool_name, thing, total):
        from fw_context_mcp.mcp.handlers import callgraph

        rows = getattr(callgraph, tool_name)("on_data", project_root=str(project), offset=900)
        assert rows == [{"info": f"No {thing} at offset 900; the answer holds {total}."}]
