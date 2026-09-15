"""A page walk stays inside one answer, and a page always holds a row.

The two faults here have one shape: the tool reads "no rows" as "no
answer".  An empty page of an answer that DOES hold rows means only that
the reader walked past the end.

``tests/test_paging.py`` holds the same subject at the database layer.
These tests go through the handler, because both faults live in the
handler and not in the query.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.utils import compute_source_hash

CH = "hash-handlers"
PROJECT_ID = "proj-handlers"

#: The keys of the page notice.  A reader finds that row by its keys and
#: not by its position, because a warning row can come before it.
NOTICE_KEYS = {"total", "offset", "shown", "more"}

#: Keys that say where an answer came from, and are not part of it.
SIDE_KEYS = {"warning", "info", "error"}


def _notice(rows: list[dict]) -> dict | None:
    return next((r for r in rows if NOTICE_KEYS <= set(r)), None)


def _answers(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not NOTICE_KEYS <= set(r) and not SIDE_KEYS & set(r)]


def _info(rows: list[dict]) -> dict | None:
    return next((r for r in rows if "info" in r), None)


def _symbol(
    file_id: int, path: str, name: str, qualified_name: str, usr: str, line: int,
    *, kind: str = "method", parent_usr: str = "",
) -> tuple:
    """One symbol row in the column order of ``insert_symbols_batch``."""
    return (
        CH, file_id, path, name, usr, name, qualified_name, kind,
        line, 0, line + 3, 1, f"void {qualified_name}()", "", None,
        1 if kind == "method" else 0, 0, parent_usr, 0, "", 1, 0.0, "", 0,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project that a handler can open, built to separate two answers.

    Three classes override one base method:

    * ``Sd::stop`` has two call sites of its own.
    * ``Flash::stop`` has six, and they must stay out of reach while
      ``Sd::stop`` answers.
    * ``Rom::stop`` has none, thus the peer-override fallback answers for
      it and the tests can show that the fallback still works.

    Some functions are defined and never referenced, so that
    ``find_dead_code`` has rows to report, and two methods carry call
    sites, so that ``find_hotspots`` has rows to rank.
    """
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{PROJECT_ID}"\n\n[build]\n\n[index]\ndb_dir = "{tmp_path}"\n',
        encoding="utf-8",
    )
    source = root / "src" / "dl.cpp"
    source.write_text("// download manager\n" * 220, encoding="utf-8")

    db_path = tmp_path / PROJECT_ID / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, PROJECT_ID, root.name, str(root))
            upsert_build_config(conn, CH, PROJECT_ID, str(root / "compile_commands.json"))
            fid = upsert_file(
                conn, CH, "src/dl.cpp", "cpp",
                mtime=source.stat().st_mtime,
                source_hash=compute_source_hash(source),
            )
            insert_symbols_batch(conn, [
                _symbol(fid, "src/dl.cpp", "Base", "Base", "u_base", 1, kind="class"),
                _symbol(fid, "src/dl.cpp", "stop", "Base::stop", "u_base_stop", 2,
                        parent_usr="u_base"),
                _symbol(fid, "src/dl.cpp", "Sd", "Sd", "u_sd", 10, kind="class"),
                _symbol(fid, "src/dl.cpp", "stop", "Sd::stop", "u_sd_stop", 11,
                        parent_usr="u_sd"),
                _symbol(fid, "src/dl.cpp", "Flash", "Flash", "u_flash", 20, kind="class"),
                _symbol(fid, "src/dl.cpp", "stop", "Flash::stop", "u_flash_stop", 21,
                        parent_usr="u_flash"),
                _symbol(fid, "src/dl.cpp", "Rom", "Rom", "u_rom", 30, kind="class"),
                _symbol(fid, "src/dl.cpp", "stop", "Rom::stop", "u_rom_stop", 31,
                        parent_usr="u_rom"),
                _symbol(fid, "src/dl.cpp", "run", "App::run", "u_app", 40,
                        kind="function"),
                _symbol(fid, "src/dl.cpp", "dead_one", "dead_one", "u_dead1", 50,
                        kind="function"),
                _symbol(fid, "src/dl.cpp", "dead_two", "dead_two", "u_dead2", 54,
                        kind="function"),
                _symbol(fid, "src/dl.cpp", "dead_three", "dead_three", "u_dead3", 58,
                        kind="function"),
            ])
            conn.executemany(
                "INSERT INTO overrides(config_hash, derived_usr, base_usr) VALUES (?,?,?)",
                [
                    (CH, "u_sd_stop", "u_base_stop"),
                    (CH, "u_flash_stop", "u_base_stop"),
                    (CH, "u_rom_stop", "u_base_stop"),
                ],
            )
            insert_refs_batch(
                conn,
                [(CH, "u_sd_stop", "src/dl.cpp", 100 + i, "u_app", "call", None)
                 for i in range(2)]
                + [(CH, "u_flash_stop", "src/dl.cpp", 200 + i, "u_app", "call", None)
                   for i in range(6)],
            )
    finally:
        conn.close()
    return root


