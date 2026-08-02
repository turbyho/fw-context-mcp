"""Shared helpers for MCP tools — DB context, stale detection, SDK filtering, fallbacks.

Import from ``.context`` for the re-export hub (connection management,
readiness checks).  Individual modules:

- ``context.py`` — re-export hub (import from here)
- ``connection.py`` — DB connection caching, integrity checks, HandlerContext
- ``readiness.py`` — project readiness, build system detection
- ``stale.py`` — file staleness detection, auto-reindex, header staleness
- ``fallback.py`` — fallback to lexical FTS5 when embeddings unavailable
- ``filtering.py`` — SDK/vendor path LIKE pattern computation
- ``brace_matcher.py`` — C/C++ brace matching for function body extraction
- ``pid_file.py`` — PID file lifecycle (reindex coordination markers)
"""
