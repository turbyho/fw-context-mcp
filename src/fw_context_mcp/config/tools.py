"""AI tool registry — detection, inheritance, instruction injection targets.

Each supported AI assistant is registered with:
- How to detect it (binaries, config dirs)
- Whether it inherits from another tool (e.g. KiloCode shares Claude Code config)
- Where to inject fw-context instructions (target files, methods)
- Collision detection (marked sections, skillshare, unmarked duplicates)
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path


# ── Instruction templates ───────────────────────────────────────────────────

BASE_INSTRUCTIONS = """\
## fw-context — Build-aware code intelligence

`fw-context` MCP tools are available globally. **Use them only in embedded firmware
projects built with Mbed OS, Zephyr, or PlatformIO.** Do not use in Python, JS,
Go, or other projects — the index is built from `compile_commands.json` and only
covers C/C++ translation units.

{lean_ctx_carveout}

### Session start

Call `get_active_build()` first. If `stale: true`, remind the user to run
`fw-context index`.  If the index is missing, show the setup commands below.

### Source code — read function bodies and file structure directly

**Do NOT use Read/grep on source files for symbols that are in the index.**
Instead use:

- `get_source(name)` — full body of a function/method with line numbers.
  Uses libclang's exact extent; faster and more precise than grep+Read.
- `get_file_map(path)` — all symbols in a file grouped by kind (methods,
  variables, classes, …).  Like a table of contents — use BEFORE reading
  a large file to understand its structure.
- `get_symbol_context(name)` — body, signature, and up to 5 direct callers
  + 5 direct callees.  One-shot context for "what does X do and how does
  it fit?"

### Symbol search

- `lookup_symbol(name, exact?)` — find by exact or prefix name.  Prefer
  when you know the identifier.
- `search_code(query, kind?)` — FTS5 full-text search.  Use 1–3 words,
  omit underscores (`modem init`, not `modem_init`).  Filter by kind
  (`function`, `method`, `class`, `struct`, `enum`, …) for precision.
- `smart_search(query)` — natural-language → FTS5 + vector re-rank.
  Use when you don't know the right keywords (\"how does the modem connect?\").
  Slow — 10–30 s with Ollama; call `check_ollama()` first.

### Call graph

All graph tools require `index_refs: true` (the default).  If `find_callers`
returns \"No references indexed\", remind the user to re-index without `--no-refs`.

- `find_callers(name)` — who calls this?  Direct call sites only.
- `find_references(name)` — all uses: calls, reads, member access.
- `find_call_path(from, to, max_depth?)` — shortest paths between two
  functions via BFS.  Returns up to 5 paths with depth and chain.
- `find_all_callers_recursive(name, max_depth?)` — transitive callers,
  deduplicated by shortest distance.
- `find_callees_recursive(name, max_depth?)` — transitive callees.
- `find_hotspots(limit?)` — most-called functions ranked by caller count.
  Useful for impact analysis: changing a hotspot affects many callers.
- `find_dead_code(limit?)` — functions/methods defined but never called.
  Expect false positives (constructors called via factory, interrupt
  handlers, virtual methods).

### Code understanding (Ollama)

- `explain_symbol(name)` — plain-English explanation via local Ollama.
  10–30 s per call.  Falls back to source + prompt when Ollama is off.
- `check_ollama()` — verify Ollama is running and the configured model
  is available.  Call before the first `explain_symbol` or `smart_search`.

### Index maintenance

- `get_active_build()` — index freshness, symbol/file/ref counts, staleness.
- `reindex_file(path)` — re-parse one file after editing.  File must be in
  `compile_commands.json`.  For broader changes, remind the user to run
  `fw-context index`.
- `reset_index(confirm?)` — delete the index.  Always call without `confirm`
  first (dry-run), then with `confirm=True` to actually delete.
- `list_projects()` — show all indexed firmware projects.

### Workflow patterns

1. **Explore a file:** `get_file_map("src/modem.cpp")` → see what's inside
   → `get_source("ModemMsg::send")` to read specific functions.
2. **Trace impact:** `find_all_callers_recursive("uart_write")` → see the
   call chain → `get_source` on the callers that matter.
3. **Understand a function:** `get_symbol_context("modem_connect")` →
   body + direct callers + callees in a single call.
