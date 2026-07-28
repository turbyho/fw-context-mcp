"""DB-backed evaluation harness for retrieval quality.

Runs query datasets against a real indexed project database through
different retrieval variants (FTS5-only, dense-only, RRF-hybrid) and
produces EvalReport via ``tests/quality_eval.py``.

Usage::

    python -m experiments.eval_harness \\
        --project tests/builds/bare \\
        --dataset experiments/datasets/bare/queries.json \\
        --variant hybrid
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from fw_context_mcp.config import derive_project_id
from fw_context_mcp.config import load as load_config
from fw_context_mcp.indexer.db import _cosine_sim, get_active_config, open_db
from fw_context_mcp.llm.embedder import get_embedder
from fw_context_mcp.llm.ollama import OllamaError

log = logging.getLogger(__name__)


def _add_sys_path():
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))


_add_sys_path()
from tests.quality_eval import EvalMetrics, EvalReport, aggregate, compare, evaluate  # noqa: E402


class EvalHarness:
    """DB-backed evaluation harness for code retrieval quality."""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        cfg = load_config(project_root=self.project_root)
        project_id = derive_project_id(self.project_root)
        self.db_path = cfg.index.db_dir / project_id / "index.db"
        if not self.db_path.exists():
            raise FileNotFoundError(f"No index found at {self.db_path}. Run 'fw-context index' first.")
        self.cfg = cfg

        conn = open_db(self.db_path)
        try:
            data = get_active_config(conn, project_id)
            if not data:
                raise RuntimeError("No active build config found in index")
            self.config_hash = data["config_hash"]
        finally:
            conn.close()

    # ── FTS5-only retrieval ──────────────────────────────────────────

    def run_fts5(self, query: str, limit: int = 20) -> list[dict]:
        from fw_context_mcp.indexer.db import _expand_query, search_symbols
        from fw_context_mcp.indexer.db import open_db as _open_db

        conn = _open_db(self.db_path)
        try:
            expanded = _expand_query(query)
            rows = search_symbols(
                conn, expanded, self.config_hash,
                limit=limit, project_only=False,
            )
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Dense-only retrieval (model-filtered) ────────────────────────

    def run_dense(self, query: str, limit: int = 50) -> list[dict]:
        """Dense retrieval with explicit model filter."""
        conn = open_db(self.db_path)
        try:
            embedder = get_embedder(self.cfg.llm)
            qvecs = embedder.embed_queries([query])
            query_vec = qvecs[0]
            model_key = self.cfg.llm.embed_key()

            total = conn.execute(
                """SELECT COUNT(*)
                   FROM embeddings e
                   JOIN symbols s ON s.id = e.symbol_id
                   WHERE s.config_hash = ? AND s.is_definition = 1
                     AND e.model = ?
                     AND e.chunk_index = 0""",
                (self.config_hash, model_key),
            ).fetchone()[0]

            if total == 0:
                return []

            BATCH = 1000
            keep = limit * 5
            top_candidates: list[tuple[float, int]] = []

            for offset in range(0, total, BATCH):
                rows = conn.execute(
                    """SELECT e.symbol_id, e.embedding
                       FROM embeddings e
                       JOIN symbols s ON s.id = e.symbol_id
                       WHERE s.config_hash = ? AND s.is_definition = 1
                         AND e.model = ?
                         AND e.chunk_index = 0
                       ORDER BY e.symbol_id
                       LIMIT ? OFFSET ?""",
                    (self.config_hash, model_key, BATCH, offset),
                ).fetchall()

                for r in rows:
                    raw_sim = _cosine_sim(query_vec, r["embedding"])
                    if raw_sim is None:
                        continue
                    top_candidates.append((raw_sim, r["symbol_id"]))

                top_candidates.sort(key=lambda x: -x[0])
                if len(top_candidates) > keep:
                    top_candidates = top_candidates[:keep]

            top_candidates.sort(key=lambda x: -x[0])
            top = top_candidates[:limit]

            if not top:
                return []

            sym_ids = [sid for _, sid in top]
            placeholders = ",".join("?" * len(sym_ids))
            sym_rows = conn.execute(
                f"""SELECT * FROM symbols
                    WHERE config_hash = ? AND id IN ({placeholders})
                    ORDER BY CASE id {' '.join(f'WHEN {i} THEN {j}' for j, i in enumerate(sym_ids))} END""",
                (self.config_hash, *sym_ids),
            ).fetchall()

            return [dict(r) for r in sym_rows]
        finally:
            conn.close()

    # ── Hybrid retrieval (smart_search pipeline) ─────────────────────

    async def run_hybrid(self, query: str, limit: int = 20) -> list[dict]:
        from fw_context_mcp.search.context import PipelineContext
        from fw_context_mcp.search.pipeline import PipelineRunner, _build_smart_search

        ctx = PipelineContext.create(query=query, project_root=str(self.project_root), limit=limit)
        config = _build_smart_search()
        runner = PipelineRunner(config)
        ctx = await runner.run(ctx)
        return list(ctx.formatted_results)

    # ── Evaluation ──────────────────────────────────────────────────

    def evaluate_dataset(
        self,
        dataset_path: str | Path,
        variants: list[str] | None = None,
    ) -> list[EvalReport]:
        """Run all variants against a dataset and return reports."""
        if variants is None:
            variants = ["fts5", "dense", "hybrid"]

        with open(dataset_path) as f:
            dataset = json.load(f)
        queries = dataset["queries"]

        all_metrics: dict[str, list[EvalMetrics]] = {v: [] for v in variants}

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        for qd in queries:
            query = qd["query"]
            relevant = set(tuple(r) for r in qd["relevant"])
            split = qd.get("split", "unknown")
            params = {"split": split}

            if "fts5" in variants:
                fts5_results = self.run_fts5(query)
                metrics = evaluate(fts5_results, relevant, query, "fts5", params=params)
                all_metrics["fts5"].append(metrics)

            if "dense" in variants:
                dense_results = self.run_dense(query)
                metrics = evaluate(dense_results, relevant, query, "dense", params=params)
                all_metrics["dense"].append(metrics)

            if "hybrid" in variants:
                try:
                    hybrid_results = loop.run_until_complete(self.run_hybrid(query))
                    metrics = evaluate(hybrid_results, relevant, query, "hybrid", params=params)
                    all_metrics["hybrid"].append(metrics)
                except OllamaError:
                    log.warning("Hybrid eval skipped for '%s' — Ollama unavailable", query)

        loop.close()

        reports = []
        for variant in variants:
            if all_metrics[variant]:
                reports.append(aggregate(all_metrics[variant], variant))

        return reports


def main():
    parser = argparse.ArgumentParser(description="Evaluate code retrieval quality")
    parser.add_argument("--project", required=True, help="Project root directory")
    parser.add_argument("--dataset", required=True, help="Path to queries.json")
    parser.add_argument("--variant", default="fts5,dense,hybrid", help="Comma-separated variants")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    harness = EvalHarness(args.project)
    variants = [v.strip() for v in args.variant.split(",")]

    reports = harness.evaluate_dataset(args.dataset, variants=variants)
    for report in reports:
        print(report.summary())

    # A/B comparison for hybrid vs FTS5 baseline
    baseline = next((r for r in reports if r.variant == "fts5"), None)
    candidate = next((r for r in reports if r.variant == "hybrid"), None)
    if baseline and candidate:
        print()
        print(compare(baseline, candidate))

    # Decision gates
    dense_report = next((r for r in reports if r.variant == "dense"), None)
    if dense_report:
        split_impl_metrics = [
            m for m in dense_report.per_query
            if m.params.get("split") == "impl"
        ]
        split_sig_metrics = [
            m for m in dense_report.per_query
            if m.params.get("split") == "sig"
        ]
        if split_impl_metrics and split_sig_metrics:
            impl_report = aggregate(split_impl_metrics, "dense_impl")
            sig_report = aggregate(split_sig_metrics, "dense_sig")
            print()
            print(impl_report.summary())
            print(sig_report.summary())


if __name__ == "__main__":
    main()
