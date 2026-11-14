"""Install the embedded SDKs of the test suite into the root of the test SDKs.

Run it from the root of the repository, with the Python of the
development venv (``make test-sdk`` does it)::

    python -m tests.sdk.provision

The script installs, in this sequence:

0. The pinned CPython of ``versions.toml`` (python-build-standalone) in
   ``<root>/python``.  The script checks the archive against the
   ``SHA256SUMS`` of the release.
1. A venv in ``<root>/venv`` of that Python, with the pinned tools of
   ``tests/sdk/requirements.txt`` (west, platformio, cmake, ninja).
2. A west workspace of the Zephyr revision of ``versions.toml``, with only
   the modules that it names, and the Python packages of the build.
3. The Zephyr SDK of the version that the Zephyr tree names, with only
   the GNU toolchains of ``versions.toml``.  The script downloads the
   archives and checks their SHA-256 against the ``sha256.sum`` of the
   release.
4. The packages of each PlatformIO project of ``versions.toml``, into the
   PlatformIO core directory of the root.

With ``--extra`` (``make test-sdk-platforms``) it installs the extended set
of ``versions.toml`` too:

5. The packages of each PlatformIO project of the extended set.
6. ESP-IDF from its release archive (with the submodules), checked
   against the SHA-256 of ``versions.toml``, and its tools and Python env
   for each target in ``<root>/espressif`` (``IDF_TOOLS_PATH``).
7. Arduino CLI, checked against the checksums of its release, and its
   cores in ``<root>/arduino``.

Each step does nothing when its part is already there, thus a second run
is fast.  The last step writes the stamp that the pre-test of the
session compares (``tests/_environment.py``).

WHY no ``west sdk install``: it always runs ``setup.sh -c``, which writes
the SDK into the CMake package registry of the user (``~/.cmake/packages``
or the Windows registry).  A Zephyr build of the user that does not set
ZEPHYR_SDK_INSTALL_DIR could then find the SDK of the tests.  The builds
of the tests get ZEPHYR_SDK_INSTALL_DIR (``tests.sdk.tool_env``).
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.parse
import zipfile
from collections.abc import Sequence
from pathlib import Path

import httpx

from fw_context_mcp import net
from tests import sdk

#: Timeout of the download of one archive: the connection, and each read.
_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0)


class ProvisionError(Exception):
    """A step of the install failed."""


def _run(argv: Sequence[str | Path], env: dict[str, str], cwd: Path | None = None) -> None:
    """Run one command; its output goes to the console, because a download can take minutes."""
    print(f"$ {' '.join(str(arg) for arg in argv)}", flush=True)
    result = subprocess.run([str(arg) for arg in argv], env=env, cwd=cwd, check=False)
    if result.returncode != 0:
        raise ProvisionError(f"the command failed with exit code {result.returncode}: {' '.join(map(str, argv))}")


def _program(root: Path, name: str) -> Path:
    """Return a program of the venv of the root (``.exe`` on Windows)."""
    found = shutil.which(name, path=str(sdk.venv_bin(root)))
    if found is None:
        raise ProvisionError(f"{name} is not in the venv of the test SDKs ({sdk.venv_bin(root)})")
    return Path(found)


def ensure_python(root: Path, versions: sdk.Versions) -> Path:
    """Install the pinned CPython of ``versions.toml`` when it is not there, and return its program.

    WHY a Python of its own and not the Python that runs this script: the
    build of Zephyr needs the version of ``PYTHON_MINIMUM_REQUIRED`` of
    its tree, and a host can have an older Python.  The venv of a host
    with Python 3.11 gave a west that the Zephyr build refused
    (``relative_to(walk_up=True)``, Python 3.12).  An install of another
    build (another version, or the same version of another release) is
    replaced: the file ``PYTHON_BUILD_FILE`` names the build.
    """
    program = sdk.python_program(root)
    wanted = versions.python.version
    build = sdk.python_build_id(versions)
    if sdk.python_build_installed(root) == build and sdk.python_version_of(program) == wanted:
        return program
    name = sdk.python_archive_name(versions, sdk.python_host())
    release = f"{versions.python.releases}/{versions.python.release}"
    downloads = root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    sums_file = downloads / f"python-{versions.python.release}.SHA256SUMS"
    _download(f"{release}/SHA256SUMS", sums_file)
    sums = parse_checksums(sums_file.read_text(encoding="utf-8"))
    if name not in sums:
        raise ProvisionError(f"the SHA256SUMS of python-build-standalone {versions.python.release} names no {name}")
    archive = downloads / name
    # "+" in the name of the asset: quoted, as in the URL of the release.
    actual = _download(f"{release}/{urllib.parse.quote(name)}", archive)
    if actual != sums[name]:
        archive.unlink()
        raise ProvisionError(f"{name}: SHA-256 {actual} is not the SHA-256 of the release ({sums[name]})")
    staging = root / ".staging-python"
    if staging.exists():
        shutil.rmtree(staging)
    extract(archive, staging)
    target = sdk.python_dir(root)
    if target.exists():
        shutil.rmtree(target)
    # The archive holds python/; the rename is the last step, thus a
    # stopped install leaves no Python that looks installed.
    (staging / "python").rename(target)
    shutil.rmtree(staging)
    archive.unlink()
    found = sdk.python_version_of(program)
    if found != wanted:
        raise ProvisionError(f"the Python of {name} is {found or 'a program that does not run'}, not {wanted}")
    (target / sdk.PYTHON_BUILD_FILE).write_text(build + "\n", encoding="utf-8")
    return program


def ensure_venv(root: Path, versions: sdk.Versions, base_python: Path, env: dict[str, str]) -> Path:
    """Make the venv of the root with the pinned Python and tools, and return its Python.

    The venv uses *base_python*, the pinned CPython of the root.  A venv of
    another Python (an older install, a host Python of the same version,
    or a base Python that is gone) is made again: it holds only packages
    that this step installs.
    """
    venv_dir = root / "venv"
    python = sdk.venv_python(root)
    if not sdk.venv_is_of_the_pinned_python(root) or sdk.python_version_of(python) != versions.python.version:
        print(f"Make the venv {venv_dir} with Python {versions.python.version}", flush=True)
        if venv_dir.exists():
            shutil.rmtree(venv_dir)
        _run([base_python, "-m", "venv", venv_dir], env)
    _run([python, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", sdk.REQUIREMENTS_FILE], env)
    return python


def check_zephyr_python(root: Path, versions: sdk.Versions) -> None:
    """Refuse a pinned Python that is older than ``PYTHON_MINIMUM_REQUIRED`` of the Zephyr tree.

    WHY here: a new Zephyr revision of ``versions.toml`` can need a newer
    Python, and the build of the tests then failed only in a test.
    """
    minimum = sdk.zephyr_python_minimum(root, versions)
    if sdk.version_tuple(versions.python.version) < minimum:
        raise ProvisionError(
            f"Zephyr {versions.zephyr.revision} needs Python {'.'.join(map(str, minimum))} or newer, "
            f"and versions.toml pins Python {versions.python.version}: update [python] of tests/sdk/versions.toml"
        )


def project_filter(modules: Sequence[str]) -> str:
    """Return the value of ``manifest.project-filter`` that keeps only *modules* of the manifest."""
    return ",".join(["-.*", *(f"+{module}" for module in modules)])


def ensure_zephyr_workspace(root: Path, versions: sdk.Versions, python: Path, env: dict[str, str]) -> Path:
    """Make or update the west workspace of the Zephyr revision, and return it.

    ``west init`` clones into a ``.partial`` directory first, and the
    script renames it when the clone is complete.  Thus a stopped clone
    never looks like a workspace.  ``--depth=1``: the history of Zephyr
    is about 1 GB, and the build needs only the tree of the revision.
    """
    zephyr = versions.zephyr
    workspace = sdk.zephyr_workspace(root, versions)
    west = _program(root, "west")
    if not (workspace / ".west").is_dir():
        # A directory without .west is not a workspace that this script
        # completed (the rename below is the last step); make it again.
        for stale in (workspace, workspace.with_name(workspace.name + ".partial")):
            if stale.exists():
                shutil.rmtree(stale)
        partial = workspace.with_name(workspace.name + ".partial")
        _run([west, "init", "-m", zephyr.manifest, "--mr", zephyr.revision, "-o=--depth=1", partial], env)
        partial.rename(workspace)
    _run([west, "config", "manifest.project-filter", "--", project_filter(zephyr.modules)], env, cwd=workspace)
    _run([west, "update", "--narrow", "-o=--depth=1"], env, cwd=workspace)
    check_zephyr_python(root, versions)
    requirements: list[str | Path] = []
    for name in zephyr.python_requirements:
        requirements += ["-r", workspace / name]
    _run([python, "-m", "pip", "install", "--disable-pip-version-check", "-q", *requirements], env)
    return workspace


def zephyr_archive_names(version: str, host: str, toolchains: Sequence[str]) -> dict[str, str]:
    """Return the archive of each part of the Zephyr SDK: ``"minimal"`` and each GNU toolchain.

    The names are the names of the release assets of the SDK since 1.0
    (``setup.sh`` of the SDK uses the same pattern).  The Windows build
    has 7z archives.
    """
    ext = "7z" if host.startswith("windows") else "tar.xz"
    names = {"minimal": f"zephyr-sdk-{version}_{host}_minimal.{ext}"}
    names.update({toolchain: f"toolchain_gnu_{host}_{toolchain}.{ext}" for toolchain in toolchains})
    return names


def parse_checksums(text: str) -> dict[str, str]:
    """Return the SHA-256 of each file of a ``sha256.sum`` (lines ``<hash>  <name>``)."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0].lower()
    return sums


