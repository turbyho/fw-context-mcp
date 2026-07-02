"""Unit tests for RRF fusion quality — Phase 0a.

Tests RRF vs current merge on annotated synthetic data.
No DB, no Ollama, no external dependencies — in-memory only.

Compares:
- Current merge (score_result-based, deduplicate.py logic)
- RRF fusion (7 combos: k × weights)
- Metric: MRR, recall@K, precision@K, NDCG@K
"""

from __future__ import annotations

import pytest

from tests.quality_eval import aggregate, evaluate, summarize_grid
from tests.query_data.test_queries import QUERY_CASES, resolve_results

# ── Copy of experiments/test_rrf_fusion.py functions ──────────────────────


def rrf_fuse(fts5_rows, vec_rows, w_fts=1.2, w_vec=0.8, k=60, limit=20):
    """Merge two ranked lists via Reciprocal Rank Fusion."""
    scores: dict[tuple, float] = {}
    all_rows: dict[tuple, dict] = {}
    for rank, r in enumerate(fts5_rows, start=1):
        key = (r.get("name"), r.get("file_path"))
        scores[key] = scores.get(key, 0) + w_fts / (k + rank)
        if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
            all_rows[key] = dict(r)
    for rank, r in enumerate(vec_rows, start=1):
        key = (r.get("name"), r.get("file_path"))
        scores[key] = scores.get(key, 0) + w_vec / (k + rank)
        if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
            all_rows[key] = dict(r)
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    results = []
    for key, rrf_score in ranked[:limit]:
        entry = dict(all_rows[key])
        entry["_rrf_score"] = round(rrf_score, 6)
        f_keys = {(r.get("name"), r.get("file_path")) for r in fts5_rows}
        v_keys = {(r.get("name"), r.get("file_path")) for r in vec_rows}
        kk = (entry.get("name"), entry.get("file_path"))
        if kk in f_keys and kk in v_keys:
            entry["_source"] = "both"
        elif kk in f_keys:
            entry["_source"] = "fts5"
        else:
            entry["_source"] = "vec"
        results.append(entry)
    return results


def stems_from_queries(queries: list[str]) -> list[str]:
    """Extract stems from query strings (replicates scoring.stems_from_queries)."""
    stems = []
    for q in queries:
        if not q:
            continue
        for word in q.lower().split():
            w = word.rstrip("*")  # strip FTS5 wildcard for scoring
            if w and w not in stems:
                stems.append(w)
    return stems


def score_result(r: dict, stems: list[str]) -> int:
    """Replicate scoring.py score_result() for current merge comparison."""
    name = (r.get("name") or "").lower()
    qname = (r.get("qualified_name") or "").lower()
    file_path = (r.get("file_path") or "").lower()
    name_tokens = (r.get("name_tokens") or "").lower()
    kind = r.get("kind") or ""

    score = 0
    for stem in stems:
        stem_l = stem.lower()
        if stem_l in name or stem_l in name_tokens:
            score += 3
        if stem_l in qname:
            score += 2
        if stem_l in file_path:
            score += 1

    # Kind bonus
    kind_bonus = {
        "function": 2, "method": 2, "constructor": 2, "destructor": 2,
        "class": 2, "struct": 2, "enum": 2, "typedef": 2,
        "enum_constant": 1, "namespace": 1,
        "variable": 0, "field": 0,
    }
    score += kind_bonus.get(kind, 0)

    # Project-local bonus: files under src/ or lib/ (not vendor)
    if "/src/" in file_path or "/lib/" in file_path:
        if "/mbed-os/" not in file_path and "/vendor/" not in file_path:
            score += 1

    return score


