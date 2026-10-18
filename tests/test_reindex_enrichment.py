"""reindex_file keeps the enrichment of each symbol that it inserts again.

A re-parse of a file deletes the symbols of its translation unit and inserts
them again with new ids.  That also applies to the symbols of the headers
that the unit includes, for example a ``static inline`` function.
``ON DELETE CASCADE`` then removes the embedding of each such symbol.  The
embedding step of reindex_file took only the symbols of the file itself,
thus each header symbol stayed without an embedding, and the search by
meaning did not find it.  Measured on a project of 7 builds: one re-parse
of ``hash.c`` left about 200 definitions of each build without an
embedding, and the index still said "full".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT_ID = "e1c4e1c4e1c4e1c4e1c4e1c4e1c4e100"

SOURCES = {
    "src/util.h": (
        "#ifndef UTIL_H\n#define UTIL_H\n"
        "static inline int util_clamp(int v) { return v < 0 ? 0 : (v > 100 ? 100 : v); }\n"
        "int helper_a(int x);\n#endif\n"
    ),
    "src/util.c": '#include "util.h"\nint helper_a(int x) { return util_clamp(x) + 1; }\n',
    "src/main.c": (
        '#include "util.h"\n'
        "int step_one(int v) { return helper_a(v) + util_clamp(v); }\n"
        "int step_two(int v) { return helper_a(v + 1); }\n"
    ),
}


class _FakeEmbedder:
    """An embedder without a model: each text gets the same vector."""

    name = "fake-embedder"
    max_tokens = 512

    def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


@pytest.fixture
def one_build(tmp_path: Path, monkeypatch):
    """A project of one build with embeddings for each definition; returns ``(root, db_path)``.

    The LLM analysis is off in the config, thus reindex_file calls no
    model.  The fake embedder replaces Ollama.
    """
    from fw_context_mcp.config.settings import LLMConfig
    from fw_context_mcp.indexer import _embedding
    from fw_context_mcp.indexer.db import open_db
    from fw_context_mcp.indexer.runner import run

    monkeypatch.setattr(_embedding, "get_embedder", lambda cfg: _FakeEmbedder())
    root = tmp_path / "proj"
    for name, text in SOURCES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    index_dir = tmp_path / "index"
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(f'[project]\nid = "{PROJECT_ID}"\n', encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text(
        f'[index]\ndb_dir = "{index_dir.as_posix()}"\n\n'
        '[llm]\nenabled = true\nanalyze_symbols = false\nembed_model = "fake-embedder"\n',
        encoding="utf-8",
    )
    cc = root / "compile_commands.json"
    cc.write_text(json.dumps([
        {"directory": str(root), "file": name, "arguments": ["cc", "-Isrc", "-c", name, "-o", f"{name}.o"]}
        for name in ("src/util.c", "src/main.c")
    ]), encoding="utf-8")
    db_path = index_dir / PROJECT_ID / "index.db"
    run(
        compile_commands=cc, db_path=db_path, project_root=root, project_id=PROJECT_ID,
        index_refs=True, index_embeddings=False, analyze_symbols=False, analyze_overrides=False,
    )
    conn = open_db(db_path, skip_integrity_check=True)
    try:
        config_hash = conn.execute("SELECT config_hash FROM build_configs").fetchone()[0]
        _embedding._build_embeddings(conn, config_hash, LLMConfig(embed_model="fake-embedder"), db_path.parent)
        conn.commit()
    finally:
        conn.close()
    return root, db_path


ALL_EMBEDDED = [("helper_a", True), ("step_one", True), ("step_two", True), ("util_clamp", True)]


def _embedded(db_path: Path) -> list[tuple[str, bool]]:
    """For each function definition, by name: True when it has an embedding (chunk 0).

    A list and not a dict: two rows of one name must not merge without a
    word.
    """
    from fw_context_mcp.indexer.db import open_db

    conn = open_db(db_path, skip_integrity_check=True)
    try:
        return [
            (row[0], bool(row[1])) for row in conn.execute(
                "SELECT s.name, EXISTS (SELECT 1 FROM embeddings e"
                "                       WHERE e.symbol_id = s.id AND e.chunk_index = 0)"
                " FROM symbols s WHERE s.kind = 'function' AND s.is_definition = 1"
                " ORDER BY s.name"
            )
        ]
    finally:
        conn.close()


def _remove_embedding(db_path: Path, name: str) -> None:
    """Make an older gap: delete the embedding of the function *name*."""
    from fw_context_mcp.indexer.db import open_db

    conn = open_db(db_path, skip_integrity_check=True)
    try:
        conn.execute(
            "DELETE FROM embeddings WHERE symbol_id IN (SELECT id FROM symbols WHERE name = ?)", (name,),
        )
        conn.commit()
    finally:
        conn.close()


def test_the_fixture_embeds_each_definition(one_build):
    """The precondition of the tests below: no definition starts without an embedding."""
    _, db_path = one_build

    assert _embedded(db_path) == ALL_EMBEDDED


def test_a_header_symbol_of_the_reparsed_unit_keeps_an_embedding(one_build):
    """Before: ``util_clamp`` of ``util.h`` lost its embedding when ``util.c`` was re-parsed."""
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

    root, db_path = one_build

    result = reindex_file_impl("src/util.c", str(root), with_analysis=True)

    assert "embedding_warning" not in result, result
    assert _embedded(db_path) == ALL_EMBEDDED


def test_an_older_gap_outside_the_unit_is_left_to_the_index_run(one_build):
    """A re-parse embeds only the gaps that it makes.

    An older gap can hold thousands of definitions (a run that could not
    reach Ollama, ``index_embeddings = false``), and one call of the tool
    then embedded the whole build.  ``step_two`` is in ``main.c``, a unit
    that the re-parse of ``util.c`` does not touch.
    """
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

    root, db_path = one_build
    _remove_embedding(db_path, "step_two")

    reindex_file_impl("src/util.c", str(root), with_analysis=True)

    assert _embedded(db_path) == [
        ("helper_a", True), ("step_one", True), ("step_two", False), ("util_clamp", True),
    ]
