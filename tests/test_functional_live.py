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
environment had BEFORE the suite started, for the duration of one test
(``conftest.operator_environ``).  The list of the projects reads the real
registry the same way.

They are marked ``live`` and excluded from ``make test`` and
``make test-all``, because they depend on what one machine happens to
hold.  Run them with ``make test-live``.

A test walks every indexed project, and every completed build of each one.
A project or a build that holds nothing the test can check (no dead code,
no reference) is passed by, and the test fails when NO index of the
machine holds it: the run then verified nothing, and a skip would look
like a pass.  For the same reason a machine with no indexed project, and
a project with no completed build, fail.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.conftest import operator_environ

NOTICE_KEYS = {"total", "offset", "shown", "more"}
SIDE_KEYS = {"warning", "info", "error", "_did_you_mean"}
# A lambda is a symbol of its own, and the caller of the calls in its body.
CALLABLE = ("function", "method", "constructor", "destructor", "lambda")

#: A walk stops here.  The invariant shows on the first pages; a symbol
#: with thousands of call sites would only cost time.
WALK_CAP = 300


@pytest.fixture(autouse=True)
def _real_index_dir():
    """Undo the index-directory isolation for one live test.

    The isolation of ``conftest.py`` exists so that no test writes to
    ``~/.fw-context/index``.  These tests only READ it, and they have
    nothing to read without it.  The global config of the operator comes
    back too: its ``[index] db_dir`` can name another directory.
    """
    with operator_environ("FW_CONTEXT_INDEX_DIR", global_config=True):
        yield


def _under_temp(path: Path) -> bool:
    """Is *path* inside the system temp directory?

    Before the registry had its isolation, this suite registered throwaway
    projects in the REAL global registry.  Those rows survive the run that
    made them, and a live sweep that picks them up reports skips for
    fixtures rather than answers for the operator's code: measured once,
    27 projects where the machine holds 7.
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
    # The real registry, index directory and global config ([index] db_dir).
    with operator_environ("FW_CONTEXT_PROJECTS_DB", "FW_CONTEXT_INDEX_DIR", global_config=True):
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

pytestmark = pytest.mark.live


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


class Build(NamedTuple):
    """One completed build of a live project, with the arguments that select it in a tool."""

    project: Path
    config_hash: str
    #: ``variant`` and ``image`` for a project of several builds, else empty:
    #: a query names one build exactly when a project holds more than one.
    selector: dict[str, str]

    @property
    def label(self) -> str:
        build = "/".join(v for v in self.selector.values() if v)
        return f"{self.project.name}{f' [{build}]' if build else ''}"


def _builds() -> list[Build]:
    """Every completed build of every live project; fail when the machine holds none.

    The newest row of each (variant, image): a config change leaves the
    row of the older config of the same build.  A project with no
    completed build fails: its index run never finished, and the tests
    would verify nothing of it.
    """
    from fw_context_mcp.config import derive_project_id
    from fw_context_mcp.mcp.shared.context import _db_path

    if not LIVE_PROJECTS:
        pytest.fail("no indexed project on this machine: index a project, then run make test-live")
    builds: list[Build] = []
    for project in LIVE_PROJECTS:
        conn = sqlite3.connect(f"file:{_db_path(project)}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT variant, image, config_hash FROM build_configs b
                   WHERE project_id = ? AND manifest_verification != 'indexing'
                     AND created_at = (SELECT MAX(created_at) FROM build_configs o
                                       WHERE o.project_id = b.project_id
                                         AND o.variant = b.variant AND o.image = b.image
                                         AND o.manifest_verification != 'indexing')
                   ORDER BY variant, image""",
                (derive_project_id(project),),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            pytest.fail(f"{project}: the index holds no completed build; run fw-context index there")
        several = len(rows) > 1
        builds += [
            Build(project, row["config_hash"],
                  {"variant": row["variant"], "image": row["image"]} if several else {})
            for row in rows
        ]
    return builds