@pytest.fixture
def two_devs(project: Path) -> Path:
    """Two namespaces, each with a ``Dev`` class that overrides one base.

    ``Dev::reset`` is thus the name of TWO symbols, and neither of them has
    a call site of its own.  ``Mid::reset`` overrides the same base method
    and has three call sites.

    ``Mid::reset`` stays OUT of the candidate list: the query matches it on
    the bare tail alone, which is one rank below the two that match the
    ``Dev::reset`` suffix.  The count of the direct answer is therefore
    zero, and the peer-override fallback answers.
    """
    conn = open_db(project.parent / PROJECT_ID / "index.db")
    try:
        with transaction(conn):
            fid = upsert_file(conn, CH, "src/dev.cpp", "cpp")
            insert_symbols_batch(conn, [
                _symbol(fid, "src/dev.cpp", "DevBase", "hal::DevBase", "u_devbase",
                        300, kind="class"),
                _symbol(fid, "src/dev.cpp", "reset", "hal::DevBase::reset",
                        "u_devbase_reset", 301, parent_usr="u_devbase"),
                _symbol(fid, "src/dev.cpp", "Dev", "a::Dev", "u_a_dev", 310,
                        kind="class"),
                _symbol(fid, "src/dev.cpp", "reset", "a::Dev::reset", "u_a_reset",
                        311, parent_usr="u_a_dev"),
                _symbol(fid, "src/dev.cpp", "Dev", "b::Dev", "u_b_dev", 320,
                        kind="class"),
                _symbol(fid, "src/dev.cpp", "reset", "b::Dev::reset", "u_b_reset",
                        321, parent_usr="u_b_dev"),
                _symbol(fid, "src/dev.cpp", "Mid", "hal::Mid", "u_mid", 330,
                        kind="class"),
                _symbol(fid, "src/dev.cpp", "reset", "hal::Mid::reset", "u_mid_reset",
                        331, parent_usr="u_mid"),
            ])
            conn.executemany(
                "INSERT INTO overrides(config_hash, derived_usr, base_usr) VALUES (?,?,?)",
                [
                    (CH, "u_a_reset", "u_devbase_reset"),
                    (CH, "u_b_reset", "u_devbase_reset"),
                    (CH, "u_mid_reset", "u_devbase_reset"),
                ],
            )
            insert_refs_batch(conn, [
                (CH, "u_mid_reset", "src/dl.cpp", 300 + i, "u_app", "call", None)
                for i in range(3)
            ])
    finally:
        conn.close()
    return project


