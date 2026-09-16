"""A query that FTS5 turns down is reported, and the three tools agree.

``_fts5_rejection`` says why an empty list is not enough: the caller reads
``[]`` as "this code does not exist" and rewrites the question instead of
the query.  ``search_bodies`` and ``search_content`` gave that warning;
``search_code`` let the exception reach the error boundary, thus the same
broken query came back as ``search_code failed: unterminated string``
from one tool and as a warning with a repair hint from the other two.

The second case here is a different fault with the same face.  The
name-token strategy builds one ``CASE WHEN … END`` per word of the query
and adds them together, so the word count is the DEPTH of the SQL
expression.  A query of a thousand words hit the SQLite limit and
``search_code`` answered ``Expression tree is too large (maximum depth
1000)`` — a sentence about SQLite, in a tool that answers about code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    insert_symbols_batch,
    open_db,
    rebuild_files_fts,
    split_tokens,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.mcp.handlers import search
from fw_context_mcp.search.shared_fallbacks import _MAX_QUERY_TERMS, _name_token_terms

# An unbalanced double quote.  The sanitizer repairs the punctuation of a
# code pattern, thus what is left here is deliberate syntax and FTS5 has
# to turn it down.
BROKEN = '"unbalanced AND OR NEAR(('

# A thousand words that match nothing.  They must match nothing, or the
# primary step answers and the fallbacks never run.
TOO_MANY_WORDS = "zzqx " * 1000

PROJECT_ID = "f" * 32
CONFIG_HASH = "hash-refusal"


@pytest.fixture
def indexed(tmp_path: Path) -> Path:
    """A project root whose index holds one symbol.

    Hand-written, because the fault is in the query path and not in the
    indexer: libclang would cost a minute and add nothing.
    """
    from fw_context_mcp import config
    from fw_context_mcp.mcp.shared.readiness import _index_db_path

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    marker = root / ".fw-context"
    marker.mkdir()
    (marker / "config.toml").write_text(
        f'[project]\nid = "{PROJECT_ID}"\nname = "refusal"\n', encoding="utf-8"
    )
    cc = root / "compile_commands.json"
    cc.write_text("[]", encoding="utf-8")

    db_path = _index_db_path(config.load(root))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, PROJECT_ID, "refusal", str(root))
            upsert_build_config(conn, CONFIG_HASH, PROJECT_ID, str(cc))
            file_id = upsert_file(conn, CONFIG_HASH, "src/main.c", "c")
            # search_content reads files.content through files_fts, thus an
            # empty row would let the tool answer before FTS5 sees the query.
            conn.execute(
                "UPDATE files SET content = ? WHERE id = ?",
                ("int main(void){return 0;}\n", file_id),
            )
            insert_symbols_batch(conn, [
                (CONFIG_HASH, file_id, "src/main.c", split_tokens("main", "main"),
                 "usr-main", "main", "main", "function", 1, 1, 1, 1,
                 "int main(void)", "", None, 0, 0, "", 0, "", 1, 0.0,
                 "int main(void){return 0;}", 0),
            ])
        rebuild_files_fts(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return root


def _only(result) -> dict:
    """Give the one dict that a refused query answers with."""
    assert isinstance(result, list), result
    assert len(result) == 1, result
    return result[0]


class TestABrokenQueryIsAWarningInEveryTool:
    """One query, three tools, one shape of answer."""

    @pytest.mark.parametrize(
        "tool",
        [search.search_code, search.search_bodies, search.search_content],
        ids=lambda fn: fn.__name__,
    )
    def test_the_answer_warns_and_hints(self, indexed: Path, tool):
        answer = _only(tool(query=BROKEN, project_root=str(indexed)))

        assert "error" not in answer, (
            f"{tool.__name__} reported a broken query as a failure of the tool; "
            "its siblings report it as a warning about the query"
        )
        assert "FTS5 rejected the query" in answer["warning"]
        assert answer["hint"], "the warning must say how to repair the query"

    def test_a_good_query_still_answers(self, indexed: Path):
        """The refusal path must not swallow a query that FTS5 accepts."""
        rows = search.search_code(query="main", project_root=str(indexed))
        names = [r.get("name") for r in rows]
        assert "main" in names
        # A staleness notice may ride along; a refusal of the query may not.
        assert not any("FTS5 rejected" in str(r.get("warning", "")) for r in rows)


class TestAVeryLongQueryDoesNotReachTheSqliteLimit:
    def test_search_code_answers_instead_of_failing(self, indexed: Path):
        rows = search.search_code(query=TOO_MANY_WORDS, project_root=str(indexed))

        assert not any("error" in r for r in rows), (
            f"a long query must not answer with a fault of SQLite: {rows}"
        )
        # Nothing matches a thousand nonsense words, and that is the answer.
        assert rows == []

    def test_the_terms_are_capped(self):
        """The cap is what keeps the expression under the depth limit."""
        terms = _name_token_terms(TOO_MANY_WORDS)
        assert len(terms) == _MAX_QUERY_TERMS

    def test_a_normal_query_keeps_every_term(self):
        """The cap must not touch the queries that the tool documents."""
        assert _name_token_terms("modem init uart") == ["modem", "init", "uart"]
