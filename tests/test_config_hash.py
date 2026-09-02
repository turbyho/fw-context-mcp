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

    def test_an_in_project_include_move_keeps_the_hash(self):
        """An include path INSIDE the project is per-unit state, not identity.

        This test used ``-I/inc`` against ``_ROOT = /tmp/proj`` before, so
        both of its paths were OUTSIDE the project — and after the path pass
        started keeping those, its assertion had the wrong sign.  It is
        rewritten on in-project paths, which is what it always meant to test.

        The ESP32 project has 14 distinct flag-sets but 208 distinct include paths,
        and the Mbed project has 268 in-project ones.  A directory that moves
        inside the project does not change what the compiler reads.
        """
        base = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1", "-I/tmp/proj/inc"])]
        moved = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1", "-I/tmp/proj/other/inc"])]
        added = [
            _FakeUnit("/tmp/proj/src/main.c",
                      ["-DFOO=1", "-I/tmp/proj/inc", "-I/tmp/proj/more"]),
        ]
        assert _cfg_hash(moved) == _cfg_hash(base)
        assert _cfg_hash(added) == _cfg_hash(base)

    def test_a_toolchain_move_changes_the_hash(self):
        """An include path OUTSIDE the project names the toolchain, and it counts.

        This is the out-of-project half of the old test, with its sign
        inverted.  A toolchain update used to cause NO reparse at all: Tier 1
        compares the source mtime, the staleness pass skipped everything
        outside project_root, and config_hash held the toolchain only by
        accident through the mispaired bare tokens.

        Two toolchains must map to two config_hash values.  Rows parsed
        against different system headers cannot share one build.
        """
        base = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1", "-I/opt/gcc-9/include"])]
        moved = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1", "-I/opt/gcc-13/include"])]
        assert _cfg_hash(moved) != _cfg_hash(base)

    def test_a_separated_sysroot_is_kept(self):
        """``--sysroot <dir>`` is a toolchain root, and the join makes it one token."""
        base = [_FakeUnit("/tmp/proj/src/main.c", ["--sysroot", "/opt/gcc-9/arm-none-eabi"])]
        other = [_FakeUnit("/tmp/proj/src/main.c", ["--sysroot", "/opt/gcc-13/arm-none-eabi"])]
        assert _cfg_hash(other) != _cfg_hash(base)

    def test_a_build_dir_include_is_still_dropped(self):
        """Build output is not identity, wherever it sits."""
        base = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1", "-I/elsewhere/BUILD/gen"])]
        moved = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1", "-I/elsewhere/BUILD/other"])]
        assert compute_config_hash(moved, _ROOT, "projid", ["BUILD/"]) == \
            compute_config_hash(base, _ROOT, "projid", ["BUILD/"])

    def test_adding_a_tu_with_a_new_sdk_include_keeps_the_hash(self):
        """Out-of-project paths go in as the INTERSECTION, not the union.

        Regression against the union.  On the Zephyr project one generated unit,
        validate_binding_headers.c, carries 11 devicetree binding headers, so
        a union would mint a new build identity on every overlay edit.
        Measured over 2 052 units in 7 real builds: the union has 7 units
        whose presence changes the identity, the intersection has 0.
        """
        shared = ["-DFOO=1", "-I/opt/sdk/include"]
        two = [
            _FakeUnit("/tmp/proj/src/a.c", list(shared)),
            _FakeUnit("/tmp/proj/src/b.c", list(shared)),
        ]
        three = [
            _FakeUnit("/tmp/proj/src/a.c", list(shared)),
            _FakeUnit("/tmp/proj/src/b.c", list(shared)),
            _FakeUnit("/tmp/proj/src/gen.c", [*shared, "-I/opt/sdk/generated/dts"]),
        ]
        assert _cfg_hash(three) == _cfg_hash(two)

    def test_a_path_every_tu_shares_is_in_the_hash(self):
        """The other side of the intersection: a shared path DOES count."""
        without = [
            _FakeUnit("/tmp/proj/src/a.c", ["-DFOO=1"]),
            _FakeUnit("/tmp/proj/src/b.c", ["-DFOO=1"]),
        ]
        with_sdk = [
            _FakeUnit("/tmp/proj/src/a.c", ["-DFOO=1", "-I/opt/sdk/include"]),
            _FakeUnit("/tmp/proj/src/b.c", ["-DFOO=1", "-I/opt/sdk/include"]),
        ]
        assert _cfg_hash(with_sdk) != _cfg_hash(without)

    def test_a_build_with_no_units_does_not_crash(self):
        """``None`` for "no unit seen yet" removes the empty-build special case."""
        assert len(_cfg_hash([])) == 64

    def test_include_change_is_still_caught_per_tu(self):
        """What flags_hash can tell apart, and what nobody asks it.

        Its assertion is true and it stays: compute_flags_hash DOES give two
        different values for two different include paths.  Its docstring said
        that this is "the guarantee that makes the previous test safe", and
        that part is wrong three times over.

        1. Tier 1 compares the source file mtime and stops there.  A changed
           include path moves no source mtime, so nothing ever reads
           flags_hash.
        2. compute_flags_hash expanded a response file against the process
           CWD, so a relative @rsp expanded to [] with no error and no log.
           Fixed in this commit.
        3. On the Mbed project that response file holds exactly the include paths
           this sentence is about — 269 tokens on all 873 entries.

        An out-of-project include path therefore belongs in config_hash, and
        that is what the path pass now does.
        """
        from fw_context_mcp.indexer.config_hash import compute_flags_hash

        base = {"file": "src/main.c",
                "arguments": ["g++", "-DFOO=1", "-I/inc", "src/main.c"]}
        moved = {"file": "src/main.c",
                 "arguments": ["g++", "-DFOO=1", "-I/other/inc", "src/main.c"]}
        assert compute_flags_hash(moved) != compute_flags_hash(base)

    def test_per_tu_flag_variance_does_not_multiply_the_hash(self):
        """One TU carrying an extra -I must not change build identity.

        Measured shape of a real build: the STM32 project has 216 TUs with an identical -D
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

        The ESP32 project really does this: ARDUINO_CORE_BUILD is defined for 46 of
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




class TestTheFlagValueJoin:
    """A flag and its value must survive the sort as one token.

    The pre-pass sorted the arguments before the path pass paired a flag with
    args[i + 1].  After the sort no value stands beside its own flag: values
    start with '/' (0x2F) and flags with '-' (0x2D), so sorted() puts every
    path after every flag.  Measured on the Zephyr project, one build of 257
    translation units: -isystem consumed -mabi=aapcs 230 times, and 30 bare
    directories stayed in the dialect set with no flag.
    """

    def test_separated_and_attached_include_form_agree(self):
        sep = [_FakeUnit("/tmp/proj/src/main.c", ["-isystem", "/opt/sdk/inc", "-mabi=aapcs"])]
        att = [_FakeUnit("/tmp/proj/src/main.c", ["-isystem/opt/sdk/inc", "-mabi=aapcs"])]
        assert _cfg_hash(sep) == _cfg_hash(att)

    def test_two_cpu_targets_do_not_collide(self):
        """The measured collision: -mcpu=cortex-m4 and -m33 gave the SAME hash.

        A sorted -isystem ate the neighbouring -mabi=aapcs, and with the
        target flag consumed the two builds became indistinguishable.
        """
        m4 = [_FakeUnit("/tmp/proj/src/main.c",
                        ["-isystem", "/opt/sdk/inc", "-mcpu=cortex-m4"])]
        m33 = [_FakeUnit("/tmp/proj/src/main.c",
                         ["-isystem", "/opt/sdk/inc", "-mcpu=cortex-m33"])]
        assert _cfg_hash(m4) != _cfg_hash(m33)

    def test_language_override_is_kept(self):
        c = [_FakeUnit("/tmp/proj/src/main.c", ["-x", "c"])]
        cpp = [_FakeUnit("/tmp/proj/src/main.c", ["-x", "c++"])]
        assert _cfg_hash(c) != _cfg_hash(cpp)

    def test_a_dangling_include_flag_eats_nothing(self):
        """A bare -I at the end must consume the flag alone.

        The old branch guarded on ``i + 1 < len(args)`` and then took
        args[i + 1] — a sorted neighbour that belongs to no flag.
        """
        dangling = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1", "-std=c11", "-I"])]
        without = [_FakeUnit("/tmp/proj/src/main.c", ["-DFOO=1", "-std=c11"])]
        assert _cfg_hash(dangling) == _cfg_hash(without)

    def test_two_undef_macros_do_not_collide(self):
        a = [_FakeUnit("/tmp/proj/src/main.c", ["-U", "FOO"])]
        b = [_FakeUnit("/tmp/proj/src/main.c", ["-U", "BAR"])]
        assert _cfg_hash(a) != _cfg_hash(b)

    def test_a_param_value_is_kept(self):
        """The interaction between the join and the dropped extension whitelist.

        A whitelist of "flags that take a value" would forget --param, and
        the extension test that used to catch the orphan is gone.  The value
        would then vanish and two builds with different inlining limits would
        share one hash.  Measured on the STM32 project:
        "--param max-inline-insns-single=500".
        """
        a = [_FakeUnit("/tmp/proj/src/main.c",
                       ["--param", "max-inline-insns-single=500"])]
        b = [_FakeUnit("/tmp/proj/src/main.c",
                       ["--param", "max-inline-insns-single=900"])]
        assert _cfg_hash(a) != _cfg_hash(b)

    def test_a_flag_value_starting_with_a_dash_is_not_joined(self):
        """Two flags in a row stay two flags."""
        pair = [_FakeUnit("/tmp/proj/src/main.c", ["-Wall", "-Werror"])]
        only_wall = [_FakeUnit("/tmp/proj/src/main.c", ["-Wall"])]
        only_werror = [_FakeUnit("/tmp/proj/src/main.c", ["-Werror"])]
        assert _cfg_hash(pair) != _cfg_hash(only_wall)
        assert _cfg_hash(pair) != _cfg_hash(only_werror)

    def test_an_output_pair_is_dropped_whole(self):
        """-o and its value go together, or the join would smuggle the value in.

        _is_dialect_token() recognises the BARE -o.  Once joined, "-obuild/x.o"
        is neither in _DROP_WITH_ARG nor a path flag, so it would land in the
        dialect set and make the hash depend on the output path.
        """
        clean = [_FakeUnit("/tmp/proj/src/main.c", ["-std=c11", "-DFOO=1"])]
        with_output = [_FakeUnit("/tmp/proj/src/main.c",
                                 ["-std=c11", "-DFOO=1", "-o", "build/main.o"])]
        assert _cfg_hash(with_output) == _cfg_hash(clean)


class TestNoBareTokenReachesTheDialectSet:
    """A dialect token always starts with '-'.

    The suffix whitelist that used to guard this could not stay complete, and
    a suffix outside the list put a filename into the hash.
    """

    def test_no_bare_token_reaches_the_dialect_set(self):
        """Call compute_config_hash directly, not through parse_compile_commands.

        Going through the parser would let normalize_args strip the bare
        tokens first, and the test would then prove nothing about this
        function's own guard.
        """
        clean = [_FakeUnit("/tmp/proj/src/main.c", ["-std=c11"])]
        stray = [_FakeUnit("/tmp/proj/src/main.c", ["-std=c11", "config.pb.h"])]
        assert _cfg_hash(stray) == _cfg_hash(clean)

    def test_an_unmatched_map_flag_is_kept(self):
        """A deliberate change in the other direction.

        The suffix test used to drop -Wl,-Map=firmware.map because of its
        ".map" suffix.  Without build_dir_patterns that matches it, the flag
        now stays.  Such a name is stable per build, so the risk is low, but
        it is a behaviour change and it is recorded here on purpose.
        """
        a = [_FakeUnit("/tmp/proj/src/main.c", ["-Wl,-Map=firmware.map"])]
        b = [_FakeUnit("/tmp/proj/src/main.c", ["-Wl,-Map=other.map"])]
        assert _cfg_hash(a) != _cfg_hash(b)


class TestResponseFileExpansion:
    """A relative @rsp resolves against the ENTRY's directory, not the CWD.

    Measured on the Mbed project: all 873 entries carry a relative @./BUILD/...
    response file with 269 -I tokens inside, and expand_response_file returns
    [] for a file it cannot find, with no error and no log.  flags_hash
    therefore depended on the directory fw-context ran from, and the whole
    build read as changed after a `cd`.
    """

    def test_a_relative_response_file_is_expanded_from_the_entry_directory(
        self, tmp_path, monkeypatch,
    ):
        from fw_context_mcp.indexer.config_hash import _normalize_entry

        build = tmp_path / "BUILD"
        build.mkdir()
        (build / "flags.rsp").write_text("-DFROM_RSP=1 -I/opt/sdk/inc\n")
        # The probe: the process CWD must NOT be the entry directory, or the
        # test passes whether or not the fix is there.
        monkeypatch.chdir(tmp_path.parent)

        norm = _normalize_entry({
            "file": "src/main.c",
            "directory": str(tmp_path),
            "arguments": ["gcc", "@./BUILD/flags.rsp", "src/main.c"],
        })

        assert "-DFROM_RSP=1" in norm["args"]
        assert "-I/opt/sdk/inc" in norm["args"]

    def test_flags_hash_does_not_depend_on_the_process_cwd(self, tmp_path, monkeypatch):
        from fw_context_mcp.indexer.config_hash import compute_flags_hash

        build = tmp_path / "BUILD"
        build.mkdir()
        (build / "flags.rsp").write_text("-DFROM_RSP=1\n")
        entry = {
            "file": "src/main.c",
            "directory": str(tmp_path),
            "arguments": ["gcc", "@./BUILD/flags.rsp", "src/main.c"],
        }

        monkeypatch.chdir(tmp_path)
        from_project = compute_flags_hash(entry)
        monkeypatch.chdir(tmp_path.parent)
        from_elsewhere = compute_flags_hash(entry)

        assert from_project == from_elsewhere

    def test_an_absolute_response_file_still_expands(self, tmp_path, monkeypatch):
        from fw_context_mcp.indexer.config_hash import _normalize_entry

        rsp = tmp_path / "flags.rsp"
        rsp.write_text("-DABS=1\n")
        monkeypatch.chdir(tmp_path.parent)

        norm = _normalize_entry({
            "file": "src/main.c",
            "directory": str(tmp_path / "other"),
            "arguments": ["gcc", f"@{rsp}", "src/main.c"],
        })

        assert "-DABS=1" in norm["args"]

    def test_an_entry_without_a_directory_still_works(self, tmp_path, monkeypatch):
        """cwd=None stays legitimate for an absolute token."""
        from fw_context_mcp.indexer.config_hash import _normalize_entry

        rsp = tmp_path / "flags.rsp"
        rsp.write_text("-DABS=1\n")

        norm = _normalize_entry({
            "file": "src/main.c",
            "arguments": ["gcc", f"@{rsp}", "src/main.c"],
        })

        assert "-DABS=1" in norm["args"]