class TestOneAnswerOwnsTheWalk:
    """The step that answers owns every page of the walk.

    ``find_callers`` falls back to the callers of the peer overrides when
    a virtual method has none of its own.  That fallback used to be gated
    on an empty PAGE.  An offset past the end of a symbol that DOES have
    callers leaves the page empty, thus page 2 came back with the callers
    of a sibling class, a ``total`` of its own and a hint that led the
    reader further into that other answer.

    ``search_code`` and ``lookup_symbol`` gate their own fallbacks on a
    COUNT for the same reason.
    """

    def test_the_first_page_answers_for_the_symbol(self, project: Path):
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        rows = find_callers("Sd::stop", project_root=str(project), limit=2)

        notice = _notice(rows)
        assert notice is not None, f"no page notice: {rows}"
        assert notice["total"] == 2, f"the peers must not reach the count: {notice}"
        assert notice["more"] is False, notice
        lines = sorted(r["line"] for r in _answers(rows))
        assert lines == [100, 101], f"the rows belong to another symbol: {lines}"

    def test_a_page_past_the_end_does_not_switch_to_the_peers(self, project: Path):
        """The walk is over, thus the answer says so and stops."""
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        rows = find_callers("Sd::stop", project_root=str(project), limit=2, offset=2)

        assert _notice(rows) is None, (
            f"page 2 started another answer: {rows}"
        )
        info = _info(rows)
        assert info is not None, f"no info row: {rows}"
        assert "offset 2" in info["info"], info
        assert "holds 2" in info["info"], (
            f"the count must describe the symbol, not its peers: {info}"
        )

    def test_a_deep_offset_reports_the_same_count(self, project: Path):
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        rows = find_callers("Sd::stop", project_root=str(project), limit=2, offset=99)

        info = _info(rows)
        assert info is not None, f"no info row: {rows}"
        assert "holds 2" in info["info"], info

    def test_a_method_with_no_callers_still_reaches_the_peers(self, project: Path):
        """The fallback stays reachable — only its gate changed.

        ``Rom::stop`` has no call site of its own, thus the callers of
        ``Sd::stop`` and ``Flash::stop`` are the answer.
        """
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        rows = find_callers("Rom::stop", project_root=str(project), limit=50)

        notice = _notice(rows)
        assert notice is not None, f"the fallback lost its page notice: {rows}"
        assert notice["total"] == 8, f"two peers hold 2 + 6 call sites: {notice}"
        assert len(_answers(rows)) == 8

    def test_the_peer_answer_pages_on_its_own_count(self, project: Path):
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        rows = find_callers("Rom::stop", project_root=str(project), limit=3, offset=3)

        notice = _notice(rows)
        assert notice is not None, rows
        assert notice == {"total": 8, "offset": 3, "shown": 3, "more": True,
                          "hint": notice.get("hint", "")}, notice
        assert "offset=6" in notice["hint"], notice

    def test_a_named_symbol_needs_no_ambiguity_row(self, project: Path):
        """One candidate, thus nothing to choose and nothing to report."""
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        rows = find_callers("Rom::stop", project_root=str(project), limit=50)

        assert not any("warning" in r for r in rows), rows

    def test_the_peer_answer_names_the_symbol_it_took(self, two_devs: Path):
        """The rows describe ONE hierarchy, thus the answer must name it.

        ``Dev::reset`` means two methods here and neither has a call site
        of its own, thus the peer-override fallback answers.  Its rows
        carry no ``target_qualified_name``, because they describe one
        hierarchy and not several symbols.  Without a row that names the
        symbol, a reader cannot tell which of the two the answer is about.
        """
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        rows = find_callers("Dev::reset", project_root=str(two_devs), limit=50)

        warning = next((r["warning"] for r in rows if "warning" in r), None)
        assert warning is not None, f"the answer named no symbol: {rows}"
        assert "matches 2 symbols" in warning, warning
        assert "Dev::reset" in warning, warning
        notice = _notice(rows)
        assert notice is not None, rows
        assert notice["total"] == 3, f"the peer holds three call sites: {notice}"


class TestAZeroLimitCannotStallTheWalk:
    """A page must hold a row, otherwise its own hint leads nowhere.

    ``page_notice`` reads ``more`` from ``offset + shown < total``.  With
    ``shown`` at zero the page is always "not the last one" and the hint
    names the offset the reader already gave.

    These two tools had a second fault beside that one: with no row to
    show, each answered with the ``info`` row that it keeps for an empty
    index.  ``find_dead_code`` then reported a codebase in which every
    function has a caller, and ``find_hotspots`` reported a project with
    no hotspot at all.

    ``tests/test_search_paging.py`` pins the same clamp on the search
    tools and on ``lookup_symbol``.
    """

    @pytest.mark.parametrize("limit", [0, -3])
    def test_find_dead_code_still_answers_with_a_row(self, project: Path, limit: int):
        from fw_context_mcp.mcp.handlers.callgraph import find_dead_code

        truth = _notice(find_dead_code(project_root=str(project), limit=50))
        assert truth is not None and truth["total"] >= 3, truth

        rows = find_dead_code(project_root=str(project), limit=limit)

        notice = _notice(rows)
        assert notice is not None, f"a dead function was reported as none: {rows}"
        assert notice["shown"] >= 1, notice
        assert notice["total"] == truth["total"], (
            f"the count must not move with the page bound: {notice}"
        )
        assert len(_answers(rows)) >= 1

    @pytest.mark.parametrize("limit", [0, -3])
    def test_find_hotspots_still_answers_with_a_row(self, project: Path, limit: int):
        from fw_context_mcp.mcp.handlers.callgraph import find_hotspots

        truth = _notice(find_hotspots(project_root=str(project), limit=50))
        assert truth is not None and truth["total"] == 2, truth

        rows = find_hotspots(project_root=str(project), limit=limit)

        notice = _notice(rows)
        assert notice is not None, f"a hotspot was reported as none: {rows}"
        assert notice["shown"] >= 1, notice
        assert notice["total"] == 2, notice
        assert len(_answers(rows)) >= 1
