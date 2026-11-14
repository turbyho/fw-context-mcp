"""Helpers of the tests that copy a project of ``tests/builds``, run ``fw-context init`` and ``index --build``, and read the index.

The tests of the native projects of the test SDKs (``tests/test_sdk_projects.py``)
and of the builders over a toolchain of the SDKs (``tests/test_builder_projects.py``)
use them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.config import derive_project_id
from fw_context_mcp.config import load as load_config
from fw_context_mcp.indexer import manifest
from fw_context_mcp.indexer.db import open_db
from fw_context_mcp.path_key import canonical_path
from fw_context_mcp.utils import program_file_names
from tests import sdk


@dataclass(frozen=True)
class InstalledSdks:
    """The root of the test SDKs, their pinned versions and the directory of the Zephyr SDK."""

    root: Path
    versions: sdk.Versions
    zephyr_sdk: Path


@dataclass(frozen=True)
class IndexedProject:
    """A project after init and index: its root, its index database and the config hash of each image."""

    root: Path
    db_path: Path
    builds: dict[str, str]


def copy_project(fixture: str, destination: Path) -> Path:
    """Copy the files that git tracks in ``tests/builds/<fixture>`` to *destination*, as a repository of its own; return it.

    WHY the tracked files: an earlier run in the tree of the repository can
    have left a build or a ``.fw-context`` there.  A new file of a fixture
    thus needs ``git add`` before the test sees it.  WHY ``git init``: the
    root of a project is the nearest directory with ``.git``, and a git
    command of the build (``git describe`` of Zephyr) or of the index must
    not read a repository around the temporary directory.
    """
    source = Path("tests") / "builds" / fixture
    listed = subprocess.run(
        ["git", "ls-files", "-z", str(source)], cwd=sdk.REPO, capture_output=True, check=True,
    ).stdout.decode("utf-8").split("\0")
    for name in filter(None, listed):
        target = destination / Path(name).relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sdk.REPO / name, target)
    subprocess.run(["git", "init", "-q", str(destination)], check=True, capture_output=True)
    return destination


#: The tools of an SDK that a shell of the developer can have on PATH.
#: WHY not arduino-cli: it is a package of the system (``/usr/bin`` on Arch,
#: measured), and without that directory the index run had no clang.  The
#: Arduino test puts the arduino-cli of the test SDKs first on its PATH.
_SDK_TOOLS = ("west", "pio", "idf.py")
#: The variables of the SDKs that a shell of the developer can set.
_SDK_VARIABLES = ("ZEPHYR_", "PLATFORMIO_", "IDF_", "ESP_IDF_", "ARDUINO_")


def _is_tool_directory(entry: str, root: Path) -> bool:
    """Say if the PATH entry *entry* is in the root of the test SDKs, or holds a tool of ``_SDK_TOOLS``."""
    if not entry:
        return False
    directory = Path(entry)
    try:
        if canonical_path(directory).is_relative_to(canonical_path(root)):
            return True
    except (OSError, ValueError):
        return False
    return any((directory / name).is_file() for tool in _SDK_TOOLS for name in program_file_names(tool))


def daemon_env(root: Path) -> dict[str, str]:
    """The environment of a build in the background: no variable of an SDK, and no tool of an SDK on PATH.

    WHY each directory with a tool of ``_SDK_TOOLS``: a tool of the shell of
    the developer (a global ``west``) answered for a config that lacked
    ``[build] extra_path``, and the test passed for a broken config.  The
    same holds for an exported ESP-IDF (``IDF_PYTHON_ENV_PATH``, ``idf.py``).
    """
    env = {
        name: value for name, value in os.environ.items()
        if not name.startswith(_SDK_VARIABLES) and name != "VIRTUAL_ENV"
    }
    env["PATH"] = os.pathsep.join(
        entry for entry in env.get("PATH", "").split(os.pathsep) if not _is_tool_directory(entry, root)
    )
    return env


def fw_context(args: list[str], project: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the CLI of fw-context for *project*; fail the test with its output when it fails."""
    result = subprocess.run(
        [sys.executable, "-m", "fw_context_mcp.cli", *args, "--project", str(project)],
        cwd=project, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        pytest.fail(f"fw-context {' '.join(args)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result


def init_project(project: Path, env: dict[str, str]) -> None:
    fw_context(["init", "--quick", "--non-interactive", "--skip-doctor", "--skip-build"], project, env)


def index_project(project: Path, env: dict[str, str]) -> IndexedProject:
    """Build and index *project*; return the database and the config hash of each image of the build."""
    fw_context(["index", "--build", "--no-analyze", "--no-embeddings"], project, env)
    cfg = load_config(project_root=project)
    project_id = derive_project_id(project)
    db_path = cfg.index.db_dir / project_id / "index.db"
    conn = open_db(db_path)
    try:
        rows = conn.execute(
            "SELECT variant, image, config_hash FROM build_configs WHERE project_id = ?", (project_id,),
        ).fetchall()
    finally:
        conn.close()
    assert all(row["variant"] == "" for row in rows), [tuple(row) for row in rows]
    return IndexedProject(project, db_path, {row["image"]: row["config_hash"] for row in rows})


def definitions(project: IndexedProject, image: str, names: set[str]) -> set[str]:
    """The names of *names* that have a definition in the build of *image*."""
    conn = open_db(project.db_path)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT name FROM symbols WHERE config_hash = ? AND is_definition = 1 "
            f"AND name IN ({', '.join('?' * len(names))})",
            (project.builds[image], *names),
        ).fetchall()
    finally:
        conn.close()
    return {row["name"] for row in rows}


def definition_kinds(project: IndexedProject, image: str, names: set[str]) -> dict[str, set[str]]:
    """The kinds (``function``, ``struct``, ``enum`` ...) of the definitions of each name of *names* in the build of *image*.

    A set: one name can have two definitions, ``typedef enum {...} Mode``
    gives the enum and the typedef.
    """
    conn = open_db(project.db_path)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT name, kind FROM symbols WHERE config_hash = ? AND is_definition = 1 "
            f"AND name IN ({', '.join('?' * len(names))})",
            (project.builds[image], *names),
        ).fetchall()
    finally:
        conn.close()
    kinds: dict[str, set[str]] = {}
    for row in rows:
        kinds.setdefault(row["name"], set()).add(row["kind"])
    return kinds


