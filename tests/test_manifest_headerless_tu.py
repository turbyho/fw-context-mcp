"""A translation unit with no recorded header gets a current manifest entry.

The runner recorded the headers of a re-parsed TU only when the list was not
empty.  A TU without a header kept the entry of the preliminary manifest,
with an empty source hash, and the staleness check reported it as stale
after each run.  Measured on one PlatformIO project: 63 of 215 TUs, sources
of the framework that the board compiles to nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3

PROJECT_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def indexed(tmp_path: Path, monkeypatch) -> tuple[Path, Path, str]:
    """Index a project of two TUs: one includes a header, one includes none."""
    import fw_context_mcp.config.settings as settings
    from fw_context_mcp.indexer.runner import run

    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)

    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.h").write_text("int a(void);\n", encoding="utf-8")
    (root / "a.c").write_text('#include "a.h"\nint a(void) { return 1; }\n', encoding="utf-8")
    (root / "b.c").write_text("int b(void) { return 2; }\n", encoding="utf-8")
    (root / "compile_commands.json").write_text(
        json.dumps([
            {"directory": str(root), "file": name, "arguments": ["cc", "-c", name, "-o", name + ".o"]}
            for name in ("a.c", "b.c")
        ]),
        encoding="utf-8",
    )
    index_dir = tmp_path / "index"
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PROJECT_ID}"\n', encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text(f'[index]\ndb_dir = "{index_dir}"\n', encoding="utf-8")

    db_path = index_dir / PROJECT_ID / "index.db"
    config_hash = run(
        compile_commands=root / "compile_commands.json",
        db_path=db_path,
        project_root=root,
        project_id=PROJECT_ID,
        index_refs=False,
        index_embeddings=False,
        analyze_symbols=False,
        analyze_overrides=False,
    )
    return root, db_path, config_hash


@pytest.mark.libclang
def test_a_tu_without_a_header_is_current_after_the_run(indexed):
    from fw_context_mcp.indexer.manifest import check_tu_staleness, resolve_headers
    from fw_context_mcp.indexer.manifest import load as load_manifest
    from fw_context_mcp.utils import compute_source_hash

    root, db_path, config_hash = indexed
    manifest = load_manifest(db_path.parent, config_hash)
    entries = {entry["file"]: entry for entry in manifest["entries"]}

    headerless = entries["b.c"]
    assert headerless["source_hash"] == compute_source_hash(root / "b.c")
    for name, entry in entries.items():
        stale, _ = check_tu_staleness(entry, root, headers=resolve_headers(entry, manifest.get("headers")))
        assert not stale, f"{name} reads as stale right after the run"
