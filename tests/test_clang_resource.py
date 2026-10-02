"""The clang compiler headers of the libclang version: the bundled copy and the lookup.

The libclang wheel has no compiler headers.  Without them a GCC build parsed
with the headers of GCC: the ``arm_acle.h`` of ARM GCC 12 gave 2574 errors
in one STM32 build.  The headers of another clang version break too: clang
22's ``arm_neon.h`` gave 312 errors in libclang 18.  fw-context therefore
ships the headers of the libclang version it pins.

The tests unpack into ``tmp_path``; the fake bundles are built in memory.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

from fw_context_mcp.indexer import _clang_resource
from fw_context_mcp.indexer._clang_resource import (
    InstallError,
    bundled_source,
    find_resource_include,
    install_managed,
)


@pytest.fixture(autouse=True)
def _forget_failures():
    """A failed unpack is kept per process; a test must not see the failure of another."""
    _clang_resource._FAILED_UNPACK.clear()
    yield
    _clang_resource._FAILED_UNPACK.clear()


@pytest.fixture
def managed(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "clang-resource"
    monkeypatch.setenv(_clang_resource.MANAGED_ROOT_ENV, str(root))
    return root


def _fake_bundle(directory: Path, files: dict[str, bytes], *, links: dict[str, str] | None = None,
                 major: int = 18, sha256: str | None = None) -> Path:
    """Write a bundle directory: an archive of *files* and its SOURCE.json."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    data = buffer.getvalue()
    directory.mkdir(parents=True)
    (directory / f"clang-{major}-include.tar.xz").write_bytes(data)
    (directory / "SOURCE.json").write_text(json.dumps({
        "major": major, "archive": f"clang-{major}-include.tar.xz",
        "archive_sha256": sha256 or hashlib.sha256(data).hexdigest(),
    }))
    return directory


def _no_system_clang(monkeypatch) -> None:
    monkeypatch.setattr(_clang_resource, "_system_candidates", lambda major: [])
    monkeypatch.setattr(_clang_resource.shutil, "which", lambda name: None)


class TestTheShippedArchive:
    def test_the_package_ships_the_headers_of_the_pinned_libclang(self) -> None:
        """pyproject pins libclang <19; the bundle must be that major version."""
        major = _clang_resource.libclang_major()
        assert major == 18
        source = bundled_source(major)
        assert source is not None
        data = _clang_resource._bundle_dir().joinpath(source["archive"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == source["archive_sha256"]
        assert source["debian"]["signing_key_fingerprint"] == "04B54C3CDCA79751B16BC6B5225629DF75B188BD"

    def test_the_shipped_archive_unpacks_with_its_license(self, managed) -> None:
        include = install_managed(18)
        assert (include / "stddef.h").is_file()
        assert (include / "arm_acle.h").is_file() and (include / "arm_neon.h").is_file()
        assert len([p for p in include.rglob("*") if p.is_file()]) == bundled_source(18)["files"]
        assert _clang_resource._bundle_dir().joinpath("LICENSE.TXT").is_file()


class TestUnpack:
    def test_a_wrong_hash_is_refused_and_leaves_nothing(self, tmp_path, managed, monkeypatch) -> None:
        bundle = _fake_bundle(tmp_path / "b", {"include/stddef.h": b"x"}, sha256="0" * 64)
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: bundle)
        with pytest.raises(InstallError, match="SHA-256"):
            install_managed(18)
        assert not list(managed.glob("18-*"))

    def test_a_path_that_leaves_the_copy_is_refused(self, tmp_path, managed, monkeypatch) -> None:
        bundle = _fake_bundle(tmp_path / "b", {"include/stddef.h": b"x", "include/../../escape.h": b"x"})
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: bundle)
        with pytest.raises(InstallError, match="unsafe path"):
            install_managed(18)
        assert not list(managed.glob("18-*"))

    def test_a_link_in_the_archive_is_not_followed(self, tmp_path, managed, monkeypatch) -> None:
        bundle = _fake_bundle(tmp_path / "b", {"include/stddef.h": b"x"}, links={"include/evil.h": "/etc/passwd"})
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: bundle)
        assert not (install_managed(18) / "evil.h").exists()

    def test_a_windows_separator_in_a_name_is_refused(self, tmp_path, managed, monkeypatch) -> None:
        """On Windows `include\\..\\x.h` leaves the copy although its POSIX parts hold no `..`."""
        bundle = _fake_bundle(tmp_path / "b", {"include/stddef.h": b"x", "include\\..\\..\\x.h": b"x"})
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: bundle)
        with pytest.raises(InstallError, match="unsafe path"):
            install_managed(18)

    def test_a_damaged_archive_is_an_install_error(self, tmp_path, managed, monkeypatch) -> None:
        bundle = tmp_path / "b"
        bundle.mkdir()
        (bundle / "clang-18-include.tar.xz").write_bytes(b"not xz")
        (bundle / "SOURCE.json").write_text(json.dumps({
            "major": 18, "archive": "clang-18-include.tar.xz", "archive_sha256": hashlib.sha256(b"not xz").hexdigest(),
        }))
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: bundle)
        with pytest.raises(InstallError, match="damaged"):
            install_managed(18)

    @pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX modes and a non-root user")
    def test_a_read_only_root_is_an_install_error_and_not_a_crash(self, tmp_path, monkeypatch) -> None:
        """A read-only home ended the whole parse with a PermissionError."""
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        monkeypatch.setenv(_clang_resource.MANAGED_ROOT_ENV, str(locked / "clang-resource"))
        try:
            with pytest.raises(InstallError, match="cannot create"):
                install_managed(18)
            _no_system_clang(monkeypatch)
            assert find_resource_include(18) is None
        finally:
            locked.chmod(0o755)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_the_copy_is_readable_by_other_users(self, managed) -> None:
        assert install_managed(18).parent.stat().st_mode & 0o777 == 0o755

    def test_a_complete_copy_of_the_same_archive_is_not_replaced(self, managed) -> None:
        """A second process must not remove the copy that the first one already parses with."""
        include = install_managed(18)
        before = (include / "stddef.h").stat().st_ino
        install_managed(18)
        assert (include / "stddef.h").stat().st_ino == before

    def test_another_major_version_ships_nothing(self, tmp_path, managed, monkeypatch) -> None:
        bundle = _fake_bundle(tmp_path / "b", {"include/stddef.h": b"x"}, major=18)
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: bundle)
        with pytest.raises(InstallError, match="clang 19"):
            install_managed(19)