def _download(url: str, target: Path) -> str:
    """Download *url* into *target* and return the SHA-256 of the content."""
    digest = hashlib.sha256()
    partial = target.with_name(target.name + ".partial")
    print(f"Download {url}", flush=True)
    with net.stream("GET", url, follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT) as response:
        if response.status_code != 200:
            raise ProvisionError(f"download of {url} failed: HTTP {response.status_code}")
        with partial.open("wb") as out:
            for chunk in response.iter_bytes():
                digest.update(chunk)
                out.write(chunk)
    partial.replace(target)
    return digest.hexdigest()


def extract(archive: Path, target: Path) -> None:
    """Unpack *archive* (``.tar.xz``, ``.tar.gz``, ``.7z`` or ``.zip``) into the directory *target*.

    ``filter="data"`` refuses members that write outside *target*, and
    special files.  py7zr refuses such paths too.  An archive that cannot
    be read, or that holds such a member, is a ProvisionError.
    """
    if archive.name.endswith(".zip"):
        _extract_zip(archive, target)
        return
    if archive.name.endswith(".7z"):
        import py7zr  # Only on Windows (pyproject.toml, extra dev).
        from py7zr.exceptions import AbsolutePathError, ArchiveError, PasswordRequired

        try:
            with py7zr.SevenZipFile(archive) as seven:
                seven.extractall(target)
        except (ArchiveError, AbsolutePathError, PasswordRequired) as exc:
            raise ProvisionError(f"{archive.name}: {exc}") from exc
        return
    if not hasattr(tarfile, "data_filter"):
        # The extraction filters came in Python 3.11.4.  Without them an
        # archive member can write outside the target.
        raise ProvisionError(f"Python {sys.version.split()[0]} has no tarfile filters; use Python 3.11.4 or newer")
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(target, filter="data")
    except tarfile.TarError as exc:
        raise ProvisionError(f"{archive.name}: {exc}") from exc


