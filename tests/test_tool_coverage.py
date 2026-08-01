"""Tool coverage tests — exercises all 27 MCP tools against real indexed projects.

Usage::

    python3 test_tools/test_tool_coverage.py

Discovers all indexed projects, classifies their capabilities, and runs adaptive
tests per project. Positive, negative, and edge-case scenarios for every tool.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

# ── Path setup ──────────────────────────────────────────────────────────────────
_repo_root = Path(__file__).resolve().parents[1]
_src = _repo_root / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from fw_context_mcp.mcp.handlers.maintenance import (  # noqa: E402
    check_ollama,
    get_active_build,
    list_projects,
    reindex_file,
    reset_index,
)
from fw_context_mcp.mcp.handlers._lookup import (  # noqa: E402
    lookup_symbol,
)
from fw_context_mcp.mcp.handlers.search import (  # noqa: E402
    search_code,
    semantic_search,
    smart_search,
)
from fw_context_mcp.mcp.handlers.callgraph import (  # noqa: E402
    find_all_callers_recursive,
    find_call_path,
    find_callees_recursive,
    find_callers,
    find_dead_code,
    find_hotspots,
    find_references,
    find_wrapper_callers,
    trace_data_flow,
)
from fw_context_mcp.mcp.handlers.source import (  # noqa: E402
    explain_symbol,
    get_file_map,
    get_source,
    get_symbol_context,
)
from fw_context_mcp.mcp.handlers.inheritance import (  # noqa: E402
    get_class_members,
    get_inheritance_chain,
    get_method_overrides,
    get_template_instances,
)

# ── Helpers ─────────────────────────────────────────────────────────────────────

INDEX_ROOT = Path.home() / ".fw-context" / "index"


def _db_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _is_list_of_dicts(obj) -> bool:
    return isinstance(obj, list) and all(isinstance(x, dict) for x in obj)


def _is_dict(obj) -> bool:
    return isinstance(obj, dict)


# ── Results tracker ─────────────────────────────────────────────────────────────


class CheckResults:
    """Per-project test statistics."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.total = 0
        self.failures: list[str] = []
        self._section_counts: dict[str, int] = {}

    def check(self, desc: str, cond: bool) -> None:
        self.total += 1
        if cond:
            self.passed += 1
            print(f"  [{self.total:03d}] \033[32mOK\033[0m     {desc}")
        else:
            self.failed += 1
            self.failures.append(desc)
            print(f"  [{self.total:03d}] \033[31mFAIL\033[0m   {desc}")

    def section(self, title: str) -> None:
        count = self._section_counts.get(title, 0)
        self._section_counts[title] = count + 1
        if count == 0:
            print(f"\n── {title}")

    def summary(self, project_count: int) -> None:
        print(f"\n{'=' * 60}")
        print(f"  SUMMARY: {self.passed}/{self.total} passed, {self.failed} failed  "
              f"[{project_count} project(s)]")
        print(f"{'=' * 60}")
        if self.failed == 0:
            print("  ALL TESTS PASSED \033[32m✓\033[0m")
        else:
            print(f"  \033[31m{self.failed} FAILURES:\033[0m")
            for f in self.failures:
                print(f"    - {f}")


# ── Safe tool caller ────────────────────────────────────────────────────────────