def _open(build: Build) -> sqlite3.Connection:
    """A read-only connection to the index of *build*."""
    from fw_context_mcp.mcp.shared.context import _db_path

    conn = sqlite3.connect(f"file:{_db_path(build.project)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _no_error(build: Build, tool_name: str, rows: list[dict]) -> list[dict]:
    """Return *rows*; fail when the tool answered with an error.

    WHY: an error row has no answer row, and a test read it as a build
    with nothing to check.  Measured: an index of an older version answered
    ``schema_mismatch``, and the test passed by it.
    """
    error = next((r["error"] for r in rows if "error" in r), None)
    assert error is None, f"{build.label}: {tool_name} refused the build: {error}"
    return rows


def _hottest(build: Build) -> dict | None:
    """The most-called function of *build*, which gives the longest walk to test."""
    from fw_context_mcp.mcp.handlers.callgraph import find_hotspots

    rows = _no_error(build, "find_hotspots", find_hotspots(
        project_root=str(build.project), limit=1, project_only=False, **build.selector))
    body = [r for r in rows if "caller_count" in r]
    return body[0] if body else None


class TestTheIndexerNamesOnlyCallables:
    """A variable, a class or an enum calls nothing, thus none is a caller.

    The AST walk tracks the enclosing definition on a stack, and the
    caller repair fills what that walk left empty.  Both must name a
    callable: a static table of function pointers holds a callee and calls
    nothing, and so does a member initializer inside a class body.
    """

    def test_no_non_callable_is_recorded_as_a_caller(self):
        placeholders = ", ".join("?" * len(CALLABLE))
        faults = []
        for build in _builds():
            conn = _open(build)
            try:
                bad = conn.execute(
                    f"""SELECT s.kind, COUNT(*) AS n
                        FROM refs r JOIN symbols s
                          ON s.usr = r.from_usr AND s.config_hash = r.config_hash
                        WHERE r.config_hash = ? AND s.kind NOT IN ({placeholders})
                        GROUP BY s.kind ORDER BY n DESC""",
                    (build.config_hash, *CALLABLE),
                ).fetchall()
            finally:
                conn.close()
            if bad:
                faults.append(f"{build.label}: " + ", ".join(f"{r['kind']}={r['n']}" for r in bad))
        assert not faults, "a non-callable reads as a caller: " + "; ".join(faults)


class TestAPageBoundIsNotAFact:
    """A limit of zero must not turn into a statement about the codebase."""

    @pytest.mark.parametrize("tool_name", ["find_dead_code", "find_hotspots"])
    def test_a_zero_limit_still_answers_with_a_row(self, tool_name):
        from fw_context_mcp.mcp.handlers import callgraph

        tool = getattr(callgraph, tool_name)
        checked = 0
        for build in _builds():
            answer = _no_error(build, tool_name, tool(project_root=str(build.project), limit=20, **build.selector))
            full = _notice(answer)
            if full is None or full["total"] == 0:
                continue  # this build has nothing to report; another one must
            zero = _notice(tool(project_root=str(build.project), limit=0, **build.selector))
            assert zero is not None, f"{build.label}: {tool_name}(limit=0) answered with no page"
            assert zero["shown"] >= 1, (build.label, zero)
            assert zero["total"] == full["total"], f"{build.label}: the page bound moved the count"
            checked += 1
        assert checked, f"no index of this machine has a row of {tool_name}: the run verified nothing"


class TestAWalkStaysInsideOneAnswer:
    """Two pages of one walk never overlap, never skip, never switch."""

    def test_the_busiest_symbol_walks_once_and_whole(self):
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        checked = 0
        for build in _builds():
            hot = _hottest(build)
            if hot is None:
                continue  # no reference in this build; another one must have one
            name = hot["qualified_name"]
            keys, total = _walk(
                lambda name=name, build=build, **kw: _no_error(build, "find_callers", find_callers(
                    name, project_root=str(build.project), **build.selector, **kw)),
                ("file", "line", "ref_kind", "caller", "target_qualified_name"),
            )
            assert len(keys) == len(set(keys)), (
                f"{build.label} {name}: a row came back twice ({len(keys)} rows, "
                f"{len(set(keys))} distinct)"
            )
            assert total is not None
            assert len(keys) >= min(total, WALK_CAP), f"{build.label} {name}: walked {len(keys)} of {total}"
            checked += 1
        assert checked, "no index of this machine holds a reference: the run verified nothing"

    def test_a_page_past_the_end_starts_no_other_answer(self):
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        checked = 0
        for build in _builds():
            hot = _hottest(build)
            if hot is None:
                continue  # no reference in this build; another one must have one
            name = hot["qualified_name"]
            first = _notice(_no_error(build, "find_callers", find_callers(
                name, project_root=str(build.project), limit=5, **build.selector)))
            assert first is not None, f"{build.label} {name}: no page notice"

            past = find_callers(
                name, project_root=str(build.project), limit=5, offset=first["total"] + 50, **build.selector,
            )
            assert _notice(past) is None, f"{build.label}: page past the end paged again: {past[:1]}"
            assert any("info" in r for r in past), (build.label, past[:1])
            checked += 1
        assert checked, "no index of this machine holds a reference: the run verified nothing"


class TestVirtualDispatchSaysWhereItsRowsSit:
    """An answer that comes from a peer override must not read as direct.

    A method with no call site of its own answers with the call sites
    recorded against the base method it overrides, and against the other
    overrides of that base.  A row on the BASE reaches this symbol through
    dispatch; a row on a SIBLING does not.  The answer must carry both
    facts, because the page notice counts them all.
    """

    def test_a_dispatch_answer_names_what_each_row_is_recorded_against(self):
        from fw_context_mcp.mcp.handlers.callgraph import find_callers

        checked = 0
        for build in _builds():
            conn = _open(build)
            try:
                # The tool resolves a NAME, thus the whole name must carry no
                # reference — not only this USR.  An overload shares the
                # qualified name, and one overload with a call site sends the
                # query down the direct path, which owes no provenance.
                # Measured: three projects failed this test on that alone.
                #
                # Several candidates, because most overrides without a
                # reference have peers without one either.
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
                    (build.config_hash, build.config_hash),
                ).fetchall()
            finally:
                conn.close()
            for candidate in candidates:
                name = candidate["qualified_name"]
                rows = _no_error(build, "find_callers", find_callers(
                    name, project_root=str(build.project), limit=10, **build.selector))
                body = _answers(rows)
                if not body:
                    continue  # the peers hold no caller either
                assert any("warning" in r for r in rows), (
                    f"{build.label} {name}: peer call sites passed as its own"
                )
                assert all("recorded_against" in r for r in body), (build.label, body[:1])
                assert all("reaches_this_symbol" in r for r in body), (build.label, body[:1])
                assert any(r["recorded_against"] != name for r in body), (
                    f"{build.label} {name}: a dispatch answer recorded against itself"
                )
                checked += 1
                break
        assert checked, (
            "no index of this machine holds an override without a reference whose peers hold a caller: "
            "the run verified nothing"
        )
