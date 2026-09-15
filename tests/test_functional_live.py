"""Functional tests over the indexes that THIS machine holds.

Every other test in this suite builds its rows to force a fault.  That is
the right shape for a regression, and it cannot find what a fixture never
thought of: measured during one review, a page walk over seven real
indexes reported five faults that no seeded test produced — four were a
fault in the CHECK and one was real.  A real index is the only place
where that comes out.

These tests run the tools end to end over whatever the machine has
indexed, and they hold the invariants that a paged answer must keep:

* a walk never repeats a row and never skips one,
* a page past the end says so and does not start another answer,
* a page bound of zero never reads as a fact about the index,
* an answer that comes from virtual dispatch says where its rows sit,
* the indexer names no non-callable as a caller.

── What they touch ──

Only the call-graph tools run here.  Those write nothing and start no
watcher.  The search tools and ``lookup_symbol`` are deliberately absent:
each may start the file watcher for a file that changed on disk, and a
test must not reindex the operator's project as a side effect.  Their
paging is pinned on seeded rows in ``tests/test_search_paging.py``.

``conftest.py`` points ``FW_CONTEXT_INDEX_DIR`` at a temp directory for
the whole session, so that no test writes to the real index.  A live test
needs the real one, thus the fixture below restores the value that the
environment had BEFORE the suite started, for the duration of one test.

They are marked ``live`` and excluded from ``make test`` and
``make test-all``, because they depend on what one machine happens to
hold.  Run them with ``make test-live``.  With no indexed project they
skip, thus a checkout without one is not a failure.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

#: Captured at IMPORT time, which is collection — before the session
#: fixture of conftest.py redirects the index directory.
_REAL_INDEX_DIR = os.environ.get("FW_CONTEXT_INDEX_DIR")

NOTICE_KEYS = {"total", "offset", "shown", "more"}
SIDE_KEYS = {"warning", "info", "error", "_did_you_mean"}
CALLABLE = ("function", "method", "constructor", "destructor")

#: A walk stops here.  The invariant shows on the first pages; a symbol
#: with thousands of call sites would only cost time.
WALK_CAP = 300


@pytest.fixture(autouse=True)
def _real_index_dir():
    """Undo the index-directory isolation for one live test.

    The session fixture in ``conftest.py`` exists so that no test writes
    to ``~/.fw-context/index``.  These tests only READ it, and they have
    nothing to read without it.
    """
    isolated = os.environ.get("FW_CONTEXT_INDEX_DIR")
    if _REAL_INDEX_DIR is None:
        os.environ.pop("FW_CONTEXT_INDEX_DIR", None)
    else:
        os.environ["FW_CONTEXT_INDEX_DIR"] = _REAL_INDEX_DIR
    try:
        yield
    finally:
        if isolated is None:
            os.environ.pop("FW_CONTEXT_INDEX_DIR", None)
        else:
            os.environ["FW_CONTEXT_INDEX_DIR"] = isolated


def _under_temp(path: Path) -> bool:
    """Is *path* inside the system temp directory?

    This suite registers throwaway projects in the REAL global registry —
    the isolation of ``conftest.py`` covers the index directory and not
    the registry.  Those rows survive the run that made them, and a live
    sweep that picks them up reports skips for fixtures rather than
    answers for the operator's code: measured once, 27 projects where the
    machine holds 7.
    """
    try:
        return path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError):  # pragma: no cover — an unreadable path
        return True


def _live_projects() -> list[Path]:
    """Every registered project whose root and index are both present.

    The registry holds a row per registration, thus one project can appear
    twice; the paths are deduplicated.
    """
    try:
        from fw_context_mcp.config.global_db import open_global_db
        from fw_context_mcp.mcp.shared.context import _db_path
    except ImportError:  # pragma: no cover — the package is always importable
        return []
    try:
        conn = open_global_db()
        rows = conn.execute("SELECT root_path FROM projects").fetchall()
    except sqlite3.Error:  # pragma: no cover — no registry is not a failure
        return []
    out: dict[Path, None] = {}
    for row in rows:
        root = Path(row["root_path"] or "")
        if not root.is_dir() or not (root / ".fw-context").is_dir():
            continue
        if _under_temp(root):
            continue
        try:
            if _db_path(root).exists():
                out.setdefault(root, None)
        except Exception:  # noqa: BLE001 — a missing index is not a failure
            continue
    return sorted(out)


LIVE_PROJECTS = _live_projects()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not LIVE_PROJECTS, reason="no indexed project on this machine"
    ),
]


def _notice(rows: list[dict]) -> dict | None:
    """Find the page notice by its KEYS.  A warning row can precede it."""
    return next((r for r in rows if NOTICE_KEYS <= set(r)), None)


def _answers(rows: list[dict]) -> list[dict]:
    return [
        r for r in rows
        if not NOTICE_KEYS <= set(r) and not SIDE_KEYS & set(r)
    ]


def _walk(call, key_fields: tuple[str, ...], page: int = 7):
    """Page through *call* and give ``(keys, total)``.

    The key must hold every field that tells two rows apart.  One line can
    carry two REAL references of one name — two overloads, or two template
    instantiations — thus a key of the file and the line alone reads two
    facts as one row seen twice.
    """
    seen: list[tuple] = []
    offset = 0
    total: int | None = None
    while True:
        rows = call(offset=offset, limit=page)
        notice = _notice(rows)
        if notice is None:
            break
        total = notice["total"] if total is None else total
        body = _answers(rows)
        if not body:
            break
        seen += [tuple(r.get(f) for f in key_fields) for r in body]
        offset += len(body)
        if offset >= total or offset > WALK_CAP:
            break
    return seen, total


@pytest.fixture(params=LIVE_PROJECTS, ids=lambda p: p.name)
def project(request) -> Path:
    return request.param


@pytest.fixture
def index(project: Path):
    """A read-only connection to the index, and its newest completed build."""
    from fw_context_mcp.config import derive_project_id
    from fw_context_mcp.mcp.shared.context import _db_path

    conn = sqlite3.connect(f"file:{_db_path(project)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT config_hash FROM build_configs
           WHERE project_id = ? AND (manifest_verification IS NULL
              OR manifest_verification != 'indexing')
           ORDER BY created_at DESC LIMIT 1""",
        (derive_project_id(project),),
    ).fetchone()
    if row is None:
        conn.close()
        pytest.skip("the project has no completed build")
    yield conn, row["config_hash"]
    conn.close()