def _extract_zip(archive: Path, target: Path) -> None:
    """Unpack the ZIP *archive* into *target*, with the mode of each member that the archive gives.

    WHY the mode: ``ZipFile.extractall`` gives each file the mode of a new
    file, thus ``idf.py`` and the scripts of ESP-IDF lost their execute
    bit (measured, v6.1).  The mode is in the high 16 bits of
    ``external_attr`` of a member that a POSIX host wrote.  A symbolic link
    (16 in ESP-IDF v6.1) holds its target as its content.  On POSIX it
    becomes a link when the target is inside *target*; on Windows it stays
    a file with the text of the link, as git writes it without
    ``core.symlinks``.  A member or a link that goes outside *target* is a
    ProvisionError.
    """
    base = target.resolve()
    try:
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                destination = (target / member.filename).resolve()
                if not destination.is_relative_to(base):
                    raise ProvisionError(f"{archive.name}: the member {member.filename} is outside the target")
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) and sys.platform != "win32":
                    link = zipped.read(member).decode("utf-8")
                    if not (destination.parent / link).resolve().is_relative_to(base):
                        raise ProvisionError(f"{archive.name}: the link {member.filename} goes outside the target")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.symlink_to(link)
                    continue
                zipped.extract(member, target)
                if mode and not member.is_dir() and sys.platform != "win32":
                    destination.chmod(stat.S_IMODE(mode))
    except zipfile.BadZipFile as exc:
        raise ProvisionError(f"{archive.name}: {exc}") from exc


