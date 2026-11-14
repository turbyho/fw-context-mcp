"""The install of the test SDKs (``tests/sdk``) and the toolchain helper of the indexer tests (``tests/_toolchains.py``)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import urllib.parse
from pathlib import Path

import pytest

from tests import _toolchains, sdk
from tests.sdk import provision


def _versions_file(
    tmp_path: Path, revision: str = "v9.9.9", projects: tuple[str, ...] = (), extra_projects: tuple[str, ...] = (),
) -> Path:
    """Write a versions.toml with one Zephyr toolchain, one PlatformIO toolchain and an extended set, and its requirements.txt."""
    path = tmp_path / "sdk" / "versions.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"""
[python]
version = "3.12.16"
release = "20261110"
releases = "https://example.invalid/python"

[zephyr]
manifest = "https://example.invalid/zephyr"
revision = "{revision}"
modules = ["hal_x"]
gnu_toolchains = ["arm-zephyr-eabi"]
python_requirements = ["zephyr/scripts/requirements-base.txt"]
sdk_releases = "https://example.invalid/releases"

[platformio]
projects = {json.dumps(list(projects))}

[extra.platformio]
projects = {json.dumps(list(extra_projects))}

[extra.esp_idf]
version = "v9.9"
archive = "https://example.invalid/esp-idf-v9.9.zip"
sha256 = "00"
targets = ["esp32"]

[extra.arduino]
cli_version = "9.9.9"
cli_releases = "https://example.invalid/arduino-cli"
platforms = ["arduino:avr@9.9.9"]

[[toolchain]]
id = "arm-zephyr"
sdk = "zephyr"
package = "arm-zephyr-eabi"
prefix = "arm-zephyr-eabi-"
flags = ["-mthumb"]

