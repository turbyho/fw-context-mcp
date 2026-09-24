"""The limits of the cache server, in one place for the server and the client.

WHY one module: the server refuses a request that breaks one of these
limits, and the client must not send one.  The client had its own copies,
and only of some limits.  A local entry with a summary of 5001 characters
thus got 422 for its whole request, on each push, and the entries after it
never reached the server.

A leaf module with no imports outside the standard library: the client runs
where FastAPI and Pydantic are not installed, thus it cannot import
``cache_server``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# The most hashes or entries that the server takes from one batch request.
# It keeps the first MAX_BATCH_ENTRIES and reports ``truncated: true``.
MAX_BATCH_ENTRIES = 1000

# The largest request body that the server takes.  It answers 413 above it.
MAX_BODY_BYTES = 10 * 1024 * 1024

# The most characters (not bytes) in each text field of one entry.
# ``summary`` at 5000 and ``inputs``/``outputs`` at 100000 stop one entry
# from making the cache table too large.
FIELD_MAX_CHARS: Mapping[str, int] = {
    "summary": 5000,
    "inputs": 100000,
    "outputs": 100000,
    "model": 100,
}

# A content hash is a SHA-256 in lowercase hex.
HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def entry_violation(entry: Mapping[str, object]) -> str | None:
    """Return why the server would refuse *entry*, or None when it takes it.

    The checks are those of ``cache_server.app.CacheEntry``, in the same
    order, thus the reason names the first field that the server would name.
    A field that is not a string breaks the limit too: the local cache can
    hold NULL, and the server needs a string.
    """
    content_hash = entry.get("hash")
    if not isinstance(content_hash, str) or not HASH_PATTERN.match(content_hash):
        return "hash is not a 64-character lowercase hex string"
    for name, limit in FIELD_MAX_CHARS.items():
        value = entry.get(name)
        if not isinstance(value, str):
            return f"{name} is not a string"
        if len(value) > limit:
            return f"{name} has {len(value)} characters, more than {limit}"
    return None
