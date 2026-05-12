"""Thin Ollama client for fw-context LLM tools."""

from __future__ import annotations

import logging

import httpx

from ..config.settings import LLMConfig

log = logging.getLogger(__name__)

_TIMEOUT = 60.0


def call_ollama(prompt: str, cfg: LLMConfig) -> str:
    """Send a prompt to Ollama and return the response text.

    Raises OllamaError on network or API failure.
    """
    url = cfg.ollama_url.rstrip("/") + "/api/generate"
    payload = {
        "model": cfg.model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": cfg.num_ctx},
    }
    try:
        resp = httpx.post(url, json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["response"]
    except httpx.ConnectError as e:
        raise OllamaError(
            f"Cannot connect to Ollama at {cfg.ollama_url}. "
            "Make sure Ollama is running: https://ollama.com"
        ) from e
    except httpx.TimeoutException:
        raise OllamaError(f"Ollama request timed out after {_TIMEOUT}s")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise OllamaModelNotFoundError(
                cfg.model,
                cfg.ollama_url,
            ) from e
        raise OllamaError(f"Ollama HTTP {e.response.status_code}: {e.response.text[:200]}") from e
    except Exception as e:
        raise OllamaError(str(e)) from e


def check_setup(cfg: LLMConfig) -> dict:
    """Check Ollama connectivity and model availability.

    Returns a status dict suitable for returning directly as an MCP tool result.
    """
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
    except Exception as e:
        return {"status": "error", "ollama_running": False, "message": str(e)}

    models = resp.json().get("models", [])
    installed = [m["name"] for m in models]
    model_found = cfg.model in installed

    if not model_found:
        # Try prefix match (e.g. "codestral" matches "codestral:latest")
        model_found = any(m.startswith(cfg.model.split(":")[0]) for m in installed)

    result: dict = {
        "status": "ok" if model_found else "model_missing",
        "ollama_running": True,
        "ollama_url": cfg.ollama_url,
        "configured_model": cfg.model,
        "num_ctx": cfg.num_ctx,
        "installed_models": installed,
    }

    if not model_found:
        result["message"] = (
            f"Model '{cfg.model}' is not installed. "
            f"Run: ollama pull {cfg.model}"
        )
        result["available_code_models"] = [
            m for m in installed
            if any(kw in m for kw in ("coder", "codestral", "code", "deepseek", "starcoder"))
        ]

    return result


class OllamaError(RuntimeError):
    pass


class OllamaModelNotFoundError(OllamaError):
    def __init__(self, model: str, url: str) -> None:
        self.model = model
        super().__init__(
            f"Model '{model}' not found in Ollama at {url}. "
            f"Run: ollama pull {model}"
        )