[[toolchain]]
id = "riscv-esp"
sdk = "platformio"
package = "toolchain-riscv32-esp"
prefix = "riscv32-esp-elf-"
flags = ["-mabi=ilp32"]
""",
        encoding="utf-8",
        newline="\n",
    )
    path.with_name("requirements.txt").write_text("west==1.5.0\n", encoding="utf-8", newline="\n")
    return path


def _tar_xz(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:xz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


# ── versions.toml, the stamp, the places ───────────────────────────────────


def test_the_toolchains_of_the_suite_are_those_of_versions_toml():
    """The fixture `toolchain` runs each test once with each toolchain of the file, in its sequence."""
    assert _toolchains.toolchain_ids() == ["arm-zephyr", "riscv-esp"]


def test_each_toolchain_has_the_place_that_its_sdk_installs(tmp_path):
    versions = sdk.load_versions(_versions_file(tmp_path))
    root = tmp_path / "root"
    (root / "zephyr-v9.9.9" / "zephyr").mkdir(parents=True)
    (root / "zephyr-v9.9.9" / "zephyr" / "SDK_VERSION").write_text("1.2.3\n", encoding="utf-8")
    arm, riscv = versions.toolchains

    assert sdk.toolchain_bin(root, versions, arm) == root / "zephyr-sdk-1.2.3" / "gnu" / "arm-zephyr-eabi" / "bin"
    assert sdk.toolchain_bin(root, versions, riscv) == root / "platformio" / "packages" / "toolchain-riscv32-esp" / "bin"


def test_the_fingerprint_follows_each_file_that_pins_a_version(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "platformio.ini").write_text("[env:a]\nplatform = p@1.0.0\n", encoding="utf-8")
    versions_file = _versions_file(tmp_path, projects=("proj",))
    first = sdk.fingerprint(versions_file, repo=tmp_path)

    (project / "platformio.ini").write_text("[env:a]\nplatform = p@1.0.1\n", encoding="utf-8")
    after_platform = sdk.fingerprint(versions_file, repo=tmp_path)
    versions_file.with_name("requirements.txt").write_text("west==1.5.1\n", encoding="utf-8")
    after_requirements = sdk.fingerprint(versions_file, repo=tmp_path)

    assert len({first, after_platform, after_requirements}) == 3


def test_the_fingerprint_of_the_extended_set_follows_its_projects(tmp_path):
    """The extended set has a fingerprint of its own: a change of its platformio.ini leaves that of CI alone."""
    project = tmp_path / "extra"
    project.mkdir()
    (project / "platformio.ini").write_text("[env:a]\nplatform = p@1.0.0\n", encoding="utf-8")
    versions_file = _versions_file(tmp_path, extra_projects=("extra",))
    ci, extra = sdk.fingerprint(versions_file, repo=tmp_path), sdk.extra_fingerprint(versions_file, repo=tmp_path)

    (project / "platformio.ini").write_text("[env:a]\nplatform = p@1.0.1\n", encoding="utf-8")

    assert sdk.fingerprint(versions_file, repo=tmp_path) == ci
    assert sdk.extra_fingerprint(versions_file, repo=tmp_path) != extra


def test_the_fingerprint_of_a_checkout_with_crlf_is_the_same(tmp_path):
    """git can check out the files with CRLF on Windows; the versions are the same."""
    versions_file = _versions_file(tmp_path)
    lf = sdk.fingerprint(versions_file, repo=tmp_path)
    versions_file.write_bytes(versions_file.read_bytes().replace(b"\n", b"\r\n"))

    assert sdk.fingerprint(versions_file, repo=tmp_path) == lf


def test_the_environment_of_the_tools_replaces_the_zephyr_of_the_shell(tmp_path):
    """An NCS shell exports its own ZEPHYR_BASE and SDK; the builds of the tests must not take them."""
    versions = sdk.load_versions(_versions_file(tmp_path))
    root = tmp_path / "root"
    shell = {"PATH": "/usr/bin", "ZEPHYR_BASE": "/opt/ncs/zephyr", "ZEPHYR_SDK_INSTALL_DIR": "/opt/ncs/sdk"}

    env = sdk.tool_env(root, versions, zephyr_sdk=root / "zephyr-sdk-1.2.3", base=shell)
    before_sdk = sdk.tool_env(root, versions, zephyr_sdk=None, base=shell)

    assert env["ZEPHYR_BASE"] == str(root / "zephyr-v9.9.9" / "zephyr")
    assert env["ZEPHYR_SDK_INSTALL_DIR"] == str(root / "zephyr-sdk-1.2.3")
    assert env["PLATFORMIO_CORE_DIR"] == str(root / "platformio")
    assert env["PATH"].split(os.pathsep)[0] == str(sdk.venv_bin(root))
    assert "ZEPHYR_SDK_INSTALL_DIR" not in before_sdk


@pytest.mark.parametrize(("machine", "expected"), [("x86_64", "x86_64"), ("AMD64", "x86_64"), ("arm64", "aarch64")])
def test_the_host_name_of_the_sdk_archives(monkeypatch, machine, expected):
    monkeypatch.setattr(sdk.platform, "machine", lambda: machine)
    monkeypatch.setattr(sdk.sys, "platform", "linux")

    assert sdk.zephyr_sdk_host() == f"linux-{expected}"


def test_a_host_without_a_build_of_the_sdk_is_an_error(monkeypatch):
    monkeypatch.setattr(sdk.platform, "machine", lambda: "riscv64")

    with pytest.raises(sdk.SdkError, match="no build for this host"):
        sdk.zephyr_sdk_host()


def test_the_default_root_on_windows_is_short(monkeypatch):
    """A root in the user profile made paths of 269 characters on Windows, and the install failed."""
    monkeypatch.delenv(sdk.SDK_ROOT_ENV, raising=False)
    monkeypatch.setattr(sdk.sys, "platform", "win32")
    monkeypatch.setenv("SystemDrive", "D:")

    root = sdk.sdk_root.__wrapped__()

    assert root == Path("D:\\") / "fwctx-sdk"
    assert sdk.windows_root_too_long(Path("C:\\fwctx-sdk"), system="win32") is None


def test_a_root_too_long_for_windows_is_named_with_the_limit():
    root = Path("C:\\Users\\someone\\.fw-context-test-sdk")

    reason = sdk.windows_root_too_long(root, system="win32")

    assert reason is not None
    assert f"{len(str(root)) + 1 + sdk.LONGEST_PATH_IN_ROOT} characters" in reason
    assert "at most 23 characters" in reason
    assert sdk.windows_root_too_long(root, system="linux") is None


# ── The install ────────────────────────────────────────────────────────────


def test_the_archives_of_the_sdk_are_7z_only_for_windows():
    assert provision.zephyr_archive_names("1.0.1", "windows-x86_64", ["arm-zephyr-eabi"]) == {
        "minimal": "zephyr-sdk-1.0.1_windows-x86_64_minimal.7z",
        "arm-zephyr-eabi": "toolchain_gnu_windows-x86_64_arm-zephyr-eabi.7z",
    }
    assert provision.zephyr_archive_names("1.0.1", "macos-aarch64", [])["minimal"] == (
        "zephyr-sdk-1.0.1_macos-aarch64_minimal.tar.xz"
    )


def test_the_checksums_of_the_release_are_read_by_name():
    text = "ab12  zephyr-sdk-1.0.1_linux-x86_64_minimal.tar.xz\nCD34 *toolchain.7z\n\n"

    assert provision.parse_checksums(text) == {
        "zephyr-sdk-1.0.1_linux-x86_64_minimal.tar.xz": "ab12", "toolchain.7z": "cd34",
    }


def test_the_project_filter_keeps_only_the_named_modules():
    assert provision.project_filter(["hal_nordic", "mcuboot"]) == "-.*,+hal_nordic,+mcuboot"


def _sdk_release(tmp_path: Path, monkeypatch) -> tuple[Path, sdk.Versions, dict[str, bytes]]:
    """A root with a Zephyr tree that asks for SDK 1.2.3, and a fake release of that SDK."""
    versions = sdk.load_versions(_versions_file(tmp_path))
    root = tmp_path / "root"
    (root / "zephyr-v9.9.9" / "zephyr").mkdir(parents=True)
    (root / "zephyr-v9.9.9" / "zephyr" / "SDK_VERSION").write_text("1.2.3\n", encoding="utf-8")
    monkeypatch.setattr(sdk, "zephyr_sdk_host", lambda: "linux-x86_64")
    archives = tmp_path / "release"
    archives.mkdir()
    minimal = _tar_xz(archives / "minimal", {"zephyr-sdk-1.2.3/sdk_version": b"1.2.3\n"})
    gcc = _tar_xz(archives / "gcc", {"arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc": b"#!/bin/sh\n"})
    files = {
        "zephyr-sdk-1.2.3_linux-x86_64_minimal.tar.xz": minimal.read_bytes(),
        "toolchain_gnu_linux-x86_64_arm-zephyr-eabi.tar.xz": gcc.read_bytes(),
    }
    return root, versions, files


def _serve(monkeypatch, files: dict[str, bytes], sums: dict[str, str]) -> list[str]:
    """Replace the download with a copy from *files*; the sha256.sum holds *sums*.  Return the asked URLs."""
    asked: list[str] = []

    def download(url: str, target: Path) -> str:
        asked.append(url)
        name = url.rsplit("/", 1)[1]
        data = "".join(f"{digest}  {file}\n" for file, digest in sums.items()).encode() if name == "sha256.sum" else files[name]
        target.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(provision, "_download", download)
    return asked


def test_the_sdk_is_unpacked_when_each_archive_has_the_checksum_of_the_release(tmp_path, monkeypatch):
    root, versions, files = _sdk_release(tmp_path, monkeypatch)
    asked = _serve(monkeypatch, files, {name: hashlib.sha256(data).hexdigest() for name, data in files.items()})

    sdk_dir = provision.ensure_zephyr_sdk(root, versions)

    assert sdk_dir == root / "zephyr-sdk-1.2.3"
    assert (sdk_dir / "sdk_version").read_text() == "1.2.3\n"
    assert (sdk_dir / "gnu" / "arm-zephyr-eabi" / "bin" / "arm-zephyr-eabi-gcc").is_file()
    assert asked[0] == "https://example.invalid/releases/v1.2.3/sha256.sum"
    assert not [path for path in root.iterdir() if path.name.startswith(".staging")]
    # A second run finds each part and downloads nothing.
    asked.clear()
    provision.ensure_zephyr_sdk(root, versions)
    assert asked == []


def test_an_archive_with_another_checksum_is_refused_and_removed(tmp_path, monkeypatch):
    root, versions, files = _sdk_release(tmp_path, monkeypatch)
    _serve(monkeypatch, files, {name: "0" * 64 for name in files})

    with pytest.raises(provision.ProvisionError, match="is not the SHA-256 of the release"):
        provision.ensure_zephyr_sdk(root, versions)
    assert not (root / "downloads" / "zephyr-sdk-1.2.3_linux-x86_64_minimal.tar.xz").exists()
    assert not (root / "zephyr-sdk-1.2.3").exists()


def test_an_archive_that_writes_outside_its_directory_is_refused(tmp_path):
    archive = _tar_xz(tmp_path / "evil.tar.xz", {"../outside": b"x"})

    with pytest.raises(provision.ProvisionError, match="evil.tar.xz") as raised:
        provision.extract(archive, tmp_path / "target")
    assert isinstance(raised.value.__cause__, tarfile.FilterError)
    assert not (tmp_path / "outside").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows has no execute bit and no link without privilege")
def test_a_zip_keeps_the_mode_and_the_links_of_its_members(tmp_path):
    """ZipFile.extractall gave idf.py of ESP-IDF the mode 0644: its export script could not run it."""
    import stat
    import zipfile

    archive = tmp_path / "tree.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        program = zipfile.ZipInfo("tree/tools/idf.py")
        program.external_attr = (stat.S_IFREG | 0o755) << 16
        zipped.writestr(program, "#!/usr/bin/env python\n")
        link = zipfile.ZipInfo("tree/build.sh")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(link, "tools/idf.py")

    provision.extract(archive, tmp_path / "out")

    assert stat.S_IMODE((tmp_path / "out/tree/tools/idf.py").stat().st_mode) == 0o755
    assert os.readlink(tmp_path / "out/tree/build.sh") == "tools/idf.py"


@pytest.mark.parametrize(("member", "link"), [("../outside", None), ("tree/escape", "../../outside")])
def test_a_zip_member_or_link_outside_the_target_is_refused(tmp_path, member, link):
    import stat
    import zipfile

    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        info = zipfile.ZipInfo(member)
        if link is not None:
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(info, link or "x")

    if link is not None and os.name == "nt":
        pytest.skip("Windows writes a link as a file with its text, as git does")
    with pytest.raises(provision.ProvisionError, match="outside the target"):
        provision.extract(archive, tmp_path / "target")
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize(("system", "machine", "expected"), [
    ("linux", "x86_64", "arduino-cli_1.6.0_Linux_64bit.tar.gz"),
    ("linux", "aarch64", "arduino-cli_1.6.0_Linux_ARM64.tar.gz"),
    ("darwin", "arm64", "arduino-cli_1.6.0_macOS_ARM64.tar.gz"),
    ("darwin", "x86_64", "arduino-cli_1.6.0_macOS_64bit.tar.gz"),
    ("win32", "AMD64", "arduino-cli_1.6.0_Windows_64bit.zip"),
])
def test_the_archive_of_arduino_cli_of_each_host(system, machine, expected):
    """The names of the assets of the release 1.6.0 (GitHub, 2026-11-14)."""
    assert provision.arduino_cli_archive("1.6.0", system, machine) == expected


def test_a_host_without_a_build_of_arduino_cli_is_an_error():
    with pytest.raises(provision.ProvisionError, match="armv7l"):
        provision.arduino_cli_archive("1.6.0", "linux", "armv7l")


def test_an_archive_that_cannot_be_read_is_an_error_of_the_install(tmp_path):
    archive = tmp_path / "broken.tar.xz"
    archive.write_bytes(b"not an archive")

    with pytest.raises(provision.ProvisionError, match="broken.tar.xz"):
        provision.extract(archive, tmp_path / "target")


def test_the_minimal_bundle_goes_into_an_sdk_directory_that_holds_a_toolchain(tmp_path, monkeypatch):
    """A run that stopped after a toolchain left zephyr-sdk-<version>/gnu/ without the minimal bundle."""
    root, versions, files = _sdk_release(tmp_path, monkeypatch)
    toolchain_bin = root / "zephyr-sdk-1.2.3" / "gnu" / "arm-zephyr-eabi" / "bin"
    toolchain_bin.mkdir(parents=True)
    (toolchain_bin / "arm-zephyr-eabi-gcc").write_bytes(b"kept")
    asked = _serve(monkeypatch, files, {name: hashlib.sha256(data).hexdigest() for name, data in files.items()})

    sdk_dir = provision.ensure_zephyr_sdk(root, versions)

    assert (sdk_dir / "sdk_version").is_file()
    assert (toolchain_bin / "arm-zephyr-eabi-gcc").read_bytes() == b"kept"
    assert [url.rsplit("/", 1)[1] for url in asked] == ["sha256.sum", "zephyr-sdk-1.2.3_linux-x86_64_minimal.tar.xz"]


def test_a_workspace_directory_without_west_is_made_again(tmp_path, monkeypatch):
    """Only the last step of west init (the rename) makes a workspace; a directory without .west is a left-over."""
    versions = sdk.load_versions(_versions_file(tmp_path))
    root = tmp_path / "root"
    workspace = root / "zephyr-v9.9.9"
    (workspace / "modules").mkdir(parents=True)
    (workspace / "modules" / "left-over").write_text("x", encoding="utf-8")
    commands: list[list[str]] = []

    def run(argv, env, cwd=None):
        commands.append([str(arg) for arg in argv])
        if argv[1] == "init":
            (Path(argv[-1]) / ".west").mkdir(parents=True)
            # The tree of Zephyr, with the minimum of Python that the install checks.
            _zephyr_tree(Path(argv[-1]) / "zephyr", "3.12")

    monkeypatch.setattr(provision, "_program", lambda root, name: Path(name))
    monkeypatch.setattr(provision, "_run", run)

    assert provision.ensure_zephyr_workspace(root, versions, Path("python"), {}) == workspace
    assert (workspace / ".west").is_dir()
    assert not (workspace / "modules").exists()
    assert commands[0][:2] == ["west", "init"]
    assert [command[1] for command in commands[1:]] == ["config", "update", "-m"]


def test_a_relative_root_is_made_absolute(monkeypatch, tmp_path):
    """The install and the compilers run in other working directories."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(sdk.SDK_ROOT_ENV, "relative-root")

    assert sdk.sdk_root.__wrapped__() == tmp_path / "relative-root"


