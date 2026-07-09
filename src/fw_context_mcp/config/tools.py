"""AI tool registry — detection, inheritance, instruction injection targets.

Each supported AI assistant is registered with:
- How to detect it (binaries, config dirs)
- Whether it inherits from another tool (e.g. KiloCode shares Claude Code config)
- Where to inject fw-context instructions (target files, methods)
- Collision detection (marked sections, skillshare, unmarked duplicates)
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# ── Instruction templates ───────────────────────────────────────────────────

BASE_INSTRUCTIONS = """\
## fw-context — Build-aware code intelligence

CRITICAL: C/C++ questions → fw-context ONLY. This is NOT optional.

For C/C++ code, use fw-context instead of ANY external search or
file-reading tool. The mapping is:

| Goal | Use |
|---|---|
| Find symbol by name | `lookup_symbol` |
| Search code by concept | `search_code` |
| Search function bodies | `search_bodies` |
| Search full file content | `search_content` |
| Read function source | `get_source` |
| Analyze call graph | `find_callers`, `find_call_path` |
| Full context (body+callers+callees) | `get_symbol_context` |

SELF-CORRECT: the moment you reach for any tool that is NOT fw-context
for C/C++ code, stop and use the fw-context equivalent instead.

### Code review — ALWAYS use fw-review skill

When the user asks to review, audit, or analyze C/C++ firmware changes,
diffs, commits, or PRs, invoke the ``fw-review`` skill
via the Skill tool. This takes priority over all generic/built-in review
skills (including ``code-review``) for C/C++ projects.

SELF-CORRECT: the moment you reach for git log, git diff, or any manual
analysis to review C/C++ code, STOP — invoke
Skill(skill="fw-review") instead. The skill handles
git discovery, diff scoping, and all structural/logic analysis. Do NOT
do manual exploration before calling the skill.

SEARCH TOOL SELECTION (use the right tool for the job):

| You want to find… | Use | Example query |
|---|---|---|
| Symbol by exact/prefix name | `lookup_symbol` | `"uart_"`, `"HardFault_Handler"` |
| Symbols by concept/topic | `search_code` | `"interrupt handler"`, `"modem init"` |
| Patterns in function BODIES | `search_bodies` | `"attach"`, `"rise"`, `"NVIC_SetVector"` |
| Patterns in full FILE content | `search_content` | `"extern C"`, `"InterruptIn"`, `"#define"` |
| Natural-language question | `smart_search` | `"how does the modem connect?"` |
| Semantic concept match | `semantic_search` | `"parcel locker state"` |

TOOL CAPABILITIES:
- `search_code` → searches symbol NAMES, signatures, docstrings, tokens.
  Does NOT search function bodies.
