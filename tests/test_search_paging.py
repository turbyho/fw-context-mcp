"""A page of a search or a lookup must be a slice of one stable answer.

Three tools joined the paged set: ``search_bodies``, ``search_content``
and ``lookup_symbol``.  Each carried a shape that an OFFSET cannot survive,
and each is pinned here:

* ``search_bodies`` grouped project code before vendor code in PYTHON,
  over a ``limit * 3`` over-fetch.  The grouping then held only inside the
  fetched window, thus a deeper page fetched a wider window, pulled a
  project row from the tail of it to the front, and pushed the vendor rows
  behind it onto another page.
* ``search_content`` gave its LIKE fallback NO order at all, and SQLite may
  then answer differently on every call.
* ``lookup_symbol`` ordered by a definition flag and a line number, and two
  symbols tie on both — line 1 of two headers, or two instantiations of one
  template.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    count_macros,
    insert_macros_batch,
    insert_symbols_batch,
    lookup_macro,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.utils import compute_source_hash

CH = "ch"
NOTICE_KEYS = {"total", "offset", "shown", "more"}
# Rows that say where the answer came from, and are not part of it.
SIDE_KEYS = {"warning", "info", "error", "_did_you_mean"}


def _answers(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not NOTICE_KEYS <= set(r) and not SIDE_KEYS & set(r)]


def _notice(rows: list[dict]) -> dict | None:
    return next((r for r in rows if NOTICE_KEYS <= set(r)), None)


def _symbol(file_id, path, name, qualified_name, usr, line, *,
            kind="function", source="", is_project=1, parent_usr=""):
    """One symbol row in the column order of ``insert_symbols_batch``."""
    return (
        CH, file_id, path, name.replace("_", " "), usr, name, qualified_name, kind,
        line, 0, line + 3, 1, f"void {qualified_name}()", "", None, 0, 0,
        parent_usr, 0, "", is_project, 0.0, source, 0,
    )


def _file(conn, root: Path, rel: str, language: str) -> int:
    """Index one file WITH its hash, thus the index does not read as stale.

    Without the hash every call answers with a staleness warning ahead of
    the page notice, which is real behaviour of its own and only hides the
    row under test here.
    """
    on_disk = root / rel
    return upsert_file(
        conn, CH, rel, language,
        mtime=on_disk.stat().st_mtime,
        source_hash=compute_source_hash(on_disk),
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with rows built to break a paging mistake, not to look real.

    * Twelve definitions hold the word ``probe`` in their body.  The four
      project ones sit LAST by relevance, thus a project-first order that
      only reaches into a window would lose them on a deeper page.
    * Nine files hold the word ``probe`` in their text.
    * Six symbols are named ``read``, and every one of them is a definition
      at line 1 — the tie that an OFFSET needs a third column to survive.
    """
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    project_id = "proj-001"
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{project_id}"\n\n[build]\n\n[index]\ndb_dir = "{tmp_path}"\n',
        encoding="utf-8",
    )

    db_path = tmp_path / project_id / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    with transaction(conn):
        upsert_project(conn, project_id, root.name, str(root))
        upsert_build_config(conn, CH, project_id, str(root / "compile_commands.json"))

        symbols = []
        for i in range(9):
            rel = f"vendor/sdk_{i}.cpp"
            text = f"// sdk unit {i}\nvoid probe_{i}() {{ probe(); }}\n"
            (root / "vendor").mkdir(exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
            fid = _file(conn, root, rel, "cpp")
            conn.execute(
                "UPDATE files SET content=?, is_project=0"
                " WHERE config_hash=? AND path=?",
                (text, CH, rel),
            )
            symbols.append(_symbol(
                fid, rel, f"probe_{i}", f"sdk::probe_{i}", f"u_v{i}", 2,
                source=f"void probe_{i}()\n{{\n    probe();\n}}\n", is_project=0,
            ))
        for i in range(4):
            rel = f"src/app_{i}.cpp"
            text = f"// app unit {i}\nvoid app_probe_{i}() {{ probe(); }}\n"
            (root / rel).write_text(text, encoding="utf-8")
            fid = _file(conn, root, rel, "cpp")
            conn.execute(
                "UPDATE files SET content=?, is_project=1"
                " WHERE config_hash=? AND path=?",
                (text, CH, rel),
            )
            symbols.append(_symbol(
                fid, rel, f"app_probe_{i}", f"app::app_probe_{i}", f"u_a{i}", 2,
                source=f"void app_probe_{i}()\n{{\n    probe();\n}}\n",
            ))

        # Six methods named `read`, every one a definition at line 1.  Only
        # a third column in the order can tell two of them apart.
        rel = "src/readers.h"
        (root / rel).write_text("#pragma once\n", encoding="utf-8")
        fid = _file(conn, root, rel, "c")
        conn.execute(
            "UPDATE files SET is_project=1 WHERE config_hash=? AND path=?",
            (CH, rel),
        )
        for i in range(6):
            symbols.append(_symbol(
                fid, rel, "read", f"Reader{i}::read", f"u_r{i}", 1,
                kind="method", parent_usr=f"u_cls{i}",
            ))
            symbols.append(_symbol(
                fid, rel, f"Reader{i}", f"Reader{i}", f"u_cls{i}", 1, kind="class",
            ))
        # One symbol that only the "Foo::bar" suffix fallback reaches.
        symbols.append(_symbol(
            fid, rel, "flush", "deep::ns::Buffer::flush", "u_flush", 40,
            kind="method",
        ))
        insert_symbols_batch(conn, symbols)

        insert_macros_batch(conn, [
            (CH, fid, f"LIMIT_{i}", str(i), str(i), 10 + i, 0) for i in range(5)
        ])
    conn.close()
    return root


class TestSearchBodiesPages:
    def test_the_notice_leads_and_counts_every_match(self, project: Path):
        from fw_context_mcp.mcp.handlers.search import search_bodies

        rows = search_bodies("probe", project_root=str(project), limit=5)

        notice = _notice(rows)
        assert notice is not None, rows
        assert rows.index(notice) < rows.index(_answers(rows)[0]), (
            "the notice must come before the first result"
        )
        assert notice["total"] == 13, "nine vendor bodies, four project ones"
        assert notice["shown"] == 5
        assert notice["more"] is True

    def test_the_walk_holds_every_row_once(self, project: Path):
        """The defect: a project row of the tail jumped a page when the
        window grew, thus one row showed twice and another never."""
        from fw_context_mcp.mcp.handlers.search import search_bodies

        for page in (2, 3, 5):
            seen: list[str] = []
            offset = 0
            while True:
                rows = search_bodies(
                    "probe", project_root=str(project), limit=page, offset=offset,
                )
                body = _answers(rows)
                if not body:
                    break
                seen += [r["qualified_name"] for r in body]
                offset += len(body)
                notice = _notice(rows)
                assert notice["offset"] == offset - len(body)
                assert notice["shown"] == len(body)
                if not notice["more"]:
                    break
            assert len(seen) == 13, f"page={page} walked {len(seen)}"
            assert len(set(seen)) == 13, f"page={page} repeated a row"

    def test_project_code_leads_the_whole_answer(self, project: Path):
        """Not only the first window: the four project rows must come first
        even though every one of them sits last by relevance."""
        from fw_context_mcp.mcp.handlers.search import search_bodies

        rows = _answers(search_bodies("probe", project_root=str(project), limit=4))

        assert [r["qualified_name"] for r in rows] == [
            "app::app_probe_0", "app::app_probe_1",
            "app::app_probe_2", "app::app_probe_3",
        ]

    def test_an_offset_past_the_end_says_so(self, project: Path):
        from fw_context_mcp.mcp.handlers.search import search_bodies

        rows = search_bodies("probe", project_root=str(project), limit=5, offset=99)

        assert _answers(rows) == []
        assert any("99" in r.get("info", "") for r in rows), rows


class TestSearchContentPages:
    def test_the_notice_leads_and_counts_every_file(self, project: Path):
        from fw_context_mcp.mcp.handlers.search import search_content

        rows = search_content("probe", project_root=str(project), limit=4)

        notice = _notice(rows)
        assert notice is not None, rows
        assert notice["total"] == 13, "every file whose text holds the word"
        assert notice["shown"] == 4
        assert notice["more"] is True

    def test_the_walk_holds_every_file_once(self, project: Path):
        from fw_context_mcp.mcp.handlers.search import search_content

        for page in (2, 3, 5):
            seen: list[str] = []
            offset = 0
            while True:
                rows = search_content(
                    "probe", project_root=str(project), limit=page, offset=offset,
                )
                body = _answers(rows)
                if not body:
                    break
                seen += [r["file"] for r in body]
                offset += len(body)
                if not _notice(rows)["more"]:
                    break
            assert len(seen) == 13, f"page={page} walked {len(seen)}"
            assert len(set(seen)) == 13, f"page={page} repeated a file"


class TestLookupSymbolPages:
    def test_the_notice_counts_every_symbol_of_the_name(self, project: Path):
        from fw_context_mcp.mcp.handlers._lookup import lookup_symbol

        rows = lookup_symbol("read", project_root=str(project), exact=True, limit=2)

        notice = _notice(rows)
        assert notice is not None, rows
        assert notice["total"] == 6, "one `read` in each of the six classes"
        assert notice["shown"] == 2
        assert notice["more"] is True

    def test_the_order_ends_at_a_unique_column(self):
        """The tie-break, held as an invariant because no test can force it.

        Six symbols named ``read`` tie on the definition flag and on the
        line.  SQLite may then return them in any order, and a walk would
        show one twice and hide another — but measured, it keeps the rowid
        order of a small table scan every time.  The fault needs a plan
        change, which a test cannot ask for.

        What holds either way: the order must END at a column that is
        unique within one build.  ``usr`` is that column.
        """
        from fw_context_mcp.mcp.handlers._lookup import _LOOKUP_ORDER

        assert _LOOKUP_ORDER.rstrip().endswith("s.usr"), _LOOKUP_ORDER

    def test_six_symbols_that_tie_walk_once(self, project: Path):
        """Every offset lands where the notice says, and the walk is whole.

        This does not prove the tie-break (see the test above).  It proves
        that the offset reaches the query, that the count agrees with the
        rows, and that the walk ends.
        """
        from fw_context_mcp.mcp.handlers._lookup import lookup_symbol

        for page in (1, 2, 4):
            seen: list[str] = []
            offset = 0
            while True:
                rows = lookup_symbol(
                    "read", project_root=str(project), exact=True,
                    limit=page, offset=offset,
                )
                body = _answers(rows)
                if not body:
                    break
                seen += [r["qualified_name"] for r in body]
                offset += len(body)
                if not _notice(rows)["more"]:
                    break
            assert sorted(seen) == [f"Reader{i}::read" for i in range(6)], (
                f"page={page} walked {seen}"
            )

    def test_every_row_carries_its_class(self, project: Path):
        """``class`` is what tells two same-name methods apart at a glance."""
        from fw_context_mcp.mcp.handlers._lookup import lookup_symbol

        rows = _answers(lookup_symbol("read", project_root=str(project), exact=True))

        assert {r["class"] for r in rows} == {f"Reader{i}" for i in range(6)}

    def test_the_suffix_fallback_answers_and_counts(self, project: Path):
        """``Buffer::flush`` is no qualified name in the index — the suffix
        fallback finds ``deep::ns::Buffer::flush``, and it must page too."""
        from fw_context_mcp.mcp.handlers._lookup import lookup_symbol

        rows = lookup_symbol("Buffer::flush", project_root=str(project), exact=True)

        body = _answers(rows)
        assert [r["qualified_name"] for r in body] == ["deep::ns::Buffer::flush"]
        assert _notice(rows)["total"] == 1

    def test_the_suffix_fallback_refuses_a_name_that_only_shares_the_tail(
        self, project: Path,
    ):
        """The filter moved from Python into SQL and must still be a SUFFIX
        test.  ``Other::flush`` shares the short name and nothing else, thus
        the suffix path must answer with nothing.

        A symbol does come back, and that is right: the relaxed did-you-mean
        path answers when no other path does.  It marks every such row with
        ``_fallback``, and the mark is what says that the exact name was not
        found.
        """
        from fw_context_mcp.mcp.handlers._lookup import lookup_symbol

        rows = lookup_symbol("Other::flush", project_root=str(project), exact=True)

        body = _answers(rows)
        assert body, "the relaxed path answers rather than give up"
        assert all(r.get("_fallback") for r in body), (
            f"a row without the mark came from the suffix path: {body}"
        )

    def test_a_macro_fallback_carries_a_notice_too(self, project: Path):
        from fw_context_mcp.mcp.handlers._lookup import lookup_symbol

        rows = lookup_symbol("LIMIT_", project_root=str(project), limit=2)

        body = _answers(rows)
        assert {r["kind"] for r in body} == {"macro"}
        notice = _notice(rows)
        assert notice["total"] == 5, "five macros share the prefix"
        assert notice["shown"] == 2

    def test_an_offset_past_the_end_says_so(self, project: Path):
        from fw_context_mcp.mcp.handlers._lookup import lookup_symbol

        rows = lookup_symbol(
            "read", project_root=str(project), exact=True, limit=2, offset=50,
        )

        assert _answers(rows) == []
        assert any("50" in r.get("info", "") for r in rows), rows

    def test_an_offset_past_the_end_does_not_fall_back(self, project: Path):
        """The fallbacks are gated on the COUNT and not on the empty page.

        An empty page of a name that DID match means "past the end", and a
        fallback would then answer about a different symbol under the name
        the caller asked for.
        """
        from fw_context_mcp.mcp.handlers._lookup import lookup_symbol

        rows = lookup_symbol(
            "Buffer::flush", project_root=str(project), exact=True, offset=50,
        )

        assert _answers(rows) == []
        assert not any("_did_you_mean" in r for r in rows), rows


class TestMacroPaging:
    """The db layer under the macro fallback."""

    def _conn(self, project: Path):
        from fw_context_mcp.mcp.shared.context import _db_path

        return open_db(_db_path(project))

    def test_the_count_ignores_the_page_bound(self, project: Path):
        conn = self._conn(project)
        try:
            assert count_macros(conn, CH, "LIMIT_") == 5
            assert len(lookup_macro(conn, CH, "LIMIT_", limit=2)) == 2
        finally:
            conn.close()

    def test_the_offset_walks_the_macros_once(self, project: Path):
        conn = self._conn(project)
        try:
            seen = []
            for offset in (0, 2, 4):
                seen += [m["name"] for m in
                         lookup_macro(conn, CH, "LIMIT_", limit=2, offset=offset)]
            assert sorted(seen) == [f"LIMIT_{i}" for i in range(5)]
        finally:
            conn.close()