# ── The pre-test ───────────────────────────────────────────────────────────


def test_the_pretest_names_an_install_of_other_versions(tmp_path, monkeypatch):
    from tests import _environment

    root = tmp_path / "root"
    root.mkdir()
    sdk.write_stamp(root, {"fingerprint": "old"})
    monkeypatch.setattr(sdk, "sdk_root", lambda: root)
    # tmp_path is longer than a root may be on Windows; the check of the
    # length has its own tests (test_a_root_too_long_for_windows...).
    monkeypatch.setattr(sdk, "windows_root_too_long", lambda root: None)

    problems = _environment._test_sdks(tmp_path)

    assert [problem.component for problem in problems] == ["test SDKs"]
    assert "other versions than tests/sdk/versions.toml" in problems[0].message
    assert sdk.PROVISION_COMMAND in problems[0].repair


def test_the_pretest_checks_the_sdks_only_for_a_test_that_takes_a_toolchain():
    """The SDKs are about 6 GB; a run of tests that compile nothing must not need them."""
    from types import SimpleNamespace

    from tests import _environment

    def item(*fixtures: str, marker: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            fixturenames=list(fixtures), get_closest_marker=lambda name: name if name == marker else None,
        )

    assert _environment.selected_components([item("tmp_path")]) == set()
    assert _environment.selected_components([item("tmp_path"), item("toolchain")]) == {_environment.TEST_SDKS}
    # A test of a native project of the SDKs needs them by its marker, whatever its fixtures.
    assert _environment.selected_components([item("tmp_path", marker="sdk_project")]) == {_environment.TEST_SDKS}
    # The extended set only for a test of its marker.
    assert _environment.selected_components([item("tmp_path", marker="extra_platforms")]) == {
        _environment.TEST_SDKS, _environment.EXTRA_SDKS,
    }


