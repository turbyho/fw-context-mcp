"""Individual dependency check functions.

Each check returns a :class:`DepCheckResult` — never raises.
Heavy imports (httpx, clang, watchfiles) are local to the function body
so that importing this module stays fast for pre-flight.

WHY: Checks must never crash the CLI.  If a check raises an unhandled
exception, the entire audit fails and the user sees a raw traceback.
Every check catches all exceptions internally and returns an error
result instead.  Imports are deferred so ``check_pysqlite3`` and
``check_sqlite3_extensions`` (the pre-flight checks) can run without
paying the cost of importing httpx, clang, or the config subsystem.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import sqlite3
from pathlib import Path
from typing import Any

from . import DepCheckResult
from ._instructions import (
    db_integrity_instructions,
    disk_space_instructions,
    get_platform_info,
    libclang_so_instructions,
    ollama_instructions,
    pysqlite3_instructions,
    sqlite_ext_instructions,
    vec0_load_error_instructions,
    watchfiles_instructions,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _import(name: str):
    """Import a module by name — indirection point for test monkeypatching.

    WHY: Direct ``import`` statements cannot be intercepted by tests.
    Wrapping in a helper function lets test suites inject mock modules
    without modifying production code.
    """
    return importlib.import_module(name)


def _version(pkg: str) -> str:
    """Return the installed version of *pkg*, or ``"unknown"``.

    Uses ``importlib.metadata`` which is the standard library replacement
    for the deprecated ``pkg_resources``.
    """
    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# ── Checks ──────────────────────────────────────────────────────────────


def check_pysqlite3() -> DepCheckResult:
    """Verify pysqlite3 is installed and importable.

    WHY: pysqlite3 is the most common failure point on macOS and Windows.
    The Python.org installer and Homebrew Python do not ship with
    ``--enable-loadable-sqlite-extensions``, which means sqlite-vec
    cannot load at all.  pysqlite3 bundles a compatible SQLite.  The
    ``"degraded"`` status catches the case where pysqlite3 is installed
    but did not replace the stdlib sqlite3 module (e.g. wrong import
    order due to PYTHONPATH).
    """
    platform_ctx = get_platform_info()
    try:
        _import("pysqlite3")
        ver = _version("pysqlite3")
        # Verify it actually replaced stdlib sqlite3
        import sqlite3
        if "pysqlite3" in str(sqlite3.__file__):
            return DepCheckResult(
                name="pysqlite3",
                status="ok",
                message=f"pysqlite3 {ver}",
                critical=True,
            )
        return DepCheckResult(
            name="pysqlite3",
            status="degraded",
            message=f"pysqlite3 {ver} loaded but sqlite3 redirect may not be active",
            critical=True,
        )
    except ImportError:
        return DepCheckResult(
            name="pysqlite3",
            status="missing",
            message="pysqlite3 not installed — required for sqlite-vec extension support",
            fix_cmd=f"{platform_ctx['pip_cmd']} pysqlite3",
            instructions=pysqlite3_instructions(platform_ctx),
            critical=True,
        )


def check_sqlite3_extensions() -> DepCheckResult:
    """Verify sqlite3 supports loadable extensions.

    WHY: ``sqlite3.enable_load_extension(True)`` raises AttributeError
    on macOS system Python and some pyenv builds.  This is distinct from
    the pysqlite3 check — a Python with extension support but without
    pysqlite3 will pass this check but fail the sqlite-vec load test.
    """
    platform_ctx = get_platform_info()
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
            return DepCheckResult(
                name="sqlite-ext",
                status="ok",
                message="enable_load_extension() supported",
                critical=True,
            )
        except AttributeError:
            return DepCheckResult(
                name="sqlite-ext",
                status="missing",
                message="sqlite3 missing enable_load_extension() — stdlib sqlite3 on macOS/pyenv",
                fix_cmd=f"{platform_ctx['pip_cmd']} pysqlite3",
                instructions=sqlite_ext_instructions(platform_ctx),
                critical=True,
            )
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return DepCheckResult(
            name="sqlite-ext",
            status="error",
            message=f"sqlite3 in-memory connection failed: {e}",
            instructions=sqlite_ext_instructions(platform_ctx),
            critical=True,
        )


def check_sqlite_vec() -> DepCheckResult:
    """Verify sqlite-vec can load into SQLite.

    WHY: sqlite-vec can be installed (pip succeeds) but fail to load at
    runtime.  Common causes: missing vec0.so shared library dependency
    (libgcc_s.so.1 on Alpine, libc++.dylib on old macOS), SQLite version
    too old, or architecture mismatch (arm64 pip on x86_64 Python).
    The ``"degraded"`` status captures load-time failures; the
    instructions include platform-specific diagnostic commands.
    """
    platform_ctx = get_platform_info()
    try:
        mod = _import("sqlite_vec")
        ver = _version("sqlite-vec")
        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
            mod.load(conn)
            return DepCheckResult(
                name="sqlite-vec",
                status="ok",
                message=f"sqlite-vec {ver}",
                critical=True,
            )
        except sqlite3.OperationalError as e:
            msg = str(e)
            return DepCheckResult(
                name="sqlite-vec",
                status="degraded",
                message=f"sqlite-vec {ver} installed but load failed: {e}",
                instructions=vec0_load_error_instructions(msg, platform_ctx),
                critical=True,
            )
        finally:
            conn.close()
    except ImportError:
        return DepCheckResult(
            name="sqlite-vec",
            status="missing",
            message="sqlite-vec not installed",
            fix_cmd=f"{platform_ctx['pip_cmd']} sqlite-vec",
            instructions=f"{platform_ctx['pip_cmd']} sqlite-vec",
            critical=True,
        )


def check_libclang_python() -> DepCheckResult:
    """Verify the clang Python bindings are installed."""
    platform_ctx = get_platform_info()
    try:
        _import("clang.cindex")
        ver = _version("libclang")
        try:
            from packaging.version import Version
            if Version(ver) < Version("18.1.1"):
                return DepCheckResult(
                    name="libclang-python",
                    status="degraded",
                    message=f"libclang {ver} is too old — 18.1.1+ required",
                    fix_cmd=f"{platform_ctx['pip_cmd']} 'libclang>=18.1.1'",
                    instructions=f"{platform_ctx['pip_cmd']} 'libclang>=18.1.1'",
                    critical=True,
                )
        except ImportError:
            pass  # packaging not installed, skip version check
        return DepCheckResult(
            name="libclang-python",
            status="ok",
            message=f"libclang {ver}",
            critical=True,
        )
    except ImportError:
        return DepCheckResult(
            name="libclang-python",
            status="missing",
            message="clang Python bindings not installed",
            fix_cmd=f"{platform_ctx['pip_cmd']} libclang",
            instructions=f"{platform_ctx['pip_cmd']} libclang",
            critical=True,
        )


def check_libclang_so() -> DepCheckResult:
    """Verify the native libclang shared library is discoverable."""
    platform_ctx = get_platform_info()
    # First, make sure the Python bindings are available
    try:
        import clang.cindex
    except ImportError:
        return DepCheckResult(
            name="libclang-so",
            status="skipped",
            message="depends on libclang-python (missing)",
            critical=True,
        )

    # Check if the library file exists.
    # Config().lib returns a CDLL object — extract the real path via ._name.
    try:
        lib_cdll = clang.cindex.Config().lib
        lib_path = getattr(lib_cdll, "_name", str(lib_cdll))
    except Exception as exc:
        return DepCheckResult(
            name="libclang-so",
            status="degraded",
            message=f"clang.cindex.Config().lib failed: {exc}",
            instructions=libclang_so_instructions(platform_ctx),
            critical=True,
        )

    if lib_path and "/" in lib_path and Path(lib_path).exists():
        # Smoke test: try to actually load the library
        try:
            clang.cindex.Index.create()
            return DepCheckResult(
                name="libclang-so",
                status="ok",
                message=lib_path,
                critical=True,
            )
        except Exception as exc:
            return DepCheckResult(
                name="libclang-so",
                status="degraded",
                message=f"library found at {lib_path} but failed to load: {exc}",
                instructions=libclang_so_instructions(platform_ctx),
                critical=True,
            )

    return DepCheckResult(
        name="libclang-so",
        status="degraded",
        message=f"libclang not found at {lib_path}",
        instructions=libclang_so_instructions(platform_ctx),
        critical=True,
    )


def check_watchfiles() -> DepCheckResult:
    """Verify watchfiles is installed."""
    platform_ctx = get_platform_info()
    try:
        _import("watchfiles")
        ver = _version("watchfiles")
        return DepCheckResult(
            name="watchfiles",
            status="ok",
            message=f"watchfiles {ver}",
            critical=False,
        )
    except ImportError:
        return DepCheckResult(
            name="watchfiles",
            status="missing",
            message="watchfiles not installed — background file watcher unavailable",
            fix_cmd=f"{platform_ctx['pip_cmd']} watchfiles",
            instructions=watchfiles_instructions(platform_ctx),
            critical=False,
        )


def check_ollama_running(cfg) -> DepCheckResult:
    """Verify Ollama is reachable."""
    if not cfg.enabled:
        return DepCheckResult(name="ollama", status="skipped", message="LLM disabled in config", critical=False)

    platform_ctx = get_platform_info()
    from ..llm._diag import check_setup

    result = check_setup(cfg)
    if result.get("status") == "error":
        return DepCheckResult(
            name="ollama",
            status="degraded",
            message=result.get("message", "Ollama not reachable"),
            instructions=ollama_instructions(platform_ctx),
            critical=False,
        )
    return DepCheckResult(
        name="ollama",
        status="ok",
        message=f"{cfg.ollama_url} (running)",
        critical=False,
    )


def check_ollama_chat_model(cfg) -> DepCheckResult:
    """Verify the configured chat model is installed."""
    if not cfg.enabled:
        return DepCheckResult(name="chat-model", status="skipped", message="LLM disabled in config", critical=False)

    from ..llm._diag import check_setup

    result = check_setup(cfg)
    if result.get("status") == "error":
        return DepCheckResult(name="chat-model", status="skipped", message="Ollama not reachable", critical=False)
    if result.get("status") == "model_missing":
        return DepCheckResult(
            name="chat-model",
            status="missing",
            message=f"model '{cfg.model}' not installed",
            fix_cmd=f"ollama pull {cfg.model}",
            instructions=f"ollama pull {cfg.model}",
            critical=False,
        )
    return DepCheckResult(
        name="chat-model",
        status="ok",
        message=cfg.model,
        critical=False,
    )


def check_ollama_embed_model(cfg) -> DepCheckResult:
    """Verify the configured embedding model is installed."""
    if not cfg.enabled:
        return DepCheckResult(name="embed-model", status="skipped", message="LLM disabled in config", critical=False)

    from ..llm._diag import check_setup

    result = check_setup(cfg)
    if result.get("status") == "error":
        return DepCheckResult(name="embed-model", status="skipped", message="Ollama not reachable", critical=False)
    if not result.get("embedding_installed"):
        return DepCheckResult(
            name="embed-model",
            status="missing",
            message=f"embedding model '{cfg.embed_model}' not installed",
            fix_cmd=f"ollama pull {cfg.embed_model}",
            instructions=f"ollama pull {cfg.embed_model}",
            critical=False,
        )
    return DepCheckResult(
        name="embed-model",
        status="ok",
        message=cfg.embed_model,
        critical=False,
    )


def check_db_integrity(project_root: str | Path | None = None) -> DepCheckResult:
    """Verify the index database is not corrupt."""
    platform_ctx = get_platform_info()
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..utils import resolve_project_root

    try:
        root = resolve_project_root(str(project_root) if project_root else None)
    except OSError:
        return DepCheckResult(
            name="db-integrity",
            status="skipped",
            message="no project root found — nothing to check",
            critical=True,
        )

    cfg = load_config(root)
    if not cfg.project.id:
        return DepCheckResult(
            name="db-integrity",
            status="skipped",
            message="project not initialized — nothing to check",
            critical=True,
        )

    project_id = derive_project_id(root)
    db_path = cfg.index.db_dir / project_id / "index.db"
    if not db_path.exists():
        return DepCheckResult(
            name="db-integrity",
            status="ok",
            message="no index yet — nothing to check",
            critical=True,
        )

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if row and row[0] == "ok":
                # Get file size for context
                size_mb = db_path.stat().st_size / (1024 * 1024)
                return DepCheckResult(
                    name="db-integrity",
                    status="ok",
                    message=f"quick_check: ok (index.db, {size_mb:.0f} MB)",
                    critical=True,
                )
            # quick_check returned an error — run full integrity_check
            detail = conn.execute("PRAGMA integrity_check").fetchone()
            detail_msg = detail[0] if detail else "unknown error"
            return DepCheckResult(
                name="db-integrity",
                status="error",
                message=f"integrity_check: {detail_msg}",
                instructions=db_integrity_instructions(platform_ctx),
                critical=True,
            )
        except sqlite3.OperationalError as e:
            return DepCheckResult(
                name="db-integrity",
                status="error",
                message=f"cannot open database: {e}",
                instructions=db_integrity_instructions(platform_ctx),
                critical=True,
            )
        finally:
            conn.close()
    except sqlite3.Error as e:
        return DepCheckResult(
            name="db-integrity",
            status="error",
            message=f"database error: {e}",
            instructions=db_integrity_instructions(platform_ctx),
            critical=True,
        )


def check_disk_space(project_root: str | Path | None = None) -> DepCheckResult:
    """Verify sufficient free disk space on the index volume."""
    platform_ctx = get_platform_info()
    import shutil

    # Resolve the actual index directory from config when available,
    # so the check reflects the disk where the index lives.
    target = Path.home() / ".fw-context"
    try:
        from ..config import load as load_config
        from ..utils import resolve_project_root

        root = resolve_project_root(str(project_root) if project_root else None)
        cfg = load_config(root)
        db_dir = cfg.index.db_dir
        # Use db_dir if it exists, otherwise try its parent
        if db_dir.exists():
            target = db_dir
        elif db_dir.parent.exists():
            target = db_dir.parent
    except (OSError, RuntimeError):
        pass
    # Only create the directory if it already exists (read-only audit)
    if not target.exists():
        target = Path.home()

    try:
        usage = shutil.disk_usage(target)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < 128:
            return DepCheckResult(
                name="disk-space",
                status="error",
                message=f"{free_mb:.0f} MB free on {target} — critically low",
                instructions=disk_space_instructions(platform_ctx),
                critical=False,
            )
        if free_mb < 512:
            return DepCheckResult(
                name="disk-space",
                status="degraded",
                message=f"{free_mb:.0f} MB free on {target} — low",
                instructions=disk_space_instructions(platform_ctx),
                critical=False,
            )
        free_gb = usage.free / (1024 * 1024 * 1024)
        return DepCheckResult(
            name="disk-space",
            status="ok",
            message=f"{free_gb:.1f} GB free on {target}",
            critical=False,
        )
    except OSError as e:
        return DepCheckResult(
            name="disk-space",
            status="error",
            message=f"cannot check disk usage: {e}",
            critical=False,
        )


# ── Ordered check list ──────────────────────────────────────────────────

CHECK_ORDER: list[tuple[str, Any]] = [
    ("pysqlite3", check_pysqlite3),
    ("sqlite-ext", check_sqlite3_extensions),
    ("sqlite-vec", check_sqlite_vec),
    ("libclang-python", check_libclang_python),
    ("libclang-so", check_libclang_so),
    ("watchfiles", check_watchfiles),
    ("ollama", check_ollama_running),
    ("chat-model", check_ollama_chat_model),
    ("embed-model", check_ollama_embed_model),
    ("db-integrity", check_db_integrity),
    ("disk-space", check_disk_space),
]
