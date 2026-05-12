"""Shared fixtures for fw-context-mcp tests."""

import tempfile
from pathlib import Path

import pytest


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

    # lib/gcc/arm-none-eabi/<ver>/include
    gcc_include = toolchain / "lib" / "gcc" / "arm-none-eabi" / "12.3.1" / "include"
    gcc_include.mkdir(parents=True)
    gcc_include_fixed = toolchain / "lib" / "gcc" / "arm-none-eabi" / "12.3.1" / "include-fixed"
    gcc_include_fixed.mkdir(parents=True)

    # arm-none-eabi/include
    libc_include = toolchain / "arm-none-eabi" / "include"
    libc_include.mkdir(parents=True)

    # C++ headers
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
