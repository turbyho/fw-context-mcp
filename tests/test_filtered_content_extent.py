"""``files.content`` must span the whole file and hold only live code.

``files.content`` backs ``read_file`` and ``search_content``, and both
promise the code that compiles for the active build.  Three defects broke
that promise, and each class below pins one of them.

**The extent of the text.**  The assembly loop used to stop at
``max(active_lines)``.  The last line of a file is often inactive — a blank
line, or a line of a branch the build does not take — thus the stored text
ended early and ``read_file`` reported a line count smaller than the file.

**Inactive branches.**  The active lines came from the tokens of the TU and
from the extent of every cursor.  Tokens come from a raw lexer, which gives
tokens for a dead ``#if`` branch also, and an extent is one continuous range
of lines, thus it carries a dead block inside a function body.  Dead code
therefore reached both tools as live code, and an audit could approve code
that the compiler never sees.  ``collect_skipped_lines`` now decides.

**Comments and directives.**  A token contributed one line — the line it
starts on — and a header carried no token at all.  A block comment thus
kept its ``/*`` and lost its ``*/``, and a reader took the live code below
it as commented out.  Every comment of every header was gone with it.
``_token_lines_of_files`` now adds the lines of each token in full.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.libclang


def _fill_content(root: Path, db_path: Path, files: dict[str, str], main: str) -> dict[str, str]:
    """Index *files* through the real content-fill pass and return stored text.

    *files* maps a project-relative path to its text; *main* names the entry
    in it that compile_commands.json points at.  The return maps the same
    relative paths to the ``files.content`` the pass stored, so a test can
    compare stored text against the text it wrote to disk.
    """
    from fw_context_mcp.indexer.compile_commands import parse as parse_cc
    from fw_context_mcp.indexer.db import (
        open_db,
        transaction,
        upsert_build_config,
        upsert_file,
        upsert_project,
    )
    from fw_context_mcp.indexer.ops import _build_filtered_file_content

    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    cc = root / "compile_commands.json"
    cc.write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "file": str(root / main),
                    "arguments": ["cc", "-c", str(root / main), "-I", str(root / "src")],
                }
            ]
        ),
        encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(root))
            upsert_build_config(conn, "ch", "pid", str(cc))
            # The rows must exist before the content pass: `remaining` counts
            # files with empty content, and a zero count takes the fast path
            # that returns before the fill loop this test exercises.
            for rel in files:
                lang = "cpp" if Path(rel).suffix in {".cpp", ".hpp", ".cc"} else "c"
                upsert_file(conn, "ch", rel, lang, mtime=1.0)

        unit = next(iter(parse_cc(cc)))
        with transaction(conn):
            _build_filtered_file_content(conn, unit, "ch", root)

        return {
            row["path"]: row["content"]
            for row in conn.execute(
                "SELECT path, content FROM files WHERE config_hash='ch'"
            ).fetchall()
        }
    finally:
        conn.close()


class TestTrailingDirectivesSurvive:
    """A file whose tail holds only directives must keep its full length."""

    def test_include_guard_endif_is_not_cut(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        header = (
            "#ifndef API_H_\n"          # 1
            "#define API_H_\n"          # 2
            "\n"                        # 3
            "int kept(void);\n"         # 4
            "\n"                        # 5
            "#endif // API_H_\n"        # 6 — last token-free line
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/api.h": header,
                "src/main.c": '#include "api.h"\nint main(void) { return kept(); }\n',
            },
            main="src/main.c",
        )

        content = stored["src/api.h"]
        assert content.splitlines().__len__() == len(header.splitlines()), (
            "the stored text must be as long as the file — read_file reports "
            "len(content.splitlines()) as `lines`, and a short answer there "
            "tells the caller the file ends before it does"
        )
        assert "kept" in content, "an active declaration must survive the filter"

    def test_line_numbers_still_align_after_the_last_token(self, tmp_path: Path) -> None:
        """Padding the tail must not shift the lines that carry code."""
        root = tmp_path / "proj"
        header = (
            "#ifndef CFG_H_\n"          # 1
            "#define CFG_H_\n"          # 2
            "int marker_line_3(void);\n"  # 3
            "#endif\n"                  # 4
            "\n"                        # 5
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/cfg.h": header,
                "src/main.c": '#include "cfg.h"\nint main(void) { return marker_line_3(); }\n',
            },
            main="src/main.c",
        )

        lines = stored["src/cfg.h"].splitlines()
        assert len(lines) == 5
        assert "marker_line_3" in lines[2], (
            "line 3 must still be line 3 — the tail is padded with blank "
            "lines, never inserted before existing content"
        )


def _assert_lines(content: str, source: str, *, blank: set[int]) -> None:
    """Check each line of *content* against *source*, by line number.

    *blank* names the lines that must hold no text.  Every other line must
    match the same line of *source* exactly, which is what keeps a citation
    of ``file:line`` correct after the filter runs.
    """
    stored = content.splitlines()
    original = source.splitlines()
    assert len(stored) == len(original), (
        f"the stored text must be as long as the file: "
        f"{len(stored)} lines stored, {len(original)} on disk"
    )
    for number, (got, want) in enumerate(zip(stored, original, strict=True), start=1):
        if number in blank:
            assert got.strip() == "", (
                f"line {number} is inside an inactive #if branch, thus it "
                f"must be blank, but it holds {got!r}"
            )
        else:
            assert got == want, (
                f"line {number} is live code, thus it must survive the "
                f"filter unchanged: got {got!r}, want {want!r}"
            )


class TestInactiveBranchesAreFiltered:
    """A branch the preprocessor did not take must leave no text behind."""

    def test_dead_branch_at_file_scope_in_a_cpp_is_blank(self, tmp_path: Path) -> None:
        """The raw lexer used to keep every line of this block.

        The tokens of a TU come from ``clang_tokenize``, which does no
        preprocessing.  For the main file of the TU that was the only source
        of active lines, thus nothing was filtered at all.
        """
        root = tmp_path / "proj"
        main = (
            "int live_before(void) { return 1; }\n"   # 1
            "\n"                                      # 2
            "#ifdef FEATURE_OFF\n"                    # 3
            "int dead(void) { return 42; }\n"         # 4
            "#endif\n"                                # 5
            "\n"                                      # 6
            "int live_after(void) { return 2; }\n"    # 7
        )
        stored = _fill_content(
            root, tmp_path / "index.db", {"src/main.cpp": main}, main="src/main.cpp"
        )

        _assert_lines(stored["src/main.cpp"], main, blank={3, 4, 5})
        assert "dead" not in stored["src/main.cpp"]

    def test_dead_branch_inside_a_function_body_is_blank(self, tmp_path: Path) -> None:
        """The regression this whole change exists for.

        A dead block inside a body sits inside the extent of that body, and
        an extent is one continuous range of lines.  Both the stored file
        text and the stored body therefore kept it.  Measured on a real
        firmware project: a fan-control block behind an ``#ifdef`` whose
        macro lived only in a commented-out ``#define`` was reported as
        live code, and an audit raised a finding against it.
        """
        root = tmp_path / "proj"
        main = (
            "int host(void)\n"                        # 1
            "{\n"                                     # 2
            "    int x = 0;\n"                        # 3
            "#ifdef FEATURE_OFF\n"                    # 4
            "    x = dead_call();\n"                  # 5
            "#endif\n"                                # 6
            "    return x;\n"                         # 7
            "}\n"                                     # 8
        )
        stored = _fill_content(
            root, tmp_path / "index.db", {"src/main.cpp": main}, main="src/main.cpp"
        )

        _assert_lines(stored["src/main.cpp"], main, blank={4, 5, 6})
        assert "dead_call" not in stored["src/main.cpp"]

    def test_live_else_branch_survives(self, tmp_path: Path) -> None:
        """A skipped range ends on the directive line, thus it takes no code."""
        root = tmp_path / "proj"
        main = (
            "#define FEATURE_ON 1\n"                  # 1
            "int host(void)\n"                        # 2
            "{\n"                                     # 3
            "    int x = 0;\n"                        # 4
            "#ifdef FEATURE_OFF\n"                    # 5
            "    x = dead_call();\n"                  # 6
            "#else\n"                                 # 7
            "    x = live_call();\n"                  # 8
            "#endif\n"                                # 9
            "    return x;\n"                         # 10
            "}\n"                                     # 11
        )
        stored = _fill_content(
            root, tmp_path / "index.db", {"src/main.cpp": main}, main="src/main.cpp"
        )

        # The range covers #ifdef through #else.  Line 8 is live and line 9
        # closes a branch that was taken, thus both stay.
        _assert_lines(stored["src/main.cpp"], main, blank={5, 6, 7})
        assert "live_call" in stored["src/main.cpp"]
        assert "dead_call" not in stored["src/main.cpp"]

    def test_nested_dead_branch_keeps_the_live_inner_branch(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        main = (
            "#define OUTER_ON 1\n"                    # 1
            "int host(void)\n"                        # 2
            "{\n"                                     # 3
            "    int x = 0;\n"                        # 4
            "#ifdef OUTER_ON\n"                       # 5
            "  #ifdef INNER_OFF\n"                    # 6
            "    x = dead_call();\n"                  # 7
            "  #else\n"                               # 8
            "    x = live_call();\n"                  # 9
            "  #endif\n"                              # 10
            "#else\n"                                 # 11
            "    x = other_dead_call();\n"            # 12
            "#endif\n"                                # 13
            "    return x;\n"                         # 14
            "}\n"                                     # 15
        )
        stored = _fill_content(
            root, tmp_path / "index.db", {"src/main.cpp": main}, main="src/main.cpp"
        )

        _assert_lines(stored["src/main.cpp"], main, blank={6, 7, 8, 11, 12, 13})
        assert "live_call" in stored["src/main.cpp"]
        assert "dead_call" not in stored["src/main.cpp"]
        assert "other_dead_call" not in stored["src/main.cpp"]

    def test_define_inside_a_live_branch_stays_searchable(self, tmp_path: Path) -> None:
        """``search_content`` must still reach a ``#define``.

        A ``#define`` belongs to no definition, thus ``files.content`` is the
        only place that holds it.  The filter must not take one that the
        preprocessor read.
        """
        root = tmp_path / "proj"
        header = (
            "#ifndef CFG_H_\n"                        # 1
            "#define CFG_H_\n"                        # 2
            "#ifdef FEATURE_OFF\n"                    # 3
            "  #define DEAD_LIMIT 99\n"               # 4
            "#else\n"                                 # 5
            "  #define LIVE_LIMIT 7\n"                # 6
            "#endif\n"                                # 7
            "int kept(void);\n"                       # 8
            "#endif\n"                                # 9
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/cfg.h": header,
                "src/main.c": '#include "cfg.h"\nint main(void) { return kept() + LIVE_LIMIT; }\n',
            },
            main="src/main.c",
        )

        content = stored["src/cfg.h"]
        assert "LIVE_LIMIT" in content, "a #define the preprocessor read must survive"
        assert "DEAD_LIMIT" not in content, "a #define in a dead branch must not"
        assert "kept" in content
        assert len(content.splitlines()) == len(header.splitlines())

    def test_header_whose_declarations_are_all_dead_keeps_its_length(
        self, tmp_path: Path
    ) -> None:
        """Every declaration goes, and the file still measures its own length.

        ``read_file`` reports ``len(content.splitlines())`` as the length of
        the file, thus the blank lines must stay in place of the dead ones.

        The include guard is what puts this file in the fill loop at all: the
        ``#define`` of the guard is an active line, and a file with no active
        line never reaches the loop.  See the note in the report — a header
        with no guard and no live line is a separate hole.
        """
        root = tmp_path / "proj"
        header = (
            "#ifndef DEAD_H_\n"                       # 1
            "#define DEAD_H_\n"                       # 2
            "#ifdef FEATURE_OFF\n"                    # 3
            "int dead_one(void);\n"                   # 4
            "int dead_two(void);\n"                   # 5
            "#endif\n"                                # 6
            "#endif\n"                                # 7
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/dead.h": header,
                "src/main.c": '#include "dead.h"\nint main(void) { return 0; }\n',
            },
            main="src/main.c",
        )

        content = stored["src/dead.h"]
        assert "dead_one" not in content
        assert "dead_two" not in content
        assert len(content.splitlines()) == len(header.splitlines()), (
            "the length of the file must survive — read_file reports it"
        )

    def test_dead_branch_inside_an_inline_body_in_a_header(self, tmp_path: Path) -> None:
        """A header filtered its file scope already, but not its bodies.

        For a header the tokens of the TU give nothing — ``get_tokens`` covers
        the main file only — thus the extent of every cursor was the single
        source of active lines.  That source drops a dead declaration at file
        scope, because a skipped declaration gets no cursor.  It cannot drop a
        dead block inside a body: the body has one extent, and the extent is
        one continuous range of lines.
        """
        root = tmp_path / "proj"
        header = (
            "#ifndef HOST_H_\n"                       # 1
            "#define HOST_H_\n"                       # 2
            "static inline int hdr_host(void)\n"      # 3
            "{\n"                                     # 4
            "    int y = 0;\n"                        # 5
            "#ifdef FEATURE_OFF\n"                    # 6
            "    y = dead_call();\n"                  # 7
            "#endif\n"                                # 8
            "    return y;\n"                         # 9
            "}\n"                                     # 10
            "#endif\n"                                # 11
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/host.h": header,
                "src/main.c": '#include "host.h"\nint main(void) { return hdr_host(); }\n',
            },
            main="src/main.c",
        )

        content = stored["src/host.h"]
        assert "dead_call" not in content, (
            "a dead block inside the body of an inline function must go — "
            "the extent of the body used to carry it"
        )
        assert "return y;" in content, "the live code of the body must stay"
        assert len(content.splitlines()) == len(header.splitlines())


class TestAHeaderReachedTwiceKeepsItsLiveCode:
    """A second inclusion must not turn the live body of a header into dead code.

    ``clang_getAllSkippedRanges`` reports the ranges of the WHOLE translation
    unit, and one file can enter a TU more than once.  When the include guard
    of a header is not the controlling macro of that header, clang cannot
    apply its multiple-include optimization, thus it really preprocesses the
    file again — and the second pass skips the whole guarded block, because
    the guard macro is defined by then.

    A map that unions the ranges of both inclusions therefore reports the
    live body of the header as skipped, and the content pass blanks it out.
    The answer must be the INTERSECTION: a line is dead only when every
    inclusion skipped it.

    A guard stops being the controlling macro on any of these, and both
    appear in real SDK headers:

    * another directive follows the ``#endif`` of the guard;
    * an ``#include`` comes before the guard.
    """

    def test_include_before_the_guard_keeps_the_body(self, tmp_path: Path) -> None:
        """The common shape: a header that includes something before guarding."""
        root = tmp_path / "proj"
        two = (
            '#include "dep.h"\n'                       # 1  kills the optimization
            "#ifndef TWO_H\n"                          # 2
            "#define TWO_H\n"                          # 3
            "static inline int two_helper(void)\n"     # 4
            "{\n"                                      # 5
            "    return 42;\n"                         # 6
            "}\n"                                      # 7
            "#endif\n"                                 # 8
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/dep.h": "#define DEP_MARK 1\n",
                "src/two.h": two,
                "src/a.h": '#include "two.h"\n',
                "src/b.h": '#include "two.h"\n',
                "src/main.c": (
                    '#include "a.h"\n'
                    '#include "b.h"\n'
                    "int main(void) { return two_helper(); }\n"
                ),
            },
            main="src/main.c",
        )

        assert "two_helper" in stored["src/two.h"], (
            "the header entered the TU twice, and the second inclusion skipped "
            "its guarded block — but the first one compiled the body, thus the "
            "body is live code and must survive"
        )
        assert "return 42" in stored["src/two.h"]

    def test_directive_after_the_guard_keeps_the_body(self, tmp_path: Path) -> None:
        """The other shape: a trailing conditional after the guard's ``#endif``."""
        root = tmp_path / "proj"
        two = (
            "#ifndef TWO_H\n"                          # 1
            "#define TWO_H\n"                          # 2
            "static inline int two_helper(void)\n"     # 3
            "{\n"                                      # 4
            "    return 42;\n"                         # 5
            "}\n"                                      # 6
            "#endif\n"                                 # 7
            "#ifdef TWO_EXTRA\n"                       # 8  kills the optimization
            "int two_extra(void);\n"                   # 9
            "#endif\n"                                 # 10
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/two.h": two,
                "src/a.h": '#include "two.h"\n',
                "src/b.h": '#include "two.h"\n',
                "src/main.c": (
                    '#include "a.h"\n'
                    '#include "b.h"\n'
                    "int main(void) { return two_helper(); }\n"
                ),
            },
            main="src/main.c",
        )

        assert "two_helper" in stored["src/two.h"]
        assert "return 42" in stored["src/two.h"]
        assert "two_extra" not in stored["src/two.h"], (
            "TWO_EXTRA is defined in no inclusion, thus every inclusion "
            "skipped this branch and it stays dead"
        )

    def test_a_branch_dead_in_every_inclusion_stays_filtered(self, tmp_path: Path) -> None:
        """The intersection must not become a way of keeping dead code.

        Both inclusions skip this branch, thus it is dead under either rule.
        Without this test a fix could simply drop the filtering for any file
        that appears twice, and every dead branch of a shared header would
        come back.
        """
        root = tmp_path / "proj"
        two = (
            '#include "dep.h"\n'                       # 1
            "#ifndef TWO_H\n"                          # 2
            "#define TWO_H\n"                          # 3
            "static inline int live_one(void) { return 1; }\n"   # 4
            "#ifdef NEVER_SET\n"                       # 5
            "int dead_in_header(void);\n"              # 6
            "#endif\n"                                 # 7
            "#endif\n"                                 # 8
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/dep.h": "#define DEP_MARK 1\n",
                "src/two.h": two,
                "src/a.h": '#include "two.h"\n',
                "src/b.h": '#include "two.h"\n',
                "src/main.c": (
                    '#include "a.h"\n'
                    '#include "b.h"\n'
                    "int main(void) { return live_one(); }\n"
                ),
            },
            main="src/main.c",
        )

        assert "live_one" in stored["src/two.h"]
        assert "dead_in_header" not in stored["src/two.h"]