def test_the_pretest_names_a_root_without_an_install(tmp_path, monkeypatch):
    from tests import _environment

    monkeypatch.setattr(sdk, "sdk_root", lambda: tmp_path / "none")
    monkeypatch.setattr(sdk, "windows_root_too_long", lambda root: None)

    problems = _environment._test_sdks(tmp_path)

    assert [problem.message for problem in problems] == [f"no complete install of the test SDKs in {tmp_path / 'none'}"]


# ── The toolchain helper ───────────────────────────────────────────────────

_C_SOURCE = "#include <stdint.h>\n#include <string.h>\nint length(const char *text) { return (int)strlen(text); }\n"
_CXX_SOURCE = (
    "#include <functional>\n#include <vector>\n"
    "int count() { std::vector<int> v{1, 2}; std::function<int()> f = [&] { return (int)v.size(); }; return f(); }\n"
)


def test_a_fixture_is_compiled_and_its_entries_name_the_compiler_of_the_sdk(tmp_path, toolchain):
    names = _toolchains.write_sources(tmp_path, {"a.c": _C_SOURCE, "src/b.cpp": _CXX_SOURCE})

    compile_commands = _toolchains.build_fixture(tmp_path, names, toolchain)

    entries = json.loads(compile_commands.read_text(encoding="utf-8"))
    # Windows: shutil.which gives the extension in the case of PATHEXT (.EXE).
    assert [Path(entry["arguments"][0]).name.lower().removesuffix(".exe") for entry in entries] == [
        f"{toolchain.prefix}gcc", f"{toolchain.prefix}g++",
    ]
    assert all(Path(entry["arguments"][0]).is_absolute() for entry in entries)
    assert all(list(toolchain.flags) == entry["arguments"][1:1 + len(toolchain.flags)] for entry in entries)
    # The object files of the check go to a temporary directory: the
    # project holds no file that the test did not write.
    assert not list(tmp_path.rglob("*.o"))


