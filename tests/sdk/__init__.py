"""The embedded SDKs of the test suite: their versions, their directories, and the stamp of an install.

The tests compile their fixtures with the toolchains of embedded SDKs, and
they build native projects of these SDKs.
``python -m tests.sdk.provision`` installs the SDKs into one root
directory.  The pre-test of the session (``tests/_environment.py``) and
the helper of the toolchains (``tests/_toolchains.py``) find them through
this module.

WHY a root of its own, and not ``~/.platformio`` or an SDK of the host:
each host must use the versions of ``versions.toml``, and the tests must
not change the PlatformIO packages that the projects of the user build
with.  A project that does not pin its platform builds with the newest
installed version.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: The environment variable that sets the root.  The default is
#: ``~/.fw-context-test-sdk``, and ``C:\fwctx-sdk`` on Windows
#: (``sdk_root``).
SDK_ROOT_ENV = "FW_CONTEXT_TEST_SDK"

#: Windows: the longest path that a program which is not long-path aware
#: can open (MAX_PATH 260, with the NUL).  The gcc of the toolchains is
#: such a program, thus ``LongPathsEnabled`` does not remove the limit.
WINDOWS_PATH_MAX = 259
#: The longest path under the root, without the root and its separator.
#: Measured 2026-11-12: a header of framework-arduinoespressif32-esp32c3-libs
#: (espressif32 8.0.0) under ``platformio/packages``.  A new version of a
#: package can make it longer: then measure again.
LONGEST_PATH_IN_ROOT = 235
#: The command that installs the SDKs (``make test-sdk`` runs it).
PROVISION_COMMAND = "python -m tests.sdk.provision"

REPO = Path(__file__).resolve().parents[2]
VERSIONS_FILE = Path(__file__).with_name("versions.toml")
REQUIREMENTS_FILE = Path(__file__).with_name("requirements.txt")
_STAMP_NAME = "installed.json"


class SdkError(Exception):
    """An SDK cannot be installed or used on this host."""


@dataclass(frozen=True)
class PythonVersions:
    """The CPython build of python-build-standalone for the venv of the test SDKs."""

    version: str
    release: str
    releases: str


@dataclass(frozen=True)
class ZephyrVersions:
    """The Zephyr workspace and the parts of the Zephyr SDK that the tests use."""

    manifest: str
    revision: str
    modules: tuple[str, ...]
    gnu_toolchains: tuple[str, ...]
    python_requirements: tuple[str, ...]
    sdk_releases: str


@dataclass(frozen=True)
class ToolchainSpec:
    """One toolchain of the indexer tests, as ``versions.toml`` gives it."""

    id: str
    sdk: str
    package: str
    prefix: str
    flags: tuple[str, ...]


@dataclass(frozen=True)
class EspIdfVersions:
    """The ESP-IDF of the extended set: a release archive with its submodules, and the chips of its tools."""

    version: str
    archive: str
    sha256: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class ArduinoVersions:
    """The Arduino CLI of the extended set, and the cores that it installs (``<core>@<version>``)."""

    cli_version: str
    cli_releases: str
    platforms: tuple[str, ...]


@dataclass(frozen=True)
class ExtraVersions:
    """The extended set of ``versions.toml``: the SDKs of the marker ``extra_platforms``."""

    platformio_projects: tuple[str, ...]
    esp_idf: EspIdfVersions
    arduino: ArduinoVersions


@dataclass(frozen=True)
class Versions:
    """The content of ``versions.toml``."""

    python: PythonVersions
    zephyr: ZephyrVersions
    platformio_projects: tuple[str, ...]
    toolchains: tuple[ToolchainSpec, ...]
    extra: ExtraVersions


def load_versions(path: Path = VERSIONS_FILE) -> Versions:
    """Read ``versions.toml``."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    zephyr = data["zephyr"]
    python = data["python"]
    extra = data["extra"]
    return Versions(
        python=PythonVersions(version=python["version"], release=python["release"], releases=python["releases"]),
        zephyr=ZephyrVersions(
            manifest=zephyr["manifest"],
            revision=zephyr["revision"],
            modules=tuple(zephyr["modules"]),
            gnu_toolchains=tuple(zephyr["gnu_toolchains"]),
            python_requirements=tuple(zephyr["python_requirements"]),
            sdk_releases=zephyr["sdk_releases"],
        ),
        platformio_projects=tuple(data["platformio"]["projects"]),
        toolchains=tuple(
            ToolchainSpec(
                id=entry["id"], sdk=entry["sdk"], package=entry["package"], prefix=entry["prefix"],
                flags=tuple(entry["flags"]),
            )
            for entry in data["toolchain"]
        ),
        extra=ExtraVersions(
            platformio_projects=tuple(extra["platformio"]["projects"]),
            esp_idf=EspIdfVersions(
                version=extra["esp_idf"]["version"], archive=extra["esp_idf"]["archive"],
                sha256=extra["esp_idf"]["sha256"], targets=tuple(extra["esp_idf"]["targets"]),
            ),
            arduino=ArduinoVersions(
                cli_version=extra["arduino"]["cli_version"], cli_releases=extra["arduino"]["cli_releases"],
                platforms=tuple(extra["arduino"]["platforms"]),
            ),
        ),
    )


