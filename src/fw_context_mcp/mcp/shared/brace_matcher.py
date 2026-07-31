"""Brace matching for C/C++ function body extraction.

String-literal and comment-aware brace counting.  Does NOT handle
C++11 raw string literals ``R"(...)"`` — rare in embedded C/C++ code.
"""

from __future__ import annotations

import enum


class _BraceState(enum.IntEnum):
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
                if ch == '\\' and j + 1 < len(line):
                    j += 1  # skip escaped character
                elif ch == '"':
                    state = _BraceState.NORMAL
            elif state == _BraceState.CHAR:
                if ch == '\\' and j + 1 < len(line):
                    j += 1  # skip escaped character
                elif ch == "'":
                    state = _BraceState.NORMAL
            elif state == _BraceState.BLOCK_COMMENT:
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