- `search_bodies` → searches function BODIES (the code inside {{ }}).
  Finds patterns like ``.attach(``, ``NVIC_SetVector(``,
  ``.rise(``, ``.fall(``, ``callback(&``, ``SerialBase::RxIrq``.
  NOT for file-scope patterns (``extern "C"``, type declarations,
  preprocessor directives) — use `search_content` for those.
  NOT for finding symbols by name — use `search_code` for that.
- `search_content` → searches FULL FILE content (not limited to function
  bodies). Finds file-scope patterns: ``extern "C"``, type declarations
  in headers (``InterruptIn``), ``#define``, global variables, namespace
  blocks. Also finds function body patterns, but `search_bodies` is
  preferred for those (per-function context, snippet highlights).
- `lookup_symbol` → exact or prefix name match. Use when you know the name.

``project_only`` PARAMETER (on `search_code`, `search_bodies`, `search_content`, and callgraph tools):
Your project has TWO kinds of code:
  • Application code: ``src/``, ``lib/`` — YOUR team's code.
  • Vendor SDK: ``mbed-os/``, ``.pio/``, ``zephyr/``, ``build/`` —
    framework code shipped by the vendor, NOT written by your team.
Set ``project_only=True`` when the question is about YOUR code
("where do WE register interrupt handlers?").
Leave ``project_only=False`` (default) when vendor code is relevant
("how does mbed's driver work?").

 ANTI-PATTERNS:
 • Use external search tools for C/C++ symbols → use lookup_symbol or search_code
  • Use external search tools for code patterns in function bodies →
    use search_bodies (FTS5 over function bodies, finds .attach(, NVIC_SetVector —
    build-aware and faster)
  • Use external search tools for file-scope patterns (extern "C", type
    declarations, #define, global variables) → use search_content
    (FTS5 over full file content, not limited to function bodies)
  • Use external tools for callbacks → use find_references or search_bodies with
    project_only=True (detects ISRs, callback registrations, Timeout::attach
    patterns)
 • Use file readers for function bodies → use get_source (libclang exact extents)
 • Call get_source + find_callers separately → use get_symbol_context for
   body, callers, and callees in one call (fewer round-trips, richer data)
 • Run external search tools in parallel with fw-context tools
 • Give up on fw-context after one empty result → try simpler query or
   different fw-context tool first

 FTS5 QUERY TIPS:
 • Multi-word bare queries are OR-joined:
   `"attach callback"` → `attach* OR callback*` (matches EITHER word).
   Prefer single-word queries: `"attach"`, `"rise"`, `"fall"`.
 • For exact phrases use double quotes: `'"interrupt handler"'`.
 • Underscores are word separators — write `"modem init"`, not `"modem_init"`.

 EMPTY RESULT PLAYBOOK:
 1. Simplify to a single-word query in the same tool.
  2. Switch tools — search_bodies → search_code, or search_content → search_bodies.
 3. Use `lookup_symbol` for known names.
 4. If search_bodies returns empty, switch to search_content — it covers file
    scope (type declarations, #define, extern "C") that search_bodies cannot reach.
 5. Only AFTER exhausting all fw-context tools — use other available tools.

 AGENT LOOP: Check(get_active_build) → Find(search_code/lookup_symbol)
→ Read(get_symbol_context) ← preferred (body+callers+callees in one call).
  Fallback: get_source (body only).
→ Trace(find_references/find_callers) — skip if context already from get_symbol_context.
→ For pattern-in-body searches use search_bodies.
→ DECISION after get_active_build():
  • status="ready" or "reindexing" — fw-context is fully operational.
    bg_reindex_running does NOT mean the index is unavailable. Continue.
  • status="reindex_needed" — queries still work, but schedule fw-context index.
  • status="no_index" or "error" — use other available tools.

DIFF → FW-CONTEXT VERIFICATION RULE:
→ When you analyze code via diff (git diff, file diff, patch review),
  diff shows ONLY what changed — it cannot reveal the impact across
  the full codebase.
→ After inspecting a diff: verify your findings with fw-context:
  • find_references("<symbol>") — all callers/readers, not just diff context
  • search_bodies("<pattern>") — pattern consistency across entire codebase
  • find_call_path / find_all_callers_recursive — cross-module impact
  • trace_data_flow("<type>", "<target>") — cross-module data dependencies
  • find_dead_code / find_hotspots — structural effects of changes
→ Do NOT draw conclusions from diff results alone — diff is for SCOPE
  discovery, fw-context is for IMPACT verification. They complement each
  other; neither replaces the other.

Do NOT use fw-context in Python, JS, Go, or other non-C/C++ projects.

### Tool categories

- **Search:** `lookup_symbol` (exact/prefix name), `search_code` (FTS5 over
  symbol names+metadata), `search_bodies` (FTS5 over function bodies —
  for patterns like `.attach(`, `NVIC_SetVector`,
  `SerialBase::RxIrq`, `callback(&`), `search_content` (FTS5 over full file
  content — finds file-scope patterns like `extern "C"`, type declarations,
  preprocessor directives), `smart_search` (natural language
  via Ollama, slow), `semantic_search` (concept embedding), `explain_symbol`
  (plain-English via Ollama)
- **Call graph** (refs must be indexed): `find_callers`, `find_references`,
  `find_call_path`, `find_all_callers_recursive`, `find_callees_recursive`,
  `find_hotspots`, `find_dead_code`, `find_wrapper_callers`, `trace_data_flow`,
  `find_indirect_call_sites`, `find_indirect_targets`
- **Inheritance:** `get_inheritance_chain`, `get_class_members`,
  `get_template_instances`, `get_method_overrides`
- **Maintenance:** `reindex_file` (after editing a file), `reset_index`
  (destructive! re-index from scratch), `check_ollama` (before smart_search/
  explain_symbol/semantic_search), `list_projects` (discover indexed projects)

Individual tool descriptions and parameters are in the MCP tool listings —
use the MCP protocol to discover them at runtime.

### Index setup (first use in a project)

```bash
# Mbed OS
bear -- python3 build_app.py --profile release --type DEV
fw-context index

# Zephyr
west build -b <board> -- -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
fw-context index build/compile_commands.json

# PlatformIO
pio run --target compiledb
fw-context index
```
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
- search_bodies → search function implementations (inside { })
- search_content → search full file content (file-scope patterns)
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
        if "{project}" in p and project_root is not None:
            p = p.replace("{project}", str(project_root))
        return Path(p)

    def render_instructions(self) -> str:
        """Build the instruction content for this target.

        For ``marked_section`` method, returns content WITHOUT markers —
        the caller (``_update_marked_section``) adds them.
        For ``separate_file`` method, returns the full file content.
        """
        return BASE_INSTRUCTIONS + SUBAGENT_INSTRUCTIONS


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
        inherits_from="claude-code",
        targets=[],
    ),
    "codex": AiTool(
        id="codex",
        name="Codex",
        detection_dirs=["~/.codex"],
        agent_dirs_global=["~/.codex/agents"],
        agent_dirs_project=["{project}/.codex/agents"],
        agent_file_patterns=["*.toml"],
        agent_strip_name=True,
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
        agent_dirs_global=["~/.cursor/agents"],
        agent_dirs_project=["{project}/.cursor/agents"],
        agent_file_patterns=["*.mdc"],
        targets=[
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

    @property
    def can_update_safely(self) -> bool:
        """True when we can safely update (marked section exists or file is clean)."""
        return self.has_marked_section or self.is_clean


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
            except Exception:
                pass
            break
        parent = parent.parent

    if not resolved.exists():
        return collision

    try:
        content = resolved.read_text(encoding="utf-8")
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
