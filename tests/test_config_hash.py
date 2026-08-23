"""Tests for fw_context_mcp.indexer.config_hash."""

from pathlib import Path

from fw_context_mcp.indexer.config_hash import _normalize_entry
from fw_context_mcp.indexer.manifest import build_scope, compute_config_hash


class TestNormalizeEntry:
    def test_removes_compiler_binary(self):
        entry = {
            "file": "src/main.cpp",
            "arguments": ["arm-none-eabi-g++", "-std=c++14", "-DFOO=1", "src/main.cpp"],
        }
        result = _normalize_entry(entry)
        assert "-std=c++14" in result["args"]
        assert "-DFOO=1" in result["args"]
        assert "arm-none-eabi-g++" not in result["args"]

    def test_removes_transient_flags(self):
        entry = {
            "file": "src/main.cpp",
            "arguments": ["g++", "-std=c++14", "-MD", "-MP", "-o", "build/main.o", "src/main.cpp"],
        }
        result = _normalize_entry(entry)
        assert "-MD" not in result["args"]
        assert "-MP" not in result["args"]
        assert "-o" not in result["args"]
        assert "build/main.o" not in result["args"]

    def test_keeps_compilation_flags(self):
        entry = {
            "file": "src/main.cpp",
            "arguments": ["g++", "-std=c++14", "-DFOO=1", "-Wall", "-Os", "src/main.cpp"],
        }
        result = _normalize_entry(entry)
        assert "-std=c++14" in result["args"]
        assert "-DFOO=1" in result["args"]
        assert "-Wall" in result["args"]
        assert "-Os" in result["args"]

    def test_args_are_sorted(self):
        """Arguments should be sorted for deterministic hashing."""
        entry = {
            "file": "src/main.cpp",
            "arguments": ["g++", "-c", "-b", "-a", "src/main.cpp"],
        }
        result = _normalize_entry(entry)
        assert result["args"] == sorted(result["args"])

    def test_normalizes_file_path(self):
        entry = {"file": "./src/main.cpp", "arguments": ["g++", "src/main.cpp"]}
        result = _normalize_entry(entry)
        assert result["file"] == "src/main.cpp"

    def test_handles_command_string(self):
        entry = {
            "file": "main.c",
            "command": "gcc -std=c11 -O2 -o build/main.o main.c",
        }
        result = _normalize_entry(entry)
        assert "-std=c11" in result["args"]
        assert "-O2" in result["args"]

    def test_expands_response_files(self, tmpdir):
        rsp = tmpdir / "flags.rsp"
        rsp.write_text("-DEXTRA=1\n")
        entry = {
            "file": "main.cpp",
            "arguments": ["g++", f"@{rsp}", "-std=c++14", "main.cpp"],
        }
        result = _normalize_entry(entry)
        assert "-DEXTRA=1" in result["args"]


class _FakeUnit:
    def __init__(self, f: str, clang_args: list[str] | None = None):
        self.file = Path(f)
        self.clang_args = ["-I/inc", "-DFOO=1"] if clang_args is None else list(clang_args)
        self.raw_entry = None
        self.directory = "/tmp"


_ROOT = Path("/tmp/proj")


def _cfg_hash(units: list[_FakeUnit]) -> str:
    return compute_config_hash(units, _ROOT, "projid", None)