class TestFind:
    def _headers(self, directory: Path) -> Path:
        directory.mkdir(parents=True)
        (directory / "stddef.h").write_text("")
        return directory

    def test_the_bundle_is_unpacked_at_first_use(self, tmp_path, managed, monkeypatch) -> None:
        bundle = _fake_bundle(tmp_path / "b", {"include/stddef.h": b"x"})
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: bundle)
        _no_system_clang(monkeypatch)
        found = find_resource_include(18)
        assert found is not None and found.source == "managed"
        assert found.include.parent.parent == managed and found.include.parent.name.startswith("18-")

    def test_a_failed_unpack_is_not_repeated_for_each_unit(self, tmp_path, managed, monkeypatch) -> None:
        """The lookup runs per unit: 2000 units must not give 2000 unpacks and warnings."""
        bundle = _fake_bundle(tmp_path / "b", {"include/stddef.h": b"x"})
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: bundle)
        _no_system_clang(monkeypatch)
        calls: list[int] = []

        def failing(major):
            calls.append(major)
            raise InstallError("disk full")

        monkeypatch.setattr(_clang_resource, "install_managed", failing)
        for _ in range(5):
            assert find_resource_include(18) is None
        assert calls == [18]

    def test_two_archives_of_one_major_have_their_own_copies(self, tmp_path, managed, monkeypatch) -> None:
        """Two installations with different archives must not replace the copy of each other.

        One shared directory per major version made them swap it at each unit.
        """
        old = _fake_bundle(tmp_path / "old", {"include/stddef.h": b"old"})
        new = _fake_bundle(tmp_path / "new", {"include/stddef.h": b"new"})
        _no_system_clang(monkeypatch)
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: old)
        old_include = find_resource_include(18).include
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: new)
        new_include = find_resource_include(18).include
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: old)
        assert find_resource_include(18).include == old_include

        assert old_include != new_include
        assert (old_include / "stddef.h").read_bytes() == b"old"
        assert (new_include / "stddef.h").read_bytes() == b"new"

    def test_a_copy_of_another_archive_serves_when_the_unpack_fails(self, tmp_path, managed, monkeypatch) -> None:
        """Other headers of the same major version are better than the headers of GCC."""
        old = _fake_bundle(tmp_path / "old", {"include/stddef.h": b"old"})
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: old)
        install_managed(18)
        new = _fake_bundle(tmp_path / "new", {"include/stddef.h": b"new"})
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: new)
        _no_system_clang(monkeypatch)
        monkeypatch.setattr(_clang_resource, "install_managed", lambda major: (_ for _ in ()).throw(InstallError("x")))
        found = find_resource_include(18)
        assert found is not None and found.source == "managed (another archive)"
        assert (found.include / "stddef.h").read_bytes() == b"old"

    def test_a_check_does_not_unpack(self, tmp_path, managed, monkeypatch) -> None:
        """`doctor` reports and must not change the machine."""
        bundle = _fake_bundle(tmp_path / "b", {"include/stddef.h": b"x"})
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: bundle)
        _no_system_clang(monkeypatch)
        assert find_resource_include(18, unpack=False) is None
        assert not list(managed.glob("18-*"))

    def test_the_bundle_comes_before_a_system_installation(self, tmp_path, managed, monkeypatch) -> None:
        """The bundle is the same on every host, thus the build identity does not depend on the host."""
        system = tmp_path / "sys" / "18"
        self._headers(system / "include")
        install_managed(18)
        monkeypatch.setattr(_clang_resource, "_system_candidates", lambda major: [system])
        found = find_resource_include(18)
        assert found is not None and found.source == "managed"

    def test_a_system_installation_serves_without_a_bundle(self, tmp_path, managed, monkeypatch) -> None:
        system = tmp_path / "sys" / "18"
        self._headers(system / "include")
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: tmp_path / "none")
        monkeypatch.setattr(_clang_resource, "_system_candidates", lambda major: [system])
        found = find_resource_include(18)
        assert found is not None and found.source == "system"

    def test_a_clang_of_another_version_on_path_is_not_used(self, tmp_path, managed, monkeypatch) -> None:
        """clang 22 headers in libclang 18: 312 errors from arm_neon.h on AArch64."""
        resource = tmp_path / "clang" / "22"
        self._headers(resource / "include")
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: tmp_path / "none")
        monkeypatch.setattr(_clang_resource, "_system_candidates", lambda major: [])
        monkeypatch.setattr(_clang_resource.shutil, "which", lambda name: "/usr/bin/clang" if name == "clang" else None)
        monkeypatch.setattr(_clang_resource, "_printed_resource_dir", lambda clang: str(resource))
        assert find_resource_include(18) is None

    def test_a_clang_of_the_same_version_on_path_is_used(self, tmp_path, managed, monkeypatch) -> None:
        resource = tmp_path / "clang" / "18"
        self._headers(resource / "include")
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: tmp_path / "none")
        monkeypatch.setattr(_clang_resource, "_system_candidates", lambda major: [])
        monkeypatch.setattr(_clang_resource.shutil, "which", lambda name: "/usr/bin/clang-18" if name == "clang-18" else None)
        monkeypatch.setattr(_clang_resource, "_printed_resource_dir", lambda clang: str(resource))
        found = find_resource_include(18)
        assert found is not None and found.source == "PATH"

    def test_a_removed_directory_is_not_given(self, tmp_path, managed, monkeypatch) -> None:
        """A long MCP process must not keep a directory that was removed."""
        include = install_managed(18)
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: tmp_path / "none")
        _no_system_clang(monkeypatch)
        assert find_resource_include(18) is not None
        (include / "stddef.h").unlink()
        assert find_resource_include(18) is None

    @pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
    def test_each_host_has_system_candidates(self, platform, monkeypatch) -> None:
        monkeypatch.setattr(_clang_resource.sys, "platform", platform)
        monkeypatch.setenv("ProgramFiles", "C:\\Program Files")
        candidates = [str(c).replace("\\", "/") for c in _clang_resource._system_candidates(18)]
        expected = {"linux": "/usr/lib/llvm-18/lib/clang/18", "darwin": "/opt/homebrew/opt/llvm@18/lib/clang/18",
                    "win32": "Program Files/LLVM/lib/clang/18"}[platform]
        assert any(c.endswith(expected) for c in candidates), candidates


