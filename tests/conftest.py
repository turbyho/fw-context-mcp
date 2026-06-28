"""Shared fixtures for fw-context-mcp tests.

Provides:
- Standard fixtures: tmpdir, temp_db, populated_db, isolation, etc.
- Auto-skip for missing dependencies (Ollama, gcc/libclang)
- Test result logging to tests/results/
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

# ── Result logging ─────────────────────────────────────────────────────────

_RESULTS_DIR = Path(__file__).resolve().parent / "results"

log = logging.getLogger(__name__)


def _ensure_results_dir() -> Path:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return _RESULTS_DIR


def _check_ollama() -> bool:
    """Check if Ollama is running and reachable."""
    try:
        import httpx
        resp = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


def _check_gcc() -> bool:
    """Check if gcc is available on PATH."""
    return shutil.which("gcc") is not None


# Cache checks so we don't spawn subprocesses / HTTP calls for every test
_OLLAMA_AVAILABLE: bool | None = None
_GCC_AVAILABLE: bool | None = None


def ollama_available() -> bool:
    global _OLLAMA_AVAILABLE
    if _OLLAMA_AVAILABLE is None:
        _OLLAMA_AVAILABLE = _check_ollama()
    return _OLLAMA_AVAILABLE


def gcc_available() -> bool:
    global _GCC_AVAILABLE
    if _GCC_AVAILABLE is None:
        _GCC_AVAILABLE = _check_gcc()
    return _GCC_AVAILABLE


# ── Auto-skip hooks ────────────────────────────────────────────────────────

_SESSION_START: float | None = None


def pytest_configure(config):
    """Register custom markers and ensure results directory exists."""
    _ensure_results_dir()


def pytest_sessionstart(session):
    """Record session start time for duration tracking."""
    global _SESSION_START
    _SESSION_START = time.monotonic()


def pytest_collection_modifyitems(config, items):
    """Auto-skip tests that require unavailable dependencies.

    - Tests marked ``ollama`` are skipped when Ollama is not running.
    - Tests marked ``libclang`` are skipped when gcc is not on PATH.
    """
    skip_ollama = pytest.mark.skip(reason="Ollama not available — install and start it for these tests")
    skip_libclang = pytest.mark.skip(reason="gcc not available — install gcc for indexing tests")

    for item in items:
        if item.get_closest_marker("ollama"):
            if not ollama_available():
                item.add_marker(skip_ollama)

        if item.get_closest_marker("libclang"):
            if not gcc_available():
                item.add_marker(skip_libclang)


def pytest_sessionfinish(session, exitstatus):
    """Write test run summary to tests/results/<timestamp>.json."""
    global _SESSION_START

    results_dir = _ensure_results_dir()
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    result_file = results_dir / f"test_run_{timestamp}.json"

    failed = getattr(session, "testsfailed", 0)
    collected = session.testscollected
    duration = round(time.monotonic() - _SESSION_START, 3) if _SESSION_START else None

    summary = {
        "timestamp": datetime.now(UTC).isoformat(),
        "exit_code": exitstatus,
        "tests_passed": collected - failed,
        "tests_failed": failed,
        "tests_collected": collected,
        "duration_s": duration,
        "ollama_available": ollama_available(),
        "gcc_available": gcc_available(),
    }

    result_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Also write latest.json for convenience
    latest = results_dir / "latest.json"
    latest.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ── Standard fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def fake_arm_gcc_toolchain(tmpdir):
    """Create a minimal fake ARM GCC toolchain directory structure."""
    toolchain = tmpdir / "gcc-arm-none-eabi-9-2020-q2-update"
    bin_dir = toolchain / "bin"
    bin_dir.mkdir(parents=True)

    compiler = bin_dir / "arm-none-eabi-g++"
    compiler.touch()

    gcc_include = toolchain / "lib" / "gcc" / "arm-none-eabi" / "12.3.1" / "include"
    gcc_include.mkdir(parents=True)
    gcc_include_fixed = toolchain / "lib" / "gcc" / "arm-none-eabi" / "12.3.1" / "include-fixed"
    gcc_include_fixed.mkdir(parents=True)

    libc_include = toolchain / "arm-none-eabi" / "include"
    libc_include.mkdir(parents=True)

    cxx_include = toolchain / "arm-none-eabi" / "include" / "c++" / "12.3.1"
    cxx_include.mkdir(parents=True)

    return toolchain, compiler


@pytest.fixture
def compile_commands_json(tmpdir):
    """Create a minimal compile_commands.json for testing."""
    data = [
        {
            "directory": str(tmpdir),
            "file": "src/main.cpp",
            "arguments": [
                "arm-none-eabi-g++",
                "-std=c++14",
                "-mcpu=cortex-m4",
                "-Os",
                "-DFOO=1",
                "-Isrc",
                "-o", "build/main.o",
                "-MD",
                "-MF", "build/main.d",
                "src/main.cpp",
            ],
        },
        {
            "directory": str(tmpdir),
            "file": "lib/helper.c",
            "arguments": [
                "arm-none-eabi-gcc",
                "-std=c11",
                "-O2",
                "-Ilib",
                "-o", "build/helper.o",
                "-MP",
                "lib/helper.c",
            ],
        },
    ]
    path = tmpdir / "compile_commands.json"
    path.write_text(__import__("json").dumps(data))
    return path


@pytest.fixture
def temp_db(tmpdir):
    """Create an empty SQLite database for testing."""
    from fw_context_mcp.indexer.db import open_db

    db_path = tmpdir / "test.db"
    conn = open_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def populated_db(temp_db):
    """Database with a project and build config pre-inserted."""
    from fw_context_mcp.indexer.db import transaction, upsert_build_config, upsert_project

    with transaction(temp_db):
        upsert_project(temp_db, "proj-001", "test-project", "/tmp/test-project")
        upsert_build_config(temp_db, "hash-deadbeef", "proj-001", "/tmp/compile_commands.json")

    return temp_db


@pytest.fixture
def isolation():
    """Isolate config file writes by redirecting to a temp directory."""
    import fw_context_mcp.config.settings as settings

    old_global = settings._GLOBAL_CONFIG_PATH
    old_project_dir = settings._PROJECT_CONFIG_DIR

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        settings._GLOBAL_CONFIG_PATH = tmp / "global.toml"
        settings._PROJECT_CONFIG_DIR = ".fw-context-test"
        yield tmp
        settings._GLOBAL_CONFIG_PATH = old_global
        settings._PROJECT_CONFIG_DIR = old_project_dir
