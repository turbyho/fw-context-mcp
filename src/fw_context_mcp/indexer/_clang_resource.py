"""The clang compiler headers of the same major version as libclang.

WHY: the libclang wheel (18.1.1) has no resource directory, thus clang's own
compiler headers (``stddef.h``, ``arm_acle.h``, ``arm_neon.h`` ...) are not
on the machine.  The parse then used the internal include directory of GCC,
whose intrinsic headers are written for GCC builtins: the ``arm_acle.h`` of
ARM GCC 12 gave 2574 errors in 143 of 215 units of one STM32 build.

WHY the SAME major version: the headers of a newer clang use types that
libclang 18 does not know.  Measured with the clang 22 headers in libclang
18: ``<arm_neon.h>`` on AArch64 gave 312 errors (``__mfp8``, clang 20+), and
``<immintrin.h>`` on x86 gave 313.  With the clang 18 headers both gave 0.

WHY the headers ship with fw-context: ``pip install fw-context-mcp`` then
gives libclang (pinned to one major version in pyproject.toml) and its
headers together, offline and on every host — the headers are plain text
and do not depend on the host.  ``data/clang-resource/`` holds them as one
archive (``scripts/build_clang_headers.py`` builds it from a Debian package
after it checked the Debian signature chain; ``SOURCE.json`` records it).

The lookup order:

1. The managed copy in ``~/.fw-context/clang-resource/<major>-<hash>/include``,
   which is the bundled archive, unpacked on first use.  First, because it
   is the exact version of the pinned libclang, with the same content on
   every host, thus the headers do not depend on what else is installed.
   (The path holds the home directory, and the path is part of the build
   identity, as the project root already is.)
2. A system installation of that clang version (a package manager,
   Homebrew, the LLVM installer for Windows), when no archive is bundled.
3. ``clang-<major>`` or ``clang`` on ``PATH``, if it is that version.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
import io
import json
import logging
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from functools import cache
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

#: Overrides where the bundled archive is unpacked; the test suite sets it,
#: and a subprocess that a test starts inherits it.
MANAGED_ROOT_ENV = "FW_CONTEXT_CLANG_RESOURCE_DIR"


def managed_root() -> Path:
    """Give the directory where the bundled archive is unpacked (read on each call)."""
    override = os.environ.get(MANAGED_ROOT_ENV)
    return Path(override) if override else Path.home() / ".fw-context" / "clang-resource"

_PROBE_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class ResourceDir:
    """One clang resource ``include`` directory.

    Attributes:
        include: The directory that holds ``stddef.h`` and the other headers.
        source: Where it came from: ``"managed"``, ``"system"`` or ``"PATH"``.
    """

    include: Path
    source: str


class InstallError(RuntimeError):
    """The managed copy could not be installed."""


def libclang_major() -> int | None:
    """Give the major version of the libclang that the parse uses, or None.

    The version of the ``libclang`` wheel is the version of its library.
    """
    try:
        version = importlib.metadata.version("libclang")
    except importlib.metadata.PackageNotFoundError:
        return None
    match = re.match(r"(\d+)\.", version)
    return int(match.group(1)) if match else None


def _copy_dir(major: int, source: dict) -> Path:
    """Give the directory of the managed copy of the archive that *source* describes.

    WHY one directory per ARCHIVE and not per major version: two
    installations of fw-context (a released one for the MCP server and a
    development one) can carry different archives of one major version.
    With one shared directory, each would replace the copy of the other at
    each unit, and remove ``stddef.h`` under the parse of the other.  With
    one directory per archive, a complete copy is never replaced.
    """
    return managed_root() / f"{major}-{str(source.get('archive_sha256', ''))[:12]}"


def managed_include(major: int) -> Path | None:
    """Give the ``include`` directory of the managed copy of the shipped archive, or None."""
    source = bundled_source(major)
    return _copy_dir(major, source) / "include" if source is not None else None


def _other_managed_include(major: int) -> Path | None:
    """Give a complete copy of another archive of clang *major*, the newest first, or None.

    Used when the shipped archive cannot be unpacked: other headers of the
    same major version are still better than the headers of GCC.
    """
    # The stat is guarded too: another process can remove a copy between the
    # glob and the stat, and an OSError here would end the whole parse.
    dated: list[tuple[float, Path]] = []
    try:
        for path in managed_root().glob(f"{major}-*"):
            try:
                if _usable(path / "include"):
                    dated.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return None
    dated.sort(reverse=True)
    return dated[0][1] / "include" if dated else None


def _bundle_dir() -> Traversable:
    return importlib.resources.files("fw_context_mcp").joinpath("data", "clang-resource")


def bundled_source(major: int) -> dict | None:
    """Give the ``SOURCE.json`` of the bundled archive for *major*, or None when none ships."""
    try:
        source = json.loads(_bundle_dir().joinpath("SOURCE.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(source, dict) or source.get("major") != major:
        return None
    return source


def install_managed(major: int) -> Path:
    """Unpack the bundled headers of clang *major* into the managed copy; give its ``include``.

    Raises ``InstallError`` (and no other error) when no archive ships for
    *major*, when the archive does not have the SHA-256 that ``SOURCE.json``
    gives, when it holds an unsafe path, or when the copy cannot be written
    — a read-only home included.  A partial copy never stays: the headers go
    to a temporary directory, which is renamed to the copy only when it is
    complete.

    WHY a complete copy is never replaced: a second process (an MCP server
    beside an indexer) can finish the same copy first, and the first
    process may already parse with it.  The directory is per archive (see
    ``_copy_dir``), thus a complete copy there is always the right one, and
    only a broken copy is removed.
    """
    source = bundled_source(major)
    if source is None:
        raise InstallError(f"no header archive for clang {major} ships with this fw-context")
    try:
        data = _bundle_dir().joinpath(str(source["archive"])).read_bytes()
    except (OSError, KeyError) as error:
        raise InstallError(f"the bundled header archive is missing: {error}") from error
    if hashlib.sha256(data).hexdigest() != source.get("archive_sha256"):
        raise InstallError("the bundled header archive does not have the SHA-256 of SOURCE.json")
    target = _copy_dir(major, source)
    if _is_current(target, source):
        return target / "include"
    headers = _headers_of(data)
    root = managed_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{major}-", dir=root))
    except OSError as error:
        raise InstallError(f"cannot create {root}: {error}") from error
    try:
        for relative, content in headers.items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (staging / "SOURCE.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
        # mkdtemp gives 0o700; another user of a shared root must read the copy.
        # B103: a directory of public compiler headers, holds no secret, and
        # stays writable by the owner alone.
        os.chmod(staging, 0o755)  # nosec B103
        if target.exists() and not _is_current(target, source):
            shutil.rmtree(target)  # a broken copy: an earlier run was killed
        os.replace(staging, target)
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        if _is_current(target, source):
            return target / "include"  # another process finished the same copy
        raise InstallError(f"cannot write {target}: {error}") from error
    return target / "include"


def _is_current(target: Path, source: dict) -> bool:
    """Say if *target* is a complete copy of the archive that *source* describes."""
    if not _usable(target / "include"):
        return False
    try:
        copied = json.loads((target / "SOURCE.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(copied, dict) and copied.get("archive_sha256") == source.get("archive_sha256")


def _headers_of(data: bytes) -> dict[str, bytes]:
    """Give the regular files under ``include/`` of the archive.

    Only regular files with a plain relative path are taken: a link, an
    absolute path or a ``..`` component is refused, thus the archive cannot
    write outside the copy.  A ``\\`` or a ``:`` is refused too: on Windows
    they are a separator and a drive, thus ``include\\..\\x.h`` would leave
    the copy although its POSIX parts hold no ``..``.
    """
    headers: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tar:
            for member in tar.getmembers():
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts or "\\" in member.name or ":" in member.name:
                    raise InstallError(f"unsafe path in the header archive: {member.name}")
                if not member.isfile() or relative.parts[:1] != ("include",):
                    continue
                stream = tar.extractfile(member)
                if stream is not None:
                    headers[str(relative)] = stream.read()
    except (tarfile.TarError, lzma.LZMAError, EOFError) as error:
        raise InstallError(f"the header archive is damaged: {error}") from error
    if "include/stddef.h" not in headers:
        raise InstallError("the header archive holds no include/stddef.h")
    return headers


def _system_candidates(major: int) -> list[Path]:
    """Give the resource directories that a system installation of clang *major* uses."""
    if sys.platform == "darwin":
        roots = [
            f"/opt/homebrew/opt/llvm@{major}", f"/usr/local/opt/llvm@{major}",
            "/opt/homebrew/opt/llvm", "/usr/local/opt/llvm",
            f"/opt/local/libexec/llvm-{major}",  # MacPorts
        ]
        return [Path(root) / "lib" / "clang" / str(major) for root in roots]
    if sys.platform == "win32":
        bases = [os.environ.get(name) for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
        return [Path(base) / "LLVM" / "lib" / "clang" / str(major) for base in bases if base]
    return [
        Path(f"/usr/lib/llvm{major}/lib/clang/{major}"),    # Arch: clang18
        Path(f"/usr/lib/llvm-{major}/lib/clang/{major}"),   # Debian, Ubuntu
        Path(f"/usr/lib64/llvm{major}/lib/clang/{major}"),  # Fedora: clang18
        Path(f"/usr/lib/clang/{major}"),
        Path(f"/usr/lib64/clang/{major}"),
        Path(f"/usr/local/lib/clang/{major}"),
    ]


def _usable(include: Path) -> bool:
    return (include / "stddef.h").is_file()


#: ``clang -print-resource-dir`` per (binary, mtime): the answer changes only
#: with the binary, and a long MCP process must see an upgrade.
_PRINTED: dict[tuple[str, int], str] = {}


def _printed_resource_dir(clang: str) -> str | None:
    try:
        key = (clang, os.stat(clang).st_mtime_ns)
    except OSError:
        return None
    if key not in _PRINTED:
        try:
            answer = subprocess.run(
                [clang, "-print-resource-dir"], capture_output=True, text=True,
                timeout=_PROBE_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            log.debug("%s -print-resource-dir failed: %s", clang, error)
            return None
        if answer.returncode != 0:
            return None
        _PRINTED[key] = answer.stdout.strip()
    return _PRINTED[key]


def find_resource_include(major: int | None = None, *, unpack: bool = True) -> ResourceDir | None:
    """Find the resource ``include`` directory of clang *major* (default: the libclang one).

    With *unpack*, a bundled archive that is not unpacked yet is unpacked
    here, thus the first parse after an installation has the headers
    without a separate step.  ``doctor`` passes False, thus a check does not
    change the machine.

    The checks run on each call, thus a directory that was removed is not
    given to a long MCP process.  Only the answers of the clang binaries are
    kept, per binary and mtime.
    """
    if major is None:
        major = libclang_major()
        if major is None:
            return None
    source = bundled_source(major)
    if source is not None:
        copy = _copy_dir(major, source)
        if _is_current(copy, source):
            return ResourceDir(copy / "include", "managed")
        if unpack and not _unpack_failed_recently(major):
            try:
                return ResourceDir(install_managed(major), "managed")
            except InstallError as error:
                _FAILED_UNPACK[major] = time.monotonic()
                log.warning("Cannot unpack the bundled clang %s headers (no retry for %d s): %s",
                            major, int(_FAILURE_TTL_S), error)
    other = _other_managed_include(major)
    if other is not None:
        return ResourceDir(other, "managed (another archive)")
    for candidate in _system_candidates(major):
        if _usable(candidate / "include"):
            return ResourceDir(candidate / "include", "system")
    for name in (f"clang-{major}", "clang"):
        clang = shutil.which(name)
        if clang is None:
            continue
        printed = _printed_resource_dir(clang)
        if not printed:
            continue
        resource = Path(printed)
        if resource.name.split(".")[0] == str(major) and _usable(resource / "include"):
            _warn_path_source(clang, str(resource))
            return ResourceDir(resource / "include", "PATH")
    return None


#: Failed unpacks per major version.  WHY: the lookup runs for each unit,
#: and an unpack that fails (a read-only home, a full disk) would otherwise
#: run again for each one — measured 44 ms an unpack, thus 90 s and 2000
#: warnings for a build of 2000 units.  Kept for a short time only, thus a
#: long MCP process tries again after the cause is gone.
_FAILED_UNPACK: dict[int, float] = {}
_FAILURE_TTL_S = 300.0


def _unpack_failed_recently(major: int) -> bool:
    failed_at = _FAILED_UNPACK.get(major)
    return failed_at is not None and time.monotonic() - failed_at < _FAILURE_TTL_S


@cache
def _warn_path_source(clang: str, resource: str) -> None:
    log.warning(
        "The clang compiler headers come from %s, which fw-context found through PATH (%s). "
        "An environment with another PATH gives other headers and another build.",
        resource, clang,
    )