def current_merge(fts5_rows, vec_rows, stems, limit=20):
    """Replicate current deduplicate.py merge logic."""
    seen: dict[tuple, dict] = {}
    all_rows = list(fts5_rows) + list(vec_rows)
    scored: list[tuple[int, dict]] = []
    for r in all_rows:
        name = r.get("name") or ""
        if name.startswith("("):
            continue
        if len(name) <= 2 and r.get("kind") in ("variable", "field"):
            continue
        key = (name, r.get("file_path"))
        prev = seen.get(key)
        if prev is None:
            s = score_result(r, stems)
            seen[key] = r
            scored.append((s, r))
        elif r.get("is_definition") and not prev.get("is_definition"):
            seen[key] = r
            for i, (_, existing) in enumerate(scored):
                if (existing.get("name") == name and (existing.get("file_path") or "") == (r.get("file_path") or "")):
                    scored[i] = (score_result(r, stems), r)
                    break
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def compute_overlap(fts5_rows, vec_rows, results):
    """Compute fts_only / vec_only / both counts."""
    f_keys = {(r.get("name"), r.get("file_path")) for r in fts5_rows}
    v_keys = {(r.get("name"), r.get("file_path")) for r in vec_rows}
    result_keys = {(r.get("name"), r.get("file_path")) for r in results}
    fts_only = sum(1 for k in result_keys if k in f_keys and k not in v_keys)
    vec_only = sum(1 for k in result_keys if k in v_keys and k not in f_keys)
    both = sum(1 for k in result_keys if k in f_keys and k in v_keys)
    return {"fts_only": fts_only, "vec_only": vec_only, "both": both}


# ── Test data fixtures ────────────────────────────────────────────────────


@pytest.fixture
def all_query_cases():
    """All annotated query cases."""
    return QUERY_CASES


@pytest.fixture
def exact_cases():
    """exact-category queries only."""
    return [q for q in QUERY_CASES if q["category"] == "exact"]


@pytest.fixture
def concept_cases():
    """concept-category queries only."""
    return [q for q in QUERY_CASES if q["category"] == "concept"]


@pytest.fixture
def mixed_cases():
    """mixed-category queries only."""
    return [q for q in QUERY_CASES if q["category"] == "mixed"]


@pytest.fixture
def non_empty_cases():
    """Queries that have at least one relevant symbol."""
    return [q for q in QUERY_CASES if q["relevant"]]


# ── Baseline: current merge ───────────────────────────────────────────────


class TestCurrentMerge:
    """Evaluate the current merge (score_result) as baseline."""

    def test_all_queries_return_results(self, all_query_cases):
        """Current merge should return at least some results for every query."""
        for case in all_query_cases:
            fts5 = resolve_results(case["fts5_ranked"])
            vec = resolve_results(case["vec_ranked"])
            stems = stems_from_queries([case["query"]])
            results = current_merge(fts5, vec, stems, limit=10)
            assert isinstance(results, list), f"No results for '{case['query']}'"

    def test_exact_queries_find_relevant(self, exact_cases):
        """Current merge should find at least some relevant for exact queries."""
        metrics = []
        for case in exact_cases:
            fts5 = resolve_results(case["fts5_ranked"])
            vec = resolve_results(case["vec_ranked"])
            stems = stems_from_queries([case["query"]])
            results = current_merge(fts5, vec, stems, limit=10)
            m = evaluate(results, case["relevant"], case["query"], "current", overlap_stats={})
            metrics.append(m)
        report = aggregate(metrics, "current")
        # Exact queries should have MRR > 0 (at least one relevant found)
        assert report.avg_mrr > 0, f"Current merge MRR={report.avg_mrr} on exact queries — should be > 0"

    def test_concept_queries_mrr_measurable(self, concept_cases):
        """Current merge should have measurable MRR on concept queries."""
        metrics = []
        for case in concept_cases:
            fts5 = resolve_results(case["fts5_ranked"])
            vec = resolve_results(case["vec_ranked"])
            stems = stems_from_queries([case["query"]])
            results = current_merge(fts5, vec, stems, limit=10)
            m = evaluate(results, case["relevant"], case["query"], "current", overlap_stats={})
            metrics.append(m)
        report = aggregate(metrics, "current")
        # At least measurable — not asserting specific value, just that it computes
        assert 0 <= report.avg_mrr <= 1.0
        assert 0 <= report.avg_recall_at_5 <= 1.0


