"""Split a recipe line of make into the commands that its shell runs.

GNU make runs each line of a recipe in a shell of its own.  On POSIX the
shell is ``$(SHELL)``.  On Windows it is a POSIX ``sh.exe`` when make
finds one, else ``cmd.exe`` through a batch file.  On Windows make also
runs a line without a special character itself, with no shell.  The
source of these rules is ``construct_command_argv_internal`` in ``job.c``
of GNU make 4.4.1.  This module gives, for one line, each simple command,
its words, its working directory, and if it runs only under a condition.

The grammar is a bounded part of each shell.  A line outside it raises
:class:`Unreadable`.  The reader never guesses the commands of such a line.

POSIX shell (XCU 2.3 to 2.9):

- Supported: quotes, the backslash, the operators ``&& || ; & |``,
  ``( )``, ``{ }``, ``if``, ``while``, ``until``, ``for``, ``!``,
  redirections and comments.
- Out of scope: ``case``, ``select``, a function definition and a
  here-document.
- A word is *dynamic* when the shell gives a part of its value at run
  time: ``$x``, ``$(...)``, a backquote, an unquoted glob character, a
  brace expansion of bash, or a leading ``~``.

cmd.exe:

- Supported: ``^``, double quotes, ``& && || |``, ``( )``, redirections,
  ``rem`` and ``::``.
- Out of scope: ``if``, ``for`` and ``start``.
- A word with ``%`` is dynamic, because cmd.exe expands a variable also
  in quotes.  A word with ``*`` or ``?`` is dynamic, because the C runtime
  of a MinGW program expands the pattern.

Rules for the flow of a line.  make ``-n`` takes each line of a recipe as
successful.  The reader takes each command of a line as successful in the
same way.  Thus a command after ``&&`` runs, and a command after ``;`` or
in a pipeline runs.  A command that a choice of the shell selects at run
time is *conditional*: the command after ``||``, a branch of ``if``, and
the body of a loop.  A ``cd`` in a conditional position gives an unknown
directory.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from ...utils import command_words


class Unreadable(ValueError):
    """The line is no command line of the shell, or it uses a part of the grammar that the reader does not know."""


@dataclass(frozen=True)
class Word:
    """One word of a command, after the removal of its quotes.

    *dynamic* is true when the shell gives a part of the value at run
    time: *text* then holds the text of the line, not the value.
    *assignment* is true for ``NAME=value`` with a name that no quote
    touches: before the program, the POSIX shell reads it as a variable
    for the command (XCU 2.9.1).
    """

    text: str
    dynamic: bool = False
    assignment: bool = False


@dataclass(frozen=True)
class Command:
    """One simple command of a line.

    *directory* is None when the shell changes to a directory that the
    reader cannot tell: a computed or conditional ``cd``, ``cd -``, or
    ``pushd``.  *conditional* is true when a choice of the shell at run
    time decides if the command runs.
    """

    words: tuple[Word, ...]
    directory: Path | None
    conditional: bool


@dataclass
class _Scope:
    """The working directory of the shell at one point of the line."""

    directory: Path | None

    def copy(self) -> _Scope:
        """Return the scope of a subshell: a ``cd`` in it does not change this scope."""
        return _Scope(self.directory)


def recipe_commands(line: str) -> list[str]:
    """Split one recipe line of the log of ``make -n`` at each newline that no backslash escapes.

    make gives the shell the text up to such a newline, and the rest as
    the next command (``restp`` of ``construct_command_argv``).  A
    backslash and a newline stay in the text, because each shell has its
    own rule for them.
    """
    commands: list[str] = []
    start = 0
    index = 0
    while index < len(line):
        if line[index] == "\\" and line[index + 1:index + 2] == "\n":
            index += 2
            continue
        if line[index] == "\n":
            commands.append(line[start:index])
            start = index + 1
        index += 1
    commands.append(line[start:])
    return commands


# ── The fast path of make on Windows ─────────────────────────────────────

#: The characters that make a line go to the shell, of ``job.c`` for
#: Windows (``sh_chars_sh`` and ``sh_chars_dos``).  The backslash rule of
#: the fast path uses the POSIX set for the two shells.
_MAKE_SPECIAL_POSIX = frozenset("#;\"*?[]&|<>(){}$`^")
_MAKE_SPECIAL_CMD = frozenset("|&<>")

#: The builtins of the shell that make does not run itself, of ``job.c``
#: for Windows (``sh_cmds_sh`` and ``sh_cmds_dos``).
_MAKE_BUILTINS_POSIX = frozenset({
    "cd", "eval", "exec", "exit", "login", "logout", "set", "umask", "wait", "while", "for", "case", "if",
    ":", ".", "break", "continue", "export", "read", "readonly", "shift", "times", "trap", "switch",
    "unset", "ulimit", "command", "test",
})
_MAKE_BUILTINS_CMD = frozenset({
    "assoc", "break", "call", "cd", "chcp", "chdir", "cls", "color", "copy", "ctty", "date", "del", "dir",
    "echo", "echo.", "endlocal", "erase", "exit", "for", "ftype", "goto", "if", "md", "mkdir", "move",
    "path", "pause", "prompt", "rd", "rem", "ren", "rename", "rmdir", "set", "setlocal", "shift", "time",
    "title", "type", "ver", "verify", "vol", ":",
})


def make_fast_words(command: str, *, posix_shell: bool) -> list[str] | None:
    """Return the arguments that GNU make on Windows gives a program that it runs itself, or None.

    None: make gives *command* to the shell (a special character, a
    builtin, a quote that does not end).  This is the fast path of
    ``construct_command_argv_internal`` (``job.c``, GNU make 4.4.1), for
    a ``WINDOWS32`` build with ``HAVE_DOS_PATHS``.  Its rules are of make,
    and not of a shell: a single and a double quote both start a string,
    and a backslash stays unless a special character of the POSIX shell
    comes after it.  make then gives the arguments to ``CreateProcess`` in
    a form that the C runtime reads back as the same list.

    *posix_shell* is true for a POSIX ``sh.exe`` and false for the batch
    mode of ``cmd.exe``.
    """
    special = _MAKE_SPECIAL_POSIX if posix_shell else _MAKE_SPECIAL_CMD
    builtins = _MAKE_BUILTINS_POSIX if posix_shell else _MAKE_BUILTINS_CMD
    text = command.lstrip(" \t")
    if not text:
        return []
    words: list[str] = []
    current: list[str] = []
    quote = ""
    word_has_equals = False
    seen_nonequals = False
    last_was_empty = False
    index = 0
    while index < len(text):
        char = text[index]
        following = text[index + 1:index + 2]
        if quote:
            if char == quote:
                quote = ""
                if not current:
                    last_was_empty = True
            elif char == "\\" and following == "\n":
                if quote == "'" and posix_shell:
                    current.append("\\\n")
                index += 1
            elif quote == '"' and posix_shell and char in "\\$`":
                return None
            elif quote == '"' and not posix_shell and char in "*?":
                return None
            elif quote == '"' and char == "\\" and following == '"':
                current.append('"')
                index += 1
            else:
                current.append(char)
        elif char in special:
            return None
        elif char == "=":
            if not seen_nonequals and posix_shell:
                return None
            word_has_equals = True
            current.append(char)
        elif char == "\\":
            if following == "\n":
                index += 1
                if not current:
                    while text[index + 1:index + 2] in (" ", "\t"):
                        index += 1
            elif following.isspace() and following and not posix_shell:
                current.append(char)
            elif following:
                if following not in ("\\", "'") and not following.isspace() and following not in _MAKE_SPECIAL_POSIX:
                    current.append(char)
                else:
                    current.append(following)
                    index += 1
        elif char in ("'", '"'):
            quote = char
        elif char in (" ", "\t"):
            words.append("".join(current))
            current = []
            last_was_empty = False
            seen_nonequals = seen_nonequals or not word_has_equals
            if word_has_equals and not seen_nonequals:
                return None
            word_has_equals = False
            if len(words) == 1 and _is_make_builtin(words[0], builtins, posix_shell):
                return None
            while text[index + 1:index + 2] in (" ", "\t"):
                index += 1
        else:
            current.append(char)
        index += 1
    if quote:
        return None
    if current or last_was_empty:
        words.append("".join(current))
    if len(words) == 1 and words[0] in builtins:
        return None
    return words


def _is_make_builtin(word: str, builtins: frozenset[str], posix_shell: bool) -> bool:
    """Say if make gives a command with the program *word* to the shell: cmd.exe compares without case."""
    return word in builtins or (not posix_shell and word.lower() in builtins)


# ── POSIX shell ──────────────────────────────────────────────────────────

_POSIX_OPERATORS = ("&&", "||", ";;", "<<-", "<<", ">>", "<&", ">&", "<>", ">|", ";", "&", "|", "(", ")", "<", ">")
_REDIRECTIONS = frozenset({"<", ">", ">>", "<&", ">&", "<>", ">|"})
_HERE_DOCUMENTS = frozenset({"<<", "<<-"})
_OPERATOR_START = frozenset("&|;<>()")
_UNSUPPORTED_WORDS = frozenset({"case", "select", "function", "coproc"})
_MISPLACED_WORDS = frozenset({"then", "else", "elif", "fi", "do", "done", "esac", "}", "in"})
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_IO_NUMBER = re.compile(r"\d+(?=[<>])")


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    word: Word | None = None
    reservable: bool = False


def _posix_tokens(text: str) -> list[_Token]:
    """Return the tokens of *text* by the token recognition of the POSIX shell (XCU 2.3)."""
    tokens: list[_Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in " \t":
            index += 1
        elif char == "\\" and text[index + 1:index + 2] == "\n":
            index += 2
        elif char == "\n":
            tokens.append(_Token("newline", "\n"))
            index += 1
        elif char == "#":
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif char in _OPERATOR_START:
            operator = next(op for op in _POSIX_OPERATORS if text.startswith(op, index))
            tokens.append(_Token("op", operator))
            index += len(operator)
        elif (io_number := _IO_NUMBER.match(text, index)) is not None:
            tokens.append(_Token("io", io_number.group()))
            index = io_number.end()
        else:
            token, index = _posix_word(text, index)
            tokens.append(token)
    return tokens


def _posix_word(text: str, index: int) -> tuple[_Token, int]:
    """Read the word that starts at *index*; return its token and the index after it."""
    start = index
    value: list[str] = []
    quoted = False
    dynamic = False
    brace: list[str] | None = None
    while index < len(text):
        char = text[index]
        if char in " \t\n" or char in _OPERATOR_START:
            break
        if char == "\\":
            following = text[index + 1:index + 2]
            if following != "\n":
                value.append(following)
                quoted = True
            index += 2
        elif char == "'":
            end = text.find("'", index + 1)
            if end < 0:
                raise Unreadable("a single quote that does not end")
            value.append(text[index + 1:end])
            quoted = True
            index = end + 1
        elif char == '"':
            part, index, expands = _double_quoted(text, index)
            value.append(part)
            quoted = True
            dynamic = dynamic or expands
        elif char == "$":
            end, expands = _dollar(text, index)
            value.append(text[index:end])
            dynamic = dynamic or expands
            index = end
        elif char == "`":
            end = _backquoted(text, index)
            value.append(text[index:end])
            dynamic = True
            index = end
        else:
            if char in "*?[" or (char == "~" and index == start):
                dynamic = True
            elif char == "{":
                brace = []
            elif char == "}" and brace is not None:
                # A brace expansion of bash (a{b,c}, a{1..3}): bash gives
                # the words at run time.  {} and {x} stay as they are.
                content = "".join(brace)
                dynamic = dynamic or "," in content or ".." in content
                brace = None
            elif brace is not None:
                brace.append(char)
            value.append(char)
            index += 1
    word_text = "".join(value)
    assignment = _ASSIGNMENT.match(text, start) is not None
    tilde_in_assignment = assignment and re.search(r"[=:]~", text[start:index]) is not None
    word = Word(word_text, dynamic or tilde_in_assignment, assignment)
    return _Token("word", word_text, word, reservable=not quoted and not dynamic), index


def _double_quoted(text: str, index: int) -> tuple[str, int, bool]:
    """Read the double-quoted part that starts at *index*; return its value, the index after it, and if it expands."""
    value: list[str] = []
    expands = False
    index += 1
    while True:
        if index >= len(text):
            raise Unreadable("a double quote that does not end")
        char = text[index]
        if char == '"':
            return "".join(value), index + 1, expands
        if char == "\\" and text[index + 1:index + 2] in ("$", "`", '"', "\\", "\n"):
            if text[index + 1] != "\n":
                value.append(text[index + 1])
            index += 2
        elif char == "$":
            end, dollar_expands = _dollar(text, index)
            value.append(text[index:end])
            expands = expands or dollar_expands
            index = end
        elif char == "`":
            end = _backquoted(text, index)
            value.append(text[index:end])
            expands = True
            index = end
        else:
            value.append(char)
            index += 1


def _dollar(text: str, index: int) -> tuple[int, bool]:
    """Read the expansion that the ``$`` at *index* starts; return the index after it, and if it expands.

    A ``$`` that starts no expansion is a literal character (XCU 2.6).
    """
    following = text[index + 1:index + 2]
    if following == "(":
        return _balanced(text, index + 1, "(", ")"), True
    if following == "{":
        return _balanced(text, index + 1, "{", "}"), True
    if following and (following.isalpha() or following == "_"):
        name = _NAME.match(text, index + 1)
        assert name is not None
        return name.end(), True
    if following and (following.isdigit() or following in "@*#?-$!"):
        return index + 2, True
    if following == "'":
        # ANSI-C quoting of bash: the reader does not decode it.
        end = index + 2
        while end < len(text) and text[end] != "'":
            end += 2 if text[end] == "\\" else 1
        if end >= len(text):
            raise Unreadable("a $' quote that does not end")
        return end + 1, True
    if following == '"':
        _, end, _ = _double_quoted(text, index + 1)
        return end, True
    return index + 1, False


def _balanced(text: str, index: int, opening: str, closing: str) -> int:
    """Return the index after the *closing* that matches the *opening* at *index*, past quotes and escapes."""
    depth = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "'":
            end = text.find("'", index + 1)
            if end < 0:
                raise Unreadable("a single quote that does not end")
            index = end + 1
            continue
        if char == '"':
            _, index, _ = _double_quoted(text, index)
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise Unreadable(f"a {opening} that does not end")


def _backquoted(text: str, index: int) -> int:
    """Return the index after the backquote that ends the one at *index*."""
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "`":
            return index + 1
        index += 1
    raise Unreadable("a backquote that does not end")


@dataclass
class _Simple:
    words: list[Word]


@dataclass
class _Pipeline:
    commands: list[_Node]


@dataclass
class _AndOr:
    pipelines: list[_Pipeline]
    operators: list[str]


@dataclass
class _List:
    items: list[tuple[_AndOr, bool]] = field(default_factory=list)


@dataclass
class _Subshell:
    body: _List


@dataclass
class _Group:
    body: _List


@dataclass
class _If:
    conditions: list[_List]
    bodies: list[_List]
    otherwise: _List | None


@dataclass
class _Loop:
    condition: _List | None
    body: _List


_Node = _Simple | _Subshell | _Group | _If | _Loop


class _PosixParser:
    """Recursive descent over the tokens of one line (XCU 2.10, without ``case`` and functions)."""

    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._index = 0

    def parse(self) -> _List:
        commands = self._list(frozenset(), top=True)
        rest = self._peek()
        if rest is not None:
            raise Unreadable(f"{rest.text!r} out of place")
        return commands

    def _peek(self) -> _Token | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _is_operator(self, *texts: str) -> bool:
        token = self._peek()
        return token is not None and token.kind == "op" and token.text in texts

    def _is_reserved(self, *texts: str) -> bool:
        token = self._peek()
        return token is not None and token.reservable and token.text in texts

    def _expect(self, text: str) -> None:
        if not self._is_reserved(text):
            raise Unreadable(f"{text} is missing")
        self._index += 1

    def _skip_newlines(self) -> None:
        while (token := self._peek()) is not None and token.kind == "newline":
            self._index += 1

    def _list(self, stop: frozenset[str], *, top: bool = False) -> _List:
        result = _List()
        while True:
            self._skip_newlines()
            token = self._peek()
            if token is None or self._is_operator(")", ";;") or (token.reservable and token.text in stop):
                break
            and_or = self._and_or()
            separator = self._peek()
            if separator is not None and separator.kind == "op" and separator.text in (";", "&"):
                result.items.append((and_or, separator.text == "&"))
                self._index += 1
                continue
            result.items.append((and_or, False))
            if separator is not None and separator.kind == "newline":
                continue
            break
        if not result.items and not top:
            raise Unreadable("a compound command with no command")
        return result

    def _and_or(self) -> _AndOr:
        pipelines = [self._pipeline()]
        operators: list[str] = []
        while (operator := self._peek()) is not None and operator.kind == "op" and operator.text in ("&&", "||"):
            operators.append(operator.text)
            self._index += 1
            self._skip_newlines()
            pipelines.append(self._pipeline())
        return _AndOr(pipelines, operators)

    def _pipeline(self) -> _Pipeline:
        if self._is_reserved("!"):
            self._index += 1
        commands = [self._command()]
        while self._is_operator("|"):
            self._index += 1
            self._skip_newlines()
            commands.append(self._command())
        return _Pipeline(commands)

    def _command(self) -> _Node:
        token = self._peek()
        if token is None:
            raise Unreadable("a command is missing")
        node: _Node
        if self._is_operator("("):
            self._index += 1
            node = _Subshell(self._list(frozenset()))
            if not self._is_operator(")"):
                raise Unreadable(") is missing")
            self._index += 1
        elif token.reservable and token.text == "{":
            self._index += 1
            node = _Group(self._list(frozenset({"}"})))
            self._expect("}")
        elif token.reservable and token.text == "if":
            node = self._if()
        elif token.reservable and token.text in ("while", "until"):
            self._index += 1
            condition = self._list(frozenset({"do"}))
            self._expect("do")
            node = _Loop(condition, self._list(frozenset({"done"})))
            self._expect("done")
        elif token.reservable and token.text == "for":
            node = self._for()
        elif token.reservable and token.text in _UNSUPPORTED_WORDS:
            raise Unreadable(f"{token.text} is out of the grammar of the reader")
        elif token.reservable and token.text in _MISPLACED_WORDS:
            raise Unreadable(f"{token.text} without its compound command")
        else:
            return self._simple()
        self._redirections()
        return node

    def _if(self) -> _If:
        self._index += 1
        conditions = [self._list(frozenset({"then"}))]
        self._expect("then")
        bodies = [self._list(frozenset({"elif", "else", "fi"}))]
        while self._is_reserved("elif"):
            self._index += 1
            conditions.append(self._list(frozenset({"then"})))
            self._expect("then")
            bodies.append(self._list(frozenset({"elif", "else", "fi"})))
        otherwise = None
        if self._is_reserved("else"):
            self._index += 1
            otherwise = self._list(frozenset({"fi"}))
        self._expect("fi")
        return _If(conditions, bodies, otherwise)

    def _for(self) -> _Loop:
        self._index += 1
        name = self._peek()
        if name is None or name.kind != "word" or not _NAME.fullmatch(name.text):
            raise Unreadable("for without a name")
        self._index += 1
        self._skip_newlines()
        if self._is_reserved("in"):
            self._index += 1
            while (token := self._peek()) is not None and token.kind == "word":
                self._index += 1
            end = self._peek()
            if end is None or not (end.kind == "newline" or (end.kind == "op" and end.text == ";")):
                raise Unreadable("for ... in without its end")
            self._index += 1
        elif self._is_operator(";"):
            self._index += 1
        self._skip_newlines()
        self._expect("do")
        body = self._list(frozenset({"done"}))
        self._expect("done")
        return _Loop(None, body)

    def _simple(self) -> _Simple:
        words: list[Word] = []
        consumed = False
        while (token := self._peek()) is not None:
            if token.kind == "word":
                assert token.word is not None
                words.append(token.word)
            elif token.kind == "io":
                if not self._is_redirection(self._index + 1):
                    raise Unreadable(f"a number {token.text} before no redirection")
            elif token.kind == "op" and token.text in _REDIRECTIONS:
                target = self._tokens[self._index + 1] if self._index + 1 < len(self._tokens) else None
                if target is None or target.kind != "word":
                    raise Unreadable(f"{token.text} without its file")
                self._index += 1
            elif token.kind == "op" and token.text in _HERE_DOCUMENTS:
                raise Unreadable("a here-document is out of the grammar of the reader")
            elif token.kind == "op" and token.text == "(" and words:
                raise Unreadable("a function definition is out of the grammar of the reader")
            else:
                break
            consumed = True
            self._index += 1
        if not consumed:
            raise Unreadable(f"a command is missing before {token.text!r}" if token is not None else "a command is missing")
        return _Simple(words)

    def _is_redirection(self, index: int) -> bool:
        return index < len(self._tokens) and self._tokens[index].kind == "op" and self._tokens[index].text in _REDIRECTIONS

    def _redirections(self) -> None:
        while (token := self._peek()) is not None and (
            token.kind == "io" or (token.kind == "op" and token.text in _REDIRECTIONS | _HERE_DOCUMENTS)
        ):
            if token.kind == "op" and token.text in _HERE_DOCUMENTS:
                raise Unreadable("a here-document is out of the grammar of the reader")
            if token.kind == "op":
                target = self._tokens[self._index + 1] if self._index + 1 < len(self._tokens) else None
                if target is None or target.kind != "word":
                    raise Unreadable(f"{token.text} without its file")
                self._index += 1
            self._index += 1


def posix_commands(command: str, directory: Path, *, cdpath: bool) -> list[Command]:
    """Return the simple commands of *command* for a POSIX shell that starts in *directory*.

    *cdpath* is true when ``CDPATH`` is set: a ``cd`` to a relative name
    can then go to a directory of ``CDPATH``.

    Raises :class:`Unreadable` for a syntax error, or for a construct out
    of the grammar of the reader.
    """
    tree = _PosixParser(_posix_tokens(command)).parse()
    commands: list[Command] = []
    _walk_list(tree, _Scope(directory), False, commands, cdpath)
    return commands


def _walk_list(commands: _List, scope: _Scope, conditional: bool, out: list[Command], cdpath: bool) -> None:
    for and_or, in_background in commands.items:
        # A list in the background runs in a subshell.
        _walk_and_or(and_or, scope.copy() if in_background else scope, conditional, out, cdpath)


def _walk_and_or(and_or: _AndOr, scope: _Scope, conditional: bool, out: list[Command], cdpath: bool) -> None:
    for position, pipeline in enumerate(and_or.pipelines):
        after_or = position > 0 and and_or.operators[position - 1] == "||"
        _walk_pipeline(pipeline, scope, conditional or after_or, out, cdpath)


def _walk_pipeline(pipeline: _Pipeline, scope: _Scope, conditional: bool, out: list[Command], cdpath: bool) -> None:
    if len(pipeline.commands) == 1:
        _walk_node(pipeline.commands[0], scope, conditional, out, cdpath)
        return
    for node in pipeline.commands:
        # bash runs each command of a pipeline in a subshell.
        _walk_node(node, scope.copy(), conditional, out, cdpath)


def _walk_node(node: _Node, scope: _Scope, conditional: bool, out: list[Command], cdpath: bool) -> None:
    if isinstance(node, _Simple):
        program = node.words[0] if node.words else None
        if program is not None and not program.dynamic and program.text == "cd":
            scope.directory = None if conditional else _posix_cd(node.words[1:], scope.directory, cdpath)
        elif program is not None and not program.dynamic and program.text in ("pushd", "popd"):
            scope.directory = None
        elif node.words:
            out.append(Command(tuple(node.words), scope.directory, conditional))
    elif isinstance(node, _Subshell):
        _walk_list(node.body, scope.copy(), conditional, out, cdpath)
    elif isinstance(node, _Group):
        _walk_list(node.body, scope, conditional, out, cdpath)
    elif isinstance(node, _If):
        # In the sequence of the text: the first condition always runs, and
        # a choice of the shell selects each part after it.
        _walk_list(node.conditions[0], scope, conditional, out, cdpath)
        for position, body in enumerate(node.bodies):
            if position > 0:
                _walk_list(node.conditions[position], scope, True, out, cdpath)
            _walk_list(body, scope, True, out, cdpath)
        if node.otherwise is not None:
            _walk_list(node.otherwise, scope, True, out, cdpath)
    else:
        if node.condition is not None:
            _walk_list(node.condition, scope, conditional, out, cdpath)
        _walk_list(node.body, scope, True, out, cdpath)


def _posix_cd(arguments: list[Word], directory: Path | None, cdpath: bool) -> Path | None:
    """Return the directory after ``cd`` with *arguments* (XCU ``cd``), or None when the reader cannot tell it."""
    physical = False
    while arguments and not arguments[0].dynamic and arguments[0].text in ("-L", "-P", "-e", "--"):
        option = arguments.pop(0).text
        if option == "--":
            break
        physical = physical or option == "-P"
    if directory is None or len(arguments) != 1 or arguments[0].dynamic or arguments[0].text in ("", "-"):
        return None
    target = arguments[0].text
    relative = not target.startswith("/")
    if cdpath and relative and not (target in (".", "..") or target.startswith(("./", "../"))):
        return None
    if physical:
        return (directory / target).resolve()
    return Path(os.path.normpath(directory / target))


# ── cmd.exe ──────────────────────────────────────────────────────────────

_CMD_COMMENT = re.compile(r"(?i:rem)(?=\s|$)|::")
_CMD_UNSUPPORTED = re.compile(r"(?i:if|for|start)(?=\s|$)")
_CMD_CD = re.compile(r"(?i:cd|chdir)(?=$|[\s./\\])(?P<rest>.*)$", re.DOTALL)


@dataclass(frozen=True)
class _CmdItem:
    kind: str
    text: str = ""


def _cmd_items(text: str) -> list[_CmdItem]:
    """Return the simple commands and the operators of the cmd.exe line *text*."""
    items: list[_CmdItem] = []
    current: list[str] = []
    quoted = False
    at_start = True
    depth = 0
    index = 0

    def finish() -> None:
        command = "".join(current).strip()
        if command:
            items.append(_CmdItem("command", command))
        current.clear()

    while index < len(text):
        char = text[index]
        if at_start:
            if char in " \t@":
                index += 1
                continue
            if char == "(":
                items.append(_CmdItem("op", "("))
                depth += 1
                index += 1
                continue
            if _CMD_COMMENT.match(text, index):
                break
            if _CMD_UNSUPPORTED.match(text, index):
                raise Unreadable(f"{text[index:].split()[0]} is out of the grammar of the reader")
            at_start = False
        if quoted:
            current.append(char)
            quoted = char != '"'
            index += 1
            continue
        if char == '"':
            quoted = True
            current.append(char)
            index += 1
        elif char == "^":
            if index + 1 < len(text):
                current.append(text[index + 1])
            index += 2
        elif char in "&|":
            finish()
            operator = text[index:index + 2] if text[index:index + 2] in ("&&", "||") else char
            items.append(_CmdItem("op", operator))
            index += len(operator)
            at_start = True
        elif char == ")" and depth > 0:
            finish()
            items.append(_CmdItem("op", ")"))
            depth -= 1
            index += 1
        elif char in "<>":
            if current and current[-1].isdigit() and (len(current) == 1 or current[-2] in " \t"):
                current.pop()
            index = _cmd_redirection(text, index)
        else:
            current.append(char)
            index += 1
    finish()
    if depth:
        raise Unreadable("( without its )")
    return items


def _cmd_redirection(text: str, index: int) -> int:
    """Skip the redirection of cmd.exe that starts at *index*; return the index after its target."""
    index += 2 if text.startswith(">>", index) else 1
    if text[index:index + 1] == "&" and text[index + 1:index + 2].isdigit():
        return index + 2
    while index < len(text) and text[index] in " \t":
        index += 1
    if index >= len(text):
        raise Unreadable("a redirection without its file")
    quoted = False
    start = index
    while index < len(text):
        char = text[index]
        if char == '"':
            quoted = not quoted
        elif not quoted and (char in " \t&|<>" or char == ")"):
            break
        index += 1
    if index == start:
        raise Unreadable("a redirection without its file")
    return index


def cmd_commands(command: str, directory: Path) -> list[Command]:
    """Return the simple commands of *command* for cmd.exe in batch mode that starts in *directory*.

    make removes each backslash and newline from the line before it writes
    the batch file (``job.c``, ``PRESERVE_BSNL``).  A ``(`` group runs in
    the same cmd.exe, thus a ``cd`` in it stays.  Each side of ``|`` runs
    in a cmd.exe of its own.  ``|`` binds closer than ``&``, ``&&`` and
    ``||``.

    Raises :class:`Unreadable` for a construct out of the grammar of the
    reader.
    """
    items = _cmd_items(command.replace("\\\n", ""))
    sequence, position = _cmd_sequence(items, 0)
    if position != len(items):
        raise Unreadable(") without its (")
    commands: list[Command] = []
    _cmd_walk(sequence, _Scope(directory), False, commands)
    return commands


#: One pipeline of cmd.exe and the operator before it: each unit is a
#: simple command or a group.
_CmdSequence = list[tuple[str, list["str | _CmdSequence"]]]


def _cmd_sequence(items: list[_CmdItem], position: int) -> tuple[_CmdSequence, int]:
    """Read the pipelines from *position* up to the ``)`` that ends a group, or to the end."""
    sequence: _CmdSequence = []
    pipeline: list[str | _CmdSequence] = []
    operator = ""
    while position < len(items):
        item = items[position]
        if item.kind == "op" and item.text == ")":
            position += 1
            break
        if item.kind == "op" and item.text == "(":
            group, position = _cmd_sequence(items, position + 1)
            pipeline.append(group)
        elif item.kind == "command":
            pipeline.append(item.text)
            position += 1
        else:
            raise Unreadable(f"{item.text} without a command before it")
        following = items[position] if position < len(items) else None
        if following is not None and following.kind == "op" and following.text == "|":
            position += 1
            continue
        sequence.append((operator, pipeline))
        pipeline = []
        if following is not None and following.kind == "op" and following.text in ("&", "&&", "||"):
            operator = following.text
            position += 1
    if pipeline:
        sequence.append((operator, pipeline))
    return sequence, position


def _cmd_walk(sequence: _CmdSequence, scope: _Scope, conditional: bool, out: list[Command]) -> None:
    for operator, pipeline in sequence:
        runs_conditionally = conditional or operator == "||"
        for unit in pipeline:
            target = scope.copy() if len(pipeline) > 1 else scope
            if isinstance(unit, str):
                _cmd_command(unit, target, runs_conditionally, out)
            else:
                _cmd_walk(unit, target, runs_conditionally, out)


def _cmd_command(text: str, scope: _Scope, conditional: bool, out: list[Command]) -> None:
    """Apply the cmd.exe command *text* to *scope*, or add it to *out*."""
    change = _CMD_CD.match(text)
    if change is not None:
        scope.directory = None if conditional else _cmd_cd(change.group("rest"), scope.directory)
        return
    words = command_words(text, windows=True)
    if words and words[0].lower() in ("pushd", "popd"):
        scope.directory = None
        return
    if words:
        out.append(Command(tuple(Word(word, dynamic=any(c in word for c in "%*?")) for word in words), scope.directory, conditional))


def _cmd_cd(rest: str, directory: Path | None) -> Path | None:
    """Return the directory after ``cd`` of cmd.exe with the text *rest*, or None when the reader cannot tell it.

    cmd.exe reads the rest of the line as one path: a space needs no
    quote.  Without ``/d``, a path on a different drive changes the
    directory of that drive, and not the current directory.
    """
    rest = rest.strip()
    change_drive = False
    if rest[:2].lower() == "/d" and (len(rest) == 2 or rest[2] in " \t"):
        change_drive = True
        rest = rest[2:].strip()
    target = rest.replace('"', "").strip()
    if directory is None or "%" in target:
        return None
    if not target:
        return directory
    pure = PureWindowsPath(target)
    if pure.drive and not pure.root:
        # A drive-relative path (D:dir) names the directory of a drive
        # that only the shell knows.
        return None
    if pure.drive and pure.drive.lower() != PureWindowsPath(directory).drive.lower() and not change_drive:
        return directory
    return Path(os.path.normpath(directory / target))