def _hottest(project: Path) -> dict | None:
    """The most-called function, which gives the longest walk to test."""
    from fw_context_mcp.mcp.handlers.callgraph import find_hotspots

    rows = find_hotspots(project_root=str(project), limit=1, project_only=False)
    body = [r for r in rows if "caller_count" in r]
    return body[0] if body else None


class TestTheIndexerNamesOnlyCallables:
    """A variable, a class or an enum calls nothing, thus none is a caller.

    The AST walk tracks the enclosing definition on a stack, and the
    caller repair fills what that walk left empty.  Both must name a
    callable: a static table of function pointers holds a callee and calls
    nothing, and so does a member initializer inside a class body.
    """

    def test_no_non_callable_is_recorded_as_a_caller(self, index):
        conn, config_hash = index
        placeholders = ", ".join("?" * len(CALLABLE))
        bad = conn.execute(
            f"""SELECT s.kind, COUNT(*) AS n
                FROM refs r JOIN symbols s
                  ON s.usr = r.from_usr AND s.config_hash = r.config_hash
                WHERE r.config_hash = ? AND s.kind NOT IN ({placeholders})
                GROUP BY s.kind ORDER BY n DESC""",
            (config_hash, *CALLABLE),
        ).fetchall()
        assert not bad, (
            "a non-callable reads as a caller: "
            + ", ".join(f"{r['kind']}={r['n']}" for r in bad)
        )


