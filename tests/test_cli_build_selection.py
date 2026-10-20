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

import pytest

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
    monkeypatch.setattr(llm_mod, "_build_llm_analysis", lambda conn, config_hash, *a, **k: analysed.append(config_hash) or True)
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
    """A row that a stopped run left: the next run of the build continues it, thus the delete asks for --force.

    The command holds the index lock, thus no run writes the row while the
    command reads it.
    """
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
    assert "An index run writes this row of build b, or a run that stopped left it" in capsys.readouterr().out
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


@pytest.mark.parametrize(("results", "marker_kept"), [([True, True], False), ([True, False], True)])
def test_analyze_force_llm_gives_each_build_one_time_and_keeps_an_unfinished_marker(
    two_builds, monkeypatch, results, marker_kept,  # noqa: F811
):
    """`analyze --force-llm` makes each analysis again, as `index --force-llm`, without an index run."""
    import fw_context_mcp.indexer._llm_analysis as llm_mod
    from fw_context_mcp.cli._export import cmd_analyze
    from fw_context_mcp.indexer import _force_llm_marker as marker

    root, db_path = two_builds
    _enable_llm(root)
    since_values: list[str | None] = []
    answers = list(results)

    def build(conn, config_hash, *a, force_since=None, **k):
        since_values.append(force_since)
        return answers.pop(0)

    monkeypatch.setattr(llm_mod, "_build_llm_analysis", build)
    args = argparse.Namespace(project=str(root), verbose=False, analyze_vendor=False, no_analyze_vendor=False,
                              force_llm=True)

    assert cmd_analyze(args) == 0
    assert len(since_values) == 2 and since_values[0] is not None and len(set(since_values)) == 1
    assert (db_path.parent / marker.MARKER).exists() is marker_kept


def test_analyze_waits_for_the_run_that_holds_the_index(two_builds, monkeypatch, capsys):  # noqa: F811
    """Two operations on one project must not write its index at the same time.

    Before, `analyze` took no index lock: it wrote analyses while an index
    run replaced the symbols.  It never takes the index over: it says that
    an index run holds the index, and waits for its end.
    """
    from contextlib import ExitStack

    import fw_context_mcp.cli._export as export_mod
    from fw_context_mcp.indexer.db._locking import index_run_lock

    root, db_path = two_builds
    _enable_llm(root)
    holder = ExitStack()
    holder.enter_context(index_run_lock(db_path.parent))
    waits: list[float] = []

    def run_ends(seconds: float) -> None:
        waits.append(seconds)
        holder.close()  # the index run that held the index ends

    monkeypatch.setattr(export_mod.time, "sleep", run_ends)

    code, analysed = _analyze(root, monkeypatch)

    assert code == 0 and analysed, "after the end of the run, the analysis runs"
    assert len(waits) == 1
    assert "waits" in capsys.readouterr().err


def test_the_llm_does_not_wait_for_analyze(two_builds, monkeypatch):  # noqa: F811
    """`analyze` pauses the background reindex, and writes no reindex.pid.

    The status tools read reindex.pid as an index run, and tell the LLM to
    wait for it; an analysis does not change the index of the code.
    """
    import fw_context_mcp.indexer._llm_analysis as llm_mod
    from fw_context_mcp.cli._export import cmd_analyze

    root, db_path = two_builds
    _enable_llm(root)
    seen: list[tuple[bool, bool]] = []

    def build(conn, config_hash, *a, **k):
        seen.append(((db_path.parent / "reindex.pause").exists(), (db_path.parent / "reindex.pid").exists()))
        return True

    monkeypatch.setattr(llm_mod, "_build_llm_analysis", build)
    args = argparse.Namespace(project=str(root), verbose=False, analyze_vendor=False, no_analyze_vendor=False)

    assert cmd_analyze(args) == 0
    assert seen and all(entry == (True, False) for entry in seen)


def test_the_daemon_waits_for_analyze_also_without_the_pause_marker(two_builds, monkeypatch):  # noqa: F811
    """A reindex_file replaces reindex.pause with its own PID and removes it at its end.

    Before, the daemon then saw no marker during `analyze`, and started a
    background run each 2 s, and each run ended on the index lock.
    """
    import fw_context_mcp.indexer._llm_analysis as llm_mod
    from fw_context_mcp.cli._export import cmd_analyze
    from fw_context_mcp.mcp.daemon import _index_held

    root, db_path = two_builds
    _enable_llm(root)
    seen: list[bool] = []

    def build(conn, config_hash, *a, **k):
        (db_path.parent / "reindex.pause").unlink(missing_ok=True)  # a marker that went away
        seen.append(_index_held(db_path.parent))
        return True

    monkeypatch.setattr(llm_mod, "_build_llm_analysis", build)
    args = argparse.Namespace(project=str(root), verbose=False, analyze_vendor=False, no_analyze_vendor=False)

    assert cmd_analyze(args) == 0
    assert seen and all(seen)
    assert not _index_held(db_path.parent), "after analyze, the index is free"


def test_db_delete_asks_before_it_takes_the_index(two_builds, monkeypatch):  # noqa: F811
    """A person who reads the question must not hold the index.

    Before, the single-build delete held the index lock while it waited
    for the answer: the status said "reindexing", and the LLM and the
    daemon waited for a prompt.
    """
    import builtins

    import fw_context_mcp.cli._db as db_mod

    root, _db_path = two_builds
    steps: list[str] = []
    monkeypatch.setattr(builtins, "input", lambda prompt: steps.append("asked") or "y")

    def locked(db_dir, args, work):
        steps.append("locked")
        return work()

    monkeypatch.setattr(db_mod, "_under_index_lock", locked)
    args = argparse.Namespace(project=str(root), all=False, config_hash=_hash_of(root, "a"), force=True, yes=False)

    assert db_mod.cmd_db_delete(args) == 0
    assert steps == ["asked", "locked"]


def test_db_delete_makes_no_new_index_when_the_index_went_away_meanwhile(two_builds, monkeypatch):  # noqa: F811
    """Another command deleted the index while the person read the question: open_db made a new empty one."""
    import builtins

    from fw_context_mcp.cli._db import cmd_db_delete

    root, db_path = two_builds
    hash_a = _hash_of(root, "a")

    def answer(prompt):
        for suffix in ("", "-wal", "-shm"):
            (db_path.parent / (db_path.name + suffix)).unlink(missing_ok=True)
        return "y"

    monkeypatch.setattr(builtins, "input", answer)
    args = argparse.Namespace(project=str(root), all=False, config_hash=hash_a, force=True, yes=False)

    assert cmd_db_delete(args) == 1
    assert not db_path.exists()
