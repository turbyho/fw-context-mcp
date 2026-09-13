"""A page of a search or a lookup must be a slice of one stable answer.

Two tools joined the paged set: ``search_bodies`` and
``search_content``.  Each carried a shape that an OFFSET cannot
survive, and each is pinned here:

* ``search_bodies`` grouped project code before vendor code in PYTHON,
  over a ``limit * 3`` over-fetch.  The grouping then held only inside the
  fetched window, thus a deeper page fetched a wider window, pulled a
  project row from the tail of it to the front, and pushed the vendor rows
  behind it onto another page.
* ``search_content`` gave its LIKE fallback NO order at all, and SQLite may
  then answer differently on every call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    insert_symbols_batch,
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

        insert_symbols_batch(conn, symbols)
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
