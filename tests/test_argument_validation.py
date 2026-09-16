"""A nonsense argument is refused, and the refusal names the parameter.

The handlers clamp: ``max(1, min(limit, 100))`` turns ``limit=-5`` into
1, and ``clamp_offset`` turns ``offset=-1`` into 0.  A clamp is right for
a value that is merely too large, but for a NEGATIVE one it hides a
mistake of the caller and answers as if nothing were wrong.

An empty string was worse than hidden — it was read as "match
everything".  Measured over 13 indexed builds:
``find_indirect_targets(name="")`` answered with 50 unrelated rows,
``trace_data_flow(type_name="")`` with 16, ``semantic_search(query="")``
with 20 and ``smart_search(query="")`` with 22.  An agent whose variable
went missing got a full page of noise instead of an error.

Pydantic decides this now, before the handler runs, thus the error names
the parameter and the bound.  These tests are driven by the REGISTERED
tool list, so a tool that arrives later is covered the day it arrives.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fw_context_mcp.mcp import server

# Zero is a real answer for these, thus only a negative value is refused.
# ``offset`` starts at the beginning, ``context_lines`` asks for a body
# with no context around it, ``max_per_kind=0`` is the documented way to
# say "no cap", and ``start_line``/``end_line`` use 0/0 for a whole file.
ZERO_IS_VALID = {"offset", "context_lines", "max_per_kind", "start_line", "end_line"}

# Zero is nonsense for these: a page of no rows is not a page, a walk of
# depth zero is not a walk, and a timeout of zero gives no time to answer.
ZERO_IS_REFUSED = {"limit", "max_depth", "timeout_ms"}

# Every parameter a tool cannot answer without.  An empty string here is
# a lost variable, never a query.
REQUIRED_STRINGS = {
    "name", "query", "file_path", "class_name", "method_name",
    "template_name", "type_name", "to_symbol", "from_name", "to_name",
}


# The registered tools, with the pydantic model that FastMCP validates
# each call against.  The model is what the server really enforces, thus
# the tests drive it and not the JSON schema beside it.
TOOLS = server.mcp._tool_manager.list_tools()


def _schema_of(tool_name: str) -> dict:
    tool = next(t for t in TOOLS if t.name == tool_name)
    return tool.parameters or {}


def _params_with(predicate) -> list[tuple[str, str]]:
    """Give ``(tool, parameter)`` for every declared parameter that matches."""
    out = []
    for tool in TOOLS:
        schema = tool.parameters or {}
        required = set(schema.get("required", []) or [])
        for name in (schema.get("properties", {}) or {}):
            if predicate(name, name in required):
                out.append((tool.name, name))
    return out


NUMERIC = _params_with(lambda n, _r: n in ZERO_IS_VALID | ZERO_IS_REFUSED)
STRINGS = _params_with(lambda n, req: req and n in REQUIRED_STRINGS)
THRESHOLDS = _params_with(lambda n, _r: n == "threshold")


def _valid_args(tool_name: str) -> dict:
    """Fill every required parameter of *tool* with a value it accepts."""
    schema = _schema_of(tool_name)
    args: dict = {}
    for name in schema.get("required", []) or []:
        spec = (schema.get("properties", {}) or {}).get(name, {})
        args[name] = 1 if spec.get("type") == "integer" else "x"
    return args


def _validate(tool_name: str, args: dict) -> None:
    """Run the arguments through the model, as the server does on a call."""
    tool = next(t for t in TOOLS if t.name == tool_name)
    tool.fn_metadata.arg_model.model_validate(args)


def test_the_suite_covers_every_tool():
    """A tool with no numeric and no required string would go unchecked."""
    covered = {t for t, _ in NUMERIC} | {t for t, _ in STRINGS}
    names = {t.name for t in TOOLS}
    uncovered = names - covered
    # These take no argument that this file can be wrong about.
    assert uncovered <= {
        "check_dependencies", "check_ollama", "configure_llm",
        "get_active_build", "get_environment_status", "get_project_info",
        "list_projects", "list_variants", "reset_index",
    }, f"a tool grew an unchecked argument: {sorted(uncovered)}"
    # Measured when this file was written: 37 numeric parameters and 21
    # required strings over 39 tools.  The floor guards against a change
    # that drops a declaration and leaves the tests passing on nothing.
    assert len(NUMERIC) >= 37, len(NUMERIC)
    assert len(STRINGS) >= 21, len(STRINGS)


@pytest.mark.parametrize(("tool", "param"), NUMERIC, ids=lambda v: str(v))
def test_a_negative_number_is_refused(tool: str, param: str):
    args = _valid_args(tool)
    args[param] = -5
    with pytest.raises(ValidationError) as caught:
        _validate(tool, args)
    assert param in str(caught.value), caught.value


@pytest.mark.parametrize(
    ("tool", "param"),
    [(t, p) for t, p in NUMERIC if p in ZERO_IS_REFUSED],
    ids=lambda v: str(v),
)
def test_zero_is_refused_where_it_means_nothing(tool: str, param: str):
    """A page of no rows and a walk of depth zero are not answers."""
    args = _valid_args(tool)
    args[param] = 0
    with pytest.raises(ValidationError):
        _validate(tool, args)


@pytest.mark.parametrize(
    ("tool", "param"),
    [(t, p) for t, p in NUMERIC if p in ZERO_IS_VALID],
    ids=lambda v: str(v),
)
def test_zero_is_kept_where_it_is_an_answer(tool: str, param: str):
    """The first page, a bare body, an uncapped map — all are real asks."""
    args = _valid_args(tool)
    args[param] = 0
    _validate(tool, args)


@pytest.mark.parametrize(("tool", "param"), STRINGS, ids=lambda v: str(v))
def test_an_empty_required_string_is_refused(tool: str, param: str):
    """An empty name is a lost variable, and it must not read as a wildcard."""
    args = _valid_args(tool)
    args[param] = ""
    with pytest.raises(ValidationError) as caught:
        _validate(tool, args)
    assert param in str(caught.value), caught.value


@pytest.mark.parametrize(("tool", "param"), THRESHOLDS, ids=lambda v: str(v))
def test_a_similarity_outside_its_range_is_refused(tool: str, param: str):
    """Cosine similarity lives in 0.0-1.0; -1.0 and 1.5 name no answer."""
    for bad in (-1.0, 1.5):
        args = _valid_args(tool)
        args[param] = bad
        with pytest.raises(ValidationError):
            _validate(tool, args)


def test_the_documented_defaults_still_validate():
    """Every default value must pass the bound that now stands beside it."""
    for tool in TOOLS:
        schema = tool.parameters or {}
        args = _valid_args(tool.name)
        for name, spec in (schema.get("properties", {}) or {}).items():
            if "default" in spec and spec["default"] is not None:
                args[name] = spec["default"]
        _validate(tool.name, args)