def test_a_fixture_that_does_not_compile_fails_the_test(tmp_path, toolchain):
    names = _toolchains.write_sources(tmp_path, {"broken.c": "int f(void) { return undeclared; }\n"})

    with pytest.raises(pytest.fail.Exception, match="the compiler does not compile .*broken.c"):
        _toolchains.build_fixture(tmp_path, names, toolchain)


def test_a_fixture_of_an_error_must_not_compile(tmp_path, toolchain):
    """A test of a parse error names its fixture in `broken`; a fixture that compiles tests no error."""
    _toolchains.write_sources(tmp_path, {"bad.c": "int f(void) { return undeclared; }\n", "good.c": "int g;\n"})
    entry = {"directory": str(tmp_path), "file": "bad.c", "arguments": toolchain.arguments("bad.c")}
    good = {"directory": str(tmp_path), "file": "good.c", "arguments": toolchain.arguments("good.c")}

    _toolchains.write_compile_commands(tmp_path / "compile_commands.json", [entry], broken={"bad.c": "undeclared"})
    with pytest.raises(pytest.fail.Exception, match="must not compile, and it compiles"):
        _toolchains.write_compile_commands(tmp_path / "compile_commands.json", [good], broken={"good.c": "undeclared"})
    with pytest.raises(pytest.fail.Exception, match="refuses bad.c, but not with 'no_such_header.h'"):
        _toolchains.write_compile_commands(
            tmp_path / "compile_commands.json", [entry], broken={"bad.c": "no_such_header.h"},
        )