def _index_bodies(root: Path, db_path: Path, source: str) -> dict[str, str]:
    """Index one TU and return the stored body of each definition.

    Asks ``extract_all`` for the TU (``return_tu=True``), because the record
    of the skipped ranges comes from it.  A caller that passes an
    ``ExtractionResult`` with no TU gets unfiltered bodies, which is the
    documented fallback and not what these tests are about.
    """
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.db import open_db, upsert_build_config, upsert_project
    from fw_context_mcp.indexer.ops import store_symbols_for_unit
    from fw_context_mcp.indexer.symbols import extract_all

    src_dir = root / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    main = src_dir / "main.cpp"
    main.write_text(source, encoding="utf-8")

    conn = open_db(db_path)
    try:
        upsert_project(conn, "pid", "p", str(root))
        upsert_build_config(conn, "ch", "pid", str(root / "compile_commands.json"))

        unit = CompilationUnit(
            file=main, directory=src_dir, language="cpp", clang_args=["-std=c++17"],
        )
        result = extract_all(unit, with_refs=False, return_tu=True)
        store_symbols_for_unit(
            conn, unit, "ch", root,
            project_patterns=["src/%"],
            pre_parsed=result,
        )
        conn.commit()

        return {
            row["qualified_name"]: row["source"]
            for row in conn.execute(
                "SELECT qualified_name, source FROM symbols "
                "WHERE config_hash='ch' AND is_definition=1"
            ).fetchall()
        }
    finally:
        conn.close()


