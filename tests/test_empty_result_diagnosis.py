"""An empty result is diagnosed once, and it warns once.

diagnose_empty_result() stats every indexed file, hashes the suspects and
lists the source tree, and it runs inside execute_sync — filesystem work
holding the one shared connection.  It must therefore run once per query.

It once ran once per build scope: nine configs on the Zephyr project meant
nine scans, and a dedup in _base.py threw the duplicate notices away
afterwards.  Both the loop and the dedup are gone, because execute_scoped
answers for ONE build — see mcp/shared/variants.py.  The invariant these
tests hold is what survived that: one query, one diagnosis, one warning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fw_context_mcp.mcp.shared.stale as stale_mod


@pytest.fixture
def project(tmp_path: Path):
    """An indexed project plus an executor over its database."""
    from fw_context_mcp.indexer.db import (
        open_db,
        transaction,
        upsert_build_config,
        upsert_file,
        upsert_project,
    )
    from fw_context_mcp.mcp.shared.context import get_executor
    from fw_context_mcp.utils import compute_source_hash

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    src = root / "src" / "main.c"
    src.write_text("int main(void){return 0;}\n", encoding="utf-8")
    cc = root / "compile_commands.json"
    cc.write_text("[]", encoding="utf-8")

    db_path = tmp_path / "index.db"
    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(root))
            for ch in ("ch1", "ch2", "ch3"):
                upsert_build_config(conn, ch, "pid", str(cc))
                # source_hash on purpose: without it the staleness check has
                # only the timestamp, and a rewrite inside the tolerance
                # band would go unseen — the test would then pass or fail on
                # how fast the machine is.
                upsert_file(
                    conn, ch, "src/main.c", "c",
                    mtime=src.stat().st_mtime,
                    source_hash=compute_source_hash(src),
                )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return root, get_executor(db_path), db_path


def _count_diagnoses(monkeypatch) -> dict:
    calls = {"n": 0}
    original = stale_mod.diagnose_empty_result

    def spy(conn, config_hash, root):
        calls["n"] += 1
        return original(conn, config_hash, root)

    monkeypatch.setattr(stale_mod, "diagnose_empty_result", spy)
    return calls


def _context(root, executor, db_path):
    """The context of one request: one build, named by its config hash."""
    from fw_context_mcp.mcp.handlers._base import DbContext

    return DbContext(
        db_path=db_path, executor=executor, config_hash="ch1", cfg=None,
        project_id="pid", root=root,
    )


class TestDiagnoseOncePerQuery:
    def test_one_query_diagnoses_once(self, project, monkeypatch):
        root, executor, db_path = project
        calls = _count_diagnoses(monkeypatch)
        db = _context(root, executor, db_path)

        db.execute_scoped(lambda conn, ch: [{"info": "no results"}])

        assert calls["n"] == 1, (
            "the diagnosis is filesystem work under the shared connection, "
            "thus one query must pay for it once"
        )

    def test_the_warning_reaches_the_caller_once(self, project, monkeypatch):
        root, executor, db_path = project
        # Make the project look behind: the bytes no longer match the hash
        # the index holds.  Content, not timestamp — see the fixture.
        (root / "src" / "main.c").write_text("int main(void){return 1;}\n",
                                             encoding="utf-8")
        db = _context(root, executor, db_path)

        result = db.execute_scoped(lambda conn, ch: [{"info": "no results"}])
        warnings = [r for r in result if isinstance(r, dict) and set(r) == {"warning"}]

        assert len(warnings) == 1, f"one problem, one warning: {result}"


# Two more changes were tried here and dropped, both recorded so they are
# not tried again:
#
# * Skipping the diagnosis for a result carrying `error`.  It looked free —
#   the record already names its reason — but "Symbol not found" IS the
#   answer a stale index explains, and test_stale_annotation.py holds that
#   deliberately.  The saving would have cost the caveat on the one answer
#   that most needs it.
#
# * Caching the diagnosis on the module TTL.  After the mtime gate was
#   fixed the whole multi-scope query costs 23 ms on the Zephyr project, so the
#   cache bought little, and a cached diagnosis outlives the edit it
#   describes for as long as its TTL.