@functools.cache
def sdk_root() -> Path:
    """Return the root of the test SDKs.

    WHY cached: the first call comes from the pre-test of the session.  A
    test that changes ``HOME`` later must not move the root of the
    toolchains that the other tests compile with.

    WHY ``C:\\fwctx-sdk`` on Windows: the root in the user profile made
    paths of 269 characters, and the install failed.  The SDKs keep their
    paths short in the same way: NCS installs into ``C:\\ncs`` and asks
    for a short build directory, and PlatformIO users move the core
    directory to a short path.
    """
    value = os.environ.get(SDK_ROOT_ENV)
    if value:
        # Absolute: the commands of the install and the compilers of the
        # tests run in other working directories.
        return Path(value).expanduser().absolute()
    if sys.platform == "win32":
        return Path(os.environ.get("SystemDrive", "C:") + "\\") / "fwctx-sdk"
    return Path.home() / ".fw-context-test-sdk"


def windows_root_too_long(root: Path, system: str = sys.platform) -> str | None:
    """Return why *root* is too long on Windows, or None when the longest path under it fits."""
    if system != "win32":
        return None
    longest = len(str(root)) + 1 + LONGEST_PATH_IN_ROOT
    if longest <= WINDOWS_PATH_MAX:
        return None
    return (
        f"the root {root} is too long for Windows: its longest path has {longest} characters, "
        f"and the limit is {WINDOWS_PATH_MAX}; use a root of at most "
        f"{WINDOWS_PATH_MAX - 1 - LONGEST_PATH_IN_ROOT} characters ({SDK_ROOT_ENV})"
    )


def venv_bin(root: Path) -> Path:
    """Return the directory of the programs of the venv of the root (west, pio, cmake, ninja)."""
    return root / "venv" / ("Scripts" if sys.platform == "win32" else "bin")


def venv_python(root: Path) -> Path:
    """Return the Python of the venv of the root."""
    from fw_context_mcp.utils import venv_program

    return venv_program(root / "venv", "python")


def venv_home(root: Path) -> Path | None:
    """Return the directory of the base Python of the venv of the root (``home`` of ``pyvenv.cfg``), or None."""
    try:
        text = (root / "venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "home":
            return Path(value.strip())
    return None


def venv_is_of_the_pinned_python(root: Path) -> bool:
    """Say if the venv of the root was made from the pinned CPython of the root (``python_program``).

    WHY: a venv of a host Python of the same version depends on that host
    Python, and a later upgrade or removal of it breaks the venv.
    """
    home = venv_home(root)
    if home is None:
        return False
    try:
        return home.resolve() == python_program(root).parent.resolve()
    except OSError:
        return False


def python_dir(root: Path) -> Path:
    """Return the directory of the pinned CPython (``versions.toml``, ``[python]``) of the root."""
    return root / "python"


def python_program(root: Path) -> Path:
    """Return the program of the pinned CPython of the root, as python-build-standalone installs it."""
    return python_dir(root) / ("python.exe" if sys.platform == "win32" else "bin/python3")


def python_host() -> str:
    """Return the target triple that the archives of python-build-standalone use for this host.

    Linux: the builds for glibc.  A Linux with another C library (musl)
    gets an error: the glibc build does not run there, and the toolchains
    of the Zephyr SDK need glibc too.
    """
    hosts = {
        ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
        ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
        ("darwin", "x86_64"): "x86_64-apple-darwin",
        ("darwin", "aarch64"): "aarch64-apple-darwin",
        ("win32", "x86_64"): "x86_64-pc-windows-msvc",
        ("win32", "aarch64"): "aarch64-pc-windows-msvc",
    }
    machines = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}
    host = hosts.get((sys.platform, machines.get(platform.machine().lower(), "")))
    if host is None:
        raise SdkError(f"python-build-standalone has no build for this host ({sys.platform}, {platform.machine()})")
    if sys.platform == "linux" and platform.libc_ver()[0] != "glibc":
        raise SdkError(f"the test SDKs need a Linux with glibc; this host has {platform.libc_ver()[0] or 'another C library'}")
    return host