def test_a_language_other_than_c_or_cxx_is_refused(toolchain):
    """`cpp`, the language name of a CompilationUnit, selected gcc without a word."""
    with pytest.raises(ValueError, match="give 'c' or 'c\\+\\+'"):
        toolchain.arguments("a.h", language="cpp")


def test_the_arguments_name_the_driver_of_the_language(toolchain):
    c_driver = Path(toolchain.arguments("a.c")[0]).name.lower()
    cxx_driver = Path(toolchain.arguments("a.c", language="c++")[0]).name.lower()

    assert c_driver.removesuffix(".exe") == f"{toolchain.prefix}gcc"
    assert cxx_driver.removesuffix(".exe") == f"{toolchain.prefix}g++"
    assert toolchain.arguments("a.c", "-DX", output="a.o")[-5:] == ["-DX", "-c", "a.c", "-o", "a.o"]


def test_a_source_is_written_with_lf(tmp_path):
    _toolchains.write_sources(tmp_path, {"a.c": "int a;\nint b;\n"})

    assert (tmp_path / "a.c").read_bytes() == b"int a;\nint b;\n"


@pytest.mark.libclang
def test_the_unit_has_the_target_of_the_driver_and_parses_without_errors(tmp_path, toolchain):
    """The path of a user unit: the driver of the SDK gives the target and the headers, libclang parses.

    WHY both languages: the C++ library of an embedded toolchain is a
    different part (libstdc++ for picolibc or newlib) from the C library.
    """
    from fw_context_mcp.indexer._libclang import cindex
    from fw_context_mcp.indexer.symbols import _get_index

    expected_arch = {"arm-zephyr": "arm", "riscv-esp": "riscv32"}[toolchain.id]
    _toolchains.write_sources(tmp_path, {"a.c": _C_SOURCE, "b.cpp": _CXX_SOURCE})
    for name in ("a.c", "b.cpp"):
        unit = _toolchains.unit_for(tmp_path, name, toolchain)
        targets = [arg.split("=", 1)[1] for arg in unit.clang_args if arg.startswith("--target=")]
        tu = _get_index().parse(str(unit.file), args=unit.clang_args)
        errors = [d.spelling for d in tu.diagnostics if d.severity >= cindex.Diagnostic.Error]

        assert targets, f"{name}: no --target in the arguments of libclang"
        assert all(target.startswith(expected_arch) for target in targets), targets
        assert errors == [], f"{name}: {errors}"


