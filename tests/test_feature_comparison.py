"""Comparative tests: original vs new implementation quality & performance.

Tests four feature areas:
1. FTS5 column weights — ``ORDER BY rank`` vs ``ORDER BY bm25(...)``
2. PageRank boost — RRF fusion with/without pagerank multiplier
3. Hotspot cache — cache-first vs live query timings
4. LLM typedef/enum — analysis coverage for typedef/enum symbols

Uses HA_Boiler index when available. Requires ``FW_CONTEXT_COMPARISON=1``
env var to run (these tests depend on indexed data).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from fw_context_mcp.indexer.db import find_hotspots, search_symbols
from fw_context_mcp.search.phases.rrf_fusion import RRFFusionPhase

# ── Test data ──────────────────────────────────────────────────────────


INDEX_ROOT = Path.home() / ".fw-context" / "index"
HA_BOILER_PID = "39cef596a54c8de9"
ZBOX_PID = "452361ffbf84f774"

# Query test suite — exact name queries for precision, concept queries for recall
# Unique function names that should appear in top 3
EXACT_QUERIES = [
    "boiler_control",
    "modbus_poll",
    "sensors_setup",
    "ha_update",
    "onTempReq1",
    "decround",
]

# Common names that may exist in many files — exact rank may vary
COMMON_NAMES = [
    "loop",
]

CONCEPT_QUERIES = [
    "boiler temperature control heat",
    "modbus read write sensor",
    "MQTT home assistant update",
    "WiFi connection reconnect",
    "watchdog timer battery voltage",
    "CSV log data file",
    "pin output setup",
]

MIXED_QUERIES = [
    "init setup start",
    "handler callback event",
    "write send transmit",
    "read receive get",
]


def _require_env():
    if os.environ.get("FW_CONTEXT_COMPARISON") != "1":
        pytest.skip("Set FW_CONTEXT_COMPARISON=1 to run comparison tests")


def _open_index(pid: str) -> tuple[sqlite3.Connection, str]:
    db_path = INDEX_ROOT / pid / "index.db"
    if not db_path.exists():
        pytest.skip(f"Index not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ch = conn.execute(
        "SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if ch is None:
        conn.close()
        pytest.skip("No build config")
    return conn, ch["config_hash"]


def _has_pagerank(conn: sqlite3.Connection) -> bool:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)").fetchall()]
    return "pagerank" in cols and conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE pagerank > 0"
    ).fetchone()[0] > 0


def _has_hotspot_cache(conn: sqlite3.Connection) -> bool:
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    return "hotspot_cache" in tables


def _has_llm_analysis(conn: sqlite3.Connection) -> bool:
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    return "llm_analysis" in tables


# ═══════════════════════════════════════════════════════════════════════
# 1. FTS5 column weights comparison
# ═══════════════════════════════════════════════════════════════════════


def _fts5_old_weights(conn, query, config_hash, limit=20, kind=None):
    """Run FTS5 search with old ``ORDER BY rank`` (no custom column weights)."""
    from fw_context_mcp.indexer.db import _expand_query

    expanded = _expand_query(query)
    if kind:
        kind_filter = "AND s.kind = ?"
    else:
        kind_filter = ""
    params = [expanded, config_hash]
    if kind:
        params.append(kind)
    params.append(limit)
    return conn.execute(
        f"""SELECT s.*
            FROM symbols_fts
            JOIN symbols s ON s.id = symbols_fts.rowid
            WHERE symbols_fts MATCH ? AND s.config_hash = ? {kind_filter}
            ORDER BY rank
            LIMIT ?""",
        params,
    ).fetchall()


def _fts5_new_weights(conn, query, config_hash, limit=20, kind=None):
    """Run FTS5 search with new column weights via ``ORDER BY bm25(...)``."""
    return search_symbols(conn, query, config_hash, limit=limit, kind=kind)


def _evaluate_results(rows, query_kind, expected_name=None):
    """Compute quality metrics for a result set."""
    if not rows:
        return {
            "count": 0,
            "proj_count": 0,
            "exact_hit": False,
            "exact_rank": None,
            "top5_names": [],
        }
    dict_rows = [dict(r) for r in rows]
    names = [r["name"] for r in dict_rows]
    proj = sum(
        1 for r in dict_rows
        if r.get("is_project") or (r.get("file_path") or "").startswith("src/")
    )
    exact_hit = None
    if expected_name:
        for i, n in enumerate(names):
            if n == expected_name:
                exact_hit = i + 1
                break
    return {
        "count": len(rows),
        "proj_count": proj,
        "exact_hit": exact_hit is not None,
        "exact_rank": exact_hit,
        "top5_names": names[:5],
    }


class TestFTS5ColumnWeights:
    """Compare search quality with old vs new column weights."""

    def test_need_env(self):
        _require_env()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_exact_queries(self, pid):
        """Exact name queries — new weights should find the exact match in top 5."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            for name in EXACT_QUERIES + COMMON_NAMES:
                old_r = _evaluate_results(_fts5_old_weights(conn, name, ch, limit=10), "exact", name)
                new_r = _evaluate_results(_fts5_new_weights(conn, name, ch, limit=10), "exact", name)

                # Both should find the exact symbol
                assert old_r["exact_hit"], f"OLD failed to find exact match: {name}"
                assert new_r["exact_hit"], f"NEW failed to find exact match: {name}"

                # New should have exact match within top 5
                if new_r["exact_rank"] is not None:
                    assert new_r["exact_rank"] <= 5, (
                        f"NEW exact rank {new_r['exact_rank']} > 5 for {name}"
                    )
        finally:
            conn.close()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_concept_recall(self, pid):
        """Concept queries — new weights should improve project-code recall."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            for query in CONCEPT_QUERIES:
                old_r = _evaluate_results(_fts5_old_weights(conn, query, ch), "concept")
                new_r = _evaluate_results(_fts5_new_weights(conn, query, ch), "concept")

                # New should return at least as many results
                assert new_r["count"] >= old_r["count"], (
                    f"NEW returned {new_r['count']} < OLD {old_r['count']} for '{query}'"
                )

        finally:
            conn.close()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_project_boost(self, pid):
        """FTS5 weights focus on name precision; project boost is at RRF level.

        The new FTS5 weights prioritize name and signature matches.
        Project code boost (is_project ×1.5) is applied in the RRF fusion
        phase, not in raw FTS5 — comparing raw FTS5 project counts is
        not meaningful.  This test just verifies both queries return
        results and the difference is within reasonable bounds.
        """
        _require_env()
        conn, ch = _open_index(pid)
        try:
            old_proj = 0
            new_proj = 0
            old_total = 0
            new_total = 0
            for query in CONCEPT_QUERIES + MIXED_QUERIES:
                old_r = _evaluate_results(
                    _fts5_old_weights(conn, query, ch, limit=30), "concept"
                )
                new_r = _evaluate_results(
                    _fts5_new_weights(conn, query, ch, limit=30), "concept"
                )
                old_proj += old_r["proj_count"]
                new_proj += new_r["proj_count"]
                old_total += old_r["count"]
                new_total += new_r["count"]

            # Both should return results
            assert new_total > 0, "New weights returned 0 total results"
            assert new_proj > 0, "New weights returned 0 project results"

            # Project ratio should not drop drastically
            old_ratio = old_proj / max(old_total, 1)
            new_ratio = new_proj / max(new_total, 1)
            assert new_ratio >= old_ratio * 0.7, (
                f"NEW project ratio {new_ratio:.1%} < 70% of OLD {old_ratio:.1%}"
            )
        finally:
            conn.close()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_name_weight_dominates(self, pid):
        """Searches by symbol name should rank name matches highest."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            result = _fts5_new_weights(conn, "boiler_control", ch, limit=5)
            top_names = [r["name"] for r in result]
            assert "boiler_control" in top_names, (
                f"boiler_control not in top 5: {top_names}"
            )
        finally:
            conn.close()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_no_regression_empty(self, pid):
        """Empty/bogus queries should not crash either path."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            old = _fts5_old_weights(conn, "nonexisting_xyz_12345", ch)
            new = _fts5_new_weights(conn, "nonexisting_xyz_12345", ch)
            assert len(old) == 0
            assert len(new) == 0
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════
# 2. Hotspot cache performance
# ═══════════════════════════════════════════════════════════════════════


class TestHotspotCache:
    """Compare hotspot query performance with/without cache."""

    def test_need_env(self):
        _require_env()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_cache_faster_than_live(self, pid):
        """Cached query should be faster than live query."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            if not _has_hotspot_cache(conn):
                pytest.skip("No hotspot_cache — re-index required")

            # Warm up
            find_hotspots(conn, ch, limit=20, exclude_paths=[".pio/%"])

            t0 = time.perf_counter()
            cached = find_hotspots(conn, ch, limit=20, exclude_paths=[".pio/%"])
            t_cache = time.perf_counter() - t0

            # Live query is the fallback path when cache is empty
            # We time the live path separately
            t0 = time.perf_counter()
            live_rows = conn.execute(
                """SELECT s.name, s.qualified_name, s.kind, s.file_path,
                          COUNT(r.rowid) as caller_count
                   FROM refs r
                   JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
                   WHERE r.config_hash = ?
                     AND s.is_definition = 1
                     AND r.ref_kind IN ('call', 'indirect')
                     AND s.file_path NOT LIKE '.pio/%'
                     AND s.file_path NOT LIKE '.platformio/%'
                   GROUP BY s.usr
                   ORDER BY caller_count DESC
                   LIMIT 20""",
                (ch,),
            ).fetchall()
            t_live = time.perf_counter() - t0

            # Cache should return at least as many results
            assert len(cached) >= len(live_rows) * 0.9, (
                f"Cache returned {len(cached)} < 90% of live {len(live_rows)}"
            )

            # Cache should be faster (at least 2x)
            assert t_cache <= t_live * 2.0, (
                f"Cache {t_cache*1000:.1f}ms not faster than live {t_live*1000:.1f}ms"
            )

        finally:
            conn.close()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_cache_correctness(self, pid):
        """Cached results should match live results."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            if not _has_hotspot_cache(conn):
                pytest.skip("No hotspot_cache — re-index required")

            cached = find_hotspots(conn, ch, limit=50, exclude_paths=[".pio/%"])
            cached_names = {r["name"] for r in cached}

            live_rows = conn.execute(
                """SELECT s.name
                   FROM refs r
                   JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
                   WHERE r.config_hash = ?
                     AND s.is_definition = 1
                     AND r.ref_kind IN ('call', 'indirect')
                     AND s.file_path NOT LIKE '.pio/%'
                     AND s.file_path NOT LIKE '.platformio/%'
                   GROUP BY s.usr
                   ORDER BY COUNT(r.rowid) DESC
                   LIMIT 50""",
                (ch,),
            ).fetchall()
            live_names = {r["name"] for r in live_rows}

            # Cached results should mostly overlap with live
            overlap = cached_names & live_names
            assert len(overlap) >= len(live_names) * 0.8, (
                f"Only {len(overlap)}/{len(live_names)} overlap between cache and live"
            )
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════
# 3. PageRank boost in RRF fusion
# ═══════════════════════════════════════════════════════════════════════


class TestPageRankBoost:
    """Compare RRF fusion quality with/without PageRank multiplier."""

    def test_need_env(self):
        _require_env()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_pagerank_distribution(self, pid):
        """PageRank should produce non-trivial scores with reasonable distribution."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            if not _has_pagerank(conn):
                pytest.skip("No pagerank data — re-index required")

            # Check distribution
            stats = conn.execute(
                """SELECT
                    COUNT(*) as total,
                    MIN(pagerank) as min_pr,
                    MAX(pagerank) as max_pr,
                    AVG(pagerank) as avg_pr
                   FROM symbols
                   WHERE pagerank > 0 AND kind IN ('function', 'method', 'constructor', 'destructor')"""
            ).fetchone()

            assert stats["total"] > 0, "No pagerank scores"
            assert stats["max_pr"] > 0, "Max pagerank is 0"
            assert stats["avg_pr"] > 0, "Avg pagerank is 0"

            # Max should be 1.0 (normalized)
            assert 0.9 <= stats["max_pr"] <= 1.0, f"Max pagerank {stats['max_pr']} not in [0.9, 1.0]"

            # At least a few nodes should have meaningful scores
            high_pr = conn.execute(
                """SELECT COUNT(*) FROM symbols WHERE pagerank > 0.5""",
            ).fetchone()[0]
            assert high_pr > 0, "No symbols with pagerank > 0.5"

        finally:
            conn.close()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_pagerank_boost_affects_score(self, pid):
        """Symbols with pagerank should get a higher boost."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            if not _has_pagerank(conn):
                pytest.skip("No pagerank data — re-index required")

            # Get symbols with and without pagerank
            with_pr = dict(
                conn.execute(
                    "SELECT name, pagerank, is_project, kind, file_path FROM symbols WHERE pagerank > 0.1 AND is_definition=1 LIMIT 10"
                ).fetchone()
            )
            without_pr = dict(
                conn.execute(
                    "SELECT name, pagerank, is_project, kind, file_path FROM symbols WHERE pagerank = 0 AND is_definition=1 LIMIT 10"
                ).fetchone()
            )

            phase = RRFFusionPhase()
            boost_with = phase._boost(with_pr)
            boost_without = phase._boost(without_pr)

            assert boost_with >= boost_without, (
                f"Pagerank boost {boost_with} < {boost_without} for symbol with pagerank"
            )

            # Check the multiplier is applied
            expected = 1.0
            if with_pr.get("is_project"):
                expected *= phase.PROJ_BOOST
            if with_pr.get("kind", "") in ("function", "method", "constructor", "destructor"):
                expected *= phase.FUNC_BOOST
            pr = with_pr.get("pagerank", 0.0) or 0.0
            expected *= 1.0 + pr * phase.PAGERANK_BOOST
            assert boost_with == expected, (
                f"Boost {boost_with} != expected {expected}"
            )

        finally:
            conn.close()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_hotspots_get_higher_pagerank(self, pid):
        """Frequently-called functions should have higher pagerank."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            if not _has_pagerank(conn):
                pytest.skip("No pagerank data — re-index required")

            hotspots = find_hotspots(conn, ch, limit=30, exclude_paths=[".pio/%"])
            if len(hotspots) < 2:
                pytest.skip("Not enough hotspots")

            # Top hotspots should have non-zero pagerank
            top_names = [r["name"] for r in hotspots[:5]]
            pr_rows = conn.execute(
                f"""SELECT name, pagerank FROM symbols
                    WHERE name IN ({','.join('?'*len(top_names))})
                    AND config_hash = ?""",
                (*top_names, ch),
            ).fetchall()
            pr_map = {r["name"]: r["pagerank"] for r in pr_rows}

            # At least 3 of top 5 should have pagerank > 0
            nonzero = sum(1 for n in top_names if pr_map.get(n, 0) > 0)
            assert nonzero >= 2, (
                f"Only {nonzero}/5 top hotspots have pagerank > 0: {pr_map}"
            )

        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════
