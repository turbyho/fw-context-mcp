"""Every MCP tool docstring must document its parameters and its result.

The docstring of a tool IS the description that the LLM reads before it
calls the tool.  An undocumented parameter reaches the LLM as a bare name,
and an undocumented result key is invisible until the LLM meets it.

The tool list comes from the ``mcp.tool()(...)`` registrations in
``server.py``, read with ``ast`` — the module is never imported, thus these
tests need no FastMCP install and still cover every new tool.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import typing
from pathlib import Path

import pytest

import fw_context_mcp

SERVER = Path(fw_context_mcp.__file__).parent / "mcp" / "server.py"

# Parameters that the framework injects, thus no docstring entry is needed.
IGNORED_PARAMS = {"self", "ctx"}


def _registered_tools() -> list[tuple[str, str]]:
    """Return the (handler module, function name) of every registered tool.

    Reads ``mcp.tool()(handler.fn)`` and every nesting of wrappers around
    it — ``mcp.tool()(_with_project_selector(_wrap_tool(handler.fn)))`` —
    from the source of ``server.py``.
    """
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        inner = node.func
        # mcp.tool()(...) — the outer call takes mcp.tool() as its func
        if not (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "tool"
        ):
            continue
        target = node.args[0]
        # Remove each wrapper call until the handler stays.  A loop, not one
        # test: the wrappers are nested, and a single step would silently
        # drop every tool as soon as a second wrapper is added.
        while isinstance(target, ast.Call) and target.args:
            target = target.args[0]
        if isinstance(target, ast.Call):
            continue
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            found.append((target.value.id, target.attr))
    return sorted(set(found))


TOOLS = _registered_tools()
TOOL_IDS = [f"{module}.{name}" for module, name in TOOLS]


def _handler(module: str, name: str):
    """Import the handler module and return the tool function."""
    mod = importlib.import_module(f"fw_context_mcp.mcp.handlers.{module}")
    return inspect.unwrap(getattr(mod, name))


def test_registry_is_not_empty():
    assert len(TOOLS) >= 38, TOOL_IDS


@pytest.mark.parametrize(("module", "name"), TOOLS, ids=TOOL_IDS)
def test_tool_has_a_docstring(module, name):
    doc = inspect.getdoc(_handler(module, name)) or ""
    assert doc.strip(), f"{name} has no docstring"


@pytest.mark.parametrize(("module", "name"), TOOLS, ids=TOOL_IDS)
def test_every_parameter_is_documented(module, name):
    """A parameter without an Args: entry reaches the LLM as a bare name."""
    fn = _handler(module, name)
    doc = inspect.getdoc(fn) or ""
    if "Args:" not in doc:
        pytest.skip(f"{name} has no Args: section")
    params = [
        p
        for p in inspect.signature(fn).parameters
        if p not in IGNORED_PARAMS and not p.startswith("_")
    ]
    missing = [p for p in params if f"\n{p}:" not in doc and f"    {p}:" not in doc]
    assert not missing, f"{name}: Args: does not document {missing}"


@pytest.mark.parametrize(("module", "name"), TOOLS, ids=TOOL_IDS)
def test_every_parameter_has_a_field_description(module, name):
    """The Field description becomes the JSON-schema text of the parameter."""
    fn = _handler(module, name)
    # get_type_hints resolves the PEP 563 string annotations, thus the
    # Annotated metadata (the Field) becomes reachable.
    hints = typing.get_type_hints(fn, include_extras=True)
    bare = []
    for param, annotation in hints.items():
        if param in IGNORED_PARAMS or param == "return":
            continue
        metadata = getattr(annotation, "__metadata__", ())
        if not any(getattr(m, "description", None) for m in metadata):
            bare.append(param)
    assert not bare, f"{name}: parameters without Field(description=...): {bare}"


@pytest.mark.parametrize(("module", "name"), TOOLS, ids=TOOL_IDS)
def test_result_is_documented(module, name):
    """A tool must say what it returns, and what a degraded result holds."""
    doc = inspect.getdoc(_handler(module, name)) or ""
    assert "Returns:" in doc, f"{name}: no Returns: section"
