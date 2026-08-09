"""Auto-detect the best embedding model for the current system.

Called when ``embed_model`` is empty (default).  Resolution order:

1. User-configured model → use as-is (no auto-detect)
2. GPU available → ``qwen3-embedding:8b`` (pull if missing, best-effort)
3. No GPU → ``qwen3-embedding:0.6b`` (pull if missing, best-effort)

All Ollama calls are best-effort with short timeouts — if Ollama is
offline, the model name is still set and the pull/install is retried
at first embed time by ``OllamaEmbedder``.

WHY auto-detect: The default config ships with ``embed_model = ""``.
First-time setup should "just work" without asking the operator to
choose an embedding model.  Auto-detect picks a reasonable default
based on GPU availability — larger model (8B, better quality) for
GPU, smaller model (0.6B, lower RAM) for CPU.

WHY best-effort pull: The nvidia-smi check and Ollama API probes are
fast (<3s combined).  If either fails (no network, Ollama not running),
the model name is still set — ``OllamaEmbedder`` retries with longer
timeouts at first embed time (via ``auto_pull`` if enabled).  The
operator sees a working system as soon as Ollama starts.

WHY module-level cache: ``resolve_embed_model`` is called on every
config load.  GPU detection (subprocess) and Ollama API probes
(HTTP) add latency — caching avoids re-running these on every MCP
tool invocation.  The environment (GPU presence, installed models)
doesn't change within a process lifetime.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.settings import LLMConfig

log = logging.getLogger(__name__)

_DEFAULT_CPU_MODEL = "qwen3-embedding:0.6b"
_DEFAULT_GPU_MODEL = "qwen3-embedding:8b"

# Cache: once resolved, reuse across all config loads within the same process.
# The first call does nvidia-smi + Ollama API checks; subsequent calls
# return instantly.  Cached independently per process — subprocesses
# (e.g. MCP tool workers) resolve once each.
_cached_model: str | None = None
_auto_model_lock = threading.Lock()


def _reset_auto_model_cache() -> None:
    """Reset the cached auto-detection result. Used by tests only."""
    global _cached_model
    _cached_model = None


def _gpu_available() -> bool:
    """Check for GPU availability: NVIDIA via nvidia-smi, Apple Silicon via sysctl."""
    if _apple_silicon_gpu():
        return True

    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0 and "GPU" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _apple_silicon_gpu() -> bool:
    """Check for Apple Silicon GPU (M1/M2/M3/M4/M5) via sysctl.

    On Apple Silicon, Ollama uses Metal/MLX for GPU acceleration — the
    8b embedding model runs efficiently.  We detect via the CPU brand
    string and macOS kernel (no Metal API call needed).
    """
    if sys.platform != "darwin":
        return False

    try:
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return False
        # Apple Silicon CPUs have "Apple" in the brand string
        return "Apple" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _model_name_matches(installed_name: str, configured_name: str) -> bool:
    """Check if *installed_name* matches *configured_name*.

    Matches exact name, tag variants (``model:tag``), and quantized
    variants (``model-q4_K_M``).  Does NOT match different versions of
    the same base model (``qwen3-embedding:0.6b`` does NOT match
    ``qwen3-embedding:8b``).

    WHY not match different versions: ``qwen3-embedding:0.6b`` and
    ``qwen3-embedding:8b`` are different models with different
    dimensions and quality characteristics.  Matching them would
    silently use the wrong model — the operator intended 0.6b but got
    8b (or vice versa).  Exact or prefix-with-separator matching
    prevents this ambiguity.
    """
    return (
        installed_name == configured_name
        or installed_name.startswith(configured_name + ":")
        or installed_name.startswith(configured_name + "-")
    )


def _model_installed(ollama_url: str, model: str) -> bool:
    """Check if *model* is installed in Ollama.

    Matches exact name, tag variants (``model:tag``), and suffixed
    variants (``model-q4_K_M``).  Does NOT match different versions
    of the same base model (``qwen3-embedding:0.6b`` does NOT match
    ``qwen3-embedding:8b``).
    """
    import httpx

    try:
        resp = httpx.get(
            ollama_url.rstrip("/") + "/api/tags",
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0),
        )
        resp.raise_for_status()
        models = resp.json().get("models", [])
        installed = [m["name"] for m in models]
        return any(_model_name_matches(m, model) for m in installed)
    except (ValueError, TypeError, RuntimeError, AttributeError, OSError, httpx.HTTPError):
        return False


def _try_pull(model: str, ollama_url: str) -> bool:
    """Best-effort pre-warm pull with 5s read timeout.
    Real pull (in ollama.py) uses 600s timeout — this just tries to
    start the download early so it's ready when embedder initializes."""
    import json

    import httpx

    url = ollama_url.rstrip("/") + "/api/pull"
    try:
        with httpx.stream(
            "POST", url, json={"name": model},
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=5.0, pool=5.0),
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                status = data.get("status", "")
                if status in ("downloading", "pulling"):
                    total = data.get("total", 0)
                    completed = data.get("completed", 0)
                    if total:
                        pct = int(completed / total * 100)
                        print(f"\r  Downloading {model}... {pct}%", end="", flush=True)
                    else:
                        print(f"\r  Downloading {model}...", end="", flush=True)
                elif status:
                    print(f"\r  {status} {model}...", end="", flush=True)
            print()  # final newline
        return True
    except (httpx.HTTPError, OSError) as e:
        log.debug("Pull %s failed: %s", model, e)
        return False


