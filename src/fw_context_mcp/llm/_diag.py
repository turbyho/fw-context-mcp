"""Ollama setup diagnostics.

Called by the ``get_project_status`` → ``check_ollama`` tool path and
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


def tag_models(resp) -> list[dict] | None:
    """Return the model entries of an ``/api/tags`` answer, or ``None`` when it is not the answer of Ollama.

    An entry is a table with a ``name`` that is a string.  Other entries
    do not count.  ``None`` covers a body that is not JSON (the HTML page
    of a proxy with HTTP 200), and JSON without a list ``models``.

    WHY one function: the two ``check_setup`` functions called
    ``resp.json().get("models")`` outside of each ``try``.  Such an answer
    then raised out of ``semantic_search``, ``check_ollama``, ``fw-context
    init`` and the Ollama checks of doctor.
    """
    try:
        data = resp.json()
    except ValueError:
        return None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    return [m for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)]


def not_ollama_message(url: str) -> str:
    """The message for a server whose ``/api/tags`` answer is not the answer of Ollama."""
    return (
        f"{url.rstrip('/')}/api/tags did not give the model list of Ollama. "
        "Make sure that ollama_url names an Ollama server, and not a proxy page."
    )


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

    from fw_context_mcp.llm.ollama import ollama_pull_command

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
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as e:
        # InvalidURL is not an HTTPError: "http://localhost:abc" passes the
        # URL check of the config, and httpx refuses the port.
        return {"status": "error", "ollama_running": False, "message": str(e)}

    models = tag_models(resp)
    if models is None:
        return {"status": "error", "ollama_running": False, "message": not_ollama_message(cfg.ollama_url)}
    installed = [m["name"] for m in models]
    model_found = any(_model_name_matches(m, cfg.model) for m in installed)

    # Embedding model availability is checked here (not deferred to ollama.py)
    # so the CLI diag gives a complete picture in one call.  semantic_search
    # will fail at query time if the embedding model is missing — the operator
    # should know this before they try to use it.
    # A sentence-transformers or ft:// model runs locally: Ollama need not
    # hold it.
    from fw_context_mcp.llm.embedder_factory import uses_ollama
    embed_found = not uses_ollama(cfg) or any(_model_name_matches(m, cfg.embed_model) for m in installed)

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
            f"Run: {ollama_pull_command(cfg.model, cfg.ollama_url)}"
        )
        result["available_code_models"] = [
            m for m in installed
            if any(kw in m for kw in ("coder", "codestral", "code", "deepseek", "starcoder"))
        ]

    if not embed_found:
        embed_msg = (
            f"Embedding model '{cfg.embed_model}' is not installed. "
            f"Run: {ollama_pull_command(cfg.embed_model, cfg.ollama_url)}"
        )
        if "message" in result:
            result["message"] += " " + embed_msg
        else:
            result["message"] = embed_msg

    return result


def installed_models(url: str) -> list[str] | None:
    """Return the model names of the Ollama server at *url*, or ``None`` when it gives no list.

    ``None`` covers a server that does not answer, an HTTP error, a URL
    that is not valid, and an answer that is not the JSON of ``/api/tags``
    (for example the HTML page of a proxy).  WHY no exception: the caller
    reports the server, and a doctor check must not crash on it.
    """
    import httpx

    try:
        resp = httpx.get(url.rstrip("/") + "/api/tags", timeout=5.0)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError):
        return None
    models = tag_models(resp)
    return None if models is None else [m["name"] for m in models]
