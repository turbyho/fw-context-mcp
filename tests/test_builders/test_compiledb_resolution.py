"""Finding compiledb, and the error message when it is not there.

fw-context is installed into its own virtualenv and reached through a
symlink in ``~/.local/bin``, so that virtualenv's ``bin/`` is not on PATH.
Resolution used to be "the configured interpreter, or a binary on PATH",
which meant a Makefile project failed with "Install it: pip install
compiledb" while ``~/.fw-context/.venv/bin/compiledb`` was present and
``python -m compiledb`` worked — advice that could not have helped.

These tests pin each route and the message, because the failure mode was
not a crash but a plausible lie.
"""
import sys

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer.builders import makefile as makefile_builder
from fw_context_mcp.indexer.builders.makefile import (
    _module_is_importable,
    _resolve_compiledb,
)


@pytest.fixture
def no_path_binary(monkeypatch):
    """Nothing on PATH, which is the state inside fw-context's own venv."""
    monkeypatch.setattr(makefile_builder.shutil, "which", lambda _name: None)


@pytest.fixture
def not_importable(monkeypatch):
    monkeypatch.setattr(
        makefile_builder, "_module_is_importable", lambda _name: False,
    )


@pytest.fixture
def importable(monkeypatch):
    monkeypatch.setattr(
        makefile_builder, "_module_is_importable", lambda _name: True,
    )


def test_the_configured_interpreter_wins(no_path_binary, importable):
    """An explicit choice is not second-guessed."""
    assert _resolve_compiledb("/opt/py/bin/python") == [
        "/opt/py/bin/python", "-m", "compiledb",
    ]


def test_the_running_interpreter_is_used_when_the_module_imports(
    no_path_binary, importable,
):
    """The regression: importable module, empty PATH, must NOT raise.

    This is the exact production shape that used to fail.
    """
    assert _resolve_compiledb(None) == [sys.executable, "-m", "compiledb"]


def test_a_binary_on_path_is_the_fallback(monkeypatch, not_importable):
    """A system-wide install outside any virtualenv still works."""
    monkeypatch.setattr(
        makefile_builder.shutil, "which", lambda _name: "/usr/bin/compiledb",
    )
    assert _resolve_compiledb(None) == ["/usr/bin/compiledb"]


def test_the_running_interpreter_beats_a_binary_on_path(monkeypatch, importable):
    """Prefer the environment fw-context runs in.

    A compiledb on PATH can belong to a different Python than the one that
    will parse the result, so the interpreter that owns the install is the
    safer of the two.
    """
    monkeypatch.setattr(
        makefile_builder.shutil, "which", lambda _name: "/usr/bin/compiledb",
    )
    assert _resolve_compiledb(None) == [sys.executable, "-m", "compiledb"]


def test_the_error_names_every_route_that_was_tried(no_path_binary, not_importable):
    """The old message sent the reader to reinstall something present.

    The new one has to say what was looked for, so "it is already
    installed" is a visible contradiction rather than a mystery.
    """
    with pytest.raises(RuntimeError) as raised:
        _resolve_compiledb(None)

    message = str(raised.value)
    assert "[build] python" in message
    assert "-m compiledb" in message
    assert "PATH" in message
    assert sys.executable in message, "the reader needs to know WHICH python"
    assert "bear" in message, "the documented alternative must stay"


def test_no_interpreter_path_does_not_crash(monkeypatch, no_path_binary, importable):
    """``sys.executable`` can be empty in an embedded interpreter.

    Then route 2 is unusable and the code must fall through rather than
    build a command starting with an empty string.
    """
    monkeypatch.setattr(sys, "executable", "")

    with pytest.raises(RuntimeError) as raised:
        _resolve_compiledb(None)

    assert "the running interpreter" in str(raised.value)


class TestModuleIsImportable:
    """The check has to answer, never raise — it gates a build."""

    def test_a_module_that_exists(self):
        assert _module_is_importable("json") is True

    def test_a_module_that_does_not(self):
        assert _module_is_importable("fw_context_no_such_module") is False

    def test_a_name_that_is_not_a_module_at_all(self):
        """A dotted name under a non-package parent raises inside find_spec."""
        assert _module_is_importable("json.encoder.nope") is False

    def test_compiledb_itself(self):
        """Whatever the answer, it must be a bool and must not raise.

        Asserting True would tie the suite to compiledb being installed;
        the integration tests in test_makefile.py already skip on that.
        """
        assert isinstance(_module_is_importable("compiledb"), bool)