class TestStoredBodiesAreFiltered:
    """``symbols.source`` must hold only the code that compiles.

    The column backs ``get_source``, ``get_symbol_context``,
    ``search_bodies``, ``explain_symbol``, the embeddings and the LLM
    analysis.  It is built by slicing the raw lines of the file between the
    two ends of a libclang extent, thus a dead block inside a body used to
    reach every one of those tools as live code.
    """

    def test_dead_block_is_not_in_the_stored_body(self, tmp_path: Path) -> None:
        bodies = _index_bodies(
            tmp_path / "proj",
            tmp_path / "index.db",
            "int dead_call(void);\n"                  # 1
            "int host(void)\n"                        # 2
            "{\n"                                     # 3
            "    int x = 0;\n"                        # 4
            "#ifdef FEATURE_OFF\n"                    # 5
            "    x = dead_call();\n"                  # 6
            "#endif\n"                                # 7
            "    return x;\n"                         # 8
            "}\n",                                    # 9
        )

        assert "host" in bodies, f"host was not indexed: {sorted(bodies)}"
        body = bodies["host"]
        assert "dead_call" not in body, (
            "search_bodies matches this column, thus a dead call here answers "
            "a query with code that never compiles"
        )
        assert "return x;" in body, "the live code of the body must stay"

    def test_the_stored_body_keeps_its_line_count(self, tmp_path: Path) -> None:
        """A dead line becomes a blank line, so a cited line number holds.

        ``get_source`` numbers the lines of this column from ``line``.  A
        body that dropped its dead lines instead of blanking them would
        shift every line under them.
        """
        source = (
            "int dead_call(void);\n"                  # 1
            "int host(void)\n"                        # 2
            "{\n"                                     # 3
            "    int x = 0;\n"                        # 4
            "#ifdef FEATURE_OFF\n"                    # 5
            "    x = dead_call();\n"                  # 6
            "#endif\n"                                # 7
            "    return x;\n"                         # 8
            "}\n"                                     # 9
        )
        bodies = _index_bodies(tmp_path / "proj", tmp_path / "index.db", source)

        lines = bodies["host"].splitlines()
        # The extent of host() runs from line 2 to line 9 — eight lines.
        assert len(lines) == 8, f"expected the whole extent, got {lines}"
        assert lines[-1] == "}"
        assert lines[-2].strip() == "return x;", (
            "the last statement must still be the line before the closing "
            "brace — a dropped dead line would move it up"
        )