class TestDoctor:
    def test_the_check_reports_a_missing_copy_with_a_fix(self, managed, monkeypatch) -> None:
        from fw_context_mcp.deps._checks import check_clang_resource

        _no_system_clang(monkeypatch)
        result = check_clang_resource()
        assert result.status == "degraded" and not result.critical
        assert result.fix_cmd == "fw-context doctor --fix --only clang-resource"
        assert "doctor --fix" in result.instructions
        assert not list(managed.glob("18-*")), "the check must not unpack"

    def test_the_check_reports_a_copy_of_another_archive(self, tmp_path, managed, monkeypatch) -> None:
        from fw_context_mcp.deps._checks import check_clang_resource

        old = _fake_bundle(tmp_path / "old", {"include/stddef.h": b"old"})
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: old)
        install_managed(18)
        new = _fake_bundle(tmp_path / "new", {"include/stddef.h": b"new"})
        monkeypatch.setattr(_clang_resource, "_bundle_dir", lambda: new)
        _no_system_clang(monkeypatch)
        result = check_clang_resource()
        assert result.status == "degraded" and "another fw-context version" in result.message

    def test_the_fix_unpacks_and_the_check_then_passes(self, managed, monkeypatch) -> None:
        from fw_context_mcp.deps import run_fixes
        from fw_context_mcp.deps._checks import check_clang_resource

        _no_system_clang(monkeypatch)
        fixed = run_fixes([check_clang_resource()])
        assert fixed[0].status == "ok" and "managed" in fixed[0].message

    def test_doctor_only_rejects_an_unknown_check(self, capsys) -> None:
        import argparse

        from fw_context_mcp.cli._doctor import cmd_doctor

        args = argparse.Namespace(project=None, fix=False, json=False, only="clang-resource,nope")
        assert cmd_doctor(args) == 2
        assert "nope" in capsys.readouterr().out
