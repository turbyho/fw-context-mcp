"""The CLI commands select a build by the rules of the MCP tools.

A project can hold several builds, each a ``(variant, image)`` pair.  The
CLI read the build that the last index run wrote, and said nothing about
the other builds.  The fixture ``two_builds`` (from
`tests/test_reindex_each_build.py`) indexes the two builds ``a`` and ``b``.
"""

from __future__ import annotations

import argparse
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