class TestContentHashMatchesTheStoredBody:
    """The change-detection hash must cover the text that the index stores."""

    def test_a_change_inside_a_dead_branch_does_not_change_the_hash(self) -> None:
        """Otherwise an untouched symbol reads as changed on every reindex.

        ``_detect_moved_symbols`` compares this hash to decide whether a
        symbol only moved.  A hash over text that the index does not hold
        would answer "changed" for an edit the build never sees, and the LLM
        analysis of that symbol would be thrown away.
        """
        from fw_context_mcp.indexer.ops import _compute_content_hash, _read_body

        before = [
            "int host(void)\n",       # 1
            "{\n",                    # 2
            "#ifdef FEATURE_OFF\n",   # 3
            "    old_dead();\n",      # 4
            "#endif\n",               # 5
            "    return 0;\n",        # 6
            "}\n",                    # 7
        ]
        after = list(before)
        after[3] = "    new_dead(1, 2, 3);\n"
        skipped = frozenset({3, 4, 5})

        assert _compute_content_hash(
            before, 1, 7, "int host()", "host", "", skipped
        ) == _compute_content_hash(
            after, 1, 7, "int host()", "host", "", skipped
        )
        # And the body itself holds neither version of the dead call.
        body = _read_body(after, 1, 7, skipped)
        assert "new_dead" not in body
        assert "old_dead" not in body
        assert "return 0;" in body

    def test_an_empty_skipped_set_stores_the_body_unchanged(self) -> None:
        """The common case must not pay for the filter.

        Most files hold no inactive branch, and the body must then be the
        exact text of the extent.
        """
        from fw_context_mcp.indexer.ops import _read_body

        lines = ["a\n", "b\n", "c\n", "d\n"]
        assert _read_body(lines, 1, 3, frozenset()) == "a\nb\nc\n"