def _call(results: CheckResults, desc: str, fn, *args, **kwargs):
    """Call a tool function, catching exceptions."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        results.check(f"{desc} (exception: {type(e).__name__}: {e})", False)
        return None


async def _call_async(results: CheckResults, desc: str, fn, *args, **kwargs):
    """Call an async tool function, catching exceptions."""
    try:
        return await fn(*args, **kwargs)
    except Exception as e:
        results.check(f"{desc} (exception: {type(e).__name__}: {e})", False)
        return None


def _async(results: CheckResults, fn, *args, **kwargs):
    """Run an async tool via asyncio."""
    return asyncio.run(_call_async(results, "", fn, *args, **kwargs))


# ── Project discovery ───────────────────────────────────────────────────────────


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a table has a specific column."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == column for c in cols)


def _get_config_hash(conn: sqlite3.Connection) -> str | None:
    """Get the latest config_hash, returning None if not found."""
    row = conn.execute(
        "SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["config_hash"] if row else None


def _discover_projects() -> dict[str, dict]:
    """Return {label: {root, db_path, build_system, capabilities, test_symbols}}."""
    projects: dict[str, dict] = {}
    if not INDEX_ROOT.exists():
        return projects

    for p_dir in sorted(INDEX_ROOT.iterdir()):
        if not p_dir.is_dir():
            continue
        db = p_dir / "index.db"
        if not db.exists():
            continue

        try:
            conn = _db_connect(db)

            # Check basic schema health
            ch = _get_config_hash(conn)
            if ch is None:
                conn.close()
                continue

            # Check if symbols table exists
            if not _has_column(conn, "symbols", "config_hash"):
                conn.close()
                continue

            # Get project info (handle both old and new schemas)
            if _has_column(conn, "build_configs", "build_system"):
                row = conn.execute(
                    "SELECT p.name, p.root_path, b.build_system "
                    "FROM projects p JOIN build_configs b ON b.project_id = p.project_id "
                    "ORDER BY b.created_at DESC LIMIT 1"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT p.name, p.root_path FROM projects p LIMIT 1"
                ).fetchone()

            if row is None:
                conn.close()
                continue

            label = row["name"] or p_dir.name
            root = Path(row["root_path"])
            build_sys = (row["build_system"] if _has_column(conn, "build_configs", "build_system")
                        else "unknown") or "unknown"

            # Counts
            total_syms = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,)
            ).fetchone()[0]
            total_refs = 0
            if _has_column(conn, "refs", "config_hash"):
                total_refs = conn.execute(
                    "SELECT COUNT(*) FROM refs WHERE config_hash=?", (ch,)
                ).fetchone()[0]

            # Capabilities
            has_refs = total_refs > 0
            has_classes = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND kind IN ('class','struct') AND is_definition=1 AND file_path LIKE 'src/%'",
                (ch,),
            ).fetchone()[0] > 0
            has_inheritance = False
            if _has_column(conn, "inheritance", "config_hash"):
                has_inheritance = conn.execute(
                    "SELECT COUNT(*) FROM inheritance WHERE config_hash=?", (ch,)
                ).fetchone()[0] > 0
            has_templates = False
            if _has_column(conn, "symbols", "is_template"):
                has_templates = conn.execute(
                    "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND is_template=1 AND file_path LIKE 'src/%'",
                    (ch,),
                ).fetchone()[0] > 0
            has_virtual = False
            if _has_column(conn, "symbols", "is_virtual"):
                has_virtual = conn.execute(
                    "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND (is_virtual=1 OR is_pure_virtual=1)",
                    (ch,),
                ).fetchone()[0] > 0

            # Discover test symbols
            test_symbols: dict[str, str] = {}

            # A function that definitely exists
            fn_row = conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='function' AND is_definition=1 AND file_path LIKE 'src/%' LIMIT 1",
                (ch,),
            ).fetchone()
            if fn_row:
                test_symbols["function"] = fn_row["name"]

            # A function with callers (for call-graph tests)
            if has_refs:
                fn_caller_row = conn.execute(
                    """SELECT s.name FROM symbols s
                       JOIN refs r ON r.to_usr = s.usr AND r.config_hash = s.config_hash
                       WHERE s.config_hash = ? AND s.is_definition=1
                         AND s.kind IN ('function','method')
                         AND s.file_path LIKE 'src/%'
                         AND r.ref_kind IN ('call','indirect')
                       GROUP BY s.usr ORDER BY COUNT(r.rowid) DESC LIMIT 1""",
                    (ch,),
                ).fetchone()
                if fn_caller_row:
                    test_symbols["called_function"] = fn_caller_row["name"]

                # A second function with callers (for call-path tests)
                fn_caller2_row = conn.execute(
                    """SELECT s.name FROM symbols s
                       JOIN refs r ON r.to_usr = s.usr AND r.config_hash = s.config_hash
                       WHERE s.config_hash = ? AND s.is_definition=1
                         AND s.kind IN ('function','method')
                         AND s.file_path LIKE 'src/%'
                         AND r.ref_kind IN ('call','indirect')
                         AND s.name != ?
                       GROUP BY s.usr ORDER BY COUNT(r.rowid) DESC LIMIT 1""",
                    (ch, test_symbols.get("called_function", "")),
                ).fetchone()
                if fn_caller2_row:
                    test_symbols["called_function_2"] = fn_caller2_row["name"]

            # A class/struct (for OOP tests)
            if has_classes:
                cls_row = conn.execute(
                    "SELECT name FROM symbols WHERE config_hash=? AND kind='class' AND is_definition=1 AND file_path LIKE 'src/%' LIMIT 1",
                    (ch,),
                ).fetchone()
                if cls_row:
                    test_symbols["class"] = cls_row["name"]

            # A class with inheritance (for inheritance chain)
            if has_inheritance:
                inh_row = conn.execute(
                    """SELECT s.name FROM symbols s
                       JOIN inheritance i ON i.derived_usr = s.usr AND i.config_hash = s.config_hash
                       WHERE s.config_hash = ? AND s.is_definition=1
                       LIMIT 1""",
                    (ch,),
                ).fetchone()
                if inh_row:
                    test_symbols["class_with_inheritance"] = inh_row["name"]

            # A virtual method (for override tests)
            if has_virtual:
                vm_row = conn.execute(
                    "SELECT name, qualified_name FROM symbols WHERE config_hash=? AND is_virtual=1 AND kind='method' LIMIT 1",
                    (ch,),
                ).fetchone()
                if vm_row:
                    test_symbols["virtual_method"] = vm_row["qualified_name"] or vm_row["name"]

            # A template (for template instances)
            if has_templates:
                tpl_row = conn.execute(
                    "SELECT name FROM symbols WHERE config_hash=? AND is_template=1 AND file_path LIKE 'src/%' LIMIT 1",
                    (ch,),
                ).fetchone()
                if tpl_row:
                    test_symbols["template"] = tpl_row["name"]

            # A driver class (for wrapper callers)
            drv_row = conn.execute(
                """SELECT s.name FROM symbols s
                   WHERE s.config_hash = ? AND s.kind = 'class'
                     AND s.is_definition = 1
                     AND s.name LIKE '%_DRIVER'
                   LIMIT 1""",
                (ch,),
            ).fetchone()
            if drv_row:
                test_symbols["driver_class"] = drv_row["name"]

            # A source file with many symbols
            file_row = conn.execute(
                """SELECT file_path FROM symbols
                   WHERE config_hash=? AND file_path LIKE 'src/%'
                   GROUP BY file_path ORDER BY COUNT(*) DESC LIMIT 1""",
                (ch,),
            ).fetchone()
            if file_row:
                test_symbols["top_file"] = file_row["file_path"]

            # A file with LLM analysis
            if _has_column(conn, "file_analysis", "config_hash"):
                analysis_row = conn.execute(
                    """SELECT f.path FROM file_analysis fa
                       JOIN files f ON f.id = fa.file_id
                       WHERE fa.config_hash = ? AND f.path LIKE 'src/%'
                       LIMIT 1""",
                    (ch,),
                ).fetchone()
                if analysis_row:
                    test_symbols["analyzed_file"] = analysis_row["path"]

            # A type name for data flow
            type_row = conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind IN ('struct','class') AND is_definition=1 AND file_path LIKE 'src/%' LIMIT 1",
                (ch,),
            ).fetchone()
            if type_row:
                test_symbols["struct_type"] = type_row["name"]

            conn.close()

            projects[label] = {
                "root": str(root),
                "db_path": db,
                "build_system": build_sys,
                "symbol_count": total_syms,
                "reference_count": total_refs,
                "has_refs": has_refs,
                "has_classes": has_classes,
                "has_inheritance": has_inheritance,
                "has_templates": has_templates,
                "has_virtual": has_virtual,
                "has_driver": "driver_class" in test_symbols,
                "test_symbols": test_symbols,
            }

        except Exception as e:
            conn = None
            try:
                conn = _db_connect(db)
                row = conn.execute("SELECT name, root_path FROM projects LIMIT 1").fetchone()
                label = row["name"] if row else p_dir.name
                root = row["root_path"] if row else ""
            except Exception:
                label = p_dir.name
                root = ""
            finally:
                if conn:
                    conn.close()

            projects[label] = {
                "root": root,
                "db_path": db,
                "build_system": "unknown",
                "symbol_count": 0,
                "reference_count": 0,
                "has_refs": False,
                "has_classes": False,
                "has_inheritance": False,
                "has_templates": False,
                "has_virtual": False,
                "has_driver": False,
                "test_symbols": {},
                "error": str(e),
            }

    return projects


# ── Per-project test runner ────────────────────────────────────────────────────


def run_tests_for_project(proj: dict, results: CheckResults) -> None:
    root = proj["root"]
    sym = proj["test_symbols"]
    has_refs = proj["has_refs"]
    has_cls = proj["has_classes"]
    has_tpl = proj["has_templates"]
    has_virt = proj["has_virtual"]
    has_drv = proj["has_driver"]

    if proj.get("error"):
        results.check(f"project discovery: {proj['error']}", False)
        return

    # ═══ 1. METADATA ═══════════════════════════════════════════════════════════
    results.section("1. Metadata")

    # get_active_build
    build = _call(results, "get_active_build", get_active_build, project_root=root)
    if build:
        results.check("get_active_build → has config_hash", "config_hash" in build)
        results.check("get_active_build → has project_id", "project_id" in build)
        results.check("get_active_build → has build_system", "build_system" in build)
        results.check("get_active_build → symbol_count > 0", build.get("symbol_count", 0) > 0)
        results.check("get_active_build → has schema_version", "schema_version" in build)
        results.check("get_active_build → has stale flag", "stale" in build)
    bad_build = _call(results, "get_active_build nonexistent", get_active_build,
                      project_root="/tmp/__nonexistent_fw_context_test__")
    if bad_build is not None:
        results.check("get_active_build nonexistent root → error",
                      "error" in bad_build or "config_hash" not in bad_build)

    # list_projects
    projs = _call(results, "list_projects", list_projects)
    if projs is not None:
        results.check("list_projects → returns list", isinstance(projs, list))
        results.check("list_projects → at least 1 project", len(projs) > 0)
        if projs:
            p0 = projs[0]
            results.check("list_projects → entries have project_id", "project_id" in p0)
            results.check("list_projects → entries have name", "name" in p0)
            results.check("list_projects → entries have build_system", "build_system" in p0)

    # check_ollama
    ollama = _call(results, "check_ollama", check_ollama)
    if ollama:
        results.check("check_ollama → returns dict", isinstance(ollama, dict))
        results.check("check_ollama → has status", "status" in ollama)
        results.check("check_ollama → has ollama_running", "ollama_running" in ollama)

    # ═══ 2. SYMBOL LOOKUP ═══════════════════════════════════════════════════════
    results.section("2. Symbol Lookup")

    # lookup_symbol — prefix
    r = lookup_symbol("init", project_root=root)
    results.check("lookup_symbol prefix 'init' → results", len(r) > 0)
    results.check("lookup_symbol results have name field", all("name" in x for x in r[:5]))
    results.check("lookup_symbol results have kind field", all("kind" in x for x in r[:5]))

    # lookup_symbol — exact
    r = lookup_symbol("init", project_root=root, exact=True)
    results.check("lookup_symbol exact 'init' → returns something", isinstance(r, list))

    # lookup_symbol — nonexistent
    r = lookup_symbol("___nonexistent_xyz_123___", project_root=root)
    results.check("lookup_symbol nonexistent → empty list", r == [])

    # lookup_symbol — empty string
    try:
        r = lookup_symbol("", project_root=root)
        results.check("lookup_symbol empty string → no crash", isinstance(r, list))
    except Exception as e:
        results.check(f"lookup_symbol empty string → handled ({type(e).__name__})", True)

    # lookup_symbol — limit
    r = lookup_symbol("init", project_root=root, limit=3)
    results.check("lookup_symbol limit=3 → at most 3 results", len(r) <= 3)

    # lookup_symbol — qualified name
    if sym.get("called_function"):
        r = lookup_symbol(sym["called_function"], project_root=root)
        results.check(
            f"lookup_symbol '{sym['called_function']}' → found",
            len(r) > 0,
        )

    # ═══ 3. GET SOURCE ══════════════════════════════════════════════════════════
    results.section("3. Get Source")

    if sym.get("function"):
        src = _call(results, "get_source", get_source, sym["function"], project_root=root)
        if src and "error" not in src:
            results.check("get_source → has name", "name" in src)
            results.check("get_source → has kind", "kind" in src)
            results.check("get_source → has file", "file" in src)
            results.check("get_source → has source", "source" in src)
            results.check("get_source → source is non-empty string", len(src.get("source", "")) > 0)

    # get_source — nonexistent
    src = get_source("___nonexistent_xyz___", project_root=root)
    results.check("get_source nonexistent → error", "error" in src)

    # get_source on a common symbol
    src = _call(results, "get_source 'main'", get_source, "main", project_root=root)
    if src:
        results.check("get_source 'main' → returns dict (found or error)", isinstance(src, dict))

    # ═══ 4. FILE MAP ════════════════════════════════════════════════════════════
    results.section("4. File Map")

    if sym.get("top_file"):
        fm = _call(results, "get_file_map", get_file_map, sym["top_file"], project_root=root)
        if fm and "error" not in fm:
            results.check("file_map → total_symbols > 0", fm.get("total_symbols", 0) > 0)
            results.check("file_map → has symbols dict", isinstance(fm.get("symbols"), dict))
            results.check("file_map → has file field", "file" in fm)

        # Suffix match (may not resolve for all projects)
        fname = Path(sym["top_file"]).name
        fm2 = _call(results, "get_file_map suffix", get_file_map, fname, project_root=root)
        if fm2:
            results.check(f"file_map suffix '{fname}' → no crash",
                          "error" in fm2 or fm2.get("total_symbols", -1) >= 0)

        # With signatures
        fm3 = _call(results, "get_file_map signatures", get_file_map, sym["top_file"],
                    project_root=root, signatures=True)
        results.check("file_map signatures=True → no crash", fm3 is not None)

        # With max_per_kind=0
        fm4 = _call(results, "get_file_map max=0", get_file_map, sym["top_file"],
                    project_root=root, max_per_kind=0)
        results.check("file_map max_per_kind=0 → no crash", fm4 is not None)

    # Negative
    fm_err = get_file_map("___nonexistent_file_xyz__.cpp", project_root=root)
    results.check("file_map nonexistent → error or 0 symbols",
                  "error" in fm_err or fm_err.get("total_symbols", -1) == 0)

    # ═══ 6. SYMBOL CONTEXT ══════════════════════════════════════════════════════
    results.section("6. Symbol Context")

    if sym.get("function"):
        ctx = _call(results, "get_symbol_context", get_symbol_context,
                    sym["function"], project_root=root)
        if ctx and "error" not in ctx:
            results.check("symbol_context → has name", "name" in ctx)
            results.check("symbol_context → has kind", "kind" in ctx)
            results.check("symbol_context → has callers", "callers" in ctx)
            results.check("symbol_context → has callees", "callees" in ctx)
            results.check("symbol_context → has source", "source" in ctx)

        # project_only=False
        ctx2 = _call(results, "get_symbol_context project_only=False", get_symbol_context,
                     sym["function"], project_root=root, project_only=False)
        results.check("symbol_context project_only=False → no crash", ctx2 is not None)

    ctx_err = get_symbol_context("___nonexistent_xyz___", project_root=root)
    results.check("symbol_context nonexistent → error", "error" in ctx_err)

    # ═══ 7. EXPLAIN SYMBOL ═════════════════════════════════════════════════════
    results.section("7. Explain Symbol")

    if sym.get("function"):
        expl = _async(results, explain_symbol, sym["function"], project_root=root)
        if expl and "error" not in expl:
            results.check("explain_symbol → has name", "name" in expl)
            results.check("explain_symbol → has explanation", "explanation" in expl)
            results.check("explain_symbol → has kind", "kind" in expl)

        # context_lines
        expl2 = _async(results, explain_symbol, sym["function"], project_root=root, context_lines=5)
        results.check("explain_symbol context_lines=5 → no crash", expl2 is not None)

    expl_err = _async(results, explain_symbol, "___nonexistent___", project_root=root)
    if expl_err:
        results.check("explain_symbol nonexistent → error", "error" in expl_err)

    # ═══ 8. SEARCH CODE ═════════════════════════════════════════════════════════
    results.section("8. Search Code")

    # Basic FTS5
    r = _call(results, "search_code 'init'", search_code, "init", project_root=root)
    if r is not None:
        results.check("search_code 'init' → results", len(r) > 0)
        results.check("search_code results have name", all("name" in x for x in r[:5] if isinstance(x, dict)))

    # Kind filter
    r = search_code("init", project_root=root, kind="function")
    kinds = {x.get("kind") for x in r[:10] if isinstance(x, dict)}
    results.check("search_code kind=function → only functions", kinds.issubset({"function", "method", "constructor"}))

    r = search_code("init", project_root=root, kind="class")
    results.check("search_code kind=class → no crash", isinstance(r, list))

    # Wildcard
    r = search_code("init*", project_root=root, limit=5)
    results.check("search_code 'init*' wildcard → no crash", isinstance(r, list))

    # Multi-word
    r = search_code("modem init", project_root=root, limit=5)
    results.check("search_code 'modem init' → no crash", isinstance(r, list))

    # Empty query
    try:
        r = search_code("", project_root=root)
        results.check("search_code empty → handled", isinstance(r, list))
    except Exception:
        results.check("search_code empty → handled gracefully", True)

    # Nonexistent
    r = search_code("zzz_nonexistent_term_zzz", project_root=root)
    results.check("search_code nonexistent → empty list", r == [] or all("info" in x or "error" in x for x in r))

    # limit
    r = search_code("init", project_root=root, limit=3)
    results.check("search_code limit=3 → at most 3", len(r) <= 3)

    # ═══ 9. SEMANTIC SEARCH ═════════════════════════════════════════════════════
    results.section("9. Semantic Search")

    sem = _async(results, semantic_search, "sensor reading measurement", project_root=root, limit=5)
    if sem is not None:
        results.check("semantic_search → returns list", isinstance(sem, list))
        if sem and isinstance(sem[0], dict):
            if "_similarity" in sem[0]:
                results.check("semantic_search → embedding results with _similarity", True)
            elif "_method" in sem[0]:
                results.check(f"semantic_search → fallback mode: {sem[0].get('_method')}", True)

    # threshold extreme
    sem2 = _async(results, semantic_search, "init", project_root=root, threshold=0.99, limit=3)
    results.check("semantic_search threshold=0.99 → no crash", sem2 is not None)

    # very short query
    sem3 = _async(results, semantic_search, "a", project_root=root, limit=3)
    results.check("semantic_search short query 'a' → no crash", sem3 is not None)

    # ═══ 10. SMART SEARCH ═══════════════════════════════════════════════════════
    results.section("10. Smart Search")

    sm = _async(results, smart_search, "device initialization", project_root=root, limit=5)
    if sm is not None:
        results.check("smart_search → returns list", isinstance(sm, list))
        has_metadata = any(isinstance(x, dict) and "_generated_queries" in x for x in sm)
        results.check("smart_search → has metadata entries", has_metadata or len(sm) == 0)

    # Short query
    sm2 = _async(results, smart_search, "init", project_root=root, limit=3)
    results.check("smart_search short query → no crash", sm2 is not None)

    # ═══ 11. CALL GRAPH — DIRECT ════════════════════════════════════════════════
    if has_refs:
        results.section("11. Call Graph — Direct")

        if sym.get("called_function"):
            callers = _call(results, "find_callers", find_callers,
                           sym["called_function"], project_root=root)
            if callers is not None and _is_list_of_dicts(callers):
                results.check(f"find_callers '{sym['called_function']}' → results", len(callers) > 0)
                if callers:
                    c0 = callers[0]
                    results.check("find_callers → entry has file", "file" in c0)
                    results.check("find_callers → entry has ref_kind", "ref_kind" in c0)
                    results.check("find_callers → entry has caller", "caller" in c0)

            # limit
            c2 = find_callers(sym["called_function"], project_root=root, limit=3)
            results.check("find_callers limit=3 → at most 3", len(c2) <= 3)

            # find_references
            refs = _call(results, "find_references", find_references,
                        sym["called_function"], project_root=root)
            if refs is not None and _is_list_of_dicts(refs):
                results.check(f"find_references '{sym['called_function']}' → results", len(refs) > 0)

        # Nonexistent
        cr_err = find_callers("___nonexistent___", project_root=root)
        results.check("find_callers nonexistent → error or info",
                      any(k in cr_err[0] for k in ("error", "info")) if cr_err else True)

    # ═══ 12. CALL GRAPH — RECURSIVE ═════════════════════════════════════════════
    if has_refs:
        results.section("12. Call Graph — Recursive")

        if sym.get("called_function"):
            ac = _call(results, "find_all_callers_recursive", find_all_callers_recursive,
                      sym["called_function"], project_root=root, max_depth=2, limit=20)
            if ac is not None and _is_list_of_dicts(ac):
                results.check(f"recursive callers '{sym['called_function']}' d=2 → results", len(ac) > 0)
                depths = {x.get("depth", 0) for x in ac}
                results.check("recursive callers → has depth > 1 entries", max(depths, default=0) >= 1)

            cc = _call(results, "find_callees_recursive", find_callees_recursive,
                      sym["called_function"], project_root=root, max_depth=2, limit=20)
            if cc is not None:
                results.check(f"recursive callees '{sym['called_function']}' → no crash", _is_list_of_dicts(cc))

        ac_err = find_all_callers_recursive("___nonexistent___", project_root=root)
        results.check("recursive callers nonexistent → error or info",
                      any(k in ac_err[0] for k in ("error", "info")) if ac_err else True)

    # ═══ 13. CALL PATH ══════════════════════════════════════════════════════════
    if has_refs and sym.get("called_function") and sym.get("called_function_2"):
        results.section("13. Call Path")

        path = _call(results, "find_call_path", find_call_path,
                    sym["called_function"], sym["called_function_2"],
                    project_root=root, max_depth=5)
        if path is not None:
            results.check("find_call_path → returns list", isinstance(path, list))
            if path and isinstance(path[0], dict):
                if "chain" in path[0]:
                    results.check("find_call_path → has chain field", True)
                    results.check("find_call_path → has depth field", "depth" in path[0])

        # Same symbol
        path2 = find_call_path(sym["called_function"], sym["called_function"],
                              project_root=root, max_depth=1)
        results.check("find_call_path same symbol → no crash", isinstance(path2, list))

        # Nonexistent
        path_err = find_call_path("___nonexistent___", sym["called_function"], project_root=root)
        results.check("find_call_path nonexistent → error",
                      any(k in path_err[0] for k in ("error", "info")) if path_err else True)

    # ═══ 14. CODE QUALITY ═══════════════════════════════════════════════════════
    if has_refs:
        results.section("14. Code Quality")

        # find_hotspots
        hs = _call(results, "find_hotspots", find_hotspots, project_root=root, limit=10)
        if hs is not None and _is_list_of_dicts(hs) and len(hs) > 0:
            results.check("find_hotspots → results", len(hs) > 0)
            results.check("find_hotspots → has caller_count", "caller_count" in hs[0])
            results.check("find_hotspots → has kind", "kind" in hs[0])
            # Check sorting
            counts = [x.get("caller_count", 0) for x in hs]
            results.check("find_hotspots → sorted descending",
                          all(counts[i] >= counts[i + 1] for i in range(len(counts) - 1)))

            # project_only toggle
            hs2 = find_hotspots(project_root=root, limit=10, project_only=False)
            results.check("find_hotspots project_only=False → no crash", _is_list_of_dicts(hs2))

            # exclude_paths
            hs3 = find_hotspots(project_root=root, limit=5, exclude_paths=["build/%"])
            results.check("find_hotspots exclude_paths → no crash", _is_list_of_dicts(hs3))

        # find_dead_code
        dc = _call(results, "find_dead_code", find_dead_code, project_root=root, limit=10)
        if dc is not None and _is_list_of_dicts(dc):
            kinds = {x.get("kind") for x in dc}
            results.check("find_dead_code → only callable kinds",
                          kinds.issubset({"function", "method", "constructor", "destructor"}))

            dc2 = find_dead_code(project_root=root, limit=5, project_only=False)
            results.check("find_dead_code project_only=False → no crash", _is_list_of_dicts(dc2))

    # ═══ 15. WRAPPER PATTERNS (OOP) ═════════════════════════════════════════════
    if has_refs and has_cls and has_drv:
        results.section("15. Wrapper Patterns")

        wr = _call(results, "find_wrapper_callers", find_wrapper_callers,
                  sym["driver_class"], project_root=root)
        if wr is not None:
            results.check("find_wrapper_callers → returns list", isinstance(wr, list))
            if wr and isinstance(wr[0], dict):
                results.check("find_wrapper_callers → has wrapper_class", "wrapper_class" in wr[0])

        wr_err = find_wrapper_callers("___nonexistent___", project_root=root)
        results.check("find_wrapper_callers nonexistent → error",
                      any(k in wr_err[0] for k in ("error", "info")) if wr_err else True)

    # ═══ 16. DATA FLOW ══════════════════════════════════════════════════════════
    if has_refs and sym.get("struct_type") and sym.get("called_function"):
        results.section("16. Data Flow")

        df = _call(results, "trace_data_flow", trace_data_flow,
                  sym["struct_type"], sym["called_function"], project_root=root, max_depth=5)
        if df is not None:
            results.check("trace_data_flow → returns list", isinstance(df, list))
            has_summary = any(isinstance(x, dict) and "_summary" in x for x in df)
            results.check("trace_data_flow → has _summary metadata", has_summary or len(df) == 0)

        # Nonexistent type
        df_err = trace_data_flow("___nonexistent_type___", sym["called_function"],
                                 project_root=root)
        results.check("trace_data_flow nonexistent type → info",
                      any(k in df_err[0] for k in ("info", "error")) if df_err else True)

    # ═══ 17. INHERITANCE (OOP) ═════════════════════════════════════════════════
    if has_cls:
        results.section("17. Inheritance")

        test_class = sym.get("class_with_inheritance") or sym.get("class")
        if test_class:
            inh = _call(results, "get_inheritance_chain", get_inheritance_chain,
                       test_class, project_root=root)
            if inh and "error" not in inh:
                results.check("inheritance_chain → has bases", "bases" in inh)
                results.check("inheritance_chain → has derived", "derived" in inh)
                results.check("inheritance_chain → has name", "name" in inh)

                # Transitive
                inh2 = get_inheritance_chain(test_class, project_root=root, transitive=True)
                if "all_bases" in inh2:
                    results.check("inheritance_chain transitive → has all_bases", True)
                if "all_derived" in inh2:
                    results.check("inheritance_chain transitive → has all_derived", True)

        # Non-class symbol
        if sym.get("function"):
            inh_err = get_inheritance_chain(sym["function"], project_root=root)
            results.check(
                "inheritance_chain non-class → error",
                "error" in inh_err,
            )

    # ═══ 18. CLASS MEMBERS (OOP) ════════════════════════════════════════════════
    if has_cls:
        results.section("18. Class Members")

        test_class = sym.get("class_with_inheritance") or sym.get("class")
        if test_class:
            cm = _call(results, "get_class_members", get_class_members,
                      test_class, project_root=root)
            if cm and "error" not in cm:
                results.check("class_members → has members", "members" in cm)
                results.check("class_members → has member_count", "member_count" in cm)
                results.check("class_members → has name", "name" in cm)
                results.check("class_members → members is dict", isinstance(cm.get("members"), dict))

        # Non-class
        if sym.get("function"):
            cm_err = get_class_members(sym["function"], project_root=root)
            results.check("class_members non-class → error", "error" in cm_err)

    # ═══ 19. TEMPLATE INSTANCES (OOP) ═══════════════════════════════════════════
    if has_tpl:
        results.section("19. Template Instances")

        ti = _call(results, "get_template_instances", get_template_instances,
                  sym["template"], project_root=root)
        if ti is not None:
            results.check("get_template_instances → returns list", isinstance(ti, list))
            if ti and isinstance(ti[0], dict):
                results.check("get_template_instances → has signature", "signature" in ti[0])

        # Non-template
        if sym.get("function"):
            ti_err = get_template_instances(sym["function"], project_root=root)
            if ti_err and isinstance(ti_err, list) and len(ti_err) > 0:
                results.check("template_instances non-template → error",
                              "error" in ti_err[0])

    # ═══ 20. METHOD OVERRIDES (OOP) ════════════════════════════════════════════
    if has_virt:
        results.section("20. Method Overrides")

        mo = _call(results, get_method_overrides, sym["virtual_method"], project_root=root)
        if mo and "error" not in mo:
            results.check("method_overrides → has name", "name" in mo)
            results.check("method_overrides → has overrides", "overrides" in mo)
            results.check("method_overrides → has overridden_by", "overridden_by" in mo)

        # Non-method
        if sym.get("function"):
            mo_err = _call(results, get_method_overrides, sym["function"], project_root=root)
            if mo_err:
                results.check("method_overrides non-method → error", "error" in mo_err)

    # ═══ 21. MUTATION (DRY-RUN) ════════════════════════════════════════════════
    results.section("21. Mutation (dry-run)")

    # reset_index dry-run (confirm=False)
    ri = _call(results, "reset_index dry-run", reset_index, project_root=root, confirm=False)
    if ri:
        results.check("reset_index confirm=False → action=dry_run",
                      ri.get("action") == "dry_run")
        results.check("reset_index dry-run → has symbol_count",
                      "symbol_count" in ri or "message" in ri)

    # reset_index nonexistent
    ri_err = _call(results, "reset_index nonexistent", reset_index,
                   project_root="/tmp/__nonexistent_fw_context_test__", confirm=False)
    if ri_err:
        results.check("reset_index nonexistent → error", "error" in ri_err)

    # reindex_file (safe but slow — re-parses file via libclang; skipped by default)
    # To enable, set RUN_REINDEX = True
    RUN_REINDEX = False
    if RUN_REINDEX and sym.get("top_file"):
        ri_f = _call(results, "reindex_file", reindex_file, sym["top_file"], project_root=root)
        if ri_f:
            results.check("reindex_file → returns dict", isinstance(ri_f, dict))
            results.check("reindex_file → has symbols_updated or error",
                          "symbols_updated" in ri_f or "error" in ri_f)

        ri_fe = _call(results, "reindex_file nonexistent", reindex_file,
                      "src/___nonexistent_file__.cpp", project_root=root)
        if ri_fe:
            results.check("reindex_file nonexistent → error", "error" in ri_fe)

    # ═══ 22. CROSS-PROJECT EDGE CASES ═══════════════════════════════════════════
    results.section("22. Edge Cases")

    # Special chars in lookup
    r = lookup_symbol("foo::bar::baz", project_root=root)
    results.check("lookup_symbol 'foo::bar::baz' → no crash", isinstance(r, list))

    # Very high limit
    r = search_code("init", project_root=root, limit=100)
    results.check("search_code limit=100 → no crash", isinstance(r, list))

    # kind with special name
    r = search_code("a", project_root=root, kind="function")
    results.check("search_code single char kind=function → no crash", isinstance(r, list))

    # lookup_symbol with special chars
    r = lookup_symbol("__start*", project_root=root)
    results.check("lookup_symbol '__start*' → no crash", isinstance(r, list))

    # find_call_path max_depth=1
    if has_refs and sym.get("called_function") and sym.get("called_function_2"):
        p = find_call_path(sym["called_function"], sym["called_function_2"],
                          project_root=root, max_depth=1)
        results.check("find_call_path max_depth=1 → no crash", isinstance(p, list))

    # find_hotspots limit=1
    if has_refs:
        hs1 = find_hotspots(project_root=root, limit=1)
        results.check("find_hotspots limit=1 → at most 1", len(hs1) <= 1)

    # find_dead_code with custom excludes
    if has_refs:
        dcx = find_dead_code(project_root=root, limit=5, exclude_paths=["build/%", "tests/%"])
        results.check("find_dead_code custom excludes → no crash", isinstance(dcx, list))


# ═══ Main ════════════════════════════════════════════════════════════════════════


def main() -> None:
    projects = _discover_projects()

    if not projects:
        print("No indexed projects found in ~/.fw-context/index/")
        print("Run 'fw-context index' in a firmware project first.")
        sys.exit(0)

    print(f"Found {len(projects)} project(s):")
    for label, p in projects.items():
        caps = []
        if p["has_refs"]:
            caps.append("refs")
        if p["has_classes"]:
            caps.append("OOP")
        if p["has_templates"]:
            caps.append("templates")
        if p["has_inheritance"]:
            caps.append("inheritance")
        print(f"  {label}: {p['symbol_count']} syms"
              f"{' [' + ', '.join(caps) + ']' if caps else ''}")
        if p.get("error"):
            print(f"    WARNING: {p['error']}")

    results = CheckResults()

    for label, proj in sorted(projects.items()):
        print(f"\n{'=' * 60}")
        print(f"  {label}")
        print(f"  {proj['root']}")
        print(f"{'=' * 60}")
        run_tests_for_project(proj, results)

    results.summary(len(projects))
    sys.exit(0 if results.failed == 0 else 1)


if __name__ == "__main__":
    main()
