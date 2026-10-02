#!/usr/bin/env python3
"""Build the clang compiler-header archive that the fw-context package carries.

WHY: the libclang wheel has no compiler headers (``stddef.h``,
``arm_acle.h`` ...).  fw-context carries the headers of the same clang major
version as the libclang it pins, thus ``pip install fw-context-mcp`` gives
both, offline and on every host.

The headers come from the Debian package ``libclang-common-18-dev``.  This
script checks the whole chain of trust before it writes anything:

1. ``dists/<suite>/InRelease`` has a valid signature by the Debian archive
   key with the fingerprint ``ARCHIVE_KEY_FPR`` (``gpgv --status-fd``).
2. The SHA-256 of ``main/binary-amd64/Packages.xz`` is the one in it.
3. That index gives the SHA-256 and the size of the package.
4. The downloaded package has that SHA-256 and size.

Then it writes, into ``src/fw_context_mcp/data/clang-resource/``:

- ``clang-<major>-include.tar.xz``: the ``include`` directory, deterministic
  (sorted names, mtime 0, owner 0), thus the same input gives the same bytes.
- ``SOURCE.json``: where the headers came from and the hashes.
- ``LICENSE.TXT``: the clang license (Apache 2.0 with LLVM exceptions).

It needs gpg and gpgv, thus it runs on a developer host, not on a user host.
Run it again only to move to another clang version: then change the
constants below together with the libclang pin in pyproject.toml.

Usage: python scripts/build_clang_headers.py
"""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

MAJOR = 18
SUITE = "trixie"
PACKAGE = "libclang-common-18-dev"
DEBIAN_VERSION = "1:18.1.8-18+b1"
CLANG_VERSION = "18.1.8"
MEMBER_PREFIX = "./usr/lib/llvm-18/lib/clang/18/include/"
MIRROR = "https://deb.debian.org/debian"
ARCHIVE_KEY_URL = "https://ftp-master.debian.org/keys/archive-key-13.asc"
#: The primary key fingerprint of the Debian 13/trixie archive signing key, as
#: https://ftp-master.debian.org/keys.html gives it (checked 2026-10-02).
ARCHIVE_KEY_FPR = "04B54C3CDCA79751B16BC6B5225629DF75B188BD"
LICENSE_URL = f"https://raw.githubusercontent.com/llvm/llvm-project/llvmorg-{CLANG_VERSION}/clang/LICENSE.TXT"

OUTPUT = Path(__file__).resolve().parent.parent / "src" / "fw_context_mcp" / "data" / "clang-resource"


def fetch(url: str) -> bytes:
    """Download *url* (fixed https URLs above only)."""
    with urllib.request.urlopen(url, timeout=120) as response:  # nosec B310
        return response.read()


def verified_release(work: Path) -> str:
    """Give the text of the signed Release, after gpgv checked the archive key fingerprint."""
    (work / "key.asc").write_bytes(fetch(ARCHIVE_KEY_URL))
    subprocess.run(["gpg", "--batch", "--yes", "--dearmor", "-o", str(work / "key.gpg"), str(work / "key.asc")],
                   check=True)
    (work / "InRelease").write_bytes(fetch(f"{MIRROR}/dists/{SUITE}/InRelease"))
    check = subprocess.run(
        ["gpgv", "--status-fd", "1", "--keyring", str(work / "key.gpg"),
         "--output", str(work / "Release"), str(work / "InRelease")],
        capture_output=True, text=True, check=False,
    )
    # InRelease carries several signatures (older and newer keys); one valid
    # signature by the pinned key is the requirement.  The LAST field of
    # VALIDSIG is the primary key fingerprint; the signature itself can come
    # from a subkey.  gpgv exits non-zero when one of the other signatures
    # has no public key here, thus the exit code is not the test.
    fingerprints = [line.split()[-1] for line in check.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG")]
    if ARCHIVE_KEY_FPR not in fingerprints:
        sys.exit(f"InRelease has no valid signature by {ARCHIVE_KEY_FPR}: {check.stdout}{check.stderr}")
    return (work / "Release").read_text(encoding="utf-8")


def release_sha256(release: str, path: str) -> str:
    inside = False
    for line in release.splitlines():
        if line.startswith("SHA256:"):
            inside = True
        elif inside and not line.startswith(" "):
            inside = False
        elif inside and line.split()[-1] == path:
            return line.split()[0]
    sys.exit(f"Release lists no SHA256 for {path}")


def package_entry(packages: str) -> dict[str, str]:
    for stanza in packages.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in stanza.splitlines() if ": " in line and not line.startswith(" "))
        if fields.get("Package") == PACKAGE and fields.get("Version") == DEBIAN_VERSION:
            return fields
    sys.exit(f"Packages lists no {PACKAGE} {DEBIAN_VERSION}")


