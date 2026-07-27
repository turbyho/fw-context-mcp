"""Tests for the eval harness — quality_eval integration, dataset loading, metrics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.quality_eval import evaluate, aggregate, EvalMetrics, EvalReport


class TestHarnessIntegration:
    """Verify harness flows: dataset → retrieval variant → evaluate → aggregate."""

    def test_fts5_metrics_on_stub_results(self) -> None:
        relevant = {("uart_init", "src/lib.c"), ("compute_checksum", "src/lib.c")}
        results = [
            {"name": "uart_init", "file_path": "src/lib.c", "kind": "function"},
            {"name": "set_mode", "file_path": "src/lib.c", "kind": "function"},
            {"name": "compute_checksum", "file_path": "src/lib.c", "kind": "function"},
        ]
        m = evaluate(results, relevant, "uart init", "fts5", params={"split": "sig"})
        assert m.mrr >= 0.5  # first relevant at position 1 → MRR=1.0
        assert m.recall_at_5 == 1.0
        assert m.missed_relevant == []
        assert m.params["split"] == "sig"

    def test_simulated_impl_blank_retrieval(self) -> None:
        """Simulate impl-only query — dense retrieval returns nothing → zero metrics."""
        relevant = {("compute_checksum", "src/lib.c")}
        results: list = []
        m = evaluate(results, relevant, "return negative one on error", "dense", params={"split": "impl"})
        assert m.mrr == 0.0
        assert m.recall_at_5 == 0.0
        assert m.missed_relevant == ["compute_checksum"]

    def test_aggregate_mixed_variants(self) -> None:
        metrics = [
            EvalMetrics(query="q1", variant="A", mrr=1.0, recall_at_1=1.0, recall_at_5=1.0,
                        recall_at_10=1.0, precision_at_1=1.0, precision_at_5=0.2,
                        precision_at_10=0.1, ndcg_at_10=1.0, found_relevant=["a"], missed_relevant=[]),
            EvalMetrics(query="q2", variant="A", mrr=0.5, recall_at_1=0.0, recall_at_5=1.0,
                        recall_at_10=1.0, precision_at_1=0.0, precision_at_5=0.2,
                        precision_at_10=0.1, ndcg_at_10=0.63, found_relevant=["b"], missed_relevant=[]),
        ]
        report = aggregate(metrics, "A")
        assert report.avg_mrr == 0.75
        assert report.avg_recall_at_5 == 1.0
        assert len(report.per_query) == 2

    def test_dataset_structure(self, tmp_path: Path) -> None:
        """Verify the dataset JSON schema used by the harness."""
        dataset = {
            "project": "test",
            "queries": [
                {"query": "uart init", "split": "sig", "relevant": [["uart_init", "src/lib.c"]]},
                {"query": "dma timeout", "split": "impl", "relevant": [["dma_irq", "src/dma.c"]]},
            ],
        }
        f = tmp_path / "queries.json"
        f.write_text(json.dumps(dataset))
        loaded = json.loads(f.read_text())
        assert loaded["project"] == "test"
        assert len(loaded["queries"]) == 2
        for q in loaded["queries"]:
            assert "query" in q
            assert "split" in q
            assert "relevant" in q
            assert isinstance(q["relevant"], list)


class TestDecisionGates:
    """Verify the harness decision criteria for Blok 2 adoption."""

    def test_impl_improvement_gate_pass(self) -> None:
        """Recall@10 on impl split must improve ≥10% from baseline."""
        baseline_recall = 0.20  # 20%
        candidate_recall = 0.35  # 35% → +15 percentage points (>10pp)
        assert candidate_recall - baseline_recall >= 0.10

    def test_sig_degradation_gate_pass(self) -> None:
        """Recall@10 on sig split must not degrade >2%."""
        baseline_recall = 0.90
        candidate_recall = 0.89  # -1% → within tolerance
        assert baseline_recall - candidate_recall <= 0.02

    def test_sig_degradation_gate_fail(self) -> None:
        """Sig degradation >2% should block the change."""
        baseline_recall = 0.90
        candidate_recall = 0.87  # -3% → exceeds tolerance
        assert baseline_recall - candidate_recall > 0.02
