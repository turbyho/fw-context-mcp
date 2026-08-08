"""``fw-context finetune`` — self-supervised fine-tuning of the embedding model.

Mines disagreement triples between dense (vec0 KNN) and lexical (FTS5)
retrieval, then fine-tunes the base embedding model with
MultipleNegativesRankingLoss.  The resulting model captures project-specific
naming conventions and domain vocabulary better than the generic base model.

WHY self-supervised: manual labeling of "relevant" vs "irrelevant" search
results is prohibitively expensive.  Disagreement mining automatically
identifies cases where text search finds something the embedding model
missed (or vice versa), generating training data from the index itself.

The fine-tuned model is saved to ``~/.fw-context/models/<project_id>/``
and can be activated by setting ``embed_model = "ft://<path>"`` in config.
"""

from __future__ import annotations

import argparse
import sys


def cmd_finetune(args: argparse.Namespace) -> int:
    """Self-supervised fine-tune the embedding model on project code.

    Mines disagreement triples between dense (vec0 KNN) and lexical (FTS5)
    retrieval, then fine-tunes the base model with
    MultipleNegativesRankingLoss.

    WHY disagreement mining: dense retrieval uses semantic similarity and
    may rank generic names above project-specific ones; lexical FTS5 matches
    exact tokens but misses synonyms.  When they disagree on ranking, the
    disagreement signals where the embedding model needs project-specific
    tuning.  Each triple is (anchor, positive from one method, negative from
    the other) — the model learns to prefer positives over negatives.
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..config.settings import DESCRIPTION_VERSION
    from ..indexer.finetune import FT_MODELS_DIR, run_finetune
    from ..utils import resolve_project_root

    root = resolve_project_root(args.project)
    cfg = load_config(project_root=root)
    project_id = derive_project_id(root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print(f"error: no index found at {db_path}", file=sys.stderr)
        print("Run 'fw-context index --embeddings' first.", file=sys.stderr)
        return 1

    print(f"Fine-tuning embedding model for project: {cfg.project.name or root.name}")
    print(f"  Base model:    {cfg.llm.embed_model}")
    print(f"  Description:   {DESCRIPTION_VERSION}")
    print(f"  Output:        {FT_MODELS_DIR / project_id}")
    print(f"  Sample limit:  {args.sample_limit}")
    print(f"  Epochs:        {args.epochs}")
    print(f"  Batch size:    {args.batch_size}")
    print()

    result = run_finetune(
        cfg,
        db_path,
        project_id,
        sample_limit=args.sample_limit,
        epochs=args.epochs,
        batch_size=args.batch_size,
    )
    if result is None:
        print("Fine-tuning did not produce a model — check logs above.", file=sys.stderr)
        return 1

    print(f"\nFine-tuned model saved to: {result}")
    print("To use it, set in .fw-context/local.toml:")
    print("  [llm]")
    print(f"  embed_model = \"ft://{result}\"")
    return 0