def ensure_zephyr_sdk(root: Path, versions: sdk.Versions) -> Path:
    """Install the parts of the Zephyr SDK that are not there yet, and return its directory."""
    version = sdk.zephyr_sdk_version(root, versions)
    sdk_dir = sdk.zephyr_sdk_dir(root, version)
    names = zephyr_archive_names(version, sdk.zephyr_sdk_host(), versions.zephyr.gnu_toolchains)
    places = {part: sdk_dir / "gnu" / part for part in versions.zephyr.gnu_toolchains}
    places["minimal"] = sdk_dir
    missing = [part for part in names if not _part_installed(part, places[part])]
    if not missing:
        return sdk_dir
    release = f"{versions.zephyr.sdk_releases}/v{version}"
    downloads = root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    sums_file = downloads / f"zephyr-sdk-{version}.sha256.sum"
    _download(f"{release}/sha256.sum", sums_file)
    sums = parse_checksums(sums_file.read_text(encoding="utf-8"))
    for part in missing:
        name = names[part]
        if name not in sums:
            raise ProvisionError(f"the sha256.sum of Zephyr SDK {version} names no {name}")
        archive = downloads / name
        actual = _download(f"{release}/{name}", archive)
        if actual != sums[name]:
            archive.unlink()
            raise ProvisionError(f"{name}: SHA-256 {actual} is not the SHA-256 of the release ({sums[name]})")
        staging = root / f".staging-{part}"
        if staging.exists():
            shutil.rmtree(staging)
        extract(archive, staging)
        # The minimal bundle holds zephyr-sdk-<version>/, a toolchain holds
        # <toolchain>/.
        unpacked = staging / (sdk_dir.name if part == "minimal" else part)
        _move_part(unpacked, places[part], marker="sdk_version" if part == "minimal" else "bin")
        shutil.rmtree(staging)
        archive.unlink()
    return sdk_dir


def _move_part(unpacked: Path, place: Path, marker: str) -> None:
    """Move an unpacked part of the SDK to *place*; its *marker* comes last.

    ``_part_installed`` looks for the marker, thus a stopped move leaves
    no part that looks installed.  WHY a move of each entry and not one
    rename: *place* can exist already.  The directory of the SDK holds
    ``gnu/`` when a toolchain was installed and the minimal bundle was
    not, and a left-over of a stopped move is replaced.
    """
    place.mkdir(parents=True, exist_ok=True)
    entries = sorted(unpacked.iterdir(), key=lambda entry: entry.name == marker)
    for entry in entries:
        target = place / entry.name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        entry.rename(target)


