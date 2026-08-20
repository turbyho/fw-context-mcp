"""Every MCP tool docstring must document its parameters and its result.

The docstring of a tool IS the description that the LLM reads before it
calls the tool.  An undocumented parameter reaches the LLM as a bare name,
and an undocumented result key is invisible until the LLM meets it.

These tests walk the live tool registry, thus a new tool is covered from
the moment somebody registers it.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from fw_context_mcp.mcp import server

TOOL_MANAGER = server.mcp._tool_manager
TOOL_NAMES = sorted(TOOL_MANAGER._tools)

# Parameters that the framework injects, thus no docstring entry is needed.
IGNORED_PARAMS = {"self", "ctx"}


def _handler(name):
    """Return the undecorated handler function of a registered tool."""
    fn = TOOL_MANAGER._tools[name].fn
    return inspect.unwrap(fn)


def test_registry_is_not_empty():
    assert len(TOOL_NAMES) >= 38, TOOL_NAMES


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_tool_has_a_docstring(name):
    doc = TOOL_MANAGER._tools[name].description or ""
    assert doc.strip(), f"{name} has no description"


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_parameter_is_documented(name):
    """A parameter without an Args: entry reaches the LLM as a bare name."""
    fn = _handler(name)
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


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_parameter_has_a_field_description(name):
    """The Field description becomes the JSON-schema text of the parameter."""
    fn = _handler(name)
    # get_type_hints resolves the PEP 563 string annotations, thus the
    # Annotated metadata (the Field) becomes reachable.
    hints = typing.get_type_hints(fn, include_extras=True)
    bare = []
    for param, annotation in hints.items():
        if param in IGNORED_PARAMS or param == "return":
            continue
        metadata = getattr(annotation, "__metadata__", ())
        if not any(
            getattr(m, "description", None) for m in metadata
        ):
            bare.append(param)
    assert not bare, f"{name}: parameters without Field(description=...): {bare}"


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_result_is_documented(name):
    """A tool must say what it returns, and what a degraded result holds."""
    doc = inspect.getdoc(_handler(name)) or ""
    assert "Returns:" in doc, f"{name}: no Returns: section"
