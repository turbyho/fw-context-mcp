"""Process exit codes shared by the CLI, the daemon and the indexer.

A leaf module with no imports of its own.  ``mcp/daemon.py`` took these two
constants from ``indexer.runner``, and that import pulled 38 indexer modules
including ``clang.cindex`` (0.08 s) into a file-watcher process that never
parses anything.

The exception that goes with EXIT_SUPERSEDED stays in ``indexer.runner``:
it belongs to the code that raises it, and every raiser already has the
indexer loaded.
"""

from __future__ import annotations

# Process exit status for a run that stopped because another process took the
# index over.  Distinct from 0 (done) and 1 (failed) so the daemon can tell
# the three apart: this one means the work still has to happen, from the
# start.  75 is EX_TEMPFAIL from sysexits.h — a temporary failure, try again.
EXIT_SUPERSEDED = 75

# Process exit status for a run that refused to start because another
# indexing run already owns the index.  Unlike EXIT_SUPERSEDED there is
# nothing to retry: the run that holds the lock is doing this work.
# 69 is EX_UNAVAILABLE from sysexits.h — the service is busy.
EXIT_ALREADY_RUNNING = 69
