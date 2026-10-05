"""The one answer of smart_search that the next pages are read from.

WHY a cache: an LLM generates the queries of smart_search, thus two runs
of one question can search for different words.  A page of a second run
would not continue the page of the first: rows would come twice, or never.
The pages of one answer must therefore come from ONE run.

The rules (decided by the operator):

* ``offset`` 0 — also when the caller gives no offset — is a new search.
  Its answer replaces what the slot held.
* ``offset`` above 0 reads the page out of the slot, when the slot holds
  the answer of the same query on the same build.  When it does not (an
  other query, an other project, a reindex that changed the build, a
  restart of the server), the tool runs the search again, stores it,
  and gives the page.
* No other tool touches the slot, and the slot has no time limit.

ONE slot and not a cache of many answers: the question that pages is the
question that the caller asked last.  An older answer would hold memory
for a page that nobody reads.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path


def answer_key(db_path: Path, config_hash: str, query: str) -> str:
    """The key of one answer: the database, the build and the query.

    The build is part of it because a reindex with a changed build gives
    a new ``config_hash``, and the old answer then names rows of a build
    that no longer exists.
    """
    text = "\0".join((str(db_path), config_hash, query))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachedAnswer:
    """The rows of one smart_search answer, split for the pages.

    *meta* are the rows that describe the search (``_generated_queries``,
    ``_translated_from``, a ``warning`` of a phase).  They come with each
    page.  *symbols* are the rows that the pages are cut from.
    """

    key: str
    meta: tuple[dict, ...]
    symbols: tuple[dict, ...]
    # True when the LLM setup or the embedding step failed (an ollama_warning
    # of the pipeline): the meta rows then carry the warning,
    # and no staleness warning goes with the pages.
    llm_failed: bool = False


class SmartSearchCache:
    """One slot, guarded by a lock: the tools of the server can run in threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._answer: CachedAnswer | None = None

    def get(self, key: str) -> CachedAnswer | None:
        """The stored answer when it has *key*, else None."""
        with self._lock:
            if self._answer is not None and self._answer.key == key:
                return self._answer
            return None

    def put(self, answer: CachedAnswer) -> None:
        """Store *answer* in place of what the slot held."""
        with self._lock:
            self._answer = answer

    def clear(self) -> None:
        """Empty the slot.

        The server calls it in one case only: a new search (offset 0) that
        passes the timeout.  Its partial answer is not stored, and the
        older answer must not serve the next page of the new search.
        Tests call it too.
        """
        with self._lock:
            self._answer = None


# The slot of this server process.  The MCP server serves one client, thus
# one process holds the answers of one client.
SMART_SEARCH_CACHE = SmartSearchCache()
