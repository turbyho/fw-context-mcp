"""Ollama concurrency control — semaphore for HTTP request throttling."""

from __future__ import annotations

import threading

from fw_context_mcp.config.settings import LLMConfig


class _SemaphoreWrapper:
    __slots__ = ("_sem", "_value")

    def __init__(self, value: int) -> None:
        self._sem = threading.BoundedSemaphore(value)
        self._value = value

    def acquire(self) -> bool:
        return self._sem.acquire()

    def release(self) -> None:
        self._sem.release()

    def __enter__(self) -> None:
        self._sem.__enter__()

    def __exit__(self, *args: object) -> None:
        self._sem.__exit__(*args)


_sem_wrapper: _SemaphoreWrapper = _SemaphoreWrapper(1)
_sem_lock = threading.Lock()

# Cache for Ollama model context lengths — populated lazily by OllamaEmbedder.max_tokens
_max_tokens_cache: dict[str, int] = {}


def _reconfigure_ollama_sem(max_concurrent: int) -> None:
    """Replace the semaphore when *max_concurrent* changes.

    Called from ``OllamaEmbedder.__init__`` when config provides a
    non-default ``ollama_max_concurrent``.  Idempotent — a semaphore
    with the same value is not recreated.
    Threads already holding the old wrapper reference are unaffected
    and safely drain their old semaphore.

    .. warning::
        This permanently mutates global state.  Creating an embedder with
        a non-default *max_concurrent* affects all subsequent callers in
        the same process.  Tests that create embedders with different
        values will change the production semaphore.
    """
    global _sem_wrapper
    if max_concurrent < 1:
        max_concurrent = 1
    if max_concurrent != _sem_wrapper._value:
        with _sem_lock:
            if max_concurrent != _sem_wrapper._value:
                _sem_wrapper = _SemaphoreWrapper(max_concurrent)


@contextmanager
def ollama_guard() -> Generator[None, None, None]:
    """Context manager capping concurrent Ollama requests within this process.

    All ``call_ollama`` and ``call_ollama_embed`` calls should be wrapped
    in this guard.  Cross-process serialization is handled indirectly
    through the write lock (``db.write_lock``) — only the process that
    holds the write lock can perform LLM analysis writes.
    """
    # Capture current wrapper ref atomically — threads that captured an
    # old ref before reconfiguration use their (still-valid) semaphore.
    sem = _sem_wrapper
    with sem:
        yield


