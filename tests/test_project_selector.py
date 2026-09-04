"""The ``project`` selector must reach the named project, or fail loudly.

Before this selector existed, the only way to name a project was
``project_root`` — an absolute path.  A caller that wrote
``project="<name>"`` got no error: pydantic ignores an unknown argument,
thus the tool answered about the project of the current directory.  The
answer looked correct but came from other source code.

These tests hold the four parts that together close that hole:

- ``get_projects_by_name`` — the registry lookup
- ``resolve_project_root`` — one field that takes a path, a name, or an id
- ``_with_project_selector`` — the ``project`` parameter of each tool
- ``_forbid_unknown_tool_arguments`` — an unknown argument is an error
"""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Annotated

import pytest
from pydantic import Field, TypeAdapter, ValidationError

from fw_context_mcp.config import global_db
from fw_context_mcp.mcp import server
from fw_context_mcp.utils import AmbiguousProjectError, resolve_project_root

# sqlite3 through the module that the code under test uses.  A separate
# import in this file would bind the standard library module when the
# pysqlite3 redirect has not run yet, and the two exception hierarchies
# do not match.
sqlite3 = global_db.sqlite3


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Give the global project registry its own database for one test.

    ``_global_db_path()`` reads the module attribute at call time, thus a
    monkeypatch of that attribute reaches ``open_global_db()``.  The
    cached connection must be cleared as well, otherwise the test reuses
    the connection to the real registry of the user.
    """
    monkeypatch.setattr(global_db, "_GLOBAL_DB_PATH", tmp_path / "projects.db")
    monkeypatch.setattr(global_db, "_global_conn", None)
    conn = global_db.open_global_db()
    yield conn
    conn.close()


def _register(conn, project_id: str, name: str, root_path) -> None:
    """Write one project into the test registry."""
    global_db.upsert_project_registry(conn, project_id, name, "mbed-os", str(root_path))


# ── Registry lookup ────────────────────────────────────────────────────


def test_a_name_with_no_match_gives_an_empty_list(registry):
    assert global_db.get_projects_by_name("no-such-project") == []


def test_a_name_with_one_match_gives_that_project(registry, tmp_path):
    _register(registry, "a" * 32, "boot-loader", tmp_path / "boot-loader")
    rows = global_db.get_projects_by_name("boot-loader")
    assert [r["project_id"] for r in rows] == ["a" * 32]


def test_a_duplicate_name_gives_every_match(registry, tmp_path):
    """The name column has no UNIQUE constraint, thus duplicates are real."""
    _register(registry, "a" * 32, "boot-loader", tmp_path / "work" / "boot-loader")
    _register(registry, "b" * 32, "boot-loader", tmp_path / "archive" / "boot-loader")
    rows = global_db.get_projects_by_name("boot-loader")
    assert len(rows) == 2
    assert {r["project_id"] for r in rows} == {"a" * 32, "b" * 32}


def test_the_match_is_exact(registry, tmp_path):
    """A prefix match would let one name select a different project."""
    _register(registry, "a" * 32, "boot-loader", tmp_path / "boot-loader")
    assert global_db.get_projects_by_name("boot") == []
    assert global_db.get_projects_by_name("boot-loader-v2") == []


# ── resolve_project_root ───────────────────────────────────────────────


def test_an_existing_directory_wins_over_a_registry_name(registry, tmp_path, monkeypatch):
    """A directory that the caller can see must not be shadowed by a name."""
    visible = tmp_path / "boot-loader"
    visible.mkdir()
    _register(registry, "a" * 32, "boot-loader", tmp_path / "elsewhere")
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root("boot-loader") == visible.resolve()


def test_a_name_resolves_to_the_registered_root(registry, tmp_path, monkeypatch):
    root = tmp_path / "vendor" / "boot-loader"
    root.mkdir(parents=True)
    _register(registry, "a" * 32, "boot-loader", root)
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root("boot-loader") == root.resolve()


def test_a_project_id_resolves_to_the_registered_root(registry, tmp_path, monkeypatch):
    root = tmp_path / "vendor" / "boot-loader"
    root.mkdir(parents=True)
    _register(registry, "a" * 32, "boot-loader", root)
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root("a" * 32) == root.resolve()


def test_an_ambiguous_name_names_every_candidate(registry, tmp_path, monkeypatch):
    """fw-context must not choose — the wrong choice looks correct."""
    first = tmp_path / "work" / "boot-loader"
    second = tmp_path / "archive" / "boot-loader"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    _register(registry, "a" * 32, "boot-loader", first)
    _register(registry, "b" * 32, "boot-loader", second)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AmbiguousProjectError) as exc:
        resolve_project_root("boot-loader")
    message = str(exc.value)
    assert "a" * 32 in message
    assert "b" * 32 in message
    assert str(first) in message
    assert str(second) in message


def test_an_unknown_selector_stays_a_path(registry, tmp_path, monkeypatch):
    """The behaviour for a path that does not exist yet does not change."""
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root("not-indexed") == (tmp_path / "not-indexed").resolve()


def test_a_broken_registry_does_not_stop_path_resolution(tmp_path, monkeypatch):
    """A registry that cannot be read must not break every tool call."""

    def _raise(_selector):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(global_db, "get_project_by_id", _raise)
    monkeypatch.setattr(global_db, "get_projects_by_name", _raise)
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root("boot-loader") == (tmp_path / "boot-loader").resolve()


def test_no_selector_keeps_the_git_root_search(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "drivers"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert resolve_project_root(None) == repo


# ── _merge_project_selector ────────────────────────────────────────────


def test_project_becomes_project_root():
    kwargs = server._merge_project_selector("lookup_symbol", {"name": "main", "project": "boot"})
    assert kwargs == {"name": "main", "project_root": "boot"}


def test_an_empty_project_keeps_the_default():
    kwargs = server._merge_project_selector("lookup_symbol", {"name": "main", "project": "  "})
    assert kwargs == {"name": "main"}


def test_the_same_value_in_both_fields_is_accepted():
    kwargs = server._merge_project_selector(
        "lookup_symbol", {"project": "/tmp/boot", "project_root": "/tmp/boot"}
    )
    assert kwargs == {"project_root": "/tmp/boot"}


def test_two_different_projects_in_one_call_is_an_error():
    """Choosing one of them silently would answer from the wrong project."""
    with pytest.raises(ValueError, match="different projects"):
        server._merge_project_selector(
            "lookup_symbol", {"project": "boot-loader", "project_root": "/tmp/other"}
        )


# ── The registered tools ───────────────────────────────────────────────


def _tools():
    """Return every registered MCP tool."""
    return asyncio.run(server.mcp.list_tools())


TOOLS = _tools()
TOOL_IDS = [t.name for t in TOOLS]


def test_the_server_registers_tools():
    assert len(TOOLS) >= 38, TOOL_IDS


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_a_tool_that_takes_a_project_root_also_takes_a_project(tool):
    """One tool without the parameter recreates the original failure."""
    properties = tool.inputSchema.get("properties", {})
    if "project_root" not in properties:
        pytest.skip(f"{tool.name} takes no project")
    assert "project" in properties, f"{tool.name} has project_root but no project"


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_the_project_parameter_is_described(tool):
    """The description is what the LLM reads before it calls the tool."""
    properties = tool.inputSchema.get("properties", {})
    if "project" not in properties:
        pytest.skip(f"{tool.name} takes no project")
    assert properties["project"].get("description")
    assert "list_projects" in properties["project"]["description"], (
        f"{tool.name}: the description does not say where the names come from"
    )


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_the_project_entry_sits_in_the_args_section(tool):
    """A parameter documented after Returns: is not in the parameter list.

    The model reads ``Args:`` to learn what it can pass.  An entry that
    hangs at the end of the docstring is a different kind of text, and
    every other parameter of the tool is in ``Args:``.
    """
    properties = tool.inputSchema.get("properties", {})
    if "project" not in properties:
        pytest.skip(f"{tool.name} takes no project")
    doc = tool.description or ""
    entry = re.search(r"^[ \t]*project:", doc, re.MULTILINE)
    assert entry, f"{tool.name}: the docstring has no `project:` entry"
    assert "Args:" in doc, f"{tool.name}: the docstring has no Args: section"
    assert doc.index("Args:") < entry.start(), f"{tool.name}: `project:` is before Args:"
    if "Returns:" in doc:
        assert entry.start() < doc.index("Returns:"), (
            f"{tool.name}: `project:` sits after Returns:, thus outside the parameter list"
        )


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_project_root_says_that_it_takes_a_name_too(tool):
    """resolve_project_root resolves a name and an id — the text must say so."""
    properties = tool.inputSchema.get("properties", {})
    if "project_root" not in properties:
        pytest.skip(f"{tool.name} takes no project_root")
    description = properties["project_root"].get("description", "")
    assert "project name or a project_id" in description, (
        f"{tool.name}: project_root still describes a path only"
    )


def test_the_original_project_root_text_is_kept():
    """The handlers say different useful things — none may be lost."""
    tools = {t.name: t for t in TOOLS}
    listing = tools["list_projects"].inputSchema["properties"]["project_root"]["description"]
    assert "distinguish multiple indexed projects" in listing


def test_a_docstring_without_an_args_section_gets_a_separate_block():
    """The fallback covers a handler that someone adds later."""
    result = server._document_project_parameter("Only a summary.\n")
    assert "Project selection:" in result
    assert "project:" in result


def test_the_entry_takes_the_indentation_of_its_neighbour():
    """Handler docstrings are not indented the same way."""
    doc = "Summary.\n\nArgs:\n  project_root: A path.\n  limit: A number.\n"
    result = server._document_project_parameter(doc)
    lines = result.splitlines()
    entry = next(i for i, line in enumerate(lines) if line.lstrip().startswith("project:"))
    assert lines[entry].startswith("  project:")
    assert lines[entry - 1].strip().startswith("project_root:")
    assert lines[-1].strip().startswith("limit:")


def test_a_multi_line_project_root_entry_is_not_split():
    """The new entry must go after the continuation lines, not into them."""
    doc = "Summary.\n\nArgs:\n    project_root: A path.\n        More about it.\n    limit: A number.\n"
    result = server._document_project_parameter(doc)
    lines = result.splitlines()
    entry = next(i for i, line in enumerate(lines) if line.lstrip().startswith("project:"))
    assert lines[entry - 1].strip() == "More about it."


def test_describe_project_root_keeps_the_other_field_settings():
    """Only the description changes — a constraint must survive."""
    param = inspect.Parameter(
        "project_root",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=Annotated[str | None, Field(description="A path.", max_length=42)],
    )
    result = server._describe_project_root(param)
    merged = TypeAdapter(result.annotation).json_schema()
    assert "A path." in merged["description"]
    assert "project name or a project_id" in merged["description"]
    assert merged["anyOf"][0]["maxLength"] == 42


def test_describe_project_root_leaves_other_parameters_alone():
    param = inspect.Parameter(
        "limit",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=Annotated[int, Field(description="A number.")],
    )
    assert server._describe_project_root(param) is param


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_the_wrapper_adds_the_project_parameter_and_nothing_else(tool):
    """The wrapper must not lose or rename an existing parameter."""
    handler = inspect.unwrap(server.mcp._tool_manager.get_tool(tool.name).fn)
    expected = set(inspect.signature(handler).parameters) - {"ctx"}
    got = set(tool.inputSchema.get("properties", {})) - {"project"}
    assert got == expected


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_an_unknown_argument_is_rejected(tool):
    """A misspelled argument must fail, and the failure must name it."""
    assert tool.inputSchema.get("additionalProperties") is False
    arg_model = server.mcp._tool_manager.get_tool(tool.name).fn_metadata.arg_model
    with pytest.raises(ValidationError) as exc:
        arg_model.model_validate({"projekt": "boot-loader"})
    assert "projekt" in str(exc.value)


@pytest.mark.parametrize("tool", TOOLS, ids=TOOL_IDS)
def test_the_context_parameter_stays_out_of_the_schema(tool):
    """ctx is injected by FastMCP — the LLM must never see it."""
    assert "ctx" not in tool.inputSchema.get("properties", {})


def test_a_wrapped_tool_still_gets_its_context():
    """The __signature__ override must not break progress notifications.

    FastMCP finds ctx with typing.get_type_hints(), which reads
    __annotations__, and not with inspect.signature().  A regression here
    stops the 5 s progress notifications and long queries then hit a
    client-side idle timeout.
    """
    assert server.mcp._tool_manager.get_tool("lookup_symbol").context_kwarg == "ctx"


def test_a_tool_without_a_project_root_is_left_alone():
    """get_project_info takes a project_id, thus a project selector is wrong."""
    info = next(t for t in TOOLS if t.name == "get_project_info")
    assert "project" not in info.inputSchema.get("properties", {})


def test_the_selector_reaches_the_handler():
    """End to end: project=<name> must arrive as project_root=<name>."""
    seen: dict = {}

    def handler(
        name: str,
        project_root: str | None = None,
    ) -> list[dict]:
        """Doc.

        Args:
            name: Symbol name.
            project_root: Project root.

        Returns:
            list of dicts.
        """
        seen["project_root"] = project_root
        return [{"name": name}]

    wrapped = server._with_project_selector(handler)
    assert wrapped(name="main", project="boot-loader") == [{"name": "main"}]
    assert seen["project_root"] == "boot-loader"


# ── The instructions ───────────────────────────────────────────────────


def _instruction_copies() -> tuple[tuple[str, str], ...]:
    """The copies of the instruction text that must agree.

    Two remain, and they reach the reader by different routes:

    * ``server.mcp.instructions`` — sent over the MCP protocol.  It is a
      literal in ``server.py`` because FastMCP reads it while it builds the
      object, thus before any file read could run.
    * ``BASE_INSTRUCTIONS`` — what ``fw-context init`` writes into
      ``CLAUDE.md`` and ``AGENTS.md``.

    A third copy lived in ``data/instructions.md`` until 2026-09-04.  No
    code ever read it: a claim corrected in the other two stayed wrong
    there, and the file was deleted rather than kept in step.
    """
    from fw_context_mcp.config.tools import BASE_INSTRUCTIONS

    return (
        ("FastMCP instructions", server.mcp.instructions or ""),
        ("BASE_INSTRUCTIONS", BASE_INSTRUCTIONS),
    )


def test_every_copy_of_the_instructions_teaches_the_selector():
    for label, text in _instruction_copies():
        assert "list_projects" in text, label
        assert "DIFFERENT project" in text, label


def test_no_copy_of_the_instructions_holds_a_dead_file():
    """``data/instructions.md`` is gone.  Nothing may point a reader at it."""
    from pathlib import Path

    import fw_context_mcp

    assert not (Path(fw_context_mcp.__file__).parent / "data" / "instructions.md").exists()
    for label, text in _instruction_copies():
        assert "instructions.md" not in text, label


def test_every_copy_of_the_instructions_states_the_scope_of_search_bodies():
    """The scope of ``search_bodies`` must not drift back to a false claim.

    ``symbols.source`` holds the text of EVERY definition — measured on one
    index: 1,844 struct, 1,142 class, 878 enum, 498 varglobal.  Both copies
    used to say "function BODIES … inside {}" and name a type declaration in
    a header as unreachable, and a reader then avoided the one tool that
    answers such a question.
    """
    for label, text in _instruction_copies():
        assert "TEXT OF EVERY DEFINITION" in text.upper(), label
        # The false claim, in the two shapes both copies carried.
        assert "inside {}" not in text, label
        assert "type definitions in headers" not in text, label


def test_every_copy_of_the_instructions_says_where_a_line_number_comes_from():
    """Without this the reader leaves fw-context for a text search."""
    for label, text in _instruction_copies():
        assert "_match_lines" in text, label
        assert "end_line" in text, label


def test_every_copy_of_the_instructions_marks_the_llm_analysis_untrusted():
    """A model wrote it.  A reader must not quote it as a fact."""
    for label, text in _instruction_copies():
        assert "llm_analysis" in text, label


def test_every_copy_of_the_instructions_names_the_symbol_parameter():
    """A guessed argument name costs a whole call — the tools reject it."""
    for label, text in _instruction_copies():
        assert "symbol_name" in text, label  # named as the WRONG spelling
        assert "PARAMETER NAMES" in text.upper(), label
