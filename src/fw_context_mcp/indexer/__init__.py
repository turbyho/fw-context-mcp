"""Indexer package — libclang-powered C/C++ symbol extraction and storage.

This package is the core data pipeline of fw-context.  It transforms
``compile_commands.json`` into a queryable SQLite symbol index through
the following pipeline:

1. **Build** (``build.py``) — generate ``compile_commands.json`` from
   the detected build system (Mbed OS, Zephyr, PlatformIO).
2. **Validate** (``validator.py``) — check completeness, fix Windows
   path separators, detect stale entries.
3. **Extract** (``extractor.py``) — parse every translation unit with
   libclang and extract symbol metadata (name, kind, signature, USR,
   source extent).
4. **Refs** (``_callgraph.py``, ``_references.py``) — build the
   cross-reference index: callers, callees, indirect edges through
   function pointers and ISRs.
5. **Embeddings** (``_embedding.py``) — generate vector embeddings for
   every symbol using the configured embedding model.
6. **Analyze** (``_analyzer.py``) — optional LLM-generated summaries
   (what the symbol does, its inputs, its outputs).
7. **Store** (``db/``) — write everything into the SQLite database
   with FTS5 for full-text search.

The runner (``runner.py``) orchestrates these phases and handles
incremental updates: it skips files whose mtime or content hash
has not changed since the last index run.

Design notes:
* All extraction is single-threaded by default because libclang
  is not thread-safe across translation units.
* Embedding and analysis phases are parallelized via thread pools.
* The index supports multiple build configurations (each identified
  by a ``config_hash``) in a single database.

Re-exports are deferred — see ``db/__init__.py`` for canonical imports.
"""
