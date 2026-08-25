"""One definition of the SQLite bound-parameter chunk size.

There were five: two ``_chunked`` helpers with the same body and different
element types, and three inline ``range(0, len(x), N)`` loops.  Two of them
carried a hard-coded 500 while _symbols.py imported the constant from
_refs.py with a comment about not letting the two drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

# SQLite rejects a statement that binds more parameters than
# SQLITE_MAX_VARIABLE_NUMBER — 999 on builds before SQLite 3.32.
MAX_BOUND_PARAMS = 500

T = TypeVar("T")


def chunked(items: Sequence[T], size: int = MAX_BOUND_PARAMS) -> list[list[T]]:
    """Split *items* into chunks that fit SQLite's bound-parameter limit.

    A single translation unit on an SDK-heavy project walks well over a
    thousand header files, so ``WHERE path IN (?, ?, ...)`` over the whole
    set would exceed the limit.  The statements run in batches instead.

    The rule is: ``size * binds_per_item + fixed_params <= 999``.  The
    default 500 holds for a statement that binds each item one time and adds
    one config_hash.

    Pass a smaller *size* when ONE statement in the loop binds each item MORE
    THAN ONE TIME.  _delete_dangling_incoming_refs runs five statements per
    batch; four of them bind each item one time, and the fifth matches
    fp_assignments on lhs_usr and on rhs_usr.  That one statement sets the
    limit, so its 400 keeps 1 + 2 * 400 below 999.

    Generic on purpose: the callers pass list[int] and list[str], and the
    project asks for precise types.
    """
    return [list(items[i : i + size]) for i in range(0, len(items), size)]
