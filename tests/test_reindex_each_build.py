"""reindex_file re-parses a file in each build of the project that compiles it.

A project can hold several builds, each a ``(variant, image)`` pair with its
own compile_commands.json.  An edit changes the file on disk for every build
of the tree.  The tool re-parsed only the build that was indexed last: the
other builds kept the old symbols, and a file that only another build
compiles gave an error.

The fixture indexes two builds in-process (`runner.run`), as
`tests/test_branch_switch.py` does: build ``a`` compiles ``main.c``,
``shared.c`` and ``only_a.c``; build ``b`` compiles ``main.c`` and
``shared.c``.  ``shared.c`` includes ``common.h``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT_ID = "5e1ec7ab1d5e1ec7ab1d5e1ec7ab1d00"

SOURCES = {
    "src/main.c": "int main_value(void) { return 1; }\n",
    "src/shared.c": '#include "common.h"\nint shared_value(void) { return COMMON; }\n',
    "src/only_a.c": "int only_a_value(void) { return 3; }\n",
    "src/common.h": "#define COMMON 7\nint common_decl(void);\n",
}


def _units(root: Path, files: list[str], define: str) -> list[dict]:
    return [
        {"directory": str(root), "file": name, "arguments": ["cc", f"-D{define}", "-c", name, "-o", f"{name}.o"]}
        for name in files
    ]


@pytest.fixture
def two_builds(tmp_path: Path):
    """A project of the two builds ``a`` and ``b``; returns ``(root, db_path)``."""
    from fw_context_mcp.indexer.runner import run

    root = tmp_path / "proj"
    for name, text in SOURCES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    index_dir = tmp_path / "index"
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PROJECT_ID}"\n', encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text(f'[index]\ndb_dir = "{index_dir.as_posix()}"\n', encoding="utf-8")
    db_path = index_dir / PROJECT_ID / "index.db"
    for variant, files, define in (
        ("a", ["src/main.c", "src/shared.c", "src/only_a.c"], "BOARD_A"),
        ("b", ["src/main.c", "src/shared.c"], "BOARD_B"),
    ):
        cc = root / f"cc_{variant}.json"
        cc.write_text(json.dumps(_units(root, files, define)), encoding="utf-8")
        run(
            compile_commands=cc, db_path=db_path, project_root=root, project_id=PROJECT_ID,
            index_refs=False, index_embeddings=False, analyze_symbols=False, analyze_overrides=False,
            variant=variant,
        )
    return root, db_path


def _names(db_path: Path, variant: str) -> set[str]:
    """The symbol names of the build *variant*."""
    from fw_context_mcp.indexer.db import open_db

    conn = open_db(db_path, skip_integrity_check=True)
    try:
        return {
            row[0] for row in conn.execute(
                "SELECT s.name FROM symbols s JOIN build_configs b ON b.config_hash = s.config_hash"
                " WHERE b.variant = ?", (variant,),
            )
        }
    finally:
        conn.close()


def _reindex(root: Path, path: str, **selector) -> dict:
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

    return reindex_file_impl(path, str(root), with_analysis=False, **selector)


def test_a_file_of_the_two_builds_is_reparsed_in_each(two_builds):
    """Before: only the build indexed last read the edit."""
    root, db_path = two_builds
    (root / "src/main.c").write_text("int main_value(void) { return 1; }\nint added(void) { return 2; }\n", encoding="utf-8")

    result = _reindex(root, "src/main.c")

    assert [(row["variant"], row["translation_units"]) for row in result["builds"]] == [("a", 1), ("b", 1)], result
    assert result["translation_units"] == 2
    assert result["symbols_updated"] == sum(row["symbols_updated"] for row in result["builds"])
    assert "builds_without_file" not in result
    assert "added" in _names(db_path, "a") and "added" in _names(db_path, "b")


def test_a_file_of_one_build_names_the_other_build(two_builds):
    """Before: a file that only another build compiles gave an error."""
    root, db_path = two_builds
    (root / "src/only_a.c").write_text("int only_a_value(void) { return 3; }\nint only_a_added(void) { return 4; }\n", encoding="utf-8")

    result = _reindex(root, "src/only_a.c")

    assert "error" not in result, result
    assert [row["variant"] for row in result["builds"]] == ["a"]
    assert result["builds_without_file"] == ["b"]
    assert result["translation_units"] == 1, "one build: the top level is the result of that build"
    assert "only_a_added" in _names(db_path, "a")


def test_a_file_of_no_build_is_an_error(two_builds):
    root, _ = two_builds
    (root / "src/stray.c").write_text("int stray(void) { return 0; }\n", encoding="utf-8")

    result = _reindex(root, "src/stray.c")

    assert list(result) == ["error"], result
    assert "is not built by this project" in result["error"]


def test_the_selector_reparses_only_its_build(two_builds):
    root, db_path = two_builds
    (root / "src/main.c").write_text("int main_value(void) { return 1; }\nint only_in_b(void) { return 2; }\n", encoding="utf-8")

    result = _reindex(root, "src/main.c", variant="b")

    assert [row["variant"] for row in result["builds"]] == ["b"], result
    assert "only_in_b" in _names(db_path, "b")
    assert "only_in_b" not in _names(db_path, "a"), "the selector names one build"


def test_a_selector_of_no_build_gets_the_refusal(two_builds):
    root, _ = two_builds

    result = _reindex(root, "src/main.c", variant="nope")

    assert list(result) == ["error"], result
    assert 'variant="a"' in result["error"] and 'variant="b"' in result["error"], "the refusal lists the builds"


def test_a_header_goes_through_a_unit_of_each_build(two_builds):
    root, db_path = two_builds
    (root / "src/common.h").write_text("#define COMMON 7\nint common_decl(void);\nint common_added(void);\n", encoding="utf-8")

    result = _reindex(root, "src/common.h")

    assert [row["variant"] for row in result["builds"]] == ["a", "b"], result
    assert all(row["warning"].startswith("Header re-indexed via one TU") for row in result["builds"])
    assert result["warning"] == result["builds"][0]["warning"], "each build has it, thus the top level too"
    assert "common_added" in _names(db_path, "a") and "common_added" in _names(db_path, "b")


def test_a_deleted_file_is_removed_from_each_build(two_builds):
    root, db_path = two_builds
    assert "shared_value" in _names(db_path, "a") and "shared_value" in _names(db_path, "b")
    (root / "src/shared.c").unlink()

    result = _reindex(root, "src/shared.c")

    assert result["action"] == "deleted", result
    assert [row["variant"] for row in result["builds"]] == ["a", "b"]
    assert result["symbols_removed"] == sum(row["symbols_removed"] for row in result["builds"]) > 0
    assert "shared_value" not in _names(db_path, "a") and "shared_value" not in _names(db_path, "b")


def test_the_mcp_tool_takes_the_selector():
    """`reindex_file` passes the selector on; FastMCP rejects an argument that a tool does not take."""
    import inspect

    from fw_context_mcp.mcp.handlers.maintenance import reindex_file

    assert {"variant", "image"} <= set(inspect.signature(reindex_file).parameters)


def _fail_in(monkeypatch, *variants: str) -> None:
    """Make the store of each build of *variants* fail, as a lock timeout or a DB error does."""
    from fw_context_mcp.mcp.handlers import maintenance

    store = maintenance._reindex_parse_and_store

    def _store(conn, matching, cfg_data, *args, **kwargs):
        if cfg_data["variant"] in variants:
            return 0, {"error": "DB error during reindex: database is locked"}
        return store(conn, matching, cfg_data, *args, **kwargs)

    monkeypatch.setattr(maintenance, "_reindex_parse_and_store", _store)


def test_a_failed_build_is_named_at_the_top_level(two_builds, monkeypatch):
    """The sums leave out a failed build, thus the top level must not read as a complete update."""
    root, _ = two_builds
    _fail_in(monkeypatch, "b")
    (root / "src/common.h").write_text("#define COMMON 8\nint common_decl(void);\n", encoding="utf-8")

    result = _reindex(root, "src/common.h")

    assert result["failed_builds"] == ["b"], result
    assert result["builds"][1]["error"].startswith("DB error during reindex")
    assert result["translation_units"] == 1, "the sum of the builds that did not fail"
    assert result["warning"].startswith("Header re-indexed via one TU"), "the build that did not fail has it"


def test_an_update_that_fails_in_each_build_is_an_error(two_builds, monkeypatch):
    root, _ = two_builds
    _fail_in(monkeypatch, "a", "b")

    result = _reindex(root, "src/main.c")

    assert result["error"].startswith("fw-context could not update the file in each build"), result
    assert result["failed_builds"] == ["a", "b"]
    assert "translation_units" not in result and "action" not in result, "no sums of builds that failed"
    assert [row["variant"] for row in result["builds"]] == ["a", "b"]


def test_a_deleted_file_that_each_build_cannot_remove_keeps_the_action(two_builds, monkeypatch):
    """One build gave ``action: "timeout"``; several builds lost it."""
    import fw_context_mcp.indexer.db as db_mod
    from fw_context_mcp.indexer.db._locking import WriteLockTimeout

    root, _ = two_builds
    (root / "src/shared.c").unlink()

    def _locked(*args, **kwargs):
        raise WriteLockTimeout("write.lock is held")

    monkeypatch.setattr(db_mod, "purge_file_records", _locked)

    result = _reindex(root, "src/shared.c")

    assert result["action"] == "timeout", result
    assert result["failed_builds"] == ["a", "b"]


def test_the_top_level_sums_the_units_that_did_not_parse(tmp_path):
    from fw_context_mcp.mcp.handlers.maintenance import _reindex_sums

    target = tmp_path / "x.c"
    target.write_text("", encoding="utf-8")
    done = [
        ({"variant": "a", "image": ""}, {"translation_units": 2, "symbols_updated": 3, "skipped_tus": ["x.c"], "skipped_count": 1}),
        ({"variant": "b", "image": ""}, {"translation_units": 2, "symbols_updated": 4, "skipped_tus": ["x.c"], "skipped_count": 1}),
    ]

    answer = _reindex_sums(target, done, 1.0)

    assert answer["skipped_count"] == 2, answer
    assert answer["translation_units"] == 4 and answer["symbols_updated"] == 7


def test_an_error_of_the_only_build_stays_at_the_top_level(two_builds, monkeypatch):
    """One build compiles the file: the answer keeps the shape it had before builds were a part of it."""
    root, _ = two_builds
    _fail_in(monkeypatch, "a")

    result = _reindex(root, "src/only_a.c")

    assert result["error"] == "DB error during reindex: database is locked", result
    assert result["builds_without_file"] == ["b"]


def test_a_declared_variant_without_an_index_names_the_command(two_builds):
    root, _ = two_builds
    config = root / ".fw-context" / "config.toml"
    config.write_text(config.read_text(encoding="utf-8") + '\n[[build.variants]]\nname = "c"\n', encoding="utf-8")

    result = _reindex(root, "src/main.c", variant="c")

    assert list(result) == ["error"], result
    assert "--variant c" in result["error"]
