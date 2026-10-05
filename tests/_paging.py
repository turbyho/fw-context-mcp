"""Shared checks for a tool answer that pages.

The older paging tests each keep their own copy of these helpers.  The
tests of the tools that page later use this module, thus one walk holds
every tool to the same contract.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from pathlib import Path

from fw_context_mcp.indexer.db import (
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.utils import compute_source_hash

CH = "hash-paging"
PROJECT_ID = "proj-paging"


def symbol_row(
    file_id: int, path: str, name: str, qualified_name: str, usr: str, line: int,
    *, kind: str = "function", parent_usr: str = "", template_usr: str = "",
    is_template: int = 0, signature: str = "", is_definition: int = 1,
) -> tuple:
    """One row in the column order of ``insert_symbols_batch``."""
    return (
        CH, file_id, path, name, usr, name, qualified_name, kind,
        line, 1, line + 2, is_definition, signature or f"void {qualified_name}()", "", None,
        0, 0, parent_usr, is_template, template_usr, 1, 0.0, "", 0,
    )


def make_project(tmp_path: Path, fill: Callable[..., None], *, files: int = 1) -> Path:
    """A project that a handler can open, with the rows that *fill* writes.

    *fill* gets the connection and the ids of the source files
    ``src/f0.c`` … ``src/f<files-1>.c``.  Each file exists on disk with
    the stamp of the index, thus no answer carries a stale warning.
    """
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{PROJECT_ID}"\n\n[build]\n\n[index]\ndb_dir = "{tmp_path}"\n',
        encoding="utf-8",
    )
    db_path = tmp_path / PROJECT_ID / "index.db"
    db_path.parent.mkdir(parents=True)
    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, PROJECT_ID, root.name, str(root))
            upsert_build_config(conn, CH, PROJECT_ID, str(root / "compile_commands.json"))
        file_ids = []
        for i in range(files):
            source = root / "src" / f"f{i}.c"
            source.write_text("/* source */\n" * 400, encoding="utf-8")
            file_ids.append(upsert_file(
                conn, CH, f"src/f{i}.c", "c",
                mtime=source.stat().st_mtime, source_hash=compute_source_hash(source),
            ))
        fill(conn, file_ids)
        conn.commit()
    finally:
        conn.close()
    return root

# The keys of the page notice; ``hint`` is there only when ``more`` is true.
NOTICE_KEYS = {"total", "offset", "shown", "more"}
# Rows that say where the answer came from, and are not part of it.
SIDE_KEYS = {"warning", "info", "error", "_did_you_mean"}


def notice(rows: list[dict]) -> dict | None:
    """The page notice of *rows*, found by its keys and not by its position."""
    return next((r for r in rows if NOTICE_KEYS <= set(r)), None)


def answers(rows: list[dict]) -> list[dict]:
    """The rows of *rows* that are part of the answer."""
    return [r for r in rows if not NOTICE_KEYS <= set(r) and not SIDE_KEYS & set(r)]


def walk_pages(
    call: Callable[[int], list[dict]],
    key: Callable[[dict], Hashable],
    *,
    cap: int = 1000,
    is_answer: Callable[[dict], bool] | None = None,
) -> tuple[list[Hashable], int]:
    """Read every page of one answer and hold each notice to its word.

    *call* gets the offset and returns one page.  *key* names a row.  The
    walk asserts that each page has a notice, that the notice gives the
    offset of the page and the number of rows on it, and that ``total``
    does not change between pages.  It returns the keys in walk order and
    the total.  *cap* stops a walk that would never end.

    *is_answer* picks the rows of the answer, for a tool that adds rows of
    its own kind (the ``coverage`` row of ``get_vector_table``).  Without
    it, every row that is not a notice or a side row counts.
    """
    seen: list[Hashable] = []
    total: int | None = None
    offset = 0
    while offset < cap:
        rows = call(offset)
        body = answers(rows)
        if is_answer is not None:
            body = [r for r in body if is_answer(r)]
        page = notice(rows)
        if not body:
            assert page is None or page["shown"] == 0
            break
        assert page is not None, f"page at offset {offset} has no notice"
        assert page["offset"] == offset
        assert page["shown"] == len(body)
        if total is None:
            total = page["total"]
        assert page["total"] == total, "total changed between pages"
        assert ("hint" in page) == page["more"]
        seen += [key(r) for r in body]
        offset += len(body)
        if not page["more"]:
            break
    return seen, total or 0


def assert_whole_and_once(seen: list[Hashable], total: int) -> None:
    """A finished walk reached every row of the answer, and each one once."""
    assert len(seen) == total, f"walked {len(seen)} of {total}"
    assert len(set(seen)) == len(seen), "a row came back twice"
