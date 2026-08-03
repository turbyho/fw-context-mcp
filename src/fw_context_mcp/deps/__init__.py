"""Dependency audit and auto-repair for fw-context.

Public API:
- :func:`run_preflight` — fast critical checks (<50ms), runs on every CLI invocation
- :func:`run_full_check` — complete dependency audit for ``fw-context doctor``
- :func:`run_fixes` — attempt auto-repair for fixable issues
- :func:`format_results` — human-readable table output
- :func:`exit_code` — map results to 0/1/2 exit code
- :func:`_vec_available` — cached sqlite-vec probe (shared with MCP handlers)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

# ── Data model ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DepCheckResult:
    """One dependency check outcome.

    Args:
        name: Short identifier (``"pysqlite3"``, ``"sqlite-vec"``, …).
        status: ``"ok"``, ``"missing"``, ``"degraded"``, ``"error"``, or ``"skipped"``.
        message: Human-readable status line.
        fix_cmd: Shell command to auto-repair (``None`` when not fixable).
        instructions: Multi-line platform-specific manual fix guide.
        critical: When True, blocks basic operation — pre-flight exits 1.
    """

    name: str
    status: str  # ok | missing | degraded | error | skipped
    message: str
    fix_cmd: str | None = None
    instructions: str = ""
    critical: bool = False


# ── Pre-flight ──────────────────────────────────────────────────────────


def run_preflight() -> list[DepCheckResult]:
    """Fast critical checks. Must complete within 50 ms.

    Only checks that can fail on a correctly-installed package:
    pysqlite3 import + sqlite3 extension support.  Heavy imports
    (httpx, clang, config) are deferred to :func:`run_full_check`.
    """
    from ._checks import check_pysqlite3, check_sqlite3_extensions

    return [check_pysqlite3(), check_sqlite3_extensions()]


# ── Full check ──────────────────────────────────────────────────────────


def run_full_check(project_root: str | Path | None = None) -> list[DepCheckResult]:
    """Complete dependency audit.

    Checks in dependency order; dependent checks return ``"skipped"``
    when a prerequisite failed.  Loads config once for Ollama/model checks.
    """
    from ._checks import CHECK_ORDER

    results: list[DepCheckResult] = []
    ctx: dict = {}  # mutable state shared across checks (e.g. cfg)

    for name, fn in CHECK_ORDER:
        try:
            if name in ("ollama", "chat-model", "embed-model"):
                if "cfg" not in ctx:
                    from ..config import load as load_config
                    from ..utils import resolve_project_root

                    try:
                        root = resolve_project_root(str(project_root) if project_root else None)
                    except OSError:
                        root = None
                    ctx["cfg"] = load_config(root)
                r = fn(ctx["cfg"].llm)
            elif name in ("db-integrity", "disk-space"):
                r = fn(project_root)
            else:
                r = fn()


            results.append(r)
        except Exception as exc:
            results.append(
                DepCheckResult(
                    name=name,
                    status="error",
                    message=f"check crashed: {exc}",
                    critical=True,  # crashed checks are treated as critical
                )
            )

    return results


# ── Fixes ───────────────────────────────────────────────────────────────


def run_fixes(results: list[DepCheckResult], project_root: str | Path | None = None) -> list[DepCheckResult]:
    """Attempt auto-repair for every non-ok result that has a fix.

    Returns the updated list — successfully fixed items are replaced
    with a fresh ``"ok"`` result from re-running the corresponding check.
    """
    from ._fixes import FIXABLE, run_fix

    updated: list[DepCheckResult] = []
    for r in results:
        if r.status in ("ok", "skipped", "error") or r.name not in FIXABLE:
            updated.append(r)
            continue
        ok, msg = run_fix(r.name, r, project_root)
        if ok:
            try:
                updated.append(_recheck(r.name, project_root))
                if r.name == "sqlite-vec":
                    _reset_vec_cache()
            except Exception as exc:
                updated.append(
                    DepCheckResult(
                        name=r.name,
                        status="error",
                        message=f"recheck failed: {exc}",
                        critical=r.critical,
                    )
                )
        else:
            updated.append(
                DepCheckResult(
                    name=r.name,
                    status=r.status,
                    message=f"fix failed: {msg}",
                    fix_cmd=r.fix_cmd,
                    instructions=r.instructions,
                    critical=r.critical,
                )
            )
    return updated


def _recheck(name: str, project_root: str | Path | None = None) -> DepCheckResult:
    """Re-run a single check by name after a successful fix."""
    from ._checks import (
        check_db_integrity,
        check_disk_space,
        check_libclang_python,
        check_libclang_so,
        check_ollama_chat_model,
        check_ollama_embed_model,
        check_ollama_running,
        check_pysqlite3,
        check_sqlite3_extensions,
        check_sqlite_vec,
        check_watchfiles,
    )

    cfg = None
    if name in ("ollama", "chat-model", "embed-model"):
        from ..config import load as load_config
        from ..utils import resolve_project_root

        try:
            root = resolve_project_root(str(project_root) if project_root else None)
        except OSError:
            root = None
        cfg = load_config(root)

    _map: dict = {
        "pysqlite3": check_pysqlite3,
        "sqlite-ext": check_sqlite3_extensions,
        "sqlite-vec": check_sqlite_vec,
        "libclang-python": check_libclang_python,
        "libclang-so": check_libclang_so,
        "watchfiles": check_watchfiles,
        "ollama": lambda: check_ollama_running(cfg.llm) if cfg else DepCheckResult("ollama", "skipped", "no config"),
        "chat-model": lambda: check_ollama_chat_model(cfg.llm) if cfg else DepCheckResult("chat-model", "skipped", "no config"),
        "embed-model": lambda: check_ollama_embed_model(cfg.llm) if cfg else DepCheckResult("embed-model", "skipped", "no config"),
        "db-integrity": lambda: check_db_integrity(project_root),
        "disk-space": lambda: check_disk_space(project_root),
    }
    fn = _map.get(name)
    if fn is None:
        return DepCheckResult(name, "error", f"unknown check: {name}")
    return fn()


# ── Formatting ──────────────────────────────────────────────────────────


def format_results(results: list[DepCheckResult]) -> str:
    """Format check results as a readable table for terminal output."""
    lines: list[str] = []
    max_name = max((len(r.name) for r in results), default=0)
    for r in results:
        status_label = {"ok": "OK", "missing": "MISSING", "degraded": "DEGRADED",
                        "error": "ERROR", "skipped": "SKIPPED"}.get(r.status, r.status.upper())
        lines.append(f"  {r.name:<{max_name}}  {status_label:<8}  {r.message}")
        if r.fix_cmd:
            lines.append(f"    auto-fix: {r.fix_cmd}")
        if r.instructions:
            for il in r.instructions.splitlines():
                lines.append(f"    {il}")
    if lines:
        lines.append("")
    ok = sum(1 for r in results if r.status == "ok")
    skipped = sum(1 for r in results if r.status == "skipped")
    critical = sum(1 for r in results if r.status in ("missing", "error") and r.critical)
    warnings = len(results) - ok - skipped - critical
    if critical:
        lines.append(f"Result: {warnings} warning(s), {critical} critical failure(s)")
    elif warnings:
        lines.append(f"Result: {warnings} warning(s), 0 critical failures")
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)


def exit_code(results: list[DepCheckResult]) -> int:
    """Map check results to exit code: 0 = all ok, 1 = warnings, 2 = critical."""
    has_critical = any(r.status in ("missing", "error") and r.critical for r in results)
    if has_critical:
        return 2
    has_warnings = any(r.status not in ("ok", "skipped") for r in results)
    return 1 if has_warnings else 0


# ── Cached sqlite-vec probe (shared with MCP handlers) ──────────────────


_vec_cache: tuple[bool, str | None] | None = None
_vec_cache_lock = threading.Lock()


def _vec_available() -> tuple[bool, str | None]:
    """Return ``(ok, error)`` — cached, thread-safe sqlite-vec load test.

    First call imports ``sqlite_vec`` and probes an in-memory connection
    (~10-20 ms).  Subsequent calls return the cached result instantly.
    """
    global _vec_cache
    if _vec_cache is not None:
        return _vec_cache
    with _vec_cache_lock:
        if _vec_cache is not None:
            return _vec_cache
        import sqlite3

        try:
            import sqlite_vec

            conn = sqlite3.connect(":memory:")
            try:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                _vec_cache = (True, None)
            finally:
                conn.close()
        except ImportError:
            _vec_cache = (False, "sqlite-vec Python package not installed")
        except sqlite3.OperationalError as e:
            _vec_cache = (False, str(e))
        except Exception as e:
            _vec_cache = (False, f"sqlite-vec load failed: {e}")
    return _vec_cache


def _reset_vec_cache() -> None:
    """Invalidate the ``_vec_available()`` cache — called after sqlite-vec install."""
    global _vec_cache
    with _vec_cache_lock:
        _vec_cache = None
