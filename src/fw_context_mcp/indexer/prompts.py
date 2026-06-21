"""Prompt templates and response parsers for LLM-based symbol analysis.

Uses Ollama chat models to generate structured descriptions (summary,
inputs, outputs) for C/C++ symbols. The prompt template is based on
experimental validation against qwen2.5-coder:14b on real Mbed OS
firmware symbols.

Key findings from iterative testing (6+ variants):
- Example-driven prompts with explicit FORMAT instructions produce
  flat strings consistently (avoiding nested JSON objects).
- Asking for "complete, thorough" descriptions without word limits
  yields good engineering-level detail.
- Temperature 0.1 balances determinism with quality.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# Maximum docstring length to include in the prompt (keeps token usage bounded)
_MAX_DOCSTRING_CHARS = 400

# System prompt for structured symbol analysis — example-driven to keep
# inputs/outputs as flat strings (not nested objects).
_ANALYSIS_SYSTEM = """You are a senior C/C++ embedded engineer writing comprehensive documentation.

For each symbol, output a JSON object with EXACTLY these three string fields:
  "summary": a detailed description (3-6 sentences). Explain the purpose, behavior, key design patterns, and embedded-system context.
  "inputs": a single string describing all inputs. For functions: list each parameter with type and role. For classes: describe dependencies and configuration. Format as "param1 (type): role; param2 (type): role". Write everything inline as ONE STRING, not a JSON object.
  "outputs": a single string describing all outputs and side effects. For functions: return value, error codes, and all side effects. For classes: what the class provides and manages. Format as ONE STRING, not a JSON object.

CRITICAL: "inputs" and "outputs" MUST be plain text strings. DO NOT use nested JSON objects like {"param": "desc"}. Write everything as a single string separated by semicolons or newlines.

The symbol description always includes a ``body`` (the full function source — this is the PRIMARY source of information) and may include ``called functions`` (supplementary context only — to help you understand dependencies and the surrounding system).

EXAMPLE of correct format:
{
  "summary": "Initializes the UART peripheral with the specified pins and baud rate. Configures the hardware registers for the given TX/RX pins. Sets up the baud rate generator based on the system clock. This function must be called before any UART read/write operations. The peripheral remains disabled until uart_enable() is called.",
  "inputs": "tx (PinName): UART transmit pin; rx (PinName): UART receive pin; baud (int): communication speed in bits per second",
  "outputs": "Returns 0 on success, -1 if pins are invalid. Side effects: configures GPIO alternate functions for TX/RX pins, enables UART clock, writes to USART_BRR and USART_CR1 registers"
}

Output ONLY the JSON array. No markdown fences, no commentary.
"""


def build_analysis_prompt(batch: list[dict[str, Any]]) -> str:
    """Build the analysis prompt for a batch of symbols.

    *batch* is a list of dicts with keys: id, name, qualified_name, kind,
    file_path, signature, docstring.  Optional keys improve analysis quality:
    ``body`` (function source code) and ``callees`` (list of called function names).

    Returns the full prompt string ready for Ollama.
    """
    parts: list[str] = []
    for i, sym in enumerate(batch, 1):
        kind = sym["kind"]
        qname = sym["qualified_name"] or sym.get("name", "")
        sig = sym.get("signature") or "—"
        file_path = sym.get("file_path") or "?"
        doc = (sym.get("docstring") or "").strip()
        body = (sym.get("body") or "").strip()
        callees: list[str] = sym.get("callees") or []

        doc_display = doc[:_MAX_DOCSTRING_CHARS] if doc else "(NO DOCSTRING — infer from name, signature, file path, body, and callees)"

        entry = (
            f"{i}. [{kind}] {qname}\n"
            f"   file: {file_path}\n"
            f"   sig: {sig}\n"
            f"   doc: {doc_display}"
        )

        if body:
            entry += f"\n   body:\n```cpp\n{body}\n```"
        if callees:
            entry += f"\n   called functions (supplementary context): {', '.join(callees[:30])}"
            if len(callees) > 30:
                entry += f" (and {len(callees) - 30} more)"

        parts.append(entry)

    return _ANALYSIS_SYSTEM + "\nSymbols:\n\n" + "\n\n".join(parts) + "\n\nJSON:"


def _flatten_value(value: Any) -> str:
    """Convert a parsed JSON value to a flat string.

    Handles both plain strings (ideal case) and nested dicts/arrays
    (fallback — model occasionally ignores format instructions).
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        # Flatten {"param": "desc", ...} → "param: desc; ..."
        return "; ".join(
            f"{k}: {_flatten_value(v)}"
            for k, v in value.items()
            if v
        )
    if isinstance(value, list):
        return "; ".join(_flatten_value(v) for v in value if v)
    return str(value).strip() if value else ""


