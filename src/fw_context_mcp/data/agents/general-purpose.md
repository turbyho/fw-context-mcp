---
name: general-purpose
description: General-purpose agent for complex tasks, research, and multi-step operations. Uses fw-context MCP tools exclusively for all C/C++ source access.
mode: subagent
---

<!-- fw-context -->
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
<!-- /fw-context -->
