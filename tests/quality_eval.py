"""Evaluation framework for hybrid retrieval quality.

Computes standard IR metrics (MRR, recall@K, precision@K, NDCG@K)
and provides A/B comparison between pipeline variants.

Designed for Phase 0a unit tests — no DB, no Ollama, in-memory only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalMetrics:
    """Per-query metrics for one pipeline variant."""

    query: str
    variant: str
    mrr: float
    recall_at_1: float
    recall_at_5: float
    recall_at_10: float
    precision_at_1: float
    precision_at_5: float
    precision_at_10: float
    ndcg_at_10: float
    found_relevant: list[str]
    missed_relevant: list[str]
    result_names: list[str] = field(default_factory=list)
    overlap_stats: dict[str, int] = field(default_factory=lambda: {"fts_only": 0, "vec_only": 0, "both": 0})
    params: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"  {self.variant:30s} MRR={self.mrr:.3f} "
            f"R@5={self.recall_at_5:.3f} P@5={self.precision_at_5:.3f} "
            f"NDCG@10={self.ndcg_at_10:.3f} "
            f"found={self.found_relevant} missed={self.missed_relevant}"
        )


@dataclass
class EvalReport:
    """Aggregated report for one pipeline variant across all queries."""

    variant: str
    params: dict[str, Any] = field(default_factory=dict)
    avg_mrr: float = 0.0
    avg_recall_at_5: float = 0.0
    avg_precision_at_5: float = 0.0
    avg_ndcg_at_10: float = 0.0
    per_query: list[EvalMetrics] = field(default_factory=list)
    improved: int = 0
    degraded: int = 0
    unchanged: int = 0

    def summary(self) -> str:
        n = len(self.per_query)
        return (
            f"{self.variant:30s} "
            f"MRR={self.avg_mrr:.3f} R@5={self.avg_recall_at_5:.3f} "
            f"P@5={self.avg_precision_at_5:.3f} NDCG@10={self.avg_ndcg_at_10:.3f} "
            f"(n={n})"
        )


def evaluate(
    results: list[dict],
    relevant: set[tuple[str, str]],
    query: str,
    variant: str,
    params: dict[str, Any] | None = None,
    overlap_stats: dict[str, int] | None = None,
) -> EvalMetrics:
    """Compute all IR metrics for one query × variant.

    Args:
        results: Pipeline output — list of dicts with at least ``name`` and
            ``file_path`` (or ``file``) keys.
        relevant: Ground truth — set of ``(name, file_path)`` tuples.
        query: Original query string.
        variant: Pipeline variant label (e.g. ``"rrf_k60_w1.2_0.8"``).
        params: Optional dict of hyperparameters for this variant.
        overlap_stats: Optional dict with ``fts_only``, ``vec_only``, ``both``
            counts (for RRF variants).

    Returns:
        ``EvalMetrics`` with all computed values.
    """
    if not results:
        return EvalMetrics(
            query=query,
            variant=variant,
            mrr=0.0,
            recall_at_1=0.0,
            recall_at_5=0.0,
            recall_at_10=0.0,
            precision_at_1=0.0,
            precision_at_5=0.0,
            precision_at_10=0.0,
            ndcg_at_10=0.0,
            found_relevant=[],
            missed_relevant=sorted(r[0] for r in relevant),
            params=params or {},
            overlap_stats=overlap_stats or {},
        )

    # Normalize results to (name, file_path) keys
    result_keys = [
        (r.get("name", ""), r.get("file_path") or r.get("file", ""))
        for r in results
    ]

    result_names = [r.get("name", "") for r in results]

    # Find which relevant items were retrieved
    found = []
    missed = []
    for r_name, r_fp in relevant:
        if any(rn == r_name and rfp == r_fp for rn, rfp in result_keys):
            found.append(r_name)
        else:
            missed.append(r_name)

    # Fallback: fuzzy match by name only (helps when file_path differs)
    for r_name in list(missed):
        if r_name in result_names:
            found.append(r_name)
            missed.remove(r_name)

    # MRR — reciprocal rank of first relevant result
    mrr = 0.0
    for i, (rn, _rfp) in enumerate(result_keys):
        if (rn in {r[0] for r in relevant}) or any(
            rn == r_name for r_name, _ in relevant
        ):
            mrr = 1.0 / (i + 1)
            break

    # Recall@K
    recall_at_1 = _recall_at(result_keys, relevant, 1)
    recall_at_5 = _recall_at(result_keys, relevant, 5)
    recall_at_10 = _recall_at(result_keys, relevant, 10)

    # Precision@K
    precision_at_1 = _precision_at(result_keys, relevant, 1)
    precision_at_5 = _precision_at(result_keys, relevant, 5)
    precision_at_10 = _precision_at(result_keys, relevant, 10)

    # NDCG@10
    ndcg_at_10 = _ndcg_at(result_keys, relevant, 10)

    return EvalMetrics(
        query=query,
        variant=variant,
        mrr=mrr,
        recall_at_1=recall_at_1,
        recall_at_5=recall_at_5,
        recall_at_10=recall_at_10,
        precision_at_1=precision_at_1,
        precision_at_5=precision_at_5,
        precision_at_10=precision_at_10,
        ndcg_at_10=ndcg_at_10,
        found_relevant=found,
        missed_relevant=missed,
        result_names=result_names[:10],
        params=params or {},
        overlap_stats=overlap_stats or {},
    )


def aggregate(per_query: list[EvalMetrics], variant: str, params: dict[str, Any] | None = None) -> EvalReport:
    """Aggregate per-query metrics into a single report."""
    n = len(per_query)
    if n == 0:
        return EvalReport(variant=variant, params=params or {})

    return EvalReport(
        variant=variant,
        params=params or {},
        avg_mrr=sum(m.mrr for m in per_query) / n,
        avg_recall_at_5=sum(m.recall_at_5 for m in per_query) / n,
        avg_precision_at_5=sum(m.precision_at_5 for m in per_query) / n,
        avg_ndcg_at_10=sum(m.ndcg_at_10 for m in per_query) / n,
        per_query=per_query,
    )


def compare(baseline: EvalReport, candidate: EvalReport) -> str:
    """A/B comparison — human-readable diff report."""
    lines = [
        f"{'─' * 70}",
        f"A/B comparison: {baseline.variant} vs {candidate.variant}",
        f"{'─' * 70}",
        f"{'Metric':<20} {'Baseline':>12} {'Candidate':>12} {'Delta':>12}",
        f"{'─' * 20} {'─' * 12} {'─' * 12} {'─' * 12}",
    ]

    metrics = [
        ("MRR", baseline.avg_mrr, candidate.avg_mrr),
        ("Recall@5", baseline.avg_recall_at_5, candidate.avg_recall_at_5),
        ("Precision@5", baseline.avg_precision_at_5, candidate.avg_precision_at_5),
        ("NDCG@10", baseline.avg_ndcg_at_10, candidate.avg_ndcg_at_10),
    ]

    for name, base, cand in metrics:
        delta = cand - base
        sign = "+" if delta >= 0 else ""
        lines.append(f"{name:<20} {base:>12.4f} {cand:>12.4f} {sign}{delta:>11.4f}")

    lines.append(f"{'─' * 70}")
    return "\n".join(lines)


def summarize_grid(reports: list[EvalReport], sort_by: str = "avg_mrr") -> str:
    """Sort variants by metric and return a ranked table."""
    sorted_reports = sorted(reports, key=lambda r: -getattr(r, sort_by))

    header = f"{'Rank':<5} {'Variant':<30} {'MRR':>6} {'R@5':>6} {'P@5':>6} {'NDCG@10':>8}"
    sep = f"{'─' * 5} {'─' * 30} {'─' * 6} {'─' * 6} {'─' * 6} {'─' * 8}"

    lines = [header, sep]
    for i, r in enumerate(sorted_reports, 1):
        lines.append(
            f"{i:<5} {r.variant:<30} {r.avg_mrr:>6.3f} {r.avg_recall_at_5:>6.3f} "
            f"{r.avg_precision_at_5:>6.3f} {r.avg_ndcg_at_10:>8.4f}"
        )
    lines.append(sep)
    return "\n".join(lines)


# ── Internal helpers ──────────────────────────────────────────────────────


def _recall_at(
    result_keys: list[tuple[str, str]], relevant: set[tuple[str, str]], k: int
) -> float:
    """Fraction of relevant items found in top-K results."""
    if not relevant:
        return 1.0
    top_k = {r[0] for r in result_keys[:k]}
    # Match by name (robust to file_path differences)
    found = sum(1 for r_name, _ in relevant if r_name in top_k)
    return found / len(relevant)


def _precision_at(
    result_keys: list[tuple[str, str]], relevant: set[tuple[str, str]], k: int
) -> float:
    """Fraction of top-K results that are relevant."""
    actual_k = min(k, len(result_keys))
    if actual_k == 0:
        return 0.0
    relevant_names = {r[0] for r in relevant}
    found = sum(1 for rn, _ in result_keys[:actual_k] if rn in relevant_names)
    return found / actual_k


def _ndcg_at(
    result_keys: list[tuple[str, str]], relevant: set[tuple[str, str]], k: int
) -> float:
    """Normalized Discounted Cumulative Gain at K.

    Uses binary relevance (1 if relevant, 0 otherwise). IDCG is computed
    from the ideal ranking (all relevant first).
    """
    relevant_names = {r[0] for r in relevant}
    actual_k = min(k, len(result_keys))

    # DCG
    dcg = 0.0
    for i in range(actual_k):
        rn, _ = result_keys[i]
        if rn in relevant_names:
            dcg += 1.0 / math.log2(i + 2)  # rank i+1 → log2(i+2)

    # IDCG — ideal: all |relevant| relevant items at top
    ideal_count = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_count))

    return dcg / idcg if idcg > 0 else 0.0