def targets_of_each_unit(project: IndexedProject, image: str) -> set[tuple[str, ...]]:
    """The ``--target`` flags of each unit of the build of *image*, C, C++ and assembly, from the manifest of the index."""
    loaded = manifest.load(project.db_path.parent, project.builds[image])
    assert loaded is not None, "the index run wrote no manifest"
    return {
        tuple(arg for arg in manifest.tu_arguments(loaded, entry) if arg.startswith("--target="))
        for entry in loaded["entries"]
    }


def manifest_verification(project: IndexedProject, image: str) -> str:
    conn = open_db(project.db_path)
    try:
        row = conn.execute(
            "SELECT manifest_verification FROM build_configs WHERE config_hash = ?", (project.builds[image],),
        ).fetchone()
    finally:
        conn.close()
    return str(row["manifest_verification"])


def expanded_values(project: IndexedProject, image: str, name: str, file_suffix: str) -> str:
    """The values that the macros step gave the definition of *name* in the file that ends with *file_suffix*.

    A file of the project has its path relative to the root as its key
    (``src/main.c``), a file outside it the absolute path.
    """
    conn = open_db(project.db_path)
    try:
        row = conn.execute(
            "SELECT m.expanded_values FROM macros m JOIN files f ON f.id = m.file_id "
            "WHERE m.config_hash = ? AND m.name = ? AND (f.path = ? OR f.path LIKE ?)",
            (project.builds[image], name, file_suffix, f"%/{file_suffix}"),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no definition of {name} in {file_suffix}"
    return str(row["expanded_values"])


# ── The bases of the fixtures of the modules of the native projects ──────


def installed_sdks() -> InstalledSdks:
    """The test SDKs.  The pre-test checks them for a test that takes the fixture ``test_sdks`` (``tests/_environment.py``)."""
    root = sdk.sdk_root()
    versions = sdk.load_versions()
    return InstalledSdks(root, versions, sdk.zephyr_sdk_dir(root, sdk.zephyr_sdk_version(root, versions)))


def work_directory(tmp_path_factory: pytest.TempPathFactory, sdks: InstalledSdks) -> Iterator[Path]:
    """Yield a directory for the copies of the projects.

    WHY a short directory on Windows: the build of Zephyr writes object
    files with long paths under the build directory, and GCC on Windows
    cannot open a path of more than 259 characters (``tests/sdk``,
    ``WINDOWS_PATH_MAX``).  The temporary directory of pytest is long there.
    """
    if sys.platform != "win32":
        yield tmp_path_factory.mktemp("sdk-projects")
        return
    base = sdks.root / "w"
    base.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(dir=base))
    yield directory
    shutil.rmtree(directory)
