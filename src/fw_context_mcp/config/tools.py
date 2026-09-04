"""AI tool registry — detection, inheritance, instruction injection targets.

Each supported AI assistant is registered with:
- How to detect it (binaries, config dirs)
- Whether it inherits from another tool (e.g. KiloCode shares Claude Code config)
- Where to inject fw-context instructions (target files, methods)
- Collision detection (marked sections, skillshare, unmarked duplicates)

WHY the registry is a declarative dataclass tree instead of imperative
"detect and inject" code: new AI tools appear frequently (weekly).
Adding support is a five-line ``AiTool`` entry with no new logic.
The registry pattern also supports ``fw-context init --list-tools``
output without installing anything.

WHY inheritance (``inherits_from``) exists: some tools read another
tool's config files.  Kilo Code reads OpenCode's AGENTS.md and MCP
config.  Without inheritance tracking, ``fw-context init`` would
inject instructions into both, causing duplicate sections.  Inheritance
prevents double injection while still listing both tools as "detected."

WHY collision detection exists: when a project already has fw-context
instructions (from a previous ``fw-context init`` or manual copy),
blindly appending would create duplicate blocks.  Collision detection
finds existing marked sections (safe to update), unmarked content
(warn about potential duplicates), and skillshare-managed directories
(warn that injection may be overwritten by the skill manager).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from fw_context_mcp.utils import SAFE_EXCEPT, is_fatal

# ── Instruction templates ───────────────────────────────────────────────────

BASE_INSTRUCTIONS = """\
## fw-context — Build-aware code intelligence

CRITICAL: C/C++ questions → fw-context ONLY. This is NOT optional.

For C/C++ code, use fw-context instead of ANY external search or
file-reading tool:

| You want to… | Use | Example |
|---|---|---|
| Find symbol by name | `lookup_symbol` | `"uart_"`, `"HardFault_Handler"` |
| Search by concept/topic | `search_code` | `"interrupt handler"` |
| Search the code of any definition | `search_bodies` | `"attach"`, `"BATT_TEST"` |
| Search preprocessor / file scope | `search_content` | `"extern C"`, `"#define"` |
| Natural-language query | `smart_search` | `"how does the modem connect?"` |
| Read function body | `get_source` | function name |
| Body + callers + callees | `get_symbol_context` | function name |
| Read complete file | `read_file` | `"main.cpp"` |
| File structure overview | `get_file_map` | `"main.cpp"` |
| Check index health | `get_active_build` | — always call first |

SELF-CORRECT: the moment you reach for any tool that is NOT fw-context
for C/C++ code, stop and use the fw-context equivalent instead.

### Code review — use fw-review skill

For C/C++ code review, invoke the `fw-review` skill via the Skill tool.
It handles git discovery, diff scoping, and all analysis. Do NOT do
manual exploration before calling the skill.

### project_only parameter

Your project has TWO kinds of code:
  • Application code: `src/`, `lib/` — YOUR team's code.
  • Vendor SDK: `mbed-os/`, `.pio/`, `zephyr/` — framework code.
Set `project_only=True` for questions about YOUR code.
Leave `project_only=False` (default) when vendor code is relevant.

### A question about a DIFFERENT project

Each tool answers about ONE project. Without `project` or `project_root`
this is the project of the current directory — NOT the project that the
operator asked about.

1. Call `list_projects`. It gives the `name`, the `project_id`, and the
   `root_path` of each indexed project.
2. Give `project="<name>"` (or `project_root="<root_path>"`) to EVERY
   call that follows, `get_active_build` included.

Do not invent other parameter names. An unknown argument causes an error
that names it.

### Parameter names

Every tool rejects an unknown argument, thus a guess costs a whole call.
The schema of each tool is in the tool list — read it there. Three names
cover almost every tool:

- `name` — the symbol. NOT `symbol`, NOT `symbol_name`.
- `file_path` — the file (`read_file`, `get_file_map`).
- `query` — the search terms (every search tool).

No tool takes a filler argument. `get_active_build` accepts only
`project_root` and `fast`. When a call fails on an argument, drop that
argument and REPEAT the call — never continue without the answer, and
never skip `get_active_build`.

### search_bodies reaches more than functions

`search_bodies` searches the text of EVERY definition: a function or
method body, and also the body of a class, struct, union, enum or
namespace, and a global with a multi-line initializer. An enum constant,
a bit field, and a member declaration such as `InterruptIn _pin;` are all
inside it. A match on a type answers with the type, and `_match_lines`
gives the line of the match itself.