class TestConfigHashIdentity:
    """config_hash identifies the compilation DIALECT, not the file set.

    It answers one question: "could the same source text compile to something
    different now?"  Macros and target/standard flags can change that answer;
    the list of translation units and the include search path cannot.

    Keeping the TU list in the hash was the root of the whole reuse /
    migration machinery: adding one .c file minted a new build identity for
    every unchanged TU, and their rows then had to be moved to it.  Per-file
    staleness (``files.source_hash`` / ``files.flags_hash``) is what detects
    real change; see :func:`compute_flags_hash`.
    """

    _BASE = ["-std=gnu++14", "-mcpu=cortex-m4", "-I/inc", "-DFOO=1"]

    def test_adding_a_tu_does_not_change_the_hash(self):
        one = [_FakeUnit("/tmp/proj/src/main.c", self._BASE)]
        two = one + [_FakeUnit("/tmp/proj/src/extra.c", self._BASE)]
        assert _cfg_hash(two) == _cfg_hash(one)

    def test_removing_a_tu_does_not_change_the_hash(self):
        two = [
            _FakeUnit("/tmp/proj/src/main.c", self._BASE),
            _FakeUnit("/tmp/proj/src/extra.c", self._BASE),
        ]
        one = two[:1]
        assert _cfg_hash(one) == _cfg_hash(two)

    def test_renaming_a_source_file_does_not_change_the_hash(self):
        before = [_FakeUnit("/tmp/proj/src/old_name.c", self._BASE)]
        after = [_FakeUnit("/tmp/proj/src/new_name.c", self._BASE)]
        assert _cfg_hash(after) == _cfg_hash(before)

    def test_changing_a_define_changes_the_hash(self):
        base = [_FakeUnit("/tmp/proj/src/main.c", self._BASE)]
        changed = [_FakeUnit("/tmp/proj/src/main.c", [*self._BASE, "-DEXTRA=1"])]
        assert _cfg_hash(changed) != _cfg_hash(base)

    def test_changing_a_define_value_changes_the_hash(self):
        a = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1"])]
        b = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=2"])]
        assert _cfg_hash(a) != _cfg_hash(b)

    def test_changing_the_standard_or_target_changes_the_hash(self):
        base = [_FakeUnit("/tmp/proj/src/main.c", ["-std=gnu++14", "-mcpu=cortex-m4"])]
        std = [_FakeUnit("/tmp/proj/src/main.c", ["-std=gnu++17", "-mcpu=cortex-m4"])]
        cpu = [_FakeUnit("/tmp/proj/src/main.c", ["-std=gnu++14", "-mcpu=cortex-m33"])]
        assert _cfg_hash(std) != _cfg_hash(base)
        assert _cfg_hash(cpu) != _cfg_hash(base)

    def test_changing_an_include_path_does_not_change_the_hash(self):
        """Include paths are per-TU state, not build identity.

        HA_Boiler has 14 distinct flag-sets but 208 distinct include paths —
        the include search path is where the per-directory variance lives.
        A changed path is caught by the TU's own flags_hash, which reparses
        that TU instead of invalidating the whole build.
        """
        base = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1", "-I/inc"])]
        moved = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1", "-I/other/inc"])]
        added = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1", "-I/inc", "-I/more"])]
        assert _cfg_hash(moved) == _cfg_hash(base)
        assert _cfg_hash(added) == _cfg_hash(base)

    def test_include_change_is_still_caught_per_tu(self):
        """The guarantee that makes the previous test safe."""
        from fw_context_mcp.indexer.config_hash import compute_flags_hash

        base = {"file": "src/main.c",
                "arguments": ["g++", "-DFOO=1", "-I/inc", "src/main.c"]}
        moved = {"file": "src/main.c",
                 "arguments": ["g++", "-DFOO=1", "-I/other/inc", "src/main.c"]}
        assert compute_flags_hash(moved) != compute_flags_hash(base)

    def test_per_tu_flag_variance_does_not_multiply_the_hash(self):
        """One TU carrying an extra -I must not change build identity.

        Measured shape of a real build: FM has 216 TUs with an identical -D
        set and 5 flag-sets that differ only in -I.
        """
        uniform = [
            _FakeUnit("/tmp/proj/src/a.c", ["-DFOO=1", "-I/inc"]),
            _FakeUnit("/tmp/proj/src/b.c", ["-DFOO=1", "-I/inc"]),
        ]
        one_differs = [
            _FakeUnit("/tmp/proj/src/a.c", ["-DFOO=1", "-I/inc"]),
            _FakeUnit("/tmp/proj/src/b.c", ["-DFOO=1", "-I/inc", "-I/extra"]),
        ]
        assert _cfg_hash(one_differs) == _cfg_hash(uniform)

    def test_a_define_on_a_single_tu_changes_the_hash(self):
        """A macro anywhere flips #ifdef somewhere — that IS build identity.

        HA_Boiler really does this: ARDUINO_CORE_BUILD is defined for 46 of
        its 114 TUs.
        """
        uniform = [
            _FakeUnit("/tmp/proj/src/a.c", ["-DFOO=1"]),
            _FakeUnit("/tmp/proj/src/b.c", ["-DFOO=1"]),
        ]
        one_defines = [
            _FakeUnit("/tmp/proj/src/a.c", ["-DFOO=1"]),
            _FakeUnit("/tmp/proj/src/b.c", ["-DFOO=1", "-DARDUINO_CORE_BUILD=1"]),
        ]
        assert _cfg_hash(one_defines) != _cfg_hash(uniform)

    def test_a_source_filename_in_the_args_does_not_reach_the_hash(self):
        """Guard against the file list returning through the back door.

        A source filename is neither a ``-D`` nor a path-bearing flag, so a
        naive classifier would file it under "dialect" and make the hash
        depend on the TU set again.  ``normalize_args`` strips it today; this
        test fails if that ever stops.
        """
        clean = [_FakeUnit("/tmp/proj/src/main.c", ["-std=c11", "-DFOO=1"])]
        with_name = [
            _FakeUnit("/tmp/proj/src/main.c",
                      ["-std=c11", "-DFOO=1", "src/main.c"]),
        ]
        with_output = [
            _FakeUnit("/tmp/proj/src/main.c",
                      ["-std=c11", "-DFOO=1", "-o", "build/main.o"]),
        ]
        assert _cfg_hash(with_name) == _cfg_hash(clean)
        assert _cfg_hash(with_output) == _cfg_hash(clean)

    def test_tu_order_does_not_change_the_hash(self):
        a = _FakeUnit("/tmp/proj/src/a.c", self._BASE)
        b = _FakeUnit("/tmp/proj/src/b.c", self._BASE)
        assert _cfg_hash([a, b]) == _cfg_hash([b, a])

    def test_hash_is_idempotent(self):
        units = [_FakeUnit("/tmp/proj/src/main.c", self._BASE)]
        assert _cfg_hash(units) == _cfg_hash(units)


class TestBuildScope:
    def test_empty_scope(self):
        assert build_scope() == []
        assert build_scope("", "", {}) == []

    def test_variant_image(self):
        assert build_scope("nrf52840", "app") == ["nrf52840", "app"]

    def test_env_is_sorted_and_prefixed(self):
        tokens = build_scope("nrf52840", "", {"B": "2", "A": "1"})
        assert tokens == ["nrf52840", 'env:{"A": "1", "B": "2"}']


class TestConfigHashScope:
    def _hash(self, scope=None):
        unit = _FakeUnit("/tmp/proj/src/main.c")
        return compute_config_hash([unit], Path("/tmp/proj"), "projid", None, scope=scope)

    def test_scope_changes_hash(self):
        base = self._hash()
        v1 = self._hash(scope=["nrf52840", "app"])
        v2 = self._hash(scope=["nrf52840", "stage0"])
        assert base != v1
        assert v1 != v2

    def test_same_scope_is_idempotent(self):
        assert self._hash(scope=["nrf52840", "app"]) == self._hash(scope=["nrf52840", "app"])