# ── the pinned Python of the venv ─────────────────────────────────────────


@pytest.mark.parametrize(("platform_name", "machine", "expected"), [
    ("linux", "x86_64", "x86_64-unknown-linux-gnu"),
    ("linux", "aarch64", "aarch64-unknown-linux-gnu"),
    ("darwin", "arm64", "aarch64-apple-darwin"),
    ("darwin", "x86_64", "x86_64-apple-darwin"),
    ("win32", "AMD64", "x86_64-pc-windows-msvc"),
])
def test_the_archive_of_the_python_of_each_host(tmp_path, monkeypatch, platform_name, machine, expected):
    monkeypatch.setattr(sdk.sys, "platform", platform_name)
    monkeypatch.setattr(sdk.platform, "machine", lambda: machine)
    # The C library of the host that the test plays, not of the host that runs it.
    monkeypatch.setattr(sdk.platform, "libc_ver", lambda: ("glibc", "2.40") if platform_name == "linux" else ("", ""))
    versions = sdk.load_versions(_versions_file(tmp_path))

    assert sdk.python_archive_name(versions, sdk.python_host()) == (
        f"cpython-3.12.16+20261110-{expected}-install_only.tar.gz"
    )


def test_a_linux_without_glibc_is_an_error(monkeypatch):
    """The glibc build of python-build-standalone and the toolchains of the Zephyr SDK do not run on musl."""
    monkeypatch.setattr(sdk.sys, "platform", "linux")
    monkeypatch.setattr(sdk.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(sdk.platform, "libc_ver", lambda: ("", ""))

    with pytest.raises(sdk.SdkError, match="need a Linux with glibc"):
        sdk.python_host()


def test_a_host_without_a_build_of_python_is_an_error(monkeypatch):
    monkeypatch.setattr(sdk.sys, "platform", "sunos5")
    with pytest.raises(sdk.SdkError, match="python-build-standalone has no build"):
        sdk.python_host()


def _zephyr_tree(zephyr: Path, minimum: str) -> None:
    """Write the ``python.cmake`` of the Zephyr tree *zephyr* with the minimum of Python *minimum*."""
    modules = zephyr / "cmake" / "modules"
    modules.mkdir(parents=True)
    (modules / "python.cmake").write_text(f"set(PYTHON_MINIMUM_REQUIRED {minimum})\n", encoding="utf-8")


def test_the_minimum_python_of_zephyr_comes_from_its_tree(tmp_path):
    versions = sdk.load_versions(_versions_file(tmp_path))
    _zephyr_tree(tmp_path / "root" / "zephyr-v9.9.9" / "zephyr", "3.12")

    assert sdk.zephyr_python_minimum(tmp_path / "root", versions) == (3, 12)


def test_a_pinned_python_older_than_zephyr_needs_is_refused(tmp_path):
    """A new revision of Zephyr can need a newer Python: the install names the pin to change."""
    versions = sdk.load_versions(_versions_file(tmp_path))
    _zephyr_tree(tmp_path / "root" / "zephyr-v9.9.9" / "zephyr", "3.13")

    with pytest.raises(provision.ProvisionError, match=r"needs Python 3\.13 or newer.*pins Python 3\.12\.16"):
        provision.check_zephyr_python(tmp_path / "root", versions)


def _python_release(tmp_path: Path, monkeypatch, version: str = "3.12.16") -> tuple[Path, sdk.Versions, list[str]]:
    """A release of python-build-standalone with one archive for this host; return the root, the versions and the asked URLs.

    The program of the archive prints *version*, as the real one answers
    ``python_version_of``.
    """
    from tests._stubs import write_python_tool

    versions = sdk.load_versions(_versions_file(tmp_path))
    built = tmp_path / "built"
    windows = sdk.sys.platform == "win32"
    program = write_python_tool(built / "python" / ("" if windows else "bin"), "python" if windows else "python3",
                                f"print({version!r})\n")
    archive = tmp_path / "python.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(built / "python", arcname="python")
    name = sdk.python_archive_name(versions, sdk.python_host())
    data = archive.read_bytes()
    files = {name: data, "SHA256SUMS": f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()}
    assert program.exists()
    asked: list[str] = []

    def download(url: str, target: Path) -> str:
        asked.append(url)
        content = files[urllib.parse.unquote(url.rsplit("/", 1)[1])]
        target.write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    monkeypatch.setattr(provision, "_download", download)
    return tmp_path / "root", versions, asked


def test_the_pinned_python_is_installed_from_the_release(tmp_path, monkeypatch):
    root, versions, asked = _python_release(tmp_path, monkeypatch)

    program = provision.ensure_python(root, versions)

    assert program == sdk.python_program(root)
    assert sdk.python_version_of(program) == "3.12.16"
    assert asked[0] == "https://example.invalid/python/20261110/SHA256SUMS"
    # "+" of the name is quoted in the URL, as in the URL of the release.
    assert "%2B20261110" in asked[1]
    assert not (root / ".staging-python").exists()
    assert sdk.python_build_installed(root) == "3.12.16+20261110"
    # A second run finds the Python and downloads nothing.
    asked.clear()
    provision.ensure_python(root, versions)
    assert asked == []


def test_a_python_of_another_release_is_replaced(tmp_path, monkeypatch):
    """A new release of the same version (a fix of a library): the version alone does not tell them apart."""
    root, versions, asked = _python_release(tmp_path, monkeypatch)
    provision.ensure_python(root, versions)
    (sdk.python_dir(root) / sdk.PYTHON_BUILD_FILE).write_text("3.12.16+20250101\n", encoding="utf-8")
    asked.clear()

    provision.ensure_python(root, versions)

    assert len(asked) == 2
    assert sdk.python_build_installed(root) == "3.12.16+20261110"


def test_a_venv_of_another_base_python_is_not_of_the_pinned_python(tmp_path):
    """A venv of a host Python of the same version depends on that host Python."""
    root = tmp_path / "root"
    (root / "venv").mkdir(parents=True)
    (root / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.16\n", encoding="utf-8")
    assert not sdk.venv_is_of_the_pinned_python(root)

    sdk.python_program(root).parent.mkdir(parents=True)
    (root / "venv" / "pyvenv.cfg").write_text(
        f"home = {sdk.python_program(root).parent}\nversion = 3.12.16\n", encoding="utf-8",
    )
    assert sdk.venv_is_of_the_pinned_python(root)


def test_a_python_of_another_version_in_the_archive_is_refused(tmp_path, monkeypatch):
    root, versions, _ = _python_release(tmp_path, monkeypatch, version="3.11.9")

    with pytest.raises(provision.ProvisionError, match="is 3.11.9, not 3.12.16"):
        provision.ensure_python(root, versions)


def test_the_pretest_names_a_venv_of_another_python(tmp_path, monkeypatch):
    """A venv of Python 3.11 ran a west that the build of Zephyr refused (Windows, 2026-11-13)."""
    from tests import _environment

    root = tmp_path / "root"
    root.mkdir()
    sdk.write_stamp(root, {"fingerprint": sdk.fingerprint()})
    monkeypatch.setattr(sdk, "sdk_root", lambda: root)
    monkeypatch.setattr(sdk, "windows_root_too_long", lambda root: None)
    monkeypatch.setattr(sdk, "python_version_of", lambda program: "3.11.9")
    sdk.venv_python(root).parent.mkdir(parents=True)
    sdk.venv_python(root).write_text("", encoding="utf-8")
    (root / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    problems = _environment._test_sdks(tmp_path)

    messages = [problem.message for problem in problems if "runs Python" in problem.message]
    assert messages == [
        f"the venv {sdk.venv_bin(root).parent} runs Python 3.11.9 of {Path('/usr/bin')}, not the Python "
        f"{sdk.load_versions().python.version} of tests/sdk/versions.toml in {sdk.python_dir(root)}"
    ]
