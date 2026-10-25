"""The GNU as expression evaluator, rule by rule.

Each value below was measured with `arm-none-eabi-gcc` 12.3: a `.if` on
the expression and `nm` on the object.  `tests/fixtures/asm/conditionals.S`
holds the same rules in a file the assembler checks on each run.
"""

from __future__ import annotations

import pytest

from fw_context_mcp.indexer._asm_expr import evaluate


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("7", 7),
        ("0x1F", 31),
        ("0b101", 5),
        ("010", 8),
        ("'A", 65),
        ("2 + 3 * 4", 14),
        # `|` binds tighter than `+`, the reverse of C.
        ("1 | 2 + 1", 4),
        ("6 & 3 == 2", -1),
        ("2 - 3 - 4", -5),
        ("2 * 3 % 4", 2),
        ("1 == 1 == -1", -1),
        # `&&` binds tighter than `||`.
        ("1 || 0 && 0", 1),
        ("-7 / 2", -3),
        ("-7 % 2", -1),
        ("1 << 2", 4),
        ("5 ^ 3", 6),
        ("+3", 3),
        ("~0", -1),
        ("!0", 1),
        ("!5", 0),
        # Infix `!` is "or not".
        ("7 ! 0", -1),
    ],
)
def test_a_value(text: str, value: int):
    assert evaluate(text, {}) == value


@pytest.mark.parametrize(
    ("text", "truth"),
    [
        ("1 < 2", -1),
        ("2 < 1", 0),
        ("1 <> 2", -1),
        ("1 != 1", 0),
        ("1 >= 1", -1),
        ("-1 < 0", -1),
        ("1 && 2", 1),
        ("0 || 0", 0),
    ],
)
def test_a_comparison_gives_minus_one_and_a_logical_operator_one(text: str, truth: int):
    assert evaluate(text, {}) == truth


def test_the_values_are_64_bit_twos_complement():
    assert evaluate("0x7fffffffffffffff + 1", {}) < 0
    assert evaluate("0xffffffff + 1", {}) == 0x100000000


def test_a_right_shift_is_logical():
    """Measured: `.if (-8 >> 1) == -4` is false."""
    assert evaluate("-8 >> 1", {}) == 0x7FFFFFFFFFFFFFFC


def test_a_symbol_takes_its_value():
    assert evaluate("LEVEL + 1", {"LEVEL": 2}) == 3


@pytest.mark.parametrize(
    "text",
    [
        "label_a - label_b",  # a label has an address, not a known value
        "undefined_name",
        ".",
        "1f",
        '"text"',
        "1 / 0",
        "1 % 0",
        "1 << 64",
        "(1 + 2",
        "1 +",
        "",
        "09",
    ],
)
def test_no_exact_value_gives_none(text: str):
    assert evaluate(text, {"label_a": None, "label_b": None}) is None