def resolve_embed_model(cfg: LLMConfig) -> None:
    """Resolve ``cfg.embed_model`` to the best available model.

    Only runs when ``embed_model`` is empty (default).  Sets the
    resolved value on *cfg* and applies default query/doc prompts
    for the resolved model so all downstream code (``embed_key()``,
    ``get_embedder()``, ``max_tokens``, prompts) sees a concrete,
    fully-configured model.

    Best-effort: checks if the model is installed and pulls if missing.
    All Ollama calls have short timeouts — if Ollama is offline the
    model name is still set and ``OllamaEmbedder`` retries at embed time.

    Result is cached at module level — subsequent calls return instantly
    (no subprocess or HTTP I/O).
    """
    global _cached_model

    if not cfg.enabled:
        return
    if cfg.embed_model:
        return

    # Fast path: cache already populated (no lock needed for reads after
    # the first write — Python GIL makes the reference assignment atomic).
    if _cached_model is not None:
        cfg.embed_model = _cached_model
        _apply_prompt_defaults(cfg)
        return

    with _auto_model_lock:
        if _cached_model is not None:
            cfg.embed_model = _cached_model
            _apply_prompt_defaults(cfg)
            return

        gpu = _gpu_available()
        target = _DEFAULT_GPU_MODEL if gpu else _DEFAULT_CPU_MODEL
        cfg.embed_model = target
        _cached_model = target

    # NOTE: HTTP calls are OUTSIDE the lock — _apply_prompt_defaults,
    # _model_installed, and _try_pull perform network requests which
    # would serialize all config loads if they held the lock.  The
    # cache is already populated above (inside the lock) so subsequent
    # calls take the fast path at the double-check above.
    # Worst case on first call: two threads both check/pull — harmless
    # best-effort operations with short timeouts.
    _apply_prompt_defaults(cfg)

    if _model_installed(cfg.ollama_url, target):
        log.info("Auto-detected embed model: %s (GPU=%s, already installed)", target, gpu)
        return

    log.info("Auto-detected embed model: %s (GPU=%s, pulling...)", target, gpu)
    if _try_pull(target, cfg.ollama_url):
        log.info("Pulled %s successfully", target)
    else:
        log.info("Pull skipped — Ollama will auto-pull on first embed call")


def _apply_prompt_defaults(cfg: LLMConfig) -> None:
    """Apply default query/doc prompts for the resolved model.

    Delegates to ``settings._apply_embed_prompt_defaults`` (canonical owner
    of prompt defaults).  Lazy import to avoid circular import — settings.py
    imports ``resolve_embed_model`` from this module.

    WHY prompts are applied here: Each embedding model uses different
    prompt prefixes (e.g. E5 uses "query:", BGE uses "Represent this
    sentence...").  Picking the model also means picking the prompts;
    doing both in one place ensures they stay in sync.
    """
    from ..config.settings import _apply_embed_prompt_defaults

    _apply_embed_prompt_defaults(cfg)
