"""Ollama setup diagnostics.

Called by the ``get_active_build`` → ``check_ollama`` tool path and
as a standalone entry point (``fw-context diag llm``).  Returns a
structured status report for both chat and embedding models.

WHY separate from ``ollama.check_setup``: This module performs a
simpler, faster check — it only queries ``/api/tags`` and checks
model names.  ``ollama.check_setup`` also probes the chat API
endpoint (external) and produces a richer result with compliance
warnings.  This module is used for quick CLI diagnostics; the main
check in ollama.py is used by the MCP tool handler.

The two ``check_setup`` functions have different signatures and
return different result shapes by design — they serve different
consumers (CLI diag vs MCP tool).
"""

from __future__ import annotations

from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.llm.auto_model import _model_name_matches


def check_setup(cfg: LLMConfig) -> dict:
    """Check Ollama connectivity and model availability.

    Args:
        cfg: LLM configuration.

    Returns:
        dict: {ollama_running (bool), model_installed (bool), model (str),
        message (str, on error), latency_s, embedding_model,
        embedding_installed}.
    """
    import httpx
    base_url = cfg.ollama_url.rstrip("/")
    try:
        resp = httpx.get(base_url + "/api/tags", timeout=5.0)
        resp.raise_for_status()
    except httpx.ConnectError:
        return {
            "status": "error",
            "ollama_running": False,
            "message": (
                f"Ollama is not running at {cfg.ollama_url}. "
                "Install and start: https://ollama.com"
            ),
        }
    except (httpx.HTTPError, OSError) as e:
        return {"status": "error", "ollama_running": False, "message": str(e)}

    models = resp.json().get("models", [])
    installed = [m["name"] for m in models]
    model_found = any(_model_name_matches(m, cfg.model) for m in installed)

    # Embedding model availability is checked here (not deferred to ollama.py)
    # so the CLI diag gives a complete picture in one call.  semantic_search
    # will fail at query time if the embedding model is missing — the operator
    # should know this before they try to use it.
    embed_found = any(_model_name_matches(m, cfg.embed_model) for m in installed)

    result: dict = {
        "status": "ok" if model_found else "model_missing",
        "ollama_running": True,
        "ollama_url": cfg.ollama_url,
        "configured_model": cfg.model,
        "num_ctx": cfg.num_ctx,
        "installed_models": installed,
        "configured_embed_model": cfg.embed_model,
        "embedding_installed": embed_found,
    }

    if cfg.debug_log:
        result["debug_log"] = str(cfg.debug_log)

    if not model_found:
        result["message"] = (
            f"Model '{cfg.model}' is not installed. "
            f"Run: ollama pull {cfg.model}"
        )
        result["available_code_models"] = [
            m for m in installed
            if any(kw in m for kw in ("coder", "codestral", "code", "deepseek", "starcoder"))
        ]

    if not embed_found:
        embed_msg = (
            f"Embedding model '{cfg.embed_model}' is not installed. "
            f"Run: ollama pull {cfg.embed_model}"
        )
        if "message" in result:
            result["message"] += " " + embed_msg
        else:
            result["message"] = embed_msg

    return result


