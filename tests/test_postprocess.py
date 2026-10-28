"""Tests for indexer/_postprocess.py — the order of the steps of the pipeline."""

from __future__ import annotations


class TestStepOrdering:
    """Verify postprocess step ordering invariants."""

    def test_llm_analysis_runs_before_embeddings(self):
        """Summaries must exist before embeddings are hashed.

        The embedding content hash includes the LLM summary.  When embeddings
        run first, the first index stores a hash without summaries, then the
        analysis step fills them — so the NEXT index re-embeds every analyzed
        symbol.  This ordering guard prevents that regression.
        """
        from fw_context_mcp.indexer._postprocess import _STEPS

        names = [name for name, _, _ in _STEPS]
        assert names.index("llm_analysis") < names.index("embeddings")

    def test_fts5_runs_before_embeddings_and_analysis(self):
        from fw_context_mcp.indexer._postprocess import _STEPS

        names = [name for name, _, _ in _STEPS]
        assert names.index("fts5") < names.index("embeddings")
        assert names.index("fts5") < names.index("llm_analysis")

    def test_cleanup_old_runs_last_before_checkpoint(self):
        from fw_context_mcp.indexer._postprocess import _STEPS

        names = [name for name, _, _ in _STEPS]
        assert names[-1] == "wal_checkpoint"
        assert names[-2] == "cleanup_old"