Only text that belongs to no definition is out of reach — `#define`,
`#include`, `#ifdef`, `extern "C"`, a file-scope comment. Use
`search_content` for those.

### Where a line number comes from

- `search_bodies` → `_match_lines`, the lines of the matches. `line` is
  the first line of the DEFINITION, far from the match in a long
  function. Never cite `line` for a statement.
- `get_source` / `get_file_map` → `line` and `end_line` are the extent.
  Cite `file:line-end_line`.
- `get_source` is the ONLY tool that numbers its text — every line of its
  `source` starts with the line number. The `source` of `search_bodies`
  and the `content` of `read_file` are BARE. Never count lines there.

These cover the statement-level anchor. Do not leave fw-context for a
text search to find a line number.

### llm_analysis is not evidence

`llm_analysis` (`{summary, inputs, outputs}`) is written by a model, not
by the code. Use it to find a symbol. Never quote it as fact. Quote
`source`, `signature`, or `docstring`.

### FTS5 query tips
- Multi-word bare queries are OR-joined — prefer single words.
- For exact phrases use double quotes: `'"interrupt handler"'`.
- Underscores are word separators — write `"modem init"`, not `"modem_init"`.

### Empty result playbook
1. Simplify to a single-word query in the same tool.
2. Switch tools — search_bodies → search_code, or search_content → search_bodies.
3. Use `lookup_symbol` for known names.
4. search_bodies empty → switch to search_content (covers the
   preprocessor and extern "C", which belong to no definition).
5. Only AFTER exhausting all fw-context tools — use other tools.

### Agent loop
Check(`get_active_build`) → Find(`search_code`/`lookup_symbol`)
→ Read(`get_symbol_context`) ← preferred. Fallback: `get_source` (body only).
→ Trace(`find_references`/`find_callers`) — skip if context from get_symbol_context.
→ For pattern-in-body searches use `search_bodies`.

get_active_build() status:
  • "ready" / "reindexing" — fully operational. Continue.
  • "reindex_needed" — queries work, schedule `fw-context index`.
  • "not_initialized" — ask operator, then run `fw-context init` via bash.
  • "no_index" — ask operator, then run `fw-context index --build` via bash.
  • "error" — DB corruption. Use other tools.

### Diff verification

Diff shows only what changed — it cannot reveal impact across the full
codebase. After inspecting a diff, verify with fw-context:
  • `find_references("<symbol>")` — all callers/readers
  • `search_bodies("<pattern>")` — pattern consistency across codebase
  • `find_call_path` / `find_all_callers_recursive` — cross-module impact
  • `find_dead_code` / `find_hotspots` — structural effects

Do NOT use fw-context in Python, JS, Go, or other non-C/C++ projects.

### Tool categories

Search: lookup_symbol, search_code, search_bodies, search_content,
smart_search, semantic_search, explain_symbol.
Source: get_symbol_context, get_source, get_file_map, read_file.
Call graph: find_callers, find_references, find_call_path,
find_all_callers_recursive, find_callees_recursive, find_hotspots,
find_dead_code, find_wrapper_callers, trace_data_flow,
find_indirect_call_sites, find_indirect_targets.
Inheritance: get_inheritance_chain, get_class_members,
get_template_instances, get_method_overrides.
Maintenance: get_active_build, reindex_file, reset_index,
check_ollama, list_projects, get_project_info.

Individual tool descriptions are in the MCP tool listings —
discover them at runtime.

"""

AGENT_CRITICAL_BLOCK = """\
## CRITICAL — C/C++ source access (fw-context)

ALL C/C++ source code access MUST go through fw-context MCP tools.
Raw source files contain unprocessed #ifdef noise; fw-context shows only
what actually compiles for the active build configuration.

PROHIBITED for .c/.cpp/.h/.hpp/.s/.inc files:
- Read, cat — raw preprocessor noise, not compiled code
- Grep, git grep — cannot resolve build-conditional code
- Bash find/ls for discovering source files

REQUIRED for ALL C/C++ code access:
- lookup_symbol → find symbol by exact or prefix name
- search_code → find symbols by concept/topic
- search_bodies → search the text of any definition (a function body, and
  also a class, struct, union, enum or namespace body)
- search_content → search full file content (the preprocessor, extern "C")
- get_source → read function body (libclang exact extents)
- get_symbol_context → body + callers + callees in one call
- find_callers / find_references → impact analysis
- find_all_callers_recursive → full transitive call tree