class TestMultiLineTokensKeepEveryLine:
    """A token that spans lines must keep all of them, and not only its first.

    The active lines came from ``tok.location.line``, which is the line the
    token STARTS on.  A block comment is one token whose extent spans its
    whole range, thus the stored text held the ``/*`` and lost every line
    after it — the closing ``*/`` included.

    That answer is worse than no answer.  A reader saw a comment that no
    line closes, took the live code below it as commented out, and reported
    a defect against code that the build compiles.  Measured on real
    firmware: five fault handlers read as dead this way.
    """

    def test_a_block_comment_keeps_its_closing_marker(self, tmp_path: Path) -> None:
        """The defect that this class exists for."""
        root = tmp_path / "proj"
        source = (
            "int before(void);\n"        # 1
            "/*\n"                       # 2
            "void commented(void) {}\n"  # 3
            "*/\n"                       # 4
            "int after(void) { return 0; }\n"  # 5
        )
        stored = _fill_content(
            root, tmp_path / "index.db", {"src/main.c": source}, main="src/main.c"
        )

        _assert_lines(stored["src/main.c"], source, blank=set())
        assert "*/" in stored["src/main.c"], (
            "the closing marker must survive: without it the reader cannot "
            "tell where the comment ends, and reads live code as commented out"
        )

    def test_a_line_continuation_string_keeps_its_tail(self, tmp_path: Path) -> None:
        """A backslash at the end of a line makes one token of two lines."""
        root = tmp_path / "proj"
        source = (
            'const char *s = "abc\\\n'   # 1
            'def";\n'                    # 2
            "int after(void) { return 0; }\n"  # 3
        )
        stored = _fill_content(
            root, tmp_path / "index.db", {"src/main.c": source}, main="src/main.c"
        )

        _assert_lines(stored["src/main.c"], source, blank=set())

    def test_a_raw_string_literal_keeps_its_tail(self, tmp_path: Path) -> None:
        """A raw string literal spans lines without a continuation marker."""
        root = tmp_path / "proj"
        source = (
            'const char *r = R"(raw\n'   # 1
            'multi)";\n'                 # 2
            "int after(void) { return 0; }\n"  # 3
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {"src/main.cpp": source},
            main="src/main.cpp",
        )

        _assert_lines(stored["src/main.cpp"], source, blank=set())

    def test_a_comment_in_a_dead_branch_stays_blank(self, tmp_path: Path) -> None:
        """The opposite error must not appear with the fix.

        Tokenization is a raw lexer, thus it answers for a dead ``#if``
        branch also.  ``collect_skipped_lines`` is what removes that answer,
        and a comment inside a dead branch must stay as blank as the code
        beside it.
        """
        root = tmp_path / "proj"
        source = (
            "int live(void);\n"          # 1
            "#ifdef FEATURE_OFF\n"       # 2
            "/*\n"                       # 3
            " * dead comment\n"          # 4
            " */\n"                      # 5
            "int dead(void);\n"          # 6
            "#endif\n"                   # 7
            "int main(void) { return 0; }\n"  # 8
        )
        stored = _fill_content(
            root, tmp_path / "index.db", {"src/main.c": source}, main="src/main.c"
        )

        _assert_lines(stored["src/main.c"], source, blank={2, 3, 4, 5, 6, 7})
        assert "dead comment" not in stored["src/main.c"], (
            "a comment of an inactive branch is dead text — the build reads "
            "none of it, thus search_content must not match it"
        )


