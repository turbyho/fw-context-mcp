"""Evaluate a GNU as absolute expression, or say that it cannot be done.

The assembly reader needs this for one job: to decide which branch of a
top-level ``.if`` the assembler takes.  Without it both branches reach the
index, and a symbol or a call of the dead branch reads as live code.

The rules come from the manual (``as.info``, node ``Infix Ops``) and from
the assembler itself.  Each rule that the manual does not state was
measured with ``arm-none-eabi-gcc`` 12.3 and ``nm``:

* Four infix levels.  Highest: ``* / % << >>``.  Then ``| & ^ !``.  Then
  ``+ - == <> != < > >= <=``.  Then ``&&``, and ``||`` lowest.  Thus
  ``1 | 2 + 1`` is 4, and ``6 & 3 == 2`` is true.
* A comparison gives -1 for true and 0 for false.  ``&&`` and ``||`` give
  1 and 0.  A comparison is signed.
* Prefix ``-``, ``~``, ``+`` and ``!`` (logical not).  Infix ``!`` is
  "or not": ``7 ! 0`` is -1.
* The values are 64-bit two's complement: ``0x7fffffffffffffff + 1`` is
  below 0.  ``>>`` is a LOGICAL shift: ``-8 >> 1`` is not -4.  ``/`` and
  ``%`` truncate toward zero, as in C.

None is the answer for each expression that this cannot evaluate exactly:
a symbol with no known absolute value (a label, an undefined name), the
location counter ``.``, a string, a local label reference such as ``1f``,
a division by zero, or text that does not parse.  The caller must then
not choose a branch.  A wrong branch puts dead code in the index, and that
is the error this module exists to prevent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

_BITS = 64
_MASK = (1 << _BITS) - 1

# Longest operators first, so that `<<` is never read as two `<`.
_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<number>0[xX][0-9A-Fa-f]+|0[bB][01]+|[0-9]+)(?![\w.$])"
    r"|(?P<char>'[^\\])"
    r"|(?P<name>[A-Za-z_.$][\w.$]*)"
    r"|(?P<op><<|>>|<>|<=|>=|==|!=|&&|\|\||[-+*/%|&^!~<>()])"
    r")"
)

# Binding power of each infix operator: a larger number binds tighter.
# `&&` binds tighter than `||`: "the logical or operator has a slightly
# lower precedence than logical and".
_INFIX = {
    "*": 5, "/": 5, "%": 5, "<<": 5, ">>": 5,
    "|": 4, "&": 4, "^": 4, "!": 4,
    "+": 3, "-": 3, "==": 3, "<>": 3, "!=": 3,
    "<": 3, ">": 3, ">=": 3, "<=": 3,
    "&&": 2,
    "||": 1,
}


class _Unknown(Exception):
    """The expression has no value that this can compute exactly."""


def _signed(value: int) -> int:
    """Wrap *value* to a 64-bit two's complement number."""
    value &= _MASK
    return value - (1 << _BITS) if value >> (_BITS - 1) else value


def _tokens(text: str) -> list[tuple[str, str]]:
    """Split *text* into (kind, text) tokens.  Raises _Unknown on any rest."""
    found: list[tuple[str, str]] = []
    position = 0
    stripped = text.rstrip()
    while position < len(stripped):
        match = _TOKEN.match(stripped, position)
        if match is None or match.end() == position:
            raise _Unknown
        kind = match.lastgroup
        if kind is None:
            raise _Unknown
        found.append((kind, match.group(kind)))
        position = match.end()
    return found


def _number(text: str) -> int:
    """Read an integer constant: hex, binary, octal (leading 0) or decimal."""
    lowered = text.lower()
    if lowered.startswith("0x"):
        return int(text[2:], 16)
    if lowered.startswith("0b"):
        return int(text[2:], 2)
    if len(text) > 1 and text.startswith("0"):
        if any(digit in "89" for digit in text):
            raise _Unknown
        return int(text, 8)
    return int(text, 10)


def _apply(operator: str, left: int, right: int) -> int:
    """Apply one infix operator with the semantics of the assembler."""
    if operator in ("/", "%"):
        if right == 0:
            raise _Unknown
        quotient = abs(left) // abs(right)
        if (left < 0) != (right < 0):
            quotient = -quotient
        return _signed(quotient if operator == "/" else left - quotient * right)
    if operator in ("<<", ">>"):
        if not 0 <= right < _BITS:
            raise _Unknown
        if operator == "<<":
            return _signed(left << right)
        return _signed((left & _MASK) >> right)
    simple = {
        "*": lambda: left * right,
        "|": lambda: left | right,
        "&": lambda: left & right,
        "^": lambda: left ^ right,
        "!": lambda: left | ~right,
        "+": lambda: left + right,
        "-": lambda: left - right,
        "==": lambda: -1 if left == right else 0,
        "<>": lambda: -1 if left != right else 0,
        "!=": lambda: -1 if left != right else 0,
        "<": lambda: -1 if left < right else 0,
        ">": lambda: -1 if left > right else 0,
        ">=": lambda: -1 if left >= right else 0,
        "<=": lambda: -1 if left <= right else 0,
        "&&": lambda: 1 if left and right else 0,
        "||": lambda: 1 if left or right else 0,
    }
    return _signed(simple[operator]())


class _Parser:
    """Precedence climbing over the token list."""

    def __init__(self, tokens: list[tuple[str, str]],
                 symbols: Mapping[str, int | None]) -> None:
        self.tokens = tokens
        self.symbols = symbols
        self.index = 0

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise _Unknown
        self.index += 1
        return token

    def expression(self, minimum: int = 1) -> int:
        left = self._operand()
        while True:
            token = self._peek()
            if token is None or token[0] != "op" or token[1] not in _INFIX:
                return left
            power = _INFIX[token[1]]
            if power < minimum:
                return left
            self.index += 1
            # Left associative: the right side binds only tighter operators.
            right = self.expression(power + 1)
            left = _apply(token[1], left, right)

    def _operand(self) -> int:
        kind, text = self._take()
        if kind == "number":
            return _signed(_number(text))
        if kind == "char":
            return ord(text[1])
        if kind == "name":
            value = self.symbols.get(text)
            if value is None:
                raise _Unknown
            return value
        if text == "(":
            value = self.expression()
            if self._take() != ("op", ")"):
                raise _Unknown
            return value
        if text == "-":
            return _signed(-self._operand())
        if text == "+":
            return self._operand()
        if text == "~":
            return _signed(~self._operand())
        if text == "!":
            return 0 if self._operand() else 1
        raise _Unknown


def evaluate(text: str, symbols: Mapping[str, int | None]) -> int | None:
    """Return the value of the absolute expression *text*, or None.

    *symbols* maps a name to its absolute value, or to None when the name
    is defined and its value is not known (a label).  A name that is not
    in it has no value here either.

    None means: the value cannot be computed exactly.  Never a guess.
    """
    try:
        tokens = _tokens(text)
        if not tokens:
            return None
        parser = _Parser(tokens, symbols)
        value = parser.expression()
        if parser.index != len(tokens):
            return None
        return value
    except _Unknown:
        return None
