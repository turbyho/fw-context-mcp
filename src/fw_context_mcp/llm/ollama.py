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
    payload = {"model": cfg.model, "prompt": prompt, "stream": False}
    try:
        resp = httpx.post(url, json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["response"]
    except httpx.ConnectError as e:
        raise OllamaError(f"Cannot connect to Ollama at {cfg.ollama_url}: {e}") from e
    except httpx.TimeoutException as e:
        raise OllamaError(f"Ollama request timed out ({_TIMEOUT}s)") from e
    except Exception as e:
        raise OllamaError(str(e)) from e


class OllamaError(RuntimeError):
    pass
