# Search Pipeline Experiments

Evaluation harnesses and grid search scripts developed alongside
the fw-context FTS5 + embedding search pipeline.

## Evaluation harness
- `eval_harness.py` — full evaluation harness
- `method_comparison.py` — compare search methods (FTS5 vs embedding vs hybrid)

## Grid search & tuning
- `rrf_boost_grid.py` — RRF k-parameter grid search
- `weight_grid_search.py` — weight parameter search
- `test_per_query_idf.py` — per-query IDF normalization experiment

## RRF & fusion tests
- `test_rrf_fusion.py` — RRF fusion baseline
- `test_adaptive_rrf.py` — adaptive RRF weighting
- `test_context_expansion.py` — context expansion experiment

## Graph edge weighting
- `test_graph_edges.py` → `test_graph_edges_v4.py` — graph-edge-based ranking experiments (v1–v4)

## Database analysis
- `analyze_*.py` — ad-hoc SQLite analysis scripts
- `diagnose_*.py` — diagnostic scripts for specific ranking issues
- `check_index_state.py`, `compare_before_after.py` — index state inspection

## Datasets
- `datasets/bare/` — bare-metal project queries
- `datasets/ha_boiler/` — Home Assistant boiler queries
- `datasets/zbox_ecb/` — zbox ECB parcel locker queries

All scripts require an already-indexed project. Run from workspace root.