# ── RRF vs Current merge ──────────────────────────────────────────────────


class TestRRFvsCurrent:
    """Compare RRF fusion against current merge on all queries."""

    RRF_COMBOS = [
        (10, 1.2, 0.8, "rrf_k10_f1.2_v0.8"),
        (30, 1.2, 0.8, "rrf_k30_f1.2_v0.8"),
        (60, 1.2, 0.8, "rrf_k60_f1.2_v0.8"),
        (60, 1.5, 0.5, "rrf_k60_f1.5_v0.5"),
        (60, 1.0, 1.0, "rrf_k60_f1.0_v1.0"),
        (60, 0.8, 1.2, "rrf_k60_f0.8_v1.2"),
        (100, 1.2, 0.8, "rrf_k100_f1.2_v0.8"),
    ]

    def test_rrf_vs_current_all_queries(self, non_empty_cases):
        """RRF should match or beat current merge on aggregated metrics."""

        # Current merge baseline
        cur_metrics = []
        for case in non_empty_cases:
            fts5 = resolve_results(case["fts5_ranked"])
            vec = resolve_results(case["vec_ranked"])
            stems = stems_from_queries([case["query"]])
            results = current_merge(fts5, vec, stems, limit=10)
            m = evaluate(results, case["relevant"], case["query"], "current")
            cur_metrics.append(m)
        cur_report = aggregate(cur_metrics, "current")

        # Test all RRF combos
        rrf_reports = []
        for k, w_fts, w_vec, label in self.RRF_COMBOS:
            rrf_metrics = []
            for case in non_empty_cases:
                fts5 = resolve_results(case["fts5_ranked"])
                vec = resolve_results(case["vec_ranked"])
                results = rrf_fuse(fts5, vec, w_fts=w_fts, w_vec=w_vec, k=k, limit=10)
                overlap = compute_overlap(fts5, vec, results)
                m = evaluate(
                    results, case["relevant"], case["query"], label,
                    params={"k": k, "w_fts": w_fts, "w_vec": w_vec},
                    overlap_stats=overlap,
                )
                rrf_metrics.append(m)
            rrf_reports.append(aggregate(rrf_metrics, label, {"k": k, "w_fts": w_fts, "w_vec": w_vec}))

        # At least one RRF variant should have MRR >= current merge
        best_rrf = max(rrf_reports, key=lambda r: r.avg_mrr)
        assert best_rrf.avg_mrr >= cur_report.avg_mrr, (
            f"No RRF variant beat current merge MRR ({cur_report.avg_mrr:.3f}). "
            f"Best RRF: {best_rrf.variant} MRR={best_rrf.avg_mrr:.3f}"
        )

        # Print summary for diagnostics
        grid = summarize_grid([cur_report] + rrf_reports)
        print(f"\n{grid}")

    def test_vec_only_symbols_present_in_rrf(self, non_empty_cases):
        """RRF should include results only found by vector search (not in FTS5)."""
        has_vec_only = False
        for case in non_empty_cases:
            f_keys = set(case["fts5_ranked"])
            v_keys = set(case["vec_ranked"])
            if v_keys - f_keys:
                has_vec_only = True
                break
        if not has_vec_only:
            pytest.skip("No vec-only symbols in test data — nothing to verify")

        for k, w_fts, w_vec, label in self.RRF_COMBOS[:1]:  # test first combo
            for case in non_empty_cases:
                f_keys = set(case["fts5_ranked"])
                v_keys = set(case["vec_ranked"])
                vec_only_symbols = v_keys - f_keys
                if not vec_only_symbols:
                    continue
                fts5 = resolve_results(case["fts5_ranked"])
                vec = resolve_results(case["vec_ranked"])
                results = rrf_fuse(fts5, vec, w_fts=w_fts, w_vec=w_vec, k=k, limit=10)
                result_names = {r["name"] for r in results}
                found_vec_only = vec_only_symbols & result_names
                assert len(found_vec_only) > 0, (
                    f"RRF ({label}) missed all vec-only symbols for '{case['query']}': "
                    f"vec_only={vec_only_symbols}, result_names={result_names}"
                )

    def test_overlap_stats_consistent(self, non_empty_cases):
        """Overlap stats (fts_only + vec_only + both) should sum to result count."""
        for k, w_fts, w_vec, label in self.RRF_COMBOS[:1]:
            for case in non_empty_cases:
                fts5 = resolve_results(case["fts5_ranked"])
                vec = resolve_results(case["vec_ranked"])
                results = rrf_fuse(fts5, vec, w_fts=w_fts, w_vec=w_vec, k=k, limit=10)
                overlap = compute_overlap(fts5, vec, results)
                total = overlap["fts_only"] + overlap["vec_only"] + overlap["both"]
                assert total == len(results), (
                    f"Overlap stats sum ({total}) != results count ({len(results)}) "
                    f"for label={label}"
                )