def _part_installed(part: str, place: Path) -> bool:
    """Return True when a part of the Zephyr SDK is unpacked at *place*."""
    return (place / "sdk_version").is_file() if part == "minimal" else (place / "bin").is_dir()


def ensure_platformio(root: Path, projects: Sequence[str], env: dict[str, str]) -> None:
    """Install the packages of each PlatformIO project of *projects* into the core directory of the root.

    Then remove the downloaded archives of PlatformIO: about 1.1 GB for
    ESP32-C3 (measured 2026-11-12), and no test reads them.
    """
    pio = _program(root, "pio")
    for project in projects:
        _run([pio, "pkg", "install", "-d", sdk.REPO / project], env)
    _run([pio, "system", "prune", "--cache", "--force"], env)


def ensure_esp_idf(root: Path, versions: sdk.Versions, base_python: Path, env: dict[str, str]) -> None:
    """Install the ESP-IDF tree of the extended set, and its tools and Python env for each target.

    ``idf_tools.py`` creates the Python env of ESP-IDF from the Python that
    runs it: the pinned Python of the root, which ESP-IDF supports.
    """
    spec = versions.extra.esp_idf
    tree = sdk.esp_idf_dir(root, versions)
    if not (tree / "tools" / "idf.py").is_file():
        downloads = root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        archive = downloads / Path(urllib.parse.urlparse(spec.archive).path).name
        actual = _download(spec.archive, archive)
        if actual != spec.sha256:
            archive.unlink()
            raise ProvisionError(f"{archive.name}: SHA-256 {actual} is not the SHA-256 of versions.toml ({spec.sha256})")
        staging = root / ".staging-esp-idf"
        if staging.exists():
            shutil.rmtree(staging)
        extract(archive, staging)
        if tree.exists():
            # A tree without idf.py is an earlier install that stopped.
            shutil.rmtree(tree)
        (staging / tree.name).replace(tree)
        shutil.rmtree(staging)
        archive.unlink()
    idf_env = {**env, "IDF_PATH": str(tree), "IDF_TOOLS_PATH": str(sdk.idf_tools_path(root))}
    tools = tree / "tools" / "idf_tools.py"
    _run([base_python, tools, "--non-interactive", "install", f"--targets={','.join(spec.targets)}"], idf_env)
    _run([base_python, tools, "--non-interactive", "install-python-env"], idf_env)


def arduino_cli_archive(version: str, system: str = sys.platform, machine: str | None = None) -> str:
    """Return the release archive of Arduino CLI *version* for the host."""
    import platform

    machine = (machine or platform.machine()).lower()
    if machine not in ("x86_64", "amd64", "arm64", "aarch64"):
        raise ProvisionError(f"the extended set has no Arduino CLI for this host ({system}, {machine})")
    arm = machine in ("arm64", "aarch64")
    if system == "win32":
        return f"arduino-cli_{version}_Windows_{'ARM64' if arm else '64bit'}.zip"
    if system == "darwin":
        return f"arduino-cli_{version}_macOS_{'ARM64' if arm else '64bit'}.tar.gz"
    return f"arduino-cli_{version}_Linux_{'ARM64' if arm else '64bit'}.tar.gz"


