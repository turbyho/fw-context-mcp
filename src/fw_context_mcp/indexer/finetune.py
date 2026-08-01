"""Self-supervised embedding fine-tuning per project.

Based on the vstash approach (arXiv:2604.15484):
  1. Generate synthetic queries from symbol descriptions (no LLM needed).
  2. For each query, run dense (vec0 KNN) and FTS5 searches → top-10.
  3. Mismatches between the two rankings = free training signal.
  4. Fine-tune the base embedding model with MultipleNegativesRankingLoss.
  5. Save the fine-tuned model to ~/.fw-context/models/<project>/.

vstash results: 74.5 % disagreement rate, +19.5 % NDCG@10 after FT.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .db import search_symbols

if TYPE_CHECKING:
    from ..config.settings import Config
    from ..llm.embedder import Embedder

log = logging.getLogger(__name__)

FT_MODELS_DIR = Path.home() / ".fw-context" / "models"
DISAGREEMENT_TOP_K = 10
DENSE_OVERFETCH = 30
MIN_DISAGREEMENT_TRIPLES = 100
DEFAULT_EPOCHS = 3
DEFAULT_BATCH_SIZE = 16
DEFAULT_WARMUP_STEPS = 100


# ── Query templates for synthetic query generation ──
# Each template produces a query from a symbol row (name, kind, doc, summary).
_QUERY_TEMPLATES: list[tuple[str, float]] = [
    ("function that {summary}", 0.35),
    ("code for {kind} {name}", 0.25),
    ("{summary}", 0.20),
    ("implementation of {name}", 0.10),
    ("{kind} {name} usage", 0.05),
    ("{docstring}", 0.05),
]


@dataclass
class MiningResult:
    """One disagreement mine pass output."""

    triples: list[dict] = field(default_factory=list)
    queries_generated: int = 0
    queries_skipped: int = 0
    disagreements_found: int = 0
    elapsed_s: float = 0.0


def _generate_queries(conn: sqlite3.Connection, config_hash: str) -> list[tuple[str, int]]:
    """Generate synthetic queries from symbol descriptions.

    Each symbol produces ONE query via weighted-random template selection
    (not all 6 templates — ``_QUERY_TEMPLATES`` weights bias toward
    summary-based templates which produce more meaningful queries, and
    one-per-symbol avoids bloating the training set with near-duplicates).

    Returns ``(query_text, symbol_id)`` pairs — the symbol_id anchors
    to compute disagreement later.
    """
    import random as _random

    rows = conn.execute(
        """SELECT id, name, kind, docstring, summary, is_project
           FROM symbols
           WHERE config_hash = ?
             AND is_definition = 1
             AND kind IN ('function', 'method',
                          'constructor', 'destructor')
             AND (summary IS NOT NULL OR docstring IS NOT NULL)
           ORDER BY is_project DESC, name, id""",
        (config_hash,),
    ).fetchall()

    templates = [t[0] for t in _QUERY_TEMPLATES]
    weights = [t[1] for t in _QUERY_TEMPLATES]

    queries: list[tuple[str, int]] = []
    for r in rows:
        name = r["name"] or ""
        kind = r["kind"] or ""
        doc = (r["docstring"] or "").strip()
        summary = (r["summary"] or "").strip()
        if not summary and not doc:
            continue
        doc = doc[:120]
        summary = summary[:120]

        template = _random.choices(templates, weights=weights, k=1)[0]
        try:
            q = template.format(
                name=name, kind=kind, docstring=doc, summary=summary
            )
        except (KeyError, ValueError) as e:
            log.warning("Template format failed for %r: %s", template, e)
            continue
        q = q.strip().rstrip(".")
        if len(q) < 8:
            continue
        queries.append((q, r["id"]))

    log.info(
        "Generated %d synthetic queries from %d symbol descriptions",
        len(queries), len(rows),
    )
    return queries


def _fts5_search(
    conn: sqlite3.Connection, query: str, config_hash: str, limit: int = DISAGREEMENT_TOP_K
) -> list[int]:
    """FTS5 search — returns top *limit* symbol IDs."""
    rows = search_symbols(conn, query, config_hash, limit=limit)
    return [r["id"] for r in rows]


def _dense_search(
    conn: sqlite3.Connection,
    query_vec: list[float],
    config_hash: str,
    model_key: str,
    limit: int = DENSE_OVERFETCH,
) -> list[int]:
    """Dense (vec0 KNN) search — returns top *limit* symbol IDs."""
    rows = conn.execute(
        """SELECT symbol_id, distance
           FROM vec_symbols
           WHERE embedding MATCH ?
             AND config_hash = ?
             AND k = ?
           ORDER BY distance""",
        (json.dumps(query_vec), config_hash, limit),
    ).fetchall()
    return [r["symbol_id"] for r in rows]


def mine_disagreements(
    config: Config,
    db_path: Path,
    embedder: Embedder,
    sample_limit: int = 2000,
    seed: int = 42,
) -> MiningResult:
    """Mine disagreement triples between dense and FTS5 retrieval.

    Opens the project database at *db_path*, generates synthetic queries,
    embeds them with *embedder*, runs both dense and FTS5 searches, and
    extracts training triples where the two rankings disagree.

    *sample_limit* caps the number of queries to process (avoiding
    excessive time on large projects with thousands of symbols).
    """
    import random as _random_import

    _random = _random_import.Random(seed)

    t0 = time.monotonic()
    result = MiningResult()

    try:
        import sqlite_vec
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (sqlite3.OperationalError, ImportError, AttributeError) as e:
        log.warning("Cannot open database %s: %s", db_path, e)
        return result

    try:
        with conn:
            conn.row_factory = sqlite3.Row

        config_hash = conn.execute(
            "SELECT config_hash FROM build_configs ORDER BY first_indexed_at DESC LIMIT 1"
        ).fetchone()
        if config_hash is None:
            log.warning("No build config found in database — skipping mining")
            return result
        config_hash = config_hash["config_hash"]

        queries = _generate_queries(conn, config_hash)
        result.queries_generated = len(queries)

        if len(queries) > sample_limit:
            queries = _random.sample(queries, sample_limit)
            log.info("Sampled %d queries from %d total", len(queries), result.queries_generated)

        if not queries:
            return result

        # Batch-embed all queries (much faster than one at a time)
        query_texts = [q[0] for q in queries]
        query_symbol_ids = [q[1] for q in queries]
        try:
            query_vecs = embedder.embed_queries(query_texts)
        except (RuntimeError, ConnectionError, OSError) as e:
            log.error("Failed to embed queries: %s", e)
            return result

        if len(query_vecs) != len(query_texts):
            log.error(
                "Embedder returned %d vectors for %d texts — mismatch",
                len(query_vecs), len(query_texts),
            )
            return result

        model_key = config.llm.embed_key()
        skipped = 0

        for i, (qid, qvec) in enumerate(zip(query_symbol_ids, query_vecs, strict=True)):
            query_text = query_texts[i]

            fts_ids = _fts5_search(conn, query_text, config_hash)
            dense_ids = _dense_search(conn, qvec, config_hash, model_key)

            if qid in fts_ids:
                fts_ids.remove(qid)
            if qid in dense_ids:
                dense_ids.remove(qid)

            fts_set = set(fts_ids[:DISAGREEMENT_TOP_K])
            dense_set = set(dense_ids[:DISAGREEMENT_TOP_K])

            # Skip queries where dense returned too few results
            if len(dense_set) < 3 or len(fts_set) < 3:
                skipped += 1
                continue

            # Disagreement: dense-wins symbols (in dense but not in FTS5)
            dense_wins = dense_set - fts_set
            fts_wins = fts_set - dense_set

            if not dense_wins or not fts_wins:
                continue

            # Build triples: (query, positive_dense, negative_fts)
            positive = _random.choice(list(dense_wins))
            negative = _random.choice(list(fts_wins))

            result.triples.append({
                "query": query_text,
                "positive_id": positive,
                "negative_id": negative,
            })
            result.disagreements_found += 1

            if result.disagreements_found % 100 == 0:
                elapsed = time.monotonic() - t0
                log.info(
                    "Mining: %d/%d queries, %d triples (%.1fs)",
                    i + 1, len(query_texts), result.disagreements_found, elapsed,
                )
        result.queries_skipped = skipped
        result.elapsed_s = time.monotonic() - t0

        log.info(
            "Mined %d triples from %d queries (skipped %d) in %.1fs",
            result.disagreements_found, len(queries), skipped, result.elapsed_s,
        )
        return result
    finally:
        conn.close()

def _load_triple_bodies(
    conn: sqlite3.Connection, config_hash: str, triple_ids: set[int]
) -> dict[int, str]:
    """Load description text for symbol IDs referenced in triples."""
    if not triple_ids:
        return {}
    placeholders = ",".join("?" * len(triple_ids))
    rows = conn.execute(
        f"""SELECT id, name, kind, signature, docstring, summary
           FROM symbols
           WHERE config_hash = ? AND id IN ({placeholders})""",
        (config_hash, *triple_ids),
    ).fetchall()
    bodies: dict[int, str] = {}
    for r in rows:
        parts = [f"{r['kind']} {r['name']}"]
        if r["signature"]:
            parts.append(r["signature"])
        if r["summary"]:
            parts.append(r["summary"])
        if r["docstring"]:
            parts.append(r["docstring"][:150])
        bodies[r["id"]] = " : ".join(parts)
    return bodies


def train_step(
    triples: list[dict],
    db_path: Path,
    embedder: Embedder,
    out_dir: Path,
    epochs: int = DEFAULT_EPOCHS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    warmup_steps: int = DEFAULT_WARMUP_STEPS,
) -> Path | None:
    """Fine-tune the base model on disagreement triples.

    Uses ``sentence-transformers`` ``MultipleNegativesRankingLoss``.
    The base model is loaded through *embedder* (must be
    ``SentenceTransformerEmbedder`` with the ST model loaded).

    Returns the output directory on success, None on failure.
    """
    try:
        from datasets import Dataset as HFDataset
        from sentence_transformers import losses  # type: ignore[attr-defined]
        from sentence_transformers.trainer import SentenceTransformerTrainer
        from sentence_transformers.training_args import SentenceTransformerTrainingArguments
    except ImportError as e:
        log.error("Missing dependency for fine-tuning: %s", e)
        return None

    if len(triples) < MIN_DISAGREEMENT_TRIPLES:
        log.warning(
            "Only %d triples — need >= %d for meaningful fine-tuning",
            len(triples), MIN_DISAGREEMENT_TRIPLES,
        )
        return None

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT config_hash FROM build_configs ORDER BY first_indexed_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            log.error("No build config found in database")
            return None
        config_hash = row["config_hash"]

        all_ids = set()
        for t in triples:
            all_ids.add(t["positive_id"])
            all_ids.add(t["negative_id"])
        bodies = _load_triple_bodies(conn, config_hash, all_ids)

    from ..llm.st_embedder import SentenceTransformerEmbedder

    if not isinstance(embedder, SentenceTransformerEmbedder):
        log.error("Embedder must be a SentenceTransformerEmbedder for fine-tuning")
        return None

    try:
        st_model = embedder.model
    except (RuntimeError, OSError) as e:
        log.error("Failed to load embedding model: %s", e)
        return None

    query_texts: list[str] = []
    positive_texts: list[str] = []
    for t in triples:
        pbody = bodies.get(t["positive_id"], "")
        if not pbody:
            continue
        query_texts.append(t["query"])
        positive_texts.append(pbody)

    if len(query_texts) < 10:
        log.error("Too few valid triples after body lookup")
        return None

    train_dataset = HFDataset.from_dict({
        "anchor": query_texts,
        "positive": positive_texts,
    })

    loss_fn = losses.MultipleNegativesRankingLoss(st_model)

    output_path = str(out_dir)
    args = SentenceTransformerTrainingArguments(
        output_dir=output_path,
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        warmup_steps=max(1, min(warmup_steps, len(query_texts) // 2)),
        logging_steps=10,
        save_strategy="epoch",
        save_total_limit=2,
    )

    trainer = SentenceTransformerTrainer(
        model=st_model,
        args=args,
        train_dataset=train_dataset,
        loss=loss_fn,
    )
    trainer.train()

    final_path = Path(output_path) / "final"
    final_path.mkdir(parents=True, exist_ok=True)
    st_model.save(str(final_path))
    metadata = {
        "base_model": embedder.name,
        "triples_count": len(triples),
        "created_at": time.time(),
        "desc_version": config_hash,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    log.info("Fine-tuned model saved to %s", out_dir)

    return out_dir


def run_finetune(
    config: Config,
    db_path: Path,
    project_id: str,
    sample_limit: int = 2000,
    epochs: int = DEFAULT_EPOCHS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Path | None:
    """Run the full fine-tuning pipeline: mine → train.

    Returns the model output directory on success.
    """
    from ..config.settings import DESCRIPTION_VERSION
    from ..llm.embedder_factory import get_embedder

    if not db_path.exists():
        log.error("Database not found: %s", db_path)
        return None

    embedder = get_embedder(config.llm)
    from ..llm.st_embedder import SentenceTransformerEmbedder

    if isinstance(embedder, SentenceTransformerEmbedder):
        pass  # ST-based — ok
    else:
        # Ollama-based models can't be fine-tuned locally
        log.warning(
            "Model '%s' cannot be fine-tuned with sentence-transformers. "
            "Use an ST-compatible base model (e.g., BAAI/bge-large-en-v1.5).",
            embedder.name,
        )
        return None

    log.info("Phase 1/2: Mining disagreement triples...")
    mining = mine_disagreements(config, db_path, embedder, sample_limit=sample_limit)
    if mining.disagreements_found == 0:
        log.warning("No disagreements found — couldn't mine triples")
        return None

    out_dir = FT_MODELS_DIR / project_id / f"{embedder.name.replace('/', '_')}-ft-{DESCRIPTION_VERSION}"

    log.info(
        "Phase 2/2: Fine-tuning on %d triples...",
        len(mining.triples),
    )
    result = train_step(
        mining.triples, db_path, embedder, out_dir,
        epochs=epochs, batch_size=batch_size,
    )
    return result
