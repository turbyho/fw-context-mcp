"""Ollama setup diagnostics."""

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

    # Check embedding model too — semantic_search will fail at query time if it's missing
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