def ensure_arduino(root: Path, versions: sdk.Versions, env: dict[str, str]) -> None:
    """Install Arduino CLI of the extended set, and each core of ``versions.toml`` into the root."""
    spec = versions.extra.arduino
    directory = sdk.arduino_cli_dir(root)
    config = sdk.arduino_config_file(root)
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        config.write_text("", encoding="utf-8")
    arduino_env = {**env, **sdk.arduino_env(root)}
    program = shutil.which("arduino-cli", path=str(directory))
    if program is None or _arduino_cli_version(Path(program), arduino_env) != spec.cli_version:
        downloads = root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        release = f"{spec.cli_releases}/v{spec.cli_version}"
        sums_file = downloads / f"arduino-cli-{spec.cli_version}-checksums.txt"
        _download(f"{release}/{spec.cli_version}-checksums.txt", sums_file)
        sums = parse_checksums(sums_file.read_text(encoding="utf-8"))
        name = arduino_cli_archive(spec.cli_version)
        if name not in sums:
            raise ProvisionError(f"the checksums of Arduino CLI {spec.cli_version} name no {name}")
        archive = downloads / name
        actual = _download(f"{release}/{name}", archive)
        if actual != sums[name]:
            archive.unlink()
            raise ProvisionError(f"{name}: SHA-256 {actual} is not the SHA-256 of the release ({sums[name]})")
        if directory.exists():
            shutil.rmtree(directory)
        extract(archive, directory)
        archive.unlink()
        program = shutil.which("arduino-cli", path=str(directory))
        if program is None:
            raise ProvisionError(f"{name} holds no arduino-cli")
    _run([program, "core", "update-index"], arduino_env)
    _run([program, "core", "install", *spec.platforms], arduino_env)


def _arduino_cli_version(program: Path, env: dict[str, str]) -> str | None:
    """Return the version that *program* gives (``arduino-cli version``), or None."""
    result = subprocess.run([str(program), "version", "--json"], env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    import json

    try:
        return str(json.loads(result.stdout)["VersionString"])
    except (ValueError, KeyError):
        return None


def provision(root: Path, versions: sdk.Versions, *, extra: bool = False) -> dict[str, str]:
    """Install each SDK, with *extra* also those of the extended set, write the stamp, and return it.

    A run without *extra* keeps the mark of the extended set when that
    mark is still of the current versions: the run changes no part of it.
    """
    too_long = sdk.windows_root_too_long(root)
    if too_long is not None:
        raise ProvisionError(too_long)
    root.mkdir(parents=True, exist_ok=True)
    env = sdk.tool_env(root, versions, zephyr_sdk=None)
    base_python = ensure_python(root, versions)
    python = ensure_venv(root, versions, base_python, env)
    ensure_zephyr_workspace(root, versions, python, env)
    sdk_dir = ensure_zephyr_sdk(root, versions)
    tools_env = sdk.tool_env(root, versions, zephyr_sdk=sdk_dir)
    # One run of pio for the two sets: each run ends with a prune of its cache.
    projects = versions.platformio_projects + (versions.extra.platformio_projects if extra else ())
    ensure_platformio(root, projects, tools_env)
    earlier = sdk.read_stamp(root) or {}
    stamp = {
        "fingerprint": sdk.fingerprint(),
        "python": versions.python.version,
        "zephyr_revision": versions.zephyr.revision,
        "zephyr_sdk": sdk.zephyr_sdk_version(root, versions),
    }
    if extra:
        ensure_esp_idf(root, versions, base_python, tools_env)
        ensure_arduino(root, versions, tools_env)
        stamp["extra_fingerprint"] = sdk.extra_fingerprint()
    elif earlier.get("extra_fingerprint") == sdk.extra_fingerprint():
        stamp["extra_fingerprint"] = earlier["extra_fingerprint"]
    sdk.write_stamp(root, stamp)
    return stamp


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the embedded SDKs of the test suite.")
    parser.add_argument(
        "--extra", action="store_true",
        help="Install the extended set too: the SDKs of the tests of the marker extra_platforms.",
    )
    arguments = parser.parse_args(argv)
    root = sdk.sdk_root()
    print(f"Root of the test SDKs: {root} (environment variable {sdk.SDK_ROOT_ENV})", flush=True)
    try:
        stamp = provision(root, sdk.load_versions(), extra=arguments.extra)
    except (ProvisionError, sdk.SdkError, OSError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"The test SDKs are installed: Python {stamp['python']}, Zephyr {stamp['zephyr_revision']}, "
        f"Zephyr SDK {stamp['zephyr_sdk']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
