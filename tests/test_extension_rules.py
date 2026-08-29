"""The compiler's file-suffix rules, and the fact that case is part of them.

Everything here comes from the table in `man gcc` and was checked against
gcc and clang directly.  These are rules of the COMPILER: a project cannot
decide that `.C` means C.  What a project CAN decide — which extensions its
own build compiles — is derived from compile_commands.json instead.

The rules used to be compared through `.lower()`, which merged `.c` and
`.C` into one answer.  gcc reads the first as C and the second as C++, so
the lowercase comparison gave the opposite of what the compiler did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.utils import (
    CPP_EXTENSIONS,
    CPP_HEADER_EXTENSIONS,
    CPP_SOURCE_EXTENSIONS,
    C_SOURCE_EXTENSIONS,
    HEADER_EXTENSIONS,
    TU_EXTENSIONS,
)

# man gcc: "C++ source code that must be preprocessed."
GCC_CPP_SOURCES = [".cc", ".cp", ".cxx", ".cpp", ".CPP", ".c++", ".C"]
# man gcc: "C++ header file to be turned into a precompiled header or Ada spec."
GCC_CPP_HEADERS = [".hh", ".H", ".hp", ".hxx", ".hpp", ".HPP", ".h++", ".tcc"]


class TestTheGccTable:
    @pytest.mark.parametrize("ext", GCC_CPP_SOURCES)
    def test_every_cpp_source_suffix_is_covered(self, ext: str):
        assert ext in CPP_SOURCE_EXTENSIONS
        assert ext in TU_EXTENSIONS
        assert ext in CPP_EXTENSIONS

    @pytest.mark.parametrize("ext", GCC_CPP_HEADERS)
    def test_every_cpp_header_suffix_is_covered(self, ext: str):
        assert ext in CPP_HEADER_EXTENSIONS
        assert ext in HEADER_EXTENSIONS
        assert ext in CPP_EXTENSIONS

    def test_c_source_is_only_lowercase_c(self):
        assert C_SOURCE_EXTENSIONS == {".c"}

    def test_a_plain_h_is_not_a_cpp_header(self):
        """gcc calls .h "C, C++, ObjC or ObjC++ header" — it is ambiguous."""
        assert ".h" in HEADER_EXTENSIONS
        assert ".h" not in CPP_HEADER_EXTENSIONS

    def test_the_two_source_sets_do_not_overlap(self):
        assert not (C_SOURCE_EXTENSIONS & CPP_SOURCE_EXTENSIONS)


class TestCaseIsPartOfTheRule:
    """The regression: `.C` used to be classified as C."""

    def test_uppercase_c_is_cpp_and_lowercase_c_is_c(self):
        assert ".C" in CPP_SOURCE_EXTENSIONS
        assert ".C" not in C_SOURCE_EXTENSIONS
        assert ".c" in C_SOURCE_EXTENSIONS
        assert ".c" not in CPP_SOURCE_EXTENSIONS

    def test_a_case_form_gcc_does_not_know_is_absent(self):
        """`.CPP` is in the table; `.CXX` and `.Cpp` are not."""
        assert ".CPP" in CPP_SOURCE_EXTENSIONS
        for absent in (".CXX", ".Cpp", ".CC", ".C++"):
            assert absent not in CPP_SOURCE_EXTENSIONS, (
                f"{absent} is not in the suffix table of gcc; matching it "
                "would claim the compiler builds something it does not"
            )


class TestDetectLanguageFollowsTheTable:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [("a.c", "c"), ("a.C", "cpp"), ("a.cpp", "cpp"), ("a.CPP", "cpp"),
         ("a.cc", "cpp"), ("a.cxx", "cpp"), ("a.c++", "cpp")],
    )
    def test_the_suffix_decides(self, name: str, expected: str):
        from fw_context_mcp.indexer.compile_commands import _detect_language

        assert _detect_language(Path(name), []) == expected

    def test_an_unknown_suffix_falls_through_to_std(self):
        """A project building `.S` or something unusual still gets an answer."""
        from fw_context_mcp.indexer.compile_commands import _detect_language

        assert _detect_language(Path("startup.S"), ["-std=c++17"]) == "cpp"
        assert _detect_language(Path("startup.S"), ["-std=c11"]) == "c"


class TestIsSourceFileFollowsTheTable:
    @pytest.mark.parametrize("name", ["a.c", "a.C", "a.cpp", "a.CPP", "a.c++"])
    def test_a_translation_unit_is_recognised(self, name: str):
        from fw_context_mcp.indexer.compile_commands import _is_source_file

        assert _is_source_file(name)

    @pytest.mark.parametrize("name", ["a.h", "a.hpp", "a.o", "a.Cpp", "a.txt"])
    def test_anything_else_is_not(self, name: str):
        from fw_context_mcp.indexer.compile_commands import _is_source_file

        assert not _is_source_file(name)


class TestTheWatcherUsesTheSameRules:
    """The hand-written watch set was missing real extensions.

    Measured against the manifests of the test projects: `.tcc` appears 3 to
    18 times per project and `.c++` never appeared in the set at all.
    """

    @pytest.mark.parametrize("ext", [".c", ".C", ".cpp", ".c++", ".h", ".hpp", ".tcc", ".hh"])
    def test_a_source_or_header_is_watched(self, ext: str):
        from fw_context_mcp.mcp.daemon import _is_source_file

        assert _is_source_file(f"/proj/src/file{ext}")

    @pytest.mark.parametrize("ext", [".o", ".txt", ".md", ".json"])
    def test_anything_else_is_not_watched(self, ext: str):
        from fw_context_mcp.mcp.daemon import _is_source_file

        assert not _is_source_file(f"/proj/src/file{ext}")
