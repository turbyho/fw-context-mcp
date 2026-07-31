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
_MAX_DOCSTRING_CHARS = 800

# Characters that would break JSON when reproduced by the model inside string
# values.  Replaced with text descriptions before the prompt is built so the
# model never sees them and cannot reproduce them literally.
_JSON_UNSAFE_REPLACEMENTS = {
    "\\": "[backslash]",
    "\"": "[double-quote]",
}


def _sanitize_prompt_text(text: str) -> str:
    """Replace characters that are unsafe to reproduce in JSON output."""
    for char, replacement in _JSON_UNSAFE_REPLACEMENTS.items():
        text = text.replace(char, replacement)
    return text

# System prompt for structured symbol analysis — example-driven to keep
# inputs/outputs as flat strings (not nested objects).
_ANALYSIS_SYSTEM = """You are a senior C/C++ embedded engineer writing comprehensive documentation.

For each symbol, output a JSON object with EXACTLY these three string fields:
  "summary": a detailed description (3-6 sentences). Explain the purpose, behavior, key design patterns, and embedded-system context.
    "inputs": a single string describing all inputs. For functions: list each parameter with type and role. For classes/structs/unions: describe dependencies and configuration. For typedefs: the underlying type. For enums: not applicable, use "-". For variables: describe who writes/initializes the value and what subsystem it belongs to. Format as "param1 (type): role; param2 (type): role". Write everything inline as ONE STRING, not a JSON object.
    "outputs": a single string describing all outputs and side effects. For functions: return value, error codes, and all side effects. For classes/structs/unions: what the type provides and manages. For typedefs: what the type alias represents. For enums: what each constant means and how it's used. For variables: describe what depends on this variable's value and the consequence of changing it. Format as ONE STRING, not a JSON object.

CRITICAL: Each object MUST include an "id" field with the integer symbol ID shown in the prompt.
CRITICAL: "inputs" and "outputs" MUST be plain text strings. DO NOT use nested JSON objects like {"param": "desc"}. Write everything as a single string separated by semicolons or newlines.
CRITICAL: NEVER output literal backslash or double-quote characters inside JSON string values. These break JSON syntax. Describe them by name instead: write "backslash" not backslash, write "double-quote" not double-quote.
CRITICAL: DO NOT enumerate individual fields, members, or constants. Describe the symbol's PURPOSE and ROLE — what problem it solves, what subsystem it belongs to, how it's used. For large structs with many fields, summarize the CATEGORIES of data it holds (e.g. "system config, comm settings, sensor thresholds") rather than listing every field.

The symbol description always includes a ``doc`` field (documentation comment — when present this is the AUTHORITATIVE source written by the developer), a ``body`` (the full source or declaration — use this to verify/expand on the documentation), and may include ``called functions`` (supplementary context only — to help you understand dependencies and the surrounding system).

For variables: describe what the variable stores, who modifies it, who reads it, and what subsystem it belongs to. Use the "referenced by" list to identify reader/writer functions.

**Documentation (doc:):** When present, prioritize the developer's own description. ``@brief`` describes the purpose, ``@param`` describes each parameter, ``@return`` describes the return value. Use this as the primary source — it reflects the developer's intent. If the doc field shows "(NO DOCSTRING — ...)", fall back to inferring from body, signature, file_path, and callee names.

**Union analysis:** Inspect the body before deciding the pattern. Unions are used for TWO distinct purposes:
1) **Type-punning** — different representations of the SAME memory: a raw integer/array member alongside a struct (named or anonymous) containing bit-fields (e.g. ``uint32_t flags;`` + ``struct { uint32_t field:1; ... }``). The raw integer provides efficient storage/transfer; the bit-field struct provides named access to individual bits. Also used for register access, version encoding, protocol parsing. The bit-field struct may be anonymous or named — BOTH indicate type-punning when paired with a raw type.
2) **Organizational grouping** — distinct, unrelated members sharing a namespace WITHOUT memory overlap. Members are enums, standalone structs, or sub-types that would otherwise clutter the parent scope. NO raw-type/bit-field member pairs present. The union keyword here is just a grouping mechanism — each member is independently used, never reinterpreted as another type.
ALWAYS state which pattern applies in the summary. If ANY member is a raw integer/array AND another member contains bit-fields (even inside an anonymous struct), it IS type-punning (pattern 1). Otherwise it is grouping (pattern 2).

EXAMPLES:

Function:
{
  "summary": "Initializes the UART peripheral with the specified pins and baud rate. Configures the hardware registers for the given TX/RX pins. Sets up the baud rate generator based on the system clock. This function must be called before any UART read/write operations. The peripheral remains disabled until uart_enable() is called.",
  "inputs": "tx (PinName): UART transmit pin; rx (PinName): UART receive pin; baud (int): communication speed in bits per second",
  "outputs": "Returns 0 on success, -1 if pins are invalid. Side effects: configures GPIO alternate functions for TX/RX pins, enables UART clock, writes to USART_BRR and USART_CR1 registers"
}

Class/Struct:
{
  "summary": "Holds all run-time configuration for the device, loaded from flash and validated at boot. Groups settings by subsystem: system identity (device ID, axes orientation), cellular communication (APN, data server), USSD codes, battery monitoring thresholds, and POI detection parameters. Each field is a key in a key-value config store accessed via anitra::IConfig interface.",
  "inputs": "Loaded from flash at boot; defaults to zero/false when flash is blank or corrupted. Validated by ConfigManager against schema version.",
  "outputs": "Provides read/write access to all device configuration. Changes take effect immediately for most fields; communication-related fields require a modem restart."
}

Typedef:
{
  "summary": "Type alias for a fixed-width 32-bit unsigned integer. Used throughout the firmware for portability — guarantees the size regardless of the compiler or target platform. Preferred over raw 'unsigned int' to ensure consistent register-width calculations on 32-bit ARM Cortex-M targets.",
  "inputs": "Underlying type: unsigned int (at least 32 bits on the target platform)",
  "outputs": "Provides a portable uint32_t type name used for register values, bitfields, and protocol data structures"
}

 Enum:
{
  "summary": "Defines the possible states of the modem state machine. Each constant represents a distinct operational phase. The state machine transitions linearly through the states during connection establishment, with IDLE and ERROR as the terminal entry/exit points. Designed so that ISRs and the main loop can check state via a single variable rather than multiple flags.",
  "inputs": "-",
  "outputs": "IDLE: modem powered off or not yet initialized; RESETTING: modem is being reset; WAIT_URC_AT_READY: waiting for AT ready unsolicited response; WAIT_CFUN: waiting for full functionality confirmation; WAIT_NETWORK: waiting for network registration; CONNECTED: fully operational, ready for data; ERROR: unrecoverable fault, requires power cycle"
}

Union (type-punning — two views of the same memory):
{
  "summary": "Provides dual access to a firmware version number stored in flash. The raw 32-bit integer enables efficient storage and comparison, while the packed bit-field struct within the union lets code read individual major/minor/patch components without manual bit shifting. The 8 reserved bits allow future extension without changing the storage format.",
  "inputs": "-",
  "outputs": "As raw integer (_fw_bl_version_int): the full version as uint32_t for flash read/write and comparison. As bit fields: _major (8 bits), _minor (8 bits), _patch (8 bits), plus 8 reserved bits."
}

Union (grouping — organizing related types without memory overlap):
{
  "summary": "Groups related BLE command enums into a single namespace. This union does NOT share memory — each member is a standalone enum defining command codes for a different operational context. Dispatch handles courier delivery workflows, Pickup covers customer retrieval, and Service lists diagnostic and maintenance operations. The union keyword here serves purely as organizational grouping, not type-punning.",
  "inputs": "-",
  "outputs": "CmdsDispatch: courier workflow commands (slot open, state query, size change, cancel). CmdsPickup: customer pickup commands (slot open, state query). CmdsService: 22 diagnostic/maintenance commands including LOCKER_REBOOT, SDCARD_DELETE, GET_FW_VERSION, GET_NET_INFO, GET_SHUTDOWN_REASON, SERVICE_MODE_SET."
}

Variable:
{
  "summary": "Global debug verbosity level controlling log output across all subsystems. Set at boot from flash configuration by config_manager_load(). Read by log_write() in the logging subsystem to decide which messages to emit. A value of 0 means no debug output; higher values enable progressively more verbose logging.",
  "inputs": "Written by config_manager_load() at boot and set_debug_level() via serial console. Belongs to the logging subsystem.",
  "outputs": "Read by log_write() in the logging module. Changing this value affects runtime log verbosity — higher values produce more serial output, which can impact timing in real-time paths."
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

        # Sanitize: replace characters that would break JSON when reproduced
        doc = _sanitize_prompt_text(doc)
        body = _sanitize_prompt_text(body)

        doc_display = doc[:_MAX_DOCSTRING_CHARS] if doc else "(NO DOCSTRING — infer from name, signature, file path, body, and callees)"

        entry = (
            f"{i}. [{kind}] {qname} (id: {sym['id']})\n"
            f"   file: {file_path}\n"
            f"   sig: {sig}\n"
            f"   doc: {doc_display}"
        )

        if body:
            entry += f"\n   body:\n```cpp\n{body}\n```"
        if callees:
            if kind == "varglobal":
                entry += f"\n   referenced by (supplementary context): {', '.join(callees[:30])}"
            else:
                entry += f"\n   called functions (supplementary context): {', '.join(callees[:30])}"
            if len(callees) > 30:
                entry += f" (and {len(callees) - 30} more)"

        parts.append(entry)

    prompt = _ANALYSIS_SYSTEM + "\nSymbols:\n\n" + "\n\n".join(parts) + "\n\nJSON:"
    log.debug("LLM analysis prompt (%d symbols, %d chars):\n—— prompt ——\n%s\n—— end prompt ——",
              len(batch), len(prompt), prompt)
    return prompt


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


# Valid JSON escape sequences.  Anything else (e.g. ``\'``, ``\x``) is a
# model error — the backslash is stripped so the character passes through
# literally.
# Negative lookbehind (?<!\\\\) prevents stripping the second backslash of
# a valid \\\\ pair (e.g. '\\\\' → '\\\\' stays valid; '\\' alone IS stripped).
_INVALID_JSON_ESCAPE = re.compile(r"(?<!\\)\\(?![\"\\/bfnrt]|u[0-9a-fA-F]{4})")


def _extract_first_json_array(text: str) -> str | None:
    """Extract the first complete JSON array from *text* using bracket counting.

    Returns the array substring (from ``[`` to matching ``]``) or None.
    Handles nested arrays/objects and strings correctly — unlike the
    greedy ``re.search(r\"\\[.*\\]\", text, re.DOTALL)`` which matches
    from the first ``[`` to the last ``]`` across multiple arrays.
    """
    start = text.find("[")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _sanitize_json_escapes(text: str) -> str:
    """Fix invalid JSON escape sequences produced by LLM output.

    Models occasionally emit escapes like ``\\'`` (single quote) or ``\\x``
    inside JSON strings.  These are not valid JSON and cause ``json.loads``
    to raise ``JSONDecodeError``.  We strip the errant backslash, which is
    the conservative fix — it preserves the intended character rather than
    dropping the entire response.
    """
    return _INVALID_JSON_ESCAPE.sub("", text)


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

    # Fix common LLM JSON mistakes before parsing
    text = _sanitize_json_escapes(text)

    # Parse JSON
    parsed: list[dict[str, Any]] = []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        # Fallback: find a JSON array anywhere in the text
        arr_text = _extract_first_json_array(text)
        if not arr_text:
            log.warning(
                "Cannot parse LLM response as JSON: %s (line %d col %d)\n"
                "—— response ——\n%s\n—— end response ——",
                e.msg, e.lineno, e.colno, response,
            )
            return None
        try:
            parsed = json.loads(arr_text)
        except json.JSONDecodeError as e2:
            log.warning(
                "Cannot parse LLM response as JSON (fallback failed): %s (line %d col %d)\n"
                "—— response ——\n%s\n—— end response ——",
                e2.msg, e2.lineno, e2.colno, response,
            )
            return None

    log.debug(
        "LLM analysis response parsed (%d entries):\n—— response ——\n%s\n—— end response ——",
        len(parsed) if isinstance(parsed, list) else 1, response,
    )

    # Single object → wrap in list (common with one symbol per request)
    if isinstance(parsed, dict):
        parsed = [parsed]

    if not isinstance(parsed, list):
        log.warning(
            "LLM response is not a JSON array:\n—— response ——\n%s\n—— end response ——",
            response,
        )
        return None
    # Build lookup table for identity-based mapping (stable when LLM skips items)
    batch_by_id: dict[int, dict[str, Any]] = {s["id"]: s for s in batch}

    result: list[dict[str, Any]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        summary = _flatten_value(entry.get("summary", ""))
        inputs = _flatten_value(entry.get("inputs", ""))
        outputs = _flatten_value(entry.get("outputs", ""))
        # At least one field must be non-empty to accept the entry
        if not summary and not inputs and not outputs:
            continue

        # Identity-based mapping: use 'id' from LLM response when present
        entry_id = entry.get("id")
        if entry_id is not None and isinstance(entry_id, int) and entry_id in batch_by_id:
            symbol_id = entry_id
        else:
            # Fallback to positional mapping with warning
            idx = len(result)
            if idx >= len(batch):
                log.warning("LLM returned more results than batch; truncating")
                break
            symbol_id = batch[idx]["id"]
            if entry_id is not None:
                log.debug("LLM returned id=%s which doesn't match batch; using positional fallback", entry_id)

        result.append({
            "symbol_id": symbol_id,
            "summary": summary,
            "inputs": inputs,
            "outputs": outputs,
        })

    return result if result else None
