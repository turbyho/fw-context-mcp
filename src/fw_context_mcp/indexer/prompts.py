"""Prompt templates and response parsers for LLM-based symbol analysis.

The prompt describes the task and the answer, and gives no example
answer.  WHY no examples (decision of the user, 2026-10-22): a model copies
an example.  The examples of the earlier prompt came from real firmware
projects, and the benchmark of 2026-10-20 found their phrases in the
answers for other code: "loaded from flash" for a value in RAM, "Returns 0
on success, -1" for a function without that return value, registers for a
constant.  A blind review of 30 C/C++ symbols (2026-10-22) graded the
prompt without examples better than the earlier prompt and than a prompt
with neutral examples: gemma4:26b +0.43, deepseek-v4.1-flash +0.40,
gpt-oss:20b +0.57 of 5 against the earlier prompt, qwen2.5-coder:14b the
same within the noise; fewer major errors for each model but the last.

A format skeleton with placeholders keeps the answer a flat JSON array:
30 of 30 answers parsed for each of the four models.  The measurement sent
"think": false (gpt-oss: "low").  Since 2026-10-23 the analysis sends
"think": false too (``llm.ollama.OllamaClient``); gpt-oss ignores it.  The rules ask for
the facts of the input only, and name what a firmware developer needs:
blocking waits, changes of the hardware state, memory ownership.

The second version without examples (2026-10-22) adds a rule for each
error type of that review: a value that a later line sets again, a
direction read wrong, an abbreviation of a name decoded without a source,
an invented caller or user, and local variables or called functions given
as inputs.  A global variable also gets its definition as its body
(``_llm_analysis._enrich_batch``).  A blind review of the same 30 symbols
against the first version: gemma4:26b +0.20, qwen2.5-coder:14b +0.20,
deepseek-v4.1-flash +0.40 (Ollama Cloud) and +0.47 (DeepSeek API),
gpt-oss:20b +0.13 of 5; 9 major errors against 11.  Most of the gain is
of the 3 constants with a definition; the new rules give a small gain of
the other symbols for 4 of the 5 models.
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


# System prompt of the symbol analysis: the task, the answer and the rules, without
# examples (see the module).
_ANALYSIS_SYSTEM = """You are a senior C/C++ embedded engineer. You write the documentation of the symbols of a firmware code index. A developer reads it to understand a symbol, and a search finds the symbol by its words.

TASK: for each symbol of the list, write a general description of what the symbol is and what it does, from its code. Use the names of the code (functions, types, variables, macros, registers) where the code uses them: they make the text findable.

ANSWER: a JSON array with one object for each symbol, in this form, with no other text before or after it:
[{"id": <the integer id of the symbol>, "summary": "<what the symbol is, what its body does, and its role>", "inputs": "<the parameters and the state that it reads>", "outputs": "<the return value, the values written through parameters, and the side effects>"}]

The three text fields:
- "summary": 3 to 6 sentences. The purpose of the symbol, its behavior (the main flow, the branches, the key decisions), and its role in the firmware (the subsystem, and who uses it when the input shows it).
- "inputs": ONE string.
  - Function or method: each parameter as "name (type): role", separated by semicolons; also the global state, the hardware state, or the member variables that it reads. Not its local variables, and not the functions that it calls.
  - Class, struct or union: what fills or configures it, when the input shows it.
  - Typedef: the underlying type.
  - Enum: "-".
  - Variable: its initial value (the initializer of the body), and who writes it when the input shows it.
- "outputs": ONE string.
  - Function or method: the return value and its meaning, the values written through pointer or reference parameters, and each side effect (state that changes, hardware that changes, calls that send or store data).
  - Class, struct or union: what it provides and manages.
  - Typedef: what the alias stands for.
  - Enum: what the constants mean, as groups.
  - Variable: what its value means, and who reads it when the input shows it.

RULES FOR THE JSON:
- Each object has the "id" of the symbol, an integer, as the list gives it.
- "inputs" and "outputs" are plain strings. Never a nested object or an array.
- Never write a backslash or a double-quote character inside a string value: they break the JSON. Write the word "backslash" or "double-quote" instead.

RULES FOR THE CONTENT:
- Describe the purpose and the role. Do not list each field of a struct, each member of a class or each constant of an enum: name the groups of data or of values.
- Use only the facts of the input. A wrong fact misleads a developer more than a missing one:
  - Write a return value, an error code, a register, a peripheral, an interrupt, or a storage place (RAM, flash, EEPROM, a file) only when the body or the doc shows it. Do not add a usual value, such as 0 for success, that the body does not return.
  - A value is in RAM unless the body writes it to flash, EEPROM or a file. Do not write "persists" or "stored in flash" for a value that the body only keeps in memory.
  - Read each condition, comparison and macro exactly as the code writes it. A macro or a constant stays its name, unless the input gives its value.
  - Follow each value that you name to the lines that set it. When a later line sets it again on each path, describe the final value. Read a direction, a sign and a unit as the code writes them.
  - Give the meaning of an abbreviation in a name only when the doc, a comment or the code gives it. Else write the name as it is.
  - Name a caller, a user, a writer or an overridden function only when the input shows it: the body, the doc, "called functions" or "referenced by". Do not add one from the name.
  - For a called function, use its name and the list of the called functions. Do not describe what it does inside beyond what its name says.
  - A constant, a macro value, or a declaration: describe its name, its type and its value (the initializer, and the comment of its line), and its use from the list of references. Do not invent registers, hardware or behavior for it.
  - When the body is missing, describe what the signature and the name tell, and write that the body is not available.
- Name each blocking wait that the body shows (a loop that polls a flag or a register, a delay, a wait for an event, with or without a timeout), each critical section or interrupt context, and each change of the hardware state (a GPIO, a peripheral register, an interrupt enable, a clock).
- For C++: name the class, the base classes, the virtual functions that it overrides (when the input shows them), the template parameters, and the ownership of memory (who allocates and frees it).

THE INPUT OF EACH SYMBOL:
- "doc" is the documentation comment of the developer. When it is there, it is the primary source of the intent: @brief is the purpose, @param a parameter, @return the return value. Check it against the body. When it says "(NO DOCSTRING …)", work from the body, the signature, the file path and the names of the called functions.
- "body" is the source or the declaration. It is the authority for the behavior. For a variable, it is its definition.
- "called functions" and "referenced by" are supplementary context: the dependencies, and for a variable the functions that read or write it.

UNIONS: read the body first, and state the pattern in the summary.
- Type-punning: a raw integer or array member and a struct (named or anonymous) of bit-fields over the same memory. The raw member serves storage or transfer, the bit-fields serve access to single bits.
- Grouping: independent members (enums, structs, sub-types) in one namespace, without a raw member and bit-fields over the same memory.
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

    WHY bracket counting: LLMs sometimes wrap their JSON output in markdown
    prose ("Here is the analysis: [..] let me know if...").  A greedy regex
    ``re.search(r"\\[.*\\]", text, re.DOTALL)`` would match from the first
    ``[`` to the last ``]`` across multiple arrays.  Bracket counting with
    string-aware state correctly handles nested arrays/objects and strings
    containing literal ``[`` or ``]``.

    Returns the array substring (from ``[`` to matching ``]``) or None.
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