def ar_member(data: bytes, name: str) -> bytes:
    if not data.startswith(b"!<arch>\n"):
        sys.exit("the package is not an ar archive")
    position = 8
    while position + 60 <= len(data):
        header = data[position:position + 60]
        size = int(header[48:58].decode("ascii").strip())
        position += 60
        if header[:16].decode("ascii").strip().rstrip("/") == name:
            return data[position:position + size]
        position += size + (size % 2)
    sys.exit(f"the package holds no {name}")


def headers_of(deb: bytes) -> dict[str, bytes]:
    headers: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(ar_member(deb, "data.tar.xz")), mode="r:xz") as tar:
        for member in tar.getmembers():
            if not member.name.startswith(MEMBER_PREFIX) or not member.isfile():
                continue
            relative = PurePosixPath(member.name[len(MEMBER_PREFIX):])
            if relative.is_absolute() or ".." in relative.parts:
                sys.exit(f"unsafe path in the package: {member.name}")
            stream = tar.extractfile(member)
            assert stream is not None
            headers[str(relative)] = stream.read()
    if "stddef.h" not in headers:
        sys.exit("the package holds no stddef.h")
    return headers


def deterministic_tar_xz(headers: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with lzma.open(buffer, "wb", preset=9 | lzma.PRESET_EXTREME) as xz, \
            tarfile.open(fileobj=xz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(headers):
            info = tarfile.TarInfo(f"include/{name}")
            info.size = len(headers[name])
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(headers[name]))
    return buffer.getvalue()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        release = verified_release(work)
        index_path = "main/binary-amd64/Packages.xz"
        index = fetch(f"{MIRROR}/dists/{SUITE}/{index_path}")
        if hashlib.sha256(index).hexdigest() != release_sha256(release, index_path):
            sys.exit(f"{index_path} does not have the SHA-256 that the signed Release gives")
        entry = package_entry(lzma.decompress(index).decode("utf-8"))
        deb = fetch(f"{MIRROR}/{entry['Filename']}")
        deb_sha256 = hashlib.sha256(deb).hexdigest()
        if deb_sha256 != entry["SHA256"] or len(deb) != int(entry["Size"]):
            sys.exit(f"{entry['Filename']} does not have the SHA-256 and size that the signed index gives")
        headers = headers_of(deb)
        archive = deterministic_tar_xz(headers)
        license_text = fetch(LICENSE_URL)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    archive_name = f"clang-{MAJOR}-include.tar.xz"
    (OUTPUT / archive_name).write_bytes(archive)
    (OUTPUT / "LICENSE.TXT").write_bytes(license_text)
    (OUTPUT / "SOURCE.json").write_text(json.dumps({
        "major": MAJOR,
        "clang_version": CLANG_VERSION,
        "archive": archive_name,
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "files": len(headers),
        "debian": {
            "suite": SUITE, "package": PACKAGE, "version": DEBIAN_VERSION,
            "filename": entry["Filename"], "sha256": deb_sha256,
            "signing_key_fingerprint": ARCHIVE_KEY_FPR,
        },
        "license": {"name": "Apache-2.0 WITH LLVM-exception", "source": LICENSE_URL},
    }, indent=2) + "\n", encoding="utf-8")
    print(f"{archive_name}: {len(headers)} headers, {len(archive)} bytes, from {entry['Filename']}")


if __name__ == "__main__":
    main()