class TestHeadersKeepTheirComments:
    """A header must keep its comments and its directives.

    ``tu.cursor.get_tokens()`` answers for the MAIN file only, thus the
    active lines of a header came from cursor extents alone.  A comment
    belongs to no cursor, and neither does a conditional directive, thus
    both were absent from the stored text of every header.

    The loss is largest where it costs most: a vendor SDK keeps the
    description of each register and each bit in those comments, and
    ``read_file`` is the only way an agent can reach a C header.
    """

    def test_a_header_comment_survives(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        header = (
            "#ifndef API_H_\n"           # 1
            "#define API_H_\n"           # 2
            "/*\n"                       # 3
            " * RESET_REASON_POWER_ON: the board got power.\n"  # 4
            " */\n"                      # 5
            "int kept(void);\n"          # 6
            "#endif\n"                   # 7
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/api.h": header,
                "src/main.c": '#include "api.h"\nint main(void) { return kept(); }\n',
            },
            main="src/main.c",
        )

        _assert_lines(stored["src/api.h"], header, blank=set())
        assert "RESET_REASON_POWER_ON" in stored["src/api.h"], (
            "the text of a header comment must reach read_file and "
            "search_content — for a vendor register it is the only "
            "description that exists"
        )

    def test_a_header_directive_survives(self, tmp_path: Path) -> None:
        """An include guard must show, and not read as an empty line."""
        root = tmp_path / "proj"
        header = (
            "#ifndef GUARD_H_\n"         # 1
            "#define GUARD_H_\n"         # 2
            "int kept(void);\n"          # 3
            "#endif\n"                   # 4
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/guard.h": header,
                "src/main.c": '#include "guard.h"\nint main(void) { return kept(); }\n',
            },
            main="src/main.c",
        )

        _assert_lines(stored["src/guard.h"], header, blank=set())

    def test_a_dead_branch_of_a_header_stays_blank(self, tmp_path: Path) -> None:
        """The header path must filter dead code as the main file does."""
        root = tmp_path / "proj"
        header = (
            "#ifndef API_H_\n"           # 1
            "#define API_H_\n"           # 2
            "#ifdef FEATURE_OFF\n"       # 3
            "/* dead doc */\n"           # 4
            "int dead(void);\n"          # 5
            "#endif\n"                   # 6
            "int kept(void);\n"          # 7
            "#endif\n"                   # 8
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/api.h": header,
                "src/main.c": '#include "api.h"\nint main(void) { return kept(); }\n',
            },
            main="src/main.c",
        )

        _assert_lines(stored["src/api.h"], header, blank={3, 4, 5, 6})
        assert "dead doc" not in stored["src/api.h"]