SELF-CORRECT: the moment you reach for Read/grep on C/C++ → STOP → use fw-context.
"""

SUBAGENT_INSTRUCTIONS = """
## SUBAGENT INSTRUCTIONS (applies to ALL subagents)

When you are launched as a subagent in a C/C++ project, the fw-context rules
above are MANDATORY for you — no exceptions, no excuses:

- NEVER read C/C++ source files (.c, .cpp, .h, .hpp, .s, .inc) with
  Read, cat, grep, or any non-fw-context tool.
- Raw source files contain unprocessed #ifdef noise — only fw-context shows
  what actually compiles for the active build configuration.
- If fw-context MCP tools are available, they are the ONLY allowed way to
  access C/C++ source code. No fallback to raw file reads.
- If you don't have fw-context tools available, state that limitation and
  stop — do NOT fall back to raw file reads.

The rules in the fw-context section above apply to you exactly as they apply
to the main session. There is no exemption for subagents.
"""

# Marker tags for marked-section injection
MARKER_START = "<!-- fw-context -->"
MARKER_END = "<!-- /fw-context -->"


# ── Instruction target ──────────────────────────────────────────────────────


@dataclass
class InstructionTarget:
    """Where and how to inject fw-context instructions for one tool.

    Attributes:
        path: File path. Supports ``~`` (home) and ``{project}`` (project root).
        method: ``"marked_section"`` wraps content in ``<!-- fw-context -->`` markers;
                ``"separate_file"`` writes the entire file.
        scope: ``"global"`` writes to home dir; ``"project"`` writes relative to
               the project root.
    """
    path: str
    method: str = "marked_section"
    scope: str = "global"

    def resolve(self, project_root: Path | None = None) -> Path:
        """Return the absolute path for this target."""
        p = self.path
        if p.startswith("~"):
            p = os.path.expanduser(p)
        if "{project}" in p:
            if project_root is None:
                raise ValueError(
                    f"InstructionTarget path '{self.path}' contains '{{project}}' "
                    "but no project_root was provided"
                )
            p = p.replace("{project}", str(project_root))
        return Path(p)

    def render_instructions(self) -> str:
        """Build the instruction content for this target.

        For ``marked_section`` method, returns content WITHOUT markers —
        the caller (``_update_marked_section``) adds them.
        For ``separate_file`` method, returns the full file content
        BETWEEN the markers.

        WHY the markers go into a separate file as well, although
        fw-context owns the whole file: ``check_target`` reads a file
        that holds three or more fw-context keywords and no markers as
        content of the user, and ``fw-context init`` then skips it.  A
        separate file that fw-context wrote itself holds every one of
        those keywords, thus each later run of ``init`` skipped the file
        and the instructions in it stayed at the version of the first
        run.  With the markers, ``check_target`` reports
        ``has_marked_section`` and the file gets the new text.

        A file that an earlier version wrote holds no markers.  That one
        run of ``init`` still skips it, and ``--force`` writes it once;
        every run after that finds the markers.
        """
        body = BASE_INSTRUCTIONS + SUBAGENT_INSTRUCTIONS
        if self.method == "separate_file":
            return f"{MARKER_START}\n{body}\n{MARKER_END}\n"
        return body


# ── AI tool definition ──────────────────────────────────────────────────────


@dataclass
class AiTool:
    """A supported AI assistant that can receive fw-context instructions.

    Attributes:
        id: Short machine-friendly name (``"claude-code"``).
        name: Human-readable display name (``"Claude Code"``).
        detection_binaries: CLI binary names to check in PATH.
        detection_dirs: Config directories (supports ``~``) that indicate the
                        tool is installed.
        inherits_from: ID of another tool whose instruction files this tool
                       reads.  When set, no separate injection is needed —
                       the parent tool's instructions cover this one.
        mcp_registration: CLI command for MCP registration (e.g. ``claude mcp add``).
                         ``{bin}`` is replaced with the fw-context-mcp binary path.
                         Mutually exclusive with ``mcp_config_file``.
        mcp_config_file: Path to an MCP client config file for file-based
                         registration (e.g. ``~/.config/opencode/opencode.json``).
                         Mutually exclusive with ``mcp_registration``.
        mcp_config_key: JSON key in the ``mcp`` servers map for file-based
                        registration (e.g. ``"fw-context"``).
        targets: Where to inject fw-context instructions.
    """
    id: str
    name: str
    detection_binaries: list[str] = field(default_factory=list)
    detection_dirs: list[str] = field(default_factory=list)
    inherits_from: str | None = None
    mcp_registration: str | None = None
    mcp_config_file: str | None = None
    mcp_config_key: str | None = None
    targets: list[InstructionTarget] = field(default_factory=list)
    agent_dirs_global: list[str] = field(default_factory=list)
    """Global agent directories for this tool. Supports ``~`` home expansion."""
    agent_dirs_project: list[str] = field(default_factory=list)
    """Project agent directories for this tool. Supports ``{project}`` substitution."""
    agent_file_patterns: list[str] = field(default_factory=lambda: ["*.md"])
    """Glob patterns for agent files in the agent directories."""
    agent_strip_name: bool = False
    """Strip ``name:`` from YAML frontmatter when creating template agents."""
    skill_dirs_global: list[str] = field(default_factory=list)
    """Global skill directories for this tool. Supports ``~`` home expansion."""
    skill_dirs_project: list[str] = field(default_factory=list)
    """Project skill directories for this tool. Supports ``{project}`` substitution."""

    def is_detected(self) -> bool:
        """Return True if the tool appears to be installed."""
        for binary in self.detection_binaries:
            if shutil.which(binary):
                return True
        for d in self.detection_dirs:
            if Path(os.path.expanduser(d)).exists():
                return True
        return False

    def status(self) -> str:
        """One-line status for --list-tools output."""
        if self.inherits_from:
            parent = TOOLS.get(self.inherits_from)
            parent_name = parent.name if parent else self.inherits_from
            if not self.is_detected():
                return f"[MISSING]   {self.name:12s} → inherits from {parent_name}"
            if parent and parent.is_detected():
                return f"[DETECTED]  {self.name:12s} → inherits from {parent_name} ✓"
            return f"[DETECTED]  {self.name:12s} → inherits from {parent_name} [parent NOT DETECTED]"

        if self.is_detected():
            target_paths = [t.path for t in self.targets]
            if target_paths:
                return f"[DETECTED]  {self.name:12s} → {', '.join(target_paths)}"
            return f"[DETECTED]  {self.name:12s} → (no instruction targets)"

        target_paths = [t.path for t in self.targets]
        if target_paths:
            return f"[MISSING]   {self.name:12s} → {', '.join(target_paths)}"
        return f"[MISSING]   {self.name:12s}"


# ── Tool registry ───────────────────────────────────────────────────────────


TOOLS: dict[str, AiTool] = {
    "claude-code": AiTool(
        id="claude-code",
        name="Claude Code",
        detection_binaries=["claude"],
        detection_dirs=["~/.claude"],
        mcp_registration="claude mcp add --scope user fw-context {bin}",
        agent_dirs_global=["~/.claude/agents"],
        agent_dirs_project=["{project}/.claude/agents"],
        agent_file_patterns=["*.md"],
        skill_dirs_global=["~/.claude/skills"],
        skill_dirs_project=["{project}/.claude/skills"],
        targets=[
            InstructionTarget(
                path="~/.claude/CLAUDE.md",
                method="marked_section",
                scope="global",
            ),
            InstructionTarget(
                path="{project}/CLAUDE.md",
                method="marked_section",
                scope="project",
            ),
        ],
    ),
    "opencode": AiTool(
        id="opencode",
        name="OpenCode",
        detection_dirs=["~/.config/opencode"],
        mcp_config_file="~/.config/opencode/opencode.json",
        mcp_config_key="fw-context",
        agent_dirs_global=["~/.config/opencode/agents"],
        agent_dirs_project=["{project}/.opencode/agents"],
        agent_file_patterns=["*.md"],
        agent_strip_name=True,
        skill_dirs_global=["~/.config/opencode/skills"],
        skill_dirs_project=["{project}/.opencode/skills"],
        targets=[
            InstructionTarget(
                path="~/.config/opencode/rules/fw-context.md",
                method="separate_file",
                scope="global",
            ),
            InstructionTarget(
                path="{project}/AGENTS.md",
                method="marked_section",
                scope="project",
            ),
        ],
    ),
    "kilocode": AiTool(
        id="kilocode",
        name="Kilo Code",
        detection_dirs=["~/.kilocode"],
        inherits_from="opencode",
        mcp_config_file="~/.config/kilo/kilo.json",
        mcp_config_key="fw-context",
        agent_dirs_global=["~/.kilo/agents"],
        agent_dirs_project=["{project}/.kilo/agents"],
        agent_file_patterns=["*.md"],
        agent_strip_name=True,
        skill_dirs_global=["~/.kilo/skills"],
        skill_dirs_project=["{project}/.kilo/skills"],
        targets=[
            InstructionTarget(
                path="~/.kilo/rules/fw-context.md",
                method="separate_file",
                scope="global",
            ),
            InstructionTarget(
                path="{project}/AGENTS.md",
                method="marked_section",
                scope="project",
            ),
        ],
    ),
    "codex": AiTool(
        id="codex",
        name="Codex",
        detection_dirs=["~/.codex"],
        mcp_registration="codex mcp add fw-context -- {bin}",
        agent_dirs_global=["~/.codex/agents"],
        agent_dirs_project=["{project}/.codex/agents"],
        agent_file_patterns=["*.toml"],
        agent_strip_name=True,
        skill_dirs_global=["~/.codex/skills"],
        skill_dirs_project=["{project}/.codex/skills"],
        targets=[
            InstructionTarget(
                path="~/.codex/rules/fw-context.md",
                method="separate_file",
                scope="global",
            ),
            InstructionTarget(
                path="{project}/.codex/rules/fw-context.md",
                method="separate_file",
                scope="project",
            ),
        ],
    ),
    "cursor": AiTool(
        id="cursor",
        name="Cursor",
        detection_dirs=["~/.cursor", "~/.config/Cursor"],
        mcp_config_file="~/.cursor/mcp.json",
        mcp_config_key="fw-context",
        agent_dirs_global=["~/.cursor/agents"],
        agent_dirs_project=["{project}/.cursor/agents"],
        agent_file_patterns=["*.mdc"],
        skill_dirs_global=["~/.cursor/skills"],
        skill_dirs_project=["{project}/.cursor/skills"],
        targets=[
            InstructionTarget(
                path="~/.cursor/rules/fw-context.mdc",
                method="separate_file",
                scope="global",
            ),
            InstructionTarget(
                path="{project}/.cursor/rules/fw-context.mdc",
                method="separate_file",
                scope="project",
            ),
        ],
    ),
}


# ── Cross-tool agent directories ─────────────────────────────────────────────

CROSS_TOOL_AGENT_DIRS_GLOBAL = ["~/.agents/agents"]
CROSS_TOOL_AGENT_DIRS_PROJECT = ["{project}/.agents/agents"]

CROSS_TOOL_SKILL_DIRS_GLOBAL = ["~/.agents/skills"]
CROSS_TOOL_SKILL_DIRS_PROJECT = ["{project}/.agents/skills"]


# ── Collision detection ─────────────────────────────────────────────────────


class Collision:
    """Result of checking a target for existing fw-context content."""

    def __init__(self, target: InstructionTarget, resolved_path: Path) -> None:
        self.target = target
        self.path = resolved_path
        self.has_marked_section = False
        self.has_unmarked_content = False
        self.is_skillshare_managed = False

    @property
    def is_clean(self) -> bool:
        """True when the file has no fw-context content at all — safe for fresh injection."""
        return not self.has_marked_section and not self.has_unmarked_content



def check_target(target: InstructionTarget, project_root: Path | None = None) -> Collision:
    """Check a target file for existing fw-context content.

    Detects:
    - Existing ``<!-- fw-context -->`` marked sections (safe to update)
    - Unmarked fw-context content (potential duplicate — warn)
    - skillshare-managed directories (injection may be overwritten)
    """
    resolved = target.resolve(project_root)
    collision = Collision(target, resolved)

    # Check for skillshare management
    parent = resolved.parent
    for _ in range(3):  # Check up to 3 levels up
        manifest = parent / ".skillshare-manifest.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text())
                managed = data.get("managed", {})
                if managed:  # Non-empty managed dict = skillshare active here
                    collision.is_skillshare_managed = True
            except SAFE_EXCEPT as e:
                if is_fatal(e):
                    raise
                pass
            break
        parent = parent.parent

    if not resolved.exists():
        return collision

    try:
        content = resolved.read_text(encoding="utf-8")
    except PermissionError:
        # File exists but is not readable (e.g. root-owned)
        return collision
    except (UnicodeDecodeError, OSError):
        return collision

    # Check for marked section
    if MARKER_START in content and MARKER_END in content:
        collision.has_marked_section = True
        return collision

    # Check for unmarked fw-context content (keyword density)
    fw_keywords = ["fw-context", "lookup_symbol", "search_code", "smart_search",
                   "explain_symbol", "get_active_build"]
    keyword_count = sum(1 for kw in fw_keywords if kw in content)
    if keyword_count >= 3:
        collision.has_unmarked_content = True

    return collision