def python_build_id(versions: Versions) -> str:
    """Return the name of the pinned CPython build (``3.12.16+20261110``): the version and the release."""
    return f"{versions.python.version}+{versions.python.release}"


#: The file in the directory of the pinned CPython that names its build
#: (``python_build_id``).  WHY: two releases of python-build-standalone can
#: build the same version (a fix of a library), and the version alone did
#: not tell them apart.
PYTHON_BUILD_FILE = "fw-context-build.txt"


def python_build_installed(root: Path) -> str | None:
    """Return the build id that the install of the pinned CPython of the root names, or None."""
    try:
        return (python_dir(root) / PYTHON_BUILD_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def python_archive_name(versions: Versions, host: str) -> str:
    """Return the name of the archive of the pinned CPython for *host* (``python_host``)."""
    python = versions.python
    return f"cpython-{python.version}+{python.release}-{host}-install_only.tar.gz"


def python_version_of(program: Path) -> str | None:
    """Return the version (``3.12.16``) of the Python *program*, or None when it does not run."""
    try:
        result = subprocess.run(
            [str(program), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            capture_output=True, text=True, check=False, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def zephyr_python_minimum(root: Path, versions: Versions) -> tuple[int, ...]:
    """Return ``PYTHON_MINIMUM_REQUIRED`` of the Zephyr tree (``cmake/modules/python.cmake``): the oldest Python of its build."""
    path = zephyr_workspace(root, versions) / "zephyr" / "cmake" / "modules" / "python.cmake"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SdkError(f"the Zephyr tree has no {path}: {exc}") from exc
    found = re.search(r"set\(PYTHON_MINIMUM_REQUIRED\s+(\d+(?:\.\d+)*)\)", text)
    if found is None:
        raise SdkError(f"{path} sets no PYTHON_MINIMUM_REQUIRED")
    return version_tuple(found.group(1))


def version_tuple(version: str) -> tuple[int, ...]:
    """Return ``"3.12.16"`` as ``(3, 12, 16)``."""
    return tuple(int(part) for part in version.split("."))


def zephyr_workspace(root: Path, versions: Versions) -> Path:
    """Return the west workspace of the Zephyr revision of *versions*.

    WHY one directory for each revision: a new revision gets a new
    workspace, and a partial update of an old one cannot mix two trees.
    """
    return root / f"zephyr-{versions.zephyr.revision}"


def zephyr_sdk_version(root: Path, versions: Versions) -> str:
    """Return the version of the Zephyr SDK that the Zephyr tree asks for (its file SDK_VERSION)."""
    path = zephyr_workspace(root, versions) / "zephyr" / "SDK_VERSION"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SdkError(f"the Zephyr tree has no SDK_VERSION ({path}): {exc}") from exc


def zephyr_sdk_dir(root: Path, version: str) -> Path:
    """Return the directory of the Zephyr SDK *version*."""
    return root / f"zephyr-sdk-{version}"


def platformio_core(root: Path) -> Path:
    """Return the PLATFORMIO_CORE_DIR of the tests."""
    return root / "platformio"


def zephyr_sdk_host() -> str:
    """Return the host name that the archives of the Zephyr SDK use (``linux-x86_64``, ``windows-x86_64``)."""
    systems = {"linux": "linux", "darwin": "macos", "win32": "windows"}
    machines = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}
    system = systems.get(sys.platform)
    machine = machines.get(platform.machine().lower())
    if system is None or machine is None:
        raise SdkError(f"the Zephyr SDK has no build for this host ({sys.platform}, {platform.machine()})")
    return f"{system}-{machine}"


def tool_env(
    root: Path, versions: Versions, zephyr_sdk: Path | None, base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the environment for west, pio and the builds of the test projects.

    The venv of the root comes first on PATH, thus ``west``, ``pio``,
    ``cmake`` and ``ninja`` are the pinned ones.  ZEPHYR_BASE and
    ZEPHYR_SDK_INSTALL_DIR replace the values of the shell: an NCS shell
    sets them for its own tree.  PlatformIO gets the core directory of the
    root, never the one of the user.

    *zephyr_sdk* is the directory of the Zephyr SDK.  None is only for the
    commands that run before the version of the SDK is known (``west
    init``, ``west update``): then no value of the shell stays either.
    """
    env = dict(os.environ if base is None else base)
    env["PATH"] = os.pathsep.join(filter(None, (str(venv_bin(root)), env.get("PATH", ""))))
    env["VIRTUAL_ENV"] = str(root / "venv")
    env["ZEPHYR_BASE"] = str(zephyr_workspace(root, versions) / "zephyr")
    env["ZEPHYR_TOOLCHAIN_VARIANT"] = "zephyr"
    if zephyr_sdk is None:
        env.pop("ZEPHYR_SDK_INSTALL_DIR", None)
    else:
        env["ZEPHYR_SDK_INSTALL_DIR"] = str(zephyr_sdk)
    env["PLATFORMIO_CORE_DIR"] = str(platformio_core(root))
    env["PLATFORMIO_SETTING_ENABLE_TELEMETRY"] = "No"
    return env


def fingerprint(versions_file: Path = VERSIONS_FILE, repo: Path = REPO) -> str:
    """Return the SHA-256 of each file that pins a version of the test SDKs.

    The files: ``versions.toml``, ``requirements.txt`` and the
    platformio.ini of each project that ``versions.toml`` names.  Line
    ends are made LF first: git can check out a file with CRLF on
    Windows, and the content is the same.
    """
    versions = load_versions(versions_file)
    files = [versions_file, versions_file.with_name(REQUIREMENTS_FILE.name)]
    files += [repo / project / "platformio.ini" for project in versions.platformio_projects]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def extra_fingerprint(versions_file: Path = VERSIONS_FILE, repo: Path = REPO) -> str:
    """Return the fingerprint of the extended set: that of :func:`fingerprint`, and the platformio.ini of each extra project."""
    versions = load_versions(versions_file)
    digest = hashlib.sha256(fingerprint(versions_file, repo).encode())
    for project in versions.extra.platformio_projects:
        path = repo / project / "platformio.ini"
        digest.update(path.name.encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def esp_idf_dir(root: Path, versions: Versions) -> Path:
    """Return the ESP-IDF tree of the extended set (``IDF_PATH``)."""
    return root / f"esp-idf-{versions.extra.esp_idf.version}"


def idf_tools_path(root: Path) -> Path:
    """Return the ``IDF_TOOLS_PATH`` of the tests: the tools and the Python env of ESP-IDF, never ``~/.espressif``."""
    return root / "espressif"


def esp_idf_export_script(root: Path, versions: Versions) -> Path:
    """Return the export script of ESP-IDF for the shell of the host."""
    return esp_idf_dir(root, versions) / ("export.bat" if sys.platform == "win32" else "export.sh")


def arduino_cli_dir(root: Path) -> Path:
    """Return the directory of the ``arduino-cli`` program of the tests."""
    return root / "arduino-cli"


def arduino_config_file(root: Path) -> Path:
    """Return the config file of Arduino CLI in the root.  The provision writes it empty."""
    return root / "arduino" / "arduino-cli.yaml"


def arduino_env(root: Path) -> dict[str, str]:
    """Return the config file and the directories of Arduino CLI in the root, never those of the user.

    WHY the config file: without it, arduino-cli reads ``arduino-cli.yaml``
    of the data directory of the user, and its board manager URLs and
    settings would change what the tests install and build.
    """
    return {
        "ARDUINO_CONFIG_FILE": str(arduino_config_file(root)),
        "ARDUINO_DIRECTORIES_DATA": str(root / "arduino" / "data"),
        "ARDUINO_DIRECTORIES_DOWNLOADS": str(root / "arduino" / "staging"),
        "ARDUINO_DIRECTORIES_USER": str(root / "arduino" / "user"),
    }


def read_stamp(root: Path) -> dict[str, str] | None:
    """Return the stamp that the last complete install wrote, or None when there is none."""
    try:
        data = json.loads((root / _STAMP_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_stamp(root: Path, stamp: dict[str, str]) -> None:
    """Write the stamp of a complete install."""
    (root / _STAMP_NAME).write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")


def toolchain_bin(root: Path, versions: Versions, spec: ToolchainSpec) -> Path:
    """Return the directory of the programs of the toolchain *spec*."""
    if spec.sdk == "zephyr":
        return zephyr_sdk_dir(root, zephyr_sdk_version(root, versions)) / "gnu" / spec.package / "bin"
    if spec.sdk == "platformio":
        return platformio_core(root) / "packages" / spec.package / "bin"
    raise SdkError(f"toolchain {spec.id}: unknown SDK {spec.sdk!r} in {VERSIONS_FILE.name}")
