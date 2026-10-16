"""The CLI commands select a build by the rules of the MCP tools.

A project can hold several builds, each a ``(variant, image)`` pair.  The
CLI read the build that the last index run wrote, and said nothing about
the other builds.  The fixture ``two_builds`` (from
`tests/test_reindex_each_build.py`) indexes the two builds ``a`` and ``b``.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from tests.test_reindex_each_build import two_builds  # noqa: F401  (fixture)

OLD = "2000-01-01 00:00:00"


def _rows(db_path: Path) -> dict[str, tuple[int, str, str]]:
    """``variant`` → ``(analyze_vendor, created_at, first_indexed_at)`` of each build."""
    from fw_context_mcp.indexer.db import open_db

    conn = open_db(db_path, skip_integrity_check=True)
    try:
        return {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute("SELECT variant, analyze_vendor, created_at, first_indexed_at FROM build_configs")
        }
    finally:
        conn.close()


def _age(db_path: Path) -> None:
    """Set the times of each build to a fixed old value: a write of the current time then shows."""
    from fw_context_mcp.indexer.db import open_db

    conn = open_db(db_path, skip_integrity_check=True)
    try:
        conn.execute("UPDATE build_configs SET created_at = ?, first_indexed_at = ?", (OLD, OLD))
        conn.commit()
    finally:
        conn.close()


def _enable_llm(root: Path) -> None:
    config = root / ".fw-context" / "config.toml"
    config.write_text(config.read_text(encoding="utf-8") + "\n[llm]\nenabled = true\n", encoding="utf-8")


def _analyze(root: Path, monkeypatch, *, analyze_vendor: bool = False, **selector) -> tuple[int, list[str]]:
    """Run `fw-context analyze` with no model: return the code and the config hash of each analysed build."""
    import fw_context_mcp.indexer._llm_analysis as llm_mod
    from fw_context_mcp.cli._export import cmd_analyze

    analysed: list[str] = []
    monkeypatch.setattr(llm_mod, "_build_llm_analysis", lambda conn, config_hash, *a, **k: analysed.append(config_hash))
    args = argparse.Namespace(
        project=str(root), verbose=False, analyze_vendor=analyze_vendor, no_analyze_vendor=False, **selector,
    )
    return cmd_analyze(args), analysed


def test_analyze_stores_the_vendor_scope_and_keeps_the_time_of_the_index_run(two_builds, monkeypatch):  # noqa: F811
    """`analyze` wrote the build row after its last commit, thus the close dropped the write.

    ``analyze_vendor`` never reached the index.  The write also set
    ``created_at`` to the time of the call: the build would have looked
    newer than its compile_commands.json.
    """
    root, db_path = two_builds
    _enable_llm(root)
    _age(db_path)
    newest = "b"  # the build that the last index run of the fixture wrote

    code, analysed = _analyze(root, monkeypatch, analyze_vendor=True)

    assert code == 0 and analysed, "the analysis ran"
    assert _rows(db_path)[newest] == (1, OLD, OLD)


def test_status_shows_each_build(two_builds, capsys):  # noqa: F811
    """The status showed the counts of the build that the last index run wrote, and no other build."""
    from fw_context_mcp.cli._search import cmd_status

    root, _ = two_builds

    code = cmd_status(argparse.Namespace(project=str(root)))

    out = capsys.readouterr().out
    assert code == 0, out
    assert "Builds  : 2" in out
    lines = out.splitlines()
    assert any(line.startswith("  a ") for line in lines) and any(line.startswith("  b ") for line in lines), out


def test_list_shows_the_builds_and_no_sum(two_builds, monkeypatch, capsys):  # noqa: F811
    """The list showed the counts of the newest build for a project of several builds."""
    from fw_context_mcp.cli._search import cmd_list
    from fw_context_mcp.mcp.handlers import maintenance

    root, _ = two_builds
    real = maintenance.list_projects
    # The fixture keeps its index next to the project; the list reads the
    # index directory of the config of that project.
    monkeypatch.setattr(maintenance, "list_projects", lambda project_root=None: real(project_root=str(root)))

    code = cmd_list(argparse.Namespace(verbose=False))

    out = capsys.readouterr().out
    assert code == 0, out
    # Columns are two or more spaces apart; a count can hold one space ("1 294").
    header, row = (re.split(r"\s{2,}", line.strip()) for line in out.splitlines()[:2])
    assert header == ["PROJECT", "PATH", "BUILDS", "SYMBOLS", "FILES", "INDEXED"]
    cells = dict(zip(header, row, strict=True))
    assert (cells["BUILDS"], cells["SYMBOLS"], cells["FILES"]) == ("2", "-", "-"), row


def test_status_of_a_project_without_an_index_returns_1(two_builds, capsys):  # noqa: F811
    import shutil

    from fw_context_mcp.cli._search import cmd_status

    root, db_path = two_builds
    shutil.rmtree(db_path.parent)

    assert cmd_status(argparse.Namespace(project=str(root))) == 1
    assert "No symbol index found" in capsys.readouterr().out


def test_status_names_a_declared_variant_without_an_index(two_builds, capsys):  # noqa: F811
    from fw_context_mcp.cli._search import cmd_status

    root, _ = two_builds
    config = root / ".fw-context" / "config.toml"
    config.write_text(config.read_text(encoding="utf-8") + '\n[[build.variants]]\nname = "c"\n', encoding="utf-8")

    assert cmd_status(argparse.Namespace(project=str(root))) == 0
    assert "  c  not indexed — run 'fw-context index --build --variant c'" in capsys.readouterr().out


def _search(root: Path, query: str, **selector) -> int:
    from fw_context_mcp.cli._search import cmd_search

    args = argparse.Namespace(project=str(root), query=query, limit=50, verbose=False, variant="", image="")
    for key, value in selector.items():
        setattr(args, key, value)
    return cmd_search(args)


def _export(root: Path, out: Path, **selector) -> int:
    from fw_context_mcp.cli._export import cmd_export

    args = argparse.Namespace(project=str(root), output=str(out), no_refs=True, variant="", image="")
    for key, value in selector.items():
        setattr(args, key, value)
    return cmd_export(args)


def test_search_without_a_build_lists_the_builds(two_builds, capsys):  # noqa: F811
    """The search read the build that the last index run wrote, and said nothing."""
    root, _ = two_builds

    code = _search(root, "only_a_value")

    err = capsys.readouterr().err
    assert code == 1
    assert "  --variant a\n  --variant b\n" in err, err
    assert "fw-context status" in err and "get_project_status" not in err, "the words of a command line"


def test_search_reads_the_selected_build(two_builds, capsys):  # noqa: F811
    root, _ = two_builds

    assert _search(root, "only_a_value", variant="a") == 0
    assert "only_a_value" in capsys.readouterr().out
    assert _search(root, "only_a_value", variant="b") == 0
    assert "0 result(s)" in capsys.readouterr().out, "build b does not compile only_a.c"


def test_a_selector_of_no_build_is_refused(two_builds, capsys):  # noqa: F811
    root, _ = two_builds

    assert _search(root, "main", variant="nope") == 1
    assert "No build of this project has exactly the options --variant nope." in capsys.readouterr().err


def test_export_names_its_build(two_builds, tmp_path):  # noqa: F811
    import json

    root, _ = two_builds
    out = tmp_path / "export.json"

    assert _export(root, out) == 1, "no build named"
    assert _export(root, out, variant="b") == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert (data["variant"], data["image"], data["board"]) == ("b", "", "")
    names = {symbol["name"] for symbol in data["symbols"]}
    assert "main_value" in names and "only_a_value" not in names


def test_analyze_without_a_build_analyses_each_build(two_builds, monkeypatch, capsys):  # noqa: F811
    root, db_path = two_builds
    _enable_llm(root)

    code, analysed = _analyze(root, monkeypatch)

    assert code == 0 and len(set(analysed)) == 2, analysed
    out = capsys.readouterr().out
    assert "build a\n" in out and "build b\n" in out, out


def test_analyze_with_a_build_analyses_that_build(two_builds, monkeypatch):  # noqa: F811
    root, _ = two_builds
    _enable_llm(root)

    code, analysed = _analyze(root, monkeypatch, variant="a", image="")

    assert code == 0 and analysed == [_hash_of(root, "a")]


def _hash_of(root: Path, variant: str) -> str:
    from fw_context_mcp.indexer.db import open_db
    from tests.test_reindex_each_build import PROJECT_ID

    db_path = root.parent / "index" / PROJECT_ID / "index.db"
    conn = open_db(db_path, skip_integrity_check=True)
    try:
        return conn.execute("SELECT config_hash FROM build_configs WHERE variant = ?", (variant,)).fetchone()[0]
    finally:
        conn.close()


def test_search_of_a_project_of_one_build_needs_no_option(tmp_path, capsys):
    """The common case: one build without names, and the command names none."""
    import json

    from fw_context_mcp.indexer.runner import run
    from tests.test_reindex_each_build import PROJECT_ID, SOURCES, _units

    root = tmp_path / "proj"
    for name, text in SOURCES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    index_dir = tmp_path / "index"
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PROJECT_ID}"\n', encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text(f'[index]\ndb_dir = "{index_dir.as_posix()}"\n', encoding="utf-8")
    cc = root / "compile_commands.json"
    cc.write_text(json.dumps(_units(root, ["src/only_a.c"], "BOARD_A")), encoding="utf-8")
    run(
        compile_commands=cc, db_path=index_dir / PROJECT_ID / "index.db", project_root=root, project_id=PROJECT_ID,
        index_refs=False, index_embeddings=False, analyze_symbols=False, analyze_overrides=False,
    )

    assert _search(root, "only_a_value") == 0
    assert "only_a_value" in capsys.readouterr().out


def test_search_of_a_declared_variant_without_an_index_names_the_command(two_builds, capsys):  # noqa: F811
    root, _ = two_builds
    config = root / ".fw-context" / "config.toml"
    config.write_text(config.read_text(encoding="utf-8") + '\n[[build.variants]]\nname = "c"\n', encoding="utf-8")

    assert _search(root, "main_value", variant="c") == 1
    assert "Run 'fw-context index --build --variant c'." in capsys.readouterr().err


def test_db_list_marks_the_row_of_each_build(two_builds, capsys):  # noqa: F811
    """The list marked the newest build of the project as the one active build."""
    from fw_context_mcp.cli._db import QUERIED_FOOTER, cmd_db_list

    root, _ = two_builds

    assert cmd_db_list(argparse.Namespace(project=str(root), verbose=False)) == 0

    out = capsys.readouterr().out
    heads = [line for line in out.splitlines() if line.startswith("*") and line != QUERIED_FOOTER]
    assert [head.split()[1:3] for head in sorted(heads, key=lambda h: h.split()[1])] == [
        ["a", "queried"], ["b", "queried"],
    ], out
    assert QUERIED_FOOTER in out


def test_db_delete_guards_the_row_of_each_build(two_builds, capsys):  # noqa: F811
    """The guard protected only the build that the last index run wrote."""
    from fw_context_mcp.cli._db import cmd_db_delete

    root, _ = two_builds
    hash_a = _hash_of(root, "a")

    args = argparse.Namespace(project=str(root), all=False, config_hash=hash_a, force=False, yes=True)
    assert cmd_db_delete(args) == 1
    out = capsys.readouterr().out
    assert "The queries of build a read this row." in out, out
    assert _hash_of(root, "a") == hash_a, "nothing was deleted"


def test_db_stats_names_the_build(two_builds, capsys):  # noqa: F811
    from fw_context_mcp.cli._db import cmd_db_stats

    root, _ = two_builds

    assert cmd_db_stats(argparse.Namespace(project=str(root), config_hash=_hash_of(root, "b"))) == 0
    assert "  Label:        b\n" in capsys.readouterr().out


def test_watch_status_counts_the_modified_files_of_each_build(two_builds, capsys):  # noqa: F811
    """The count was of the newest build only: an edit of a file of the other build did not show."""
    from fw_context_mcp.cli._watch import cmd_watch_status

    root, _ = two_builds
    (root / "src/only_a.c").write_text("int only_a_value(void) { return 30; }\n", encoding="utf-8")

    assert cmd_watch_status(argparse.Namespace(project=str(root))) == 0

    out = capsys.readouterr().out
    assert "Modified:   1 file(s) in build a\n" in out, out
    assert "Modified:   0 file(s) in build b\n" in out, out


def test_db_shows_and_guards_a_row_of_an_index_run(two_builds, capsys):  # noqa: F811
    """A delete of a row that a run still writes: the run wrote it again, complete, without the deleted data."""
    from fw_context_mcp.cli._db import cmd_db_delete, cmd_db_list
    from fw_context_mcp.indexer.db import open_db

    root, db_path = two_builds
    conn = open_db(db_path, skip_integrity_check=True)
    try:
        conn.execute("UPDATE build_configs SET manifest_verification = 'indexing' WHERE variant = 'b'")
        conn.commit()
    finally:
        conn.close()
    hash_b = _hash_of(root, "b")

    assert cmd_db_list(argparse.Namespace(project=str(root), verbose=False)) == 0
    assert f" {hash_b[:12]}  b  indexing  " in capsys.readouterr().out

    args = argparse.Namespace(project=str(root), all=False, config_hash=hash_b, force=False, yes=True)
    assert cmd_db_delete(args) == 1
    assert "An index run writes this row of build b" in capsys.readouterr().out
    assert _hash_of(root, "b") == hash_b, "nothing was deleted"


def test_the_uncovered_sources_are_recorded_for_the_checked_build(two_builds, monkeypatch):  # noqa: F811
    """The marker came from the newest build, while the status checks the build of `_checked_build`."""
    import fw_context_mcp.mcp.shared.stale as stale_mod
    from fw_context_mcp.cli import _index
    from fw_context_mcp.config import load as load_config
    from fw_context_mcp.indexer.db import open_db

    root, db_path = two_builds
    hash_a = _hash_of(root, "a")  # build b is the newest
    seen: list[str] = []

    def _checked(conn, project_root, cfg):
        return conn.execute("SELECT * FROM build_configs WHERE config_hash = ?", (hash_a,)).fetchone()

    def _scan(conn, config_hash, *args, **kwargs):
        seen.append(config_hash)
        return []

    monkeypatch.setattr(_index, "_checked_build", _checked)
    monkeypatch.setattr(stale_mod, "find_unindexed_sources", _scan)
    open_db(db_path, skip_integrity_check=True).close()

    _index._record_still_uncovered(root, db_path, load_config(project_root=root))

    assert seen == [hash_a]


def test_the_training_data_comes_from_a_completed_build(two_builds):  # noqa: F811
    """A row of an index run can hold a part of the symbols only."""
    from fw_context_mcp.indexer.db import open_db
    from fw_context_mcp.indexer.finetune import _NEWEST_COMPLETED_BUILD

    root, db_path = two_builds
    conn = open_db(db_path, skip_integrity_check=True)
    try:
        conn.execute("UPDATE build_configs SET manifest_verification = 'indexing' WHERE variant = 'b'")
        conn.commit()
        assert conn.execute(_NEWEST_COMPLETED_BUILD).fetchone()[0] == _hash_of(root, "a")
    finally:
        conn.close()
