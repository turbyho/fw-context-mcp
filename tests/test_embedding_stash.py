"""The stash of embeddings: a re-parsed symbol takes its old vector back.

A re-parse deletes the symbols of each file that its unit owns and inserts
them again with new ids; ON DELETE CASCADE removes their embeddings.  The
store copies the vectors into ``embedding_stash`` first, and the embedding
step takes a vector back when ``(model, content_hash, is_project)`` of the
new symbol is equal.  No libclang: the tests write the rows directly.
"""
from __future__ import annotations

from typing import Any
from unittest import mock

from fw_context_mcp.indexer._embedding import _build_embeddings, _take_from_stash
from fw_context_mcp.indexer.db import (
    clear_embedding_stash,
    purge_file_records,
    replace_file_data,
    take_stashed_embeddings,
    transaction,
)
from tests import test_functional_fixes as fixes

# Through the module: a test class imported by name is collected here again.
_FakeEmbedder = fixes._FakeEmbedder
_seed = fixes.TestBuildEmbeddingsIncremental._seed

CONFIG = "hash-deadbeef"
Connection = Any  # the pysqlite3 connection that open_db gives


def _file_id(conn: Connection) -> int:
    return conn.execute(
        "SELECT id FROM files WHERE path = 'src/foo.cpp' AND config_hash = ?", (CONFIG,),
    ).fetchone()[0]


def _symbol_id(conn: Connection) -> int:
    return conn.execute("SELECT id FROM symbols WHERE usr = 'u_foo'").fetchone()[0]


def _reparse(conn: Connection, source: str = "void foo() {}") -> None:
    """Delete the rows of the file as a re-parse does, then insert the symbol again."""
    with transaction(conn):
        replace_file_data(conn, CONFIG, [_file_id(conn)])
    _seed(conn, CONFIG, source)


def _stash_count(conn: Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embedding_stash'"
    ).fetchone()
    return conn.execute("SELECT COUNT(*) FROM embedding_stash").fetchone()[0] if exists else 0


def _embeddings_of_foo(conn: Connection) -> list[tuple[int, str]]:
    return [
        (row[0], row[1]) for row in conn.execute(
            "SELECT e.chunk_index, e.content_hash FROM embeddings e "
            "JOIN symbols s ON s.id = e.symbol_id WHERE s.usr = 'u_foo' ORDER BY e.chunk_index"
        )
    ]


def _run(conn: Connection, fake: _FakeEmbedder, tmpdir) -> None:
    with mock.patch("fw_context_mcp.indexer._embedding.get_embedder", return_value=fake):
        _build_embeddings(conn, CONFIG, None, tmpdir)


def test_a_reparsed_symbol_takes_its_vector_from_the_stash(populated_db, tmpdir):
    conn = populated_db
    _seed(conn, CONFIG)
    fake = _FakeEmbedder()
    _run(conn, fake, tmpdir)
    first_calls, before = fake.calls, _embeddings_of_foo(conn)
    old_id = _symbol_id(conn)

    _reparse(conn)
    assert _symbol_id(conn) != old_id or _embeddings_of_foo(conn) == [], "the re-parse must remove the embedding"
    assert _stash_count(conn) == len(before)
    _run(conn, fake, tmpdir)

    assert fake.calls == first_calls, "the model must not be called for an unchanged symbol"
    assert _embeddings_of_foo(conn) == before


def test_a_changed_source_is_embedded(populated_db, tmpdir):
    conn = populated_db
    _seed(conn, CONFIG)
    fake = _FakeEmbedder()
    _run(conn, fake, tmpdir)
    first_calls = fake.calls

    _reparse(conn, "void foo() { return; }")
    _run(conn, fake, tmpdir)

    assert fake.calls > first_calls


def test_a_changed_is_project_is_embedded(populated_db, tmpdir):
    """is_project selects the docstring of the description and is not in content_hash."""
    conn = populated_db
    _seed(conn, CONFIG)
    fake = _FakeEmbedder()
    _run(conn, fake, tmpdir)
    first_calls = fake.calls

    _reparse(conn)
    conn.execute("UPDATE symbols SET is_project = 0 WHERE usr = 'u_foo'")
    conn.commit()
    _run(conn, fake, tmpdir)

    assert fake.calls > first_calls


