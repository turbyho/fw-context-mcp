"""The macros step runs only when the expanded values can differ from the last run.

The step runs ``clang -dM -E`` for each unit of the build.  Measured on a
Mbed project of 878 units: 104 s in each index run that changed nothing.
The values change only when a unit was re-parsed or a file left the index.
"""

from __future__ import annotations

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer._postprocess import _STEPS, _needs_macros


def _gate():
    return next(gate for name, _, gate in _STEPS if name == "macros")


def _ctx(**kw) -> dict:
    return {"index_macros_expanded": True, "units": ["u"], "updated": 0, **kw}


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (_ctx(), False),
        (_ctx(updated=1), True),
        (_ctx(removed_files=2), True),
        (_ctx(updated=3, removed_files=1), True),
        (_ctx(updated=1, index_macros_expanded=False), False),
        (_ctx(updated=1, units=[]), False),
    ],
    ids=["nothing-changed", "a-unit-re-parsed", "a-file-left", "both", "disabled", "no-units"],
)
def test_the_gate_of_the_macros_step(ctx, expected):
    assert bool(_gate()(ctx)) is expected


def test_needs_macros_reads_only_the_two_signals():
    assert _needs_macros({}) is False
    assert _needs_macros({"updated": 0, "removed_files": 0}) is False