class TestAPageBoundIsNotAFact:
    """A limit of zero must not turn into a statement about the codebase."""

    @pytest.mark.parametrize("tool_name", ["find_dead_code", "find_hotspots"])
    def test_a_zero_limit_still_answers_with_a_row(self, project: Path, tool_name):
        from fw_context_mcp.mcp.handlers import callgraph

        tool = getattr(callgraph, tool_name)
        answer = tool(project_root=str(project), limit=20)
        error = next((r["error"] for r in answer if "error" in r), None)
        if error:
            # A project with several builds answers with the choice, which
            # is the contract: one query about code answers for one build.
            pytest.skip(f"{tool_name} needs a build named: {error[:60]}")
        full = _notice(answer)
        if full is None or full["total"] == 0:
            pytest.skip(f"{tool_name} has nothing to report for this project")

        zero = _notice(tool(project_root=str(project), limit=0))
        assert zero is not None, f"{tool_name}(limit=0) answered with no page"
        assert zero["shown"] >= 1, zero
        assert zero["total"] == full["total"], "the page bound moved the count"


class TestAWalkStaysInsideOneAnswer:
    """Two pages of one walk never overlap, never skip, never switch."""

    def test_the_busiest_symbol_walks_once_and_whole(self, project: Path):
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        hot = _hottest(project)
        if hot is None:
            pytest.skip("no reference indexed for this project")
        name = hot["qualified_name"]

        keys, total = _walk(
            lambda **kw: find_callers(name, project_root=str(project), **kw),
            ("file", "line", "ref_kind", "caller", "target_qualified_name"),
        )
        assert len(keys) == len(set(keys)), (
            f"{name}: a row came back twice ({len(keys)} rows, "
            f"{len(set(keys))} distinct)"
        )
        assert total is not None
        assert len(keys) >= min(total, WALK_CAP), f"walked {len(keys)} of {total}"

    def test_a_page_past_the_end_starts_no_other_answer(self, project: Path):
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        hot = _hottest(project)
        if hot is None:
            pytest.skip("no reference indexed for this project")
        name = hot["qualified_name"]
        first = _notice(find_callers(name, project_root=str(project), limit=5))
        assert first is not None

        past = find_callers(
            name, project_root=str(project), limit=5, offset=first["total"] + 50,
        )
        assert _notice(past) is None, f"page past the end paged again: {past[:1]}"
        assert any("info" in r for r in past), past[:1]


class TestVirtualDispatchSaysWhereItsRowsSit:
    """An answer that comes from a peer override must not read as direct.

    A method with no call site of its own answers with the call sites
    recorded against the base method it overrides, and against the other
    overrides of that base.  A row on the BASE reaches this symbol through
    dispatch; a row on a SIBLING does not.  The answer must carry both
    facts, because the page notice counts them all.
    """

    def test_a_dispatch_answer_names_what_each_row_is_recorded_against(
        self, project: Path, index,
    ):
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        conn, config_hash = index
        # The tool resolves a NAME, thus the whole name must carry no
        # reference — not only this USR.  An overload shares the qualified
        # name, and one overload with a call site sends the query down the
        # direct path, which owes no provenance.  Measured: three projects
        # failed this test on that alone.
        #
        # Several candidates, because most overrides without a reference
        # have peers without one either.
        candidates = conn.execute(
            """SELECT DISTINCT s.qualified_name FROM symbols s
               JOIN overrides o ON o.derived_usr = s.usr
                                AND o.config_hash = s.config_hash
               WHERE s.config_hash = ? AND s.qualified_name != ''
                 AND s.qualified_name NOT IN (
                     SELECT s2.qualified_name FROM symbols s2
                     JOIN refs r ON r.to_usr = s2.usr
                                AND r.config_hash = s2.config_hash
                     WHERE s2.config_hash = ?)
               LIMIT 40""",
            (config_hash, config_hash),
        ).fetchall()
        if not candidates:
            pytest.skip("no override whose whole name carries no reference")

        for candidate in candidates:
            name = candidate["qualified_name"]
            rows = find_callers(name, project_root=str(project), limit=10)
            body = _answers(rows)
            if not body:
                continue  # the peers hold no caller either
            assert any("warning" in r for r in rows), (
                f"{name}: peer call sites passed as its own"
            )
            assert all("recorded_against" in r for r in body), body[:1]
            assert all("reaches_this_symbol" in r for r in body), body[:1]
            assert any(r["recorded_against"] != name for r in body), (
                f"{name}: a dispatch answer recorded against itself"
            )
            return
        pytest.skip(
            f"the peers of all {len(candidates)} such overrides hold no caller"
        )