def test_each_chunk_of_a_donor_is_taken(populated_db, tmpdir):
    """A long body gives several chunks; the new symbol gets each one."""
    conn = populated_db
    body = "void foo() {\n" + "".join(f"    step_{i}();\n" for i in range(40)) + "}\n"
    _seed(conn, CONFIG, body)
    fake = _FakeEmbedder()
    fake.max_tokens = 20  # 60 characters for each chunk
    _run(conn, fake, tmpdir)
    first_calls, before = fake.calls, _embeddings_of_foo(conn)
    assert len(before) > 1, "the precondition: the body gives more than one chunk"

    _reparse(conn, body)
    _run(conn, fake, tmpdir)

    assert fake.calls == first_calls
    assert _embeddings_of_foo(conn) == before


def test_a_vector_of_another_dimension_is_not_taken(populated_db, tmpdir):
    conn = populated_db
    _seed(conn, CONFIG)
    fake = _FakeEmbedder()
    _run(conn, fake, tmpdir)
    _reparse(conn)
    rows = conn.execute(
        "SELECT id, name, qualified_name, kind, file_path, signature, is_definition, "
        "docstring, summary, source, is_project FROM symbols WHERE usr = 'u_foo'"
    ).fetchall()

    left = _take_from_stash(conn, rows, f"{fake.name}:desc-v5", CONFIG, fake.dim * 2, tmpdir)

    assert [row["id"] for row in left] == [row["id"] for row in rows]


def test_a_purge_stashes_nothing(populated_db, tmpdir):
    """A file that left the build: nothing inserts its symbols again."""
    conn = populated_db
    _seed(conn, CONFIG)
    _run(conn, _FakeEmbedder(), tmpdir)
    with transaction(conn):
        purge_file_records(conn, CONFIG, _file_id(conn), "src/foo.cpp")
    assert _stash_count(conn) == 0


def _stash_row(conn: Connection, content_hash: str, donor: int, chunk: int, blob: bytes) -> None:
    conn.execute(
        "INSERT INTO embedding_stash VALUES (?, ?, 1, ?, ?, ?)", ("m", content_hash, donor, chunk, blob),
    )


def test_two_texts_of_one_donor_id_keep_their_own_chunks(populated_db, tmpdir):
    """symbols.id is reused: a left-over donor and a new one can share an id."""
    conn = populated_db
    _seed(conn, CONFIG)
    _run(conn, _FakeEmbedder(), tmpdir)
    _reparse(conn)  # makes the table
    with transaction(conn):
        clear_embedding_stash(conn)
        for chunk, blob in enumerate((b"a", b"b", b"c")):
            _stash_row(conn, "h1", 7, chunk, blob)
        _stash_row(conn, "h2", 7, 0, b"new")

    found = take_stashed_embeddings(conn, "m", {("m", "h1", 1), ("m", "h2", 1)})

    assert found == {
        ("m", "h1", 1): [(0, b"a"), (1, b"b"), (2, b"c")],
        ("m", "h2", 1): [(0, b"new")],
    }


def test_a_donor_with_a_gap_in_its_chunks_does_not_serve(populated_db, tmpdir):
    conn = populated_db
    _seed(conn, CONFIG)
    _run(conn, _FakeEmbedder(), tmpdir)
    _reparse(conn)
    with transaction(conn):
        clear_embedding_stash(conn)
        _stash_row(conn, "h1", 5, 0, b"a")
        _stash_row(conn, "h1", 5, 2, b"c")
        _stash_row(conn, "h1", 9, 0, b"x")

    found = take_stashed_embeddings(conn, "m", {("m", "h1", 1)})

    assert found == {("m", "h1", 1): [(0, b"x")]}, "donor 5 has no chunk 1; donor 9 serves"


def test_clear_empties_the_stash(populated_db, tmpdir):
    conn = populated_db
    _seed(conn, CONFIG)
    _run(conn, _FakeEmbedder(), tmpdir)
    _reparse(conn)
    assert _stash_count(conn) > 0
    with transaction(conn):
        clear_embedding_stash(conn)
    assert _stash_count(conn) == 0