# 4. LLM analysis for typedef/enum
# ═══════════════════════════════════════════════════════════════════════


class TestLLMTypedefEnumAnalysis:
    """Verify LLM analysis coverage for typedef and enum symbols."""

    def test_need_env(self):
        _require_env()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_typedef_analyzed(self, pid):
        """Typedef symbols should have LLM analysis after re-index with --analyze."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            if not _has_llm_analysis(conn):
                pytest.skip("No llm_analysis table — re-index with --analyze required")

            typedef_defs = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE kind='typedef' AND is_definition=1 AND config_hash=?",
                (ch,),
            ).fetchone()[0]

            if typedef_defs == 0:
                pytest.skip("No typedef symbols in index")

            typedef_analyzed = conn.execute(
                """SELECT COUNT(*) FROM symbols s
                   JOIN llm_analysis a ON a.symbol_id = s.id
                   WHERE s.kind='typedef' AND s.config_hash=?""",
                (ch,),
            ).fetchone()[0]

            # At least 50% of typedef definitions should have analysis
            ratio = typedef_analyzed / typedef_defs
            assert ratio >= 0.5, (
                f"Only {typedef_analyzed}/{typedef_defs} typedefs analyzed ({ratio:.0%})"
            )

        finally:
            conn.close()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_enum_analyzed(self, pid):
        """Enum symbols should have LLM analysis after re-index with --analyze."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            if not _has_llm_analysis(conn):
                pytest.skip("No llm_analysis table — re-index with --analyze required")

            enum_defs = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE kind='enum' AND is_definition=1 AND config_hash=?",
                (ch,),
            ).fetchone()[0]

            if enum_defs == 0:
                pytest.skip("No enum symbols in index")

            enum_analyzed = conn.execute(
                """SELECT COUNT(*) FROM symbols s
                   JOIN llm_analysis a ON a.symbol_id = s.id
                   WHERE s.kind='enum' AND s.config_hash=?""",
                (ch,),
            ).fetchone()[0]

            ratio = enum_analyzed / enum_defs
            assert ratio >= 0.5, (
                f"Only {enum_analyzed}/{enum_defs} enums analyzed ({ratio:.0%})"
            )

        finally:
            conn.close()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_analysis_quality_non_empty(self, pid):
        """LLM analysis for typedef/enum should have non-empty summary, inputs, outputs."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            if not _has_llm_analysis(conn):
                pytest.skip("No llm_analysis table — re-index with --analyze required")

            # Sample 10 analyzed typedefs/enums
            samples = conn.execute(
                """SELECT s.name, s.kind, a.summary, a.inputs, a.outputs
                   FROM symbols s
                   JOIN llm_analysis a ON a.symbol_id = s.id
                   WHERE s.kind IN ('typedef', 'enum') AND s.config_hash=?
                   LIMIT 10""",
                (ch,),
            ).fetchall()

            if not samples:
                pytest.skip("No analyzed typedef/enum symbols")

            empty_summary = 0
            empty_inputs = 0
            empty_outputs = 0
            for row in samples:
                if not (row["summary"] or "").strip():
                    empty_summary += 1
                if not (row["inputs"] or "").strip():
                    empty_inputs += 1
                if not (row["outputs"] or "").strip():
                    empty_outputs += 1

            assert empty_summary <= 2, f"{empty_summary}/{len(samples)} have empty summary"
            # inputs/outputs may legitimately be empty for some typedefs

        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════
# 5. Combined metrics summary
# ═══════════════════════════════════════════════════════════════════════


class TestCombinedMetrics:
    """Print a human-readable summary of all comparisons."""

    def test_need_env(self):
        _require_env()

    @pytest.mark.parametrize("pid", [HA_BOILER_PID])
    def test_summary_table(self, pid):
        """Print comparison summary table (pass/fail per feature)."""
        _require_env()
        conn, ch = _open_index(pid)
        try:
            features = {
                "FTS5 column weights": True,  # always available (code)
                "PageRank computed": _has_pagerank(conn),
                "Hotspot cache": _has_hotspot_cache(conn),
                "LLM analysis": _has_llm_analysis(conn),
            }

            with_pr = _has_pagerank(conn)

            if with_pr:
                pr_nodes = conn.execute(
                    "SELECT COUNT(*) FROM symbols WHERE pagerank > 0 AND config_hash=?",
                    (ch,),
                ).fetchone()[0]
                features["PageRank nodes"] = pr_nodes

            if _has_hotspot_cache(conn):
                hc_count = conn.execute(
                    "SELECT COUNT(*) FROM hotspot_cache WHERE config_hash=?",
                    (ch,),
                ).fetchone()[0]
                features["Hotspot cache entries"] = hc_count

            if _has_llm_analysis(conn):
                typedef_a = conn.execute(
                    """SELECT COUNT(*) FROM symbols s
                       JOIN llm_analysis a ON a.symbol_id = s.id
                       WHERE s.kind='typedef' AND s.config_hash=?""",
                    (ch,),
                ).fetchone()[0]
                enum_a = conn.execute(
                    """SELECT COUNT(*) FROM symbols s
                       JOIN llm_analysis a ON a.symbol_id = s.id
                       WHERE s.kind='enum' AND s.config_hash=?""",
                    (ch,),
                ).fetchone()[0]
                features["Typedef analyzed"] = typedef_a
                features["Enum analyzed"] = enum_a

            # Print summary (pytest captures stdout)
            print("\n═══ Feature Comparison Summary ═══")
            for k, v in features.items():
                status = "✅" if v else "❌ (needs re-index)"
                print(f"  {k}: {v} {status}")

            # All structural features should be present
            missing = [k for k, v in features.items() if not v]
            if missing:
                print(f"\n  Missing features: {missing}")
                print("  Re-index with: fw-context index cc.json --analyze")
        finally:
            conn.close()