4. **Find by topic:** `search_code("ble advertising", kind="function")`
   → `get_source` on the matches → `find_callers` to see usage.

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

LEAN_CTX_CARVEOUT = """\
### lean-ctx compatibility

fw-context query results (`search_code`, `lookup_symbol`, `explain_symbol`,
`smart_search`, `reindex_file`) return structured C/C++ symbol data. Do NOT
pipe this structured output through lean-ctx compression — display it with
native tools. C/C++ code results must stay uncompressed.

Reading source files referenced by fw-context results IS fine with `ctx_read`
— those are regular files on disk, not query output.

When working on the `fw-context-mcp` source code itself
(`~/dev/sw/work/tools/fw-context-mcp/`), do NOT use lean-ctx for C/C++ code.
"""

NO_LEAN_CTX_CARVEOUT = """\
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
        include_lean_ctx_carveout: If True, include the lean-ctx compatibility
               section. Should be True for tools that also use lean-ctx.
    """
    path: str
    method: str = "marked_section"
    scope: str = "global"
    include_lean_ctx_carveout: bool = True

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
        carveout = LEAN_CTX_CARVEOUT if self.include_lean_ctx_carveout else NO_LEAN_CTX_CARVEOUT
        return BASE_INSTRUCTIONS.format(lean_ctx_carveout=carveout)


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
        mcp_registration: Command to register fw-context as an MCP server.
                          ``{bin}`` is replaced with the fw-context-mcp binary path.
        targets: Where to inject fw-context instructions.
    """
    id: str
    name: str
    detection_binaries: list[str] = field(default_factory=list)
    detection_dirs: list[str] = field(default_factory=list)
    inherits_from: str | None = None
    mcp_registration: str | None = None
    targets: list[InstructionTarget] = field(default_factory=list)

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
        targets=[
            InstructionTarget(
                path="~/.claude/CLAUDE.md",
                method="marked_section",
                scope="global",
                include_lean_ctx_carveout=True,
            ),
        ],
    ),
    "opencode": AiTool(
        id="opencode",
        name="OpenCode",
        detection_dirs=["~/.config/opencode"],
        mcp_registration=None,  # manual: edit opencode.json
        targets=[
            InstructionTarget(
                path="~/.config/opencode/rules/fw-context.md",
                method="separate_file",
                scope="global",
                include_lean_ctx_carveout=True,
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
        targets=[
            InstructionTarget(
                path="~/.codex/rules/fw-context.md",
                method="separate_file",
                scope="global",
                include_lean_ctx_carveout=True,
            ),
        ],
    ),
    "cursor": AiTool(
        id="cursor",
        name="Cursor",
        detection_dirs=["~/.cursor", "~/.config/Cursor"],
        targets=[
            InstructionTarget(
                path="{project}/.cursor/rules/fw-context.mdc",
                method="separate_file",
                scope="project",
                include_lean_ctx_carveout=False,
            ),
        ],
    ),
}


# ── Collision detection ─────────────────────────────────────────────────────


class Collision:
    """Result of checking a target for existing fw-context content."""

    def __init__(self, target: InstructionTarget, resolved_path: Path) -> None:
        self.target = target
        self.path = resolved_path
        self.has_marked_section = False
        self.has_unmarked_content = False
        self.is_skillshare_managed = False
        self.existing_marker_content: str | None = None

    @property
    def is_clean(self) -> bool:
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
                import json
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

    content = resolved.read_text(encoding="utf-8")

    # Check for marked section
    if MARKER_START in content and MARKER_END in content:
        collision.has_marked_section = True
        # Extract existing content between markers for diff display
        try:
            start = content.index(MARKER_START) + len(MARKER_START)
            end = content.index(MARKER_END)
            collision.existing_marker_content = content[start:end].strip()
        except ValueError:
            pass
        return collision

    # Check for unmarked fw-context content (keyword density)
    fw_keywords = ["fw-context", "lookup_symbol", "search_code", "smart_search",
                   "explain_symbol", "get_active_build"]
    keyword_count = sum(1 for kw in fw_keywords if kw in content)
    if keyword_count >= 3:
        collision.has_unmarked_content = True

    return collision
