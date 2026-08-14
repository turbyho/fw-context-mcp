"""Shared LLM configuration write + test path.

``configure_llm_core`` is the single implementation of "write LLM settings
to ``local.toml`` and verify the endpoint" — used by both the MCP
``configure_llm`` handler and the CLI ``prompt_llm_config`` interactive
flow.  Extracting it here keeps the two call sites from diverging.

WHY a shared module: the MCP handler and the CLI both need to (1) write
per-developer settings to ``local.toml`` without touching committed
config, (2) reload the config so the response reflects what was written,
and (3) test the endpoint with a trivial prompt.  The CLI must not import
the MCP handler layer — this module lives in the ``llm`` package so both
can depend on it without a layering violation.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path


def configure_llm_core(
    project_root: Path,
    updates: dict[str, object],
    *,
    chat_api_base: str | None = None,
    clear_keys: list[str] | None = None,
) -> dict:
    """Apply LLM settings to ``local.toml`` and test the connection.

    Writes ``updates`` into ``<project>/.fw-context/local.toml`` via
    line-level editing (preserves comments and other sections), reloads
    the config so the result reflects the new settings, then tests the
    endpoint with a trivial prompt (skipped when LLM is disabled).

    IMPORTANT: only ``local.toml`` is written — never the global config or
    the shared ``config.toml``.  When ``chat_api_base`` points to an
    external host, a ``compliance_warning`` is returned because source
    code snippets in chat prompts will be sent to that endpoint.

    Args:
        project_root: Project root directory (resolved, existing).
        updates: Dict of ``{key: value}`` pairs to write to ``[llm]``.
            ``None`` values are skipped.  Supported types: ``str``,
            ``bool``, ``int``, ``float``.
        chat_api_base: When set to an external host, adds a compliance
            warning to the result.
        clear_keys: Keys to REMOVE from the ``[llm]`` section (passed through
            to ``_update_local_toml``).  Used to clear a previously configured
            cloud endpoint when switching back to local Ollama.

    Returns:
        dict: {status ("ok"|"error"), chat_api (dict — configured, endpoint,
        model), model (str), auto_pull (bool), stream (bool),
        test_latency_s (float, on success), test_response (str, on success),
        compliance_warning (str, when chat_api_base is external),
        message (str — present on both success and error)}.  Never raises.
    """
    from ..config import load as load_config
    from ..config.settings import _is_loopback_url, _update_local_toml

    result: dict = {}
    if chat_api_base and not _is_loopback_url(chat_api_base):
        result["compliance_warning"] = (
            f"Chat API set to external host ({chat_api_base}). "
            "Source code snippets in chat prompts WILL be sent to this "
            "endpoint. Ensure compliance with your organization's data "
            "security policies. Consider using local Ollama or an internal "
            "API proxy."
        )

    try:
        _update_local_toml(project_root, updates, clear_keys=clear_keys)
    except Exception as e:
        return {"status": "error", "message": f"Failed to write local.toml: {e}"}

    try:
        new_cfg = load_config(project_root=project_root)
    except Exception as e:
        return {"status": "error", "message": f"Configuration written but reload failed: {e}"}

    try:
        if new_cfg.llm.chat_api_base:
            from .chat_router import detect_chat_endpoint

            fmt, endpoint = detect_chat_endpoint(new_cfg.llm)
            result["chat_api"] = {
                "configured": True,
                "endpoint": endpoint,
                "format": fmt,
                "model": new_cfg.llm.model,
            }
        else:
            result["chat_api"] = {
                "configured": False,
                "endpoint": new_cfg.llm.ollama_url,
                "format": "ollama",
                "model": new_cfg.llm.model,
            }
        result["model"] = new_cfg.llm.model
        result["auto_pull"] = new_cfg.llm.auto_pull
        result["stream"] = new_cfg.llm.stream

        if not new_cfg.llm.enabled:
            result["status"] = "ok"
            result["message"] = "Configuration written. LLM is disabled — no test call made."
            return result

        from .ollama import OllamaError, call_ollama

        t0 = time.monotonic()
        # Short timeout + minimal token budget: the test prompt is trivial
        # so even a slow model responds quickly.
        test_cfg = dataclasses.replace(new_cfg.llm, timeout=30.0)
        response = call_ollama(
            "Reply with exactly: OK",
            test_cfg,
            temperature=0.0,
            num_predict=10,
        )
        latency = round(time.monotonic() - t0, 2)
        result["status"] = "ok"
        result["test_latency_s"] = latency
        result["test_response"] = response[:100]
        result["message"] = f"Configuration written and tested successfully ({latency}s)."
    except OllamaError as e:
        result["status"] = "error"
        result["message"] = f"Configuration written but test call failed: {e}. Check the API URL, key, and model name."
    except Exception as e:
        # Never raise — a malformed URL or unexpected backend response must
        # surface as a structured error, not a traceback through the MCP tool.
        result["status"] = "error"
        result["message"] = f"Configuration written but verification failed: {e}"

    return result
