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
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file

    root, db_path = one_build

    result = reindex_file("src/util.c", str(root), with_analysis=True)

    assert "embedding_warning" not in result, result
    assert _embedded(db_path) == ALL_EMBEDDED


def test_an_older_gap_outside_the_unit_is_left_to_the_index_run(one_build):
    """A re-parse embeds only the gaps that it makes.

    An older gap can hold thousands of definitions (a run that could not
    reach Ollama, ``index_embeddings = false``), and one call of the tool
    then embedded the whole build.  ``step_two`` is in ``main.c``, a unit
    that the re-parse of ``util.c`` does not touch.
    """
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file

    root, db_path = one_build
    _remove_embedding(db_path, "step_two")

    reindex_file("src/util.c", str(root), with_analysis=True)

    assert _embedded(db_path) == [
        ("helper_a", True), ("step_one", True), ("step_two", False), ("util_clamp", True),
    ]


# ── A fast re-parse (with_analysis=False) ─────────────────────────────────


def _graph(db_path: Path) -> list[tuple[str, bool, int | None]]:
    """For each function definition, by name: PageRank > 0, and its caller count in the hotspot cache."""
    from fw_context_mcp.indexer.db import open_db

    conn = open_db(db_path, skip_integrity_check=True)
    try:
        return [
            (row[0], bool(row[1]), row[2]) for row in conn.execute(
                "SELECT s.name, s.pagerank > 0,"
                " (SELECT h.caller_count FROM hotspot_cache h WHERE h.symbol_id = s.id)"
                " FROM symbols s WHERE s.kind = 'function' AND s.is_definition = 1"
                " ORDER BY s.name"
            )
        ]
    finally:
        conn.close()


def test_a_fast_reparse_keeps_the_hotspots_and_the_pagerank(one_build):
    """Before: the re-inserted symbols had PageRank 0 and no row in the hotspot cache.

    Measured on a project of 7 builds: ``memset``, with 684 references, was
    not in ``find_hotspots`` after one fast re-parse.
    """
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file

    root, db_path = one_build
    before = _graph(db_path)
    # helper_a: step_one and step_two call it.  util_clamp: helper_a and step_one.
    assert ("helper_a", True, 2) in before and ("util_clamp", True, 2) in before, before

    reindex_file("src/util.c", str(root), with_analysis=False)

    assert _graph(db_path) == before


def _status_message(root: Path) -> str:
    from fw_context_mcp.mcp.handlers.maintenance import get_project_status

    return get_project_status(project_root=str(root))["index_message"]


def test_the_status_names_the_definitions_that_a_fast_reparse_left_without_an_embedding(one_build):
    """Before: the stamp of the last run said "full", and the status said "fully up to date"."""
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file

    root, _ = one_build
    assert "no embedding" not in _status_message(root)

    reindex_file("src/util.c", str(root), with_analysis=False)

    message = _status_message(root)
    assert (
        " | Embeddings: 2 definition(s) have no embedding, and the search by meaning "
        "does not find them: run `fw-context index`" in message
    ), message


@pytest.mark.parametrize(("index_table", "llm_table"), [
    ("index_embeddings = false\n", 'enabled = true\nanalyze_symbols = false\nembed_model = "fake-embedder"\n'),
    ("", "enabled = false\n"),
])
def test_no_embedding_advice_when_the_config_turns_the_embeddings_off(one_build, index_table, llm_table):
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file

    root, db_path = one_build
    reindex_file("src/util.c", str(root), with_analysis=False)
    # db_path is <index_dir>/<project id>/index.db.
    (root / ".fw-context" / "local.toml").write_text(
        f'[index]\ndb_dir = "{db_path.parent.parent.as_posix()}"\n{index_table}\n[llm]\n{llm_table}',
        encoding="utf-8",
    )

    assert "no embedding" not in _status_message(root)


def test_no_embedding_advice_for_a_build_that_no_embedding_step_wrote(one_build):
    """Without embedding_dim each definition is without an embedding, and the next run does them all."""
    from fw_context_mcp.indexer.db import open_db
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file

    root, db_path = one_build
    reindex_file("src/util.c", str(root), with_analysis=False)
    conn = open_db(db_path, skip_integrity_check=True)
    try:
        conn.execute("UPDATE build_configs SET embedding_dim = NULL")
        conn.commit()
    finally:
        conn.close()

    assert "no embedding" not in _status_message(root)


def test_a_project_of_several_builds_names_each_build_without_embeddings(tmp_path, monkeypatch):
    """One number for each build and no sum, as for the LLM analysis."""
    from fw_context_mcp.indexer.db import open_db
    from fw_context_mcp.mcp.handlers import maintenance
    from tests.test_index_state import _project

    root, db_dir = _project(tmp_path)
    (root / ".fw-context" / "local.toml").write_text("[llm]\nenabled = true\nanalyze_symbols = false\n", encoding="utf-8")
    conn = open_db(db_dir / "index.db")
    try:
        conn.execute("UPDATE build_configs SET embedding_dim = 4")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        maintenance, "symbols_without_embedding",
        lambda conn, config_hash: [1, 2, 3] if config_hash == "h-a" else [],
    )

    message = maintenance.get_project_status(project_root=str(root))["index_message"]

    assert (
        " Definitions have no embedding in 1 of 2 builds (a: 3), and the search by meaning "
        "does not find them: run `fw-context index`." in message
    ), message
