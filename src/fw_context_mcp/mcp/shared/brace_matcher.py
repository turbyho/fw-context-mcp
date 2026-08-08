"""Brace matching for C/C++ function body extraction.

String-literal and comment-aware brace counting.  Does NOT handle
C++11 raw string literals ``R"(...)"`` — rare in embedded C/C++ code.

WHY this exists instead of using libclang: when extracting a function
body for search snippet display, the full AST is not available — only
source text.  libclang exact extents require a TU parse, which is too
slow for per-result snippet generation.  A lightweight state-machine
brace counter runs in microseconds on source lines that are already
in memory.

WHY it is String and comment-aware: C/C++ source contains braces in
string literals (``"}"``), character constants (``'}'``), and comments
(``/* { */``).  Without skipping these, the depth counter would
prematurely close the function body.

WHY no raw-string support: C++11 ``R"(...)"`` raw string literals are
virtually non-existent in embedded MCU firmware codebases (C99/C11
without C++17 features).  Adding support would complicate the state
machine for zero practical benefit.
"""

from __future__ import annotations

import enum


class _BraceState(enum.IntEnum):
    """States of the brace-counting state machine.

    Each state skips a different class of "fake" braces:
    - STRING: ``"{"`` and ``"}"`` inside double-quoted literals
    - CHAR: ``'{'`` and ``'}'`` inside single-quoted character constants
    - LINE_COMMENT: everything after ``//`` to end of line
    - BLOCK_COMMENT: everything between ``/*`` and ``*/``
    - NORMAL: actual code — braces count toward depth
    """
    NORMAL = 0
    STRING = 1
    CHAR = 2
    LINE_COMMENT = 3
    BLOCK_COMMENT = 4


def find_closing_brace(
    lines: list[str],
    start_idx: int = 0,
) -> int:
    """Return the line index of the matching closing ``}``.

    Scans *lines* starting at *start_idx* (0-based) until the outermost
    brace opened after *start_idx* is balanced.  String and character
    literals, ``//`` line comments, and ``/* … */`` block comments are
    skipped so braces inside them don't affect the depth count.

    When no ``{`` is found before the first ``}``, returns *start_idx* + 2
    (truncated to the last line) — a best-effort fallback for malformed
    fragments.

    Args:
        lines: Source lines (without trailing newlines).
        start_idx: First line to examine (0-based).

    Returns:
        0-based line index of the line containing the matching ``}``.
    """
    if not lines or start_idx >= len(lines):
        return start_idx
    state = _BraceState.NORMAL
    depth = 0
    seen_open = False
    local_end = start_idx
    for i in range(start_idx, len(lines)):
        line = lines[i]
        j = 0
        while j < len(line):
            ch = line[j]
            if state == _BraceState.NORMAL:
                if ch == '"':
                    state = _BraceState.STRING
                elif ch == "'":
                    state = _BraceState.CHAR
                elif ch == '/' and j + 1 < len(line) and line[j + 1] == '/':
                    state = _BraceState.LINE_COMMENT
                    j += 1
                elif ch == '/' and j + 1 < len(line) and line[j + 1] == '*':
                    state = _BraceState.BLOCK_COMMENT
                    j += 1
                elif ch == '{':
                    depth += 1
                    seen_open = True
                elif ch == '}':
                    depth -= 1
            elif state == _BraceState.STRING:
                # In a string literal: backslash escapes the next char
                # (\" is not string end), otherwise " closes the string.
                if ch == '\\' and j + 1 < len(line):
                    j += 1  # skip escaped character
                elif ch == '"':
                    state = _BraceState.NORMAL
            elif state == _BraceState.CHAR:
                # Same escape logic for character constants.
                if ch == '\\' and j + 1 < len(line):
                    j += 1  # skip escaped character
                elif ch == "'":
                    state = _BraceState.NORMAL
            elif state == _BraceState.BLOCK_COMMENT:
                # Block comments span lines — state persists across line endings.
                if ch == '*' and j + 1 < len(line) and line[j + 1] == '/':
                    state = _BraceState.NORMAL
                    j += 1
            # LINE_COMMENT: no per-character transitions — resets at end of line
            j += 1
        # Line comments reset at end of line
        if state == _BraceState.LINE_COMMENT:
            state = _BraceState.NORMAL
        local_end = i
        if seen_open and depth <= 0 and state == _BraceState.NORMAL:
            break
    if not seen_open:
        local_end = min(len(lines) - 1, start_idx + 2)
    return local_end