def parse_analysis_response(
    response: str,
    batch: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Parse the Ollama JSON response and map entries back to symbol_ids.

    Handles common model output quirks: markdown fences, leading/trailing
    prose, partial arrays, nested objects in input/output fields.
    Accepts partial results — the returned list may be shorter than the batch.

    Returns a list of {symbol_id, summary, inputs, outputs} or None
    on total parse failure.
    """
    text = response.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        for fence_marker in ("```json", "```", "``"):
            if text.startswith(fence_marker):
                text = text[len(fence_marker):].strip()
                break
    if text.endswith("```"):
        text = text[:-3].strip()

    # Parse JSON
    parsed: list[dict[str, Any]] = []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: find a JSON array anywhere in the text
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            log.warning("Cannot parse LLM response as JSON: %.200s", response)
            return None
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            log.warning("Cannot parse LLM response as JSON (fallback failed): %.200s", response)
            return None

    if not isinstance(parsed, list):
        log.warning("LLM response is not a JSON array: %.200s", response)
        return None

    # Map parsed entries to batch symbols by position
    result: list[dict[str, Any]] = []
    for i, entry in enumerate(parsed):
        if i >= len(batch):
            break
        if not isinstance(entry, dict):
            continue
        summary = _flatten_value(entry.get("summary", ""))
        inputs = _flatten_value(entry.get("inputs", ""))
        outputs = _flatten_value(entry.get("outputs", ""))
        # At least one field must be non-empty to accept the entry
        if not summary and not inputs and not outputs:
            continue
        result.append({
            "symbol_id": batch[i]["id"],
            "summary": summary,
            "inputs": inputs,
            "outputs": outputs,
        })

    return result if result else None


# ---------------------------------------------------------------------------
# File-level analysis prompt — generates a short summary per file based on
# the symbols it contains.  Batched (5 files at a time) for efficiency.
# ---------------------------------------------------------------------------

_FILE_ANALYSIS_SYSTEM = """You are a senior C/C++ embedded engineer writing codebase documentation.

For each file below, write a 2-3 sentence summary of what the file is responsible for.
Base your summary on the symbols it contains — their names, kinds, and what they collectively accomplish.

Output a JSON array with one object per file:
  "file": the file path as given
  "summary": 2-3 sentences describing the file's purpose and responsibilities

EXAMPLE:
[
  {
    "file": "src/uart.cpp",
    "summary": "Implements the UART hardware abstraction layer. Provides uart_init() for pin/baud configuration and uart_write()/uart_read() for blocking data transfer. Manages TX/RX buffer state and interrupt handling for the USART peripheral."
  }
]

Output ONLY the JSON array. No markdown fences, no commentary.
"""


def build_file_analysis_prompt(batch: list[dict]) -> str:
    """Build the file-level analysis prompt for a batch of files.

    *batch* is a list of dicts with keys: ``file_id``, ``path``,
    ``symbols`` (list of {name, kind, qualified_name, signature} for
    up to 30 representative symbols in the file).
    """
    parts: list[str] = []
    for i, entry in enumerate(batch, 1):
        path = entry["path"]
        syms = entry.get("symbols", [])
        lines = [f"{i}. {path}"]
        for s in syms[:30]:
            qn = s.get("qualified_name") or s["name"]
            sig = s.get("signature", "")
            if sig:
                lines.append(f"   [{s['kind']}] {qn} — {sig}")
            else:
                lines.append(f"   [{s['kind']}] {qn}")
        parts.append("\n".join(lines))

    return _FILE_ANALYSIS_SYSTEM + "\nFiles:\n\n" + "\n\n".join(parts) + "\n\nJSON:"


def parse_file_analysis_response(
    response: str,
    batch: list[dict],
) -> list[dict] | None:
    """Parse the Ollama JSON response for file-level analysis.

    Returns a list of {file_id, summary} or None on total parse failure.
    Accepts partial results — the returned list may be shorter than the batch.
    """
    text = response.strip()

    # Strip markdown fences
    if text.startswith("```"):
        for fence_marker in ("```json", "```", "``"):
            if text.startswith(fence_marker):
                text = text[len(fence_marker):].strip()
                break
    if text.endswith("```"):
        text = text[:-3].strip()

    parsed: list[dict] = []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            log.warning("Cannot parse file analysis LLM response: %.200s", response)
            return None
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            log.warning("Cannot parse file analysis LLM response (fallback): %.200s", response)
            return None

    if not isinstance(parsed, list):
        log.warning("File analysis LLM response is not a JSON array: %.200s", response)
        return None

    # Build a lookup by file path for matching
    path_to_id: dict[str, int] = {}
    for entry in batch:
        path_to_id[entry["path"]] = entry["file_id"]

    result: list[dict] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        file_path = entry.get("file", "")
        summary = (entry.get("summary", "") or "").strip()
        if not summary:
            continue
        # Match by path (exact or suffix)
        file_id = path_to_id.get(file_path)
        if file_id is None:
            # Try suffix match
            for path, fid in path_to_id.items():
                if path.endswith(file_path) or file_path.endswith(path):
                    file_id = fid
                    break
        if file_id is None:
            log.warning("File analysis: cannot match path '%s' to batch", file_path)
            continue
        result.append({"file_id": file_id, "summary": summary})

    return result if result else None