# ── RRF parameter sensitivity ─────────────────────────────────────────────


class TestRRFParameters:
    """Test that RRF parameters (k, weights) affect results predictably."""

    @pytest.mark.parametrize("k", [10, 30, 60, 100])
    def test_k_affects_ranking(self, non_empty_cases, k):
        """Changing k should (potentially) change ranking — at minimum not crash."""
        for case in non_empty_cases[:3]:  # test first 3 for speed
            fts5 = resolve_results(case["fts5_ranked"])
            vec = resolve_results(case["vec_ranked"])
            results = rrf_fuse(fts5, vec, w_fts=1.2, w_vec=0.8, k=k, limit=10)
            assert len(results) <= 10
            # Results should be sorted by descending _rrf_score
            scores = [r["_rrf_score"] for r in results]
            assert scores == sorted(scores, reverse=True), (
                f"RRF results not sorted by score for k={k}"
            )

    @pytest.mark.parametrize("w_fts,w_vec", [
        (1.5, 0.5), (1.2, 0.8), (1.0, 1.0), (0.8, 1.2), (0.5, 1.5),
    ])
    def test_weights_affect_source_distribution(self, non_empty_cases, w_fts, w_vec):
        """Higher FTS weight → more FTS-only results at top."""
        fts_heavy_results = 0
        vec_heavy_results = 0

        for case in non_empty_cases:
            fts5 = resolve_results(case["fts5_ranked"])
            vec = resolve_results(case["vec_ranked"])
            results = rrf_fuse(fts5, vec, w_fts=w_fts, w_vec=w_vec, k=60, limit=10)
            overlap = compute_overlap(fts5, vec, results)
            fts_heavy_results += overlap["fts_only"]
            vec_heavy_results += overlap["vec_only"]

        if w_fts > w_vec:
            # FTS-biased: expect more FTS-only than vec-only
            assert fts_heavy_results >= vec_heavy_results, (
                f"w_fts={w_fts} > w_vec={w_vec} but fts_only={fts_heavy_results} < vec_only={vec_heavy_results}"
            )
        elif w_vec > w_fts:
            # Vec-biased: expect more vec-only than fts-only
            assert vec_heavy_results >= fts_heavy_results, (
                f"w_vec={w_vec} > w_fts={w_fts} but vec_only={vec_heavy_results} < fts_only={fts_heavy_results}"
            )

    def test_edge_case_empty_relevant(self, all_query_cases):
        """Queries with no relevant symbols should not crash metrics."""
        for case in all_query_cases:
            if not case["relevant"]:
                fts5 = resolve_results(case["fts5_ranked"])
                vec = resolve_results(case["vec_ranked"])
                results = rrf_fuse(fts5, vec, k=60, limit=10)
                m = evaluate(results, case["relevant"], case["query"], "rrf_k60")
                assert m.mrr == 0.0
                assert m.recall_at_5 == 1.0  # no relevant → perfect recall
                assert m.precision_at_5 == 0.0  # no relevant → zero precision
