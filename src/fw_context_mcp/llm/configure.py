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

from .errors import LLMError


def configure_llm_core(
    project_root: Path,
    updates: dict[str, object],
    *,
    clear_keys: list[str] | None = None,
) -> dict:
    """Apply the ``[llm]`` settings of a project to ``local.toml`` and test the connection.

    Writes ``updates`` into ``<project>/.fw-context/local.toml`` via
    line-level editing (preserves comments and other sections), reloads
    the config so the result reflects the new settings, then tests the
    chat backend with a trivial prompt (skipped when LLM is disabled).

    IMPORTANT: only ``local.toml`` is written — never the global config or
    the shared ``config.toml``.  The backend is a profile of the global
    config; a project selects it with ``profile``.  When the selected
    profile sends the chat to a host that is not this machine, a
    ``compliance_warning`` is returned.

    Args:
        project_root: Project root directory (resolved, existing).
        updates: Dict of ``{key: value}`` pairs to write to ``[llm]``
            (``profile``, ``embed_profile``, ``embed_model``, ``enabled``,
            ``analyze_symbols``, ...).  ``None`` values are skipped.
            Supported types: ``str``, ``bool``, ``int``, ``float``.
        clear_keys: Keys to REMOVE from the ``[llm]`` section (passed through
            to ``_update_local_toml``).  Used when the project goes back to
            the settings of the global config.

    Returns:
        dict: {status ("ok"|"error"), profile (str), provider (str),
        chat_api (dict — configured, endpoint, format, model), model (str),
        auto_pull (bool), stream (bool), test_latency_s (float, on success),
        test_response (str, on success), compliance_warning (str, when the
        chat goes to another host), message (str — present on both success
        and error)}.  Never raises.
    """
    from ..config import load as load_config
    from ..config.settings import _is_loopback_url, _update_local_toml

    result: dict = {}

    # WHAT the file held before the write.  A setting that fails its own
    # test must not stay behind: the tool writes AND verifies, thus a
    # failed verification means the operator keeps the setup that worked.
    # Measured: configure_llm(model="no_such_model:1b", auto_pull=False)
    # answered "Configuration written but test call failed" and left the
    # unusable model in local.toml — the next call then tested against it.
    local_toml = project_root / ".fw-context" / "local.toml"
    previous = local_toml.read_bytes() if local_toml.is_file() else None

    def _roll_back() -> str:
        """Put the previous settings back.  Gives a note for the message."""
        try:
            if previous is None:
                local_toml.unlink(missing_ok=True)
            else:
                local_toml.write_bytes(previous)
        except OSError as exc:
            return (
                f" The previous settings could NOT be restored ({exc}) — "
                f"read {local_toml} before the next call."
            )
        return " The previous settings are back; nothing was changed."

    try:
        _update_local_toml(project_root, updates, clear_keys=clear_keys)
    except Exception as e:
        return {"status": "error", "message": f"Failed to write local.toml: {e}"}

    try:
        new_cfg = load_config(project_root=project_root)
    except Exception as e:
        return {
            "status": "error",
            "message": f"The new settings could not be read back: {str(e).rstrip('.')}.{_roll_back()}",
        }

    try:
        # The chat and the embeddings: both send the source code (the
        # embeddings send the symbol descriptions and bodies).
        hosts = {
            "chat": new_cfg.llm.chat_api_base or new_cfg.llm.ollama_url,
            "embeddings": new_cfg.llm.ollama_url,
        }
        external = sorted({url for url in hosts.values() if url and not _is_loopback_url(url)})
        if external:
            parts = [what for what, url in hosts.items() if url in external]
            result["compliance_warning"] = (
                f"The {' and the '.join(parts)} of profile {new_cfg.llm.profile or '(default)'!r} go to an external "
                f"host ({', '.join(external)}). Source code snippets WILL be sent to this endpoint. "
                "Ensure compliance with your organization's data security policies."
            )
        result["profile"] = new_cfg.llm.profile
        result["provider"] = new_cfg.llm.provider
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

        from .client import call_llm

        t0 = time.monotonic()
        # Short timeout + minimal token budget: the test prompt is trivial
        # so even a slow model responds quickly.
        test_cfg = dataclasses.replace(new_cfg.llm, timeout=30.0)
        response = call_llm(
            "Reply with exactly: OK",
            test_cfg,
            temperature=0.0,
            max_tokens=10,
        )
        latency = round(time.monotonic() - t0, 2)
        result["status"] = "ok"
        result["test_latency_s"] = latency
        result["test_response"] = response[:100]
        result["message"] = f"Configuration written and tested successfully ({latency}s)."
    except LLMError as e:
        result["status"] = "error"
        result["message"] = (
            f"The test call failed: {e}. Check the URL, the API key and the model "
            f"of the profile.{_roll_back()}"
        )
    except Exception as e:
        # Never raise — a malformed URL or unexpected backend response must
        # surface as a structured error, not a traceback through the MCP tool.
        result["status"] = "error"
        result["message"] = f"Verification failed: {e}.{_roll_back()}"

    return result
