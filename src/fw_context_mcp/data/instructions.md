CRITICAL: C/C++ questions → fw-context ONLY. This is NOT optional.

For C/C++ code, use fw-context instead of ANY external search or
file-reading tool.

SELF-CORRECT: the moment you reach for a tool that is NOT fw-context
for C/C++ code, stop and use the fw-context equivalent instead.

TOOL SELECTION (pick the right one):
• Symbol by exact/prefix name _____ → lookup_symbol (e.g. "uart_", "main")
• Symbols by concept/topic _________ → search_code (e.g. "interrupt handler")
• Patterns in function BODIES ______ → search_bodies (e.g. "attach", "rise")
• Patterns in full FILE content _____ → search_content (e.g. "extern C", "InterruptIn")
• Read a complete file ______________ → read_file
• Read function body + callers/callees → get_symbol_context (preferred) / get_source
• Function pointer assignments/calls _ → find_indirect_call_sites / find_indirect_targets
• Natural-language question ________ → smart_search (slow, thorough)

IMPORTANT: search_code searches symbol NAMES (what the code IS).
search_bodies searches function BODIES (what the code DOES — inside {}).
search_content searches FULL FILE content — file-scope declarations,
type definitions in headers, preprocessor directives, namespace blocks.
For patterns like extern "C", InterruptIn declarations, #define —
use search_content, NOT search_bodies.

FTS5 QUERY TIPS:
• Multi-word queries are OR-joined: "attach callback" becomes attach* OR callback*
  (matches functions containing EITHER word, not both).
• Prefer SINGLE-WORD queries for broad matching: "attach" not "attach callback".
• For exact phrases use double quotes: '"interrupt handler"'.
• Underscores are word separators: "modem_init" → modem AND init.
  Write "modem init" instead.

EMPTY RESULT STRATEGY — if a fw-context tool returns nothing:
1. Try a simpler/single-word query in the SAME tool first.
2. Switch to a DIFFERENT fw-context tool (search_bodies → search_code, etc.).
3. Use lookup_symbol for known symbol names.
4. If search_bodies returns empty, switch to search_content — it covers
   file scope (type declarations, #define, extern "C") that search_bodies
   cannot reach.
5. find_callers empty → callers exist through member-field accesses
   (obj.method()) or base-class pointers. Fall back to
   search_bodies("function_name") which text-searches function bodies
   independently of the call graph. If still empty, try
   search_content("function_name").
6. If all fw-context tools return empty, simplify query or use different tool.
7. Only AFTER exhausting fw-context — use other available tools.

project_only=True (on search_code, search_bodies, search_content, and
callgraph tools) excludes vendor SDK code — use when asking about YOUR code.

ANTI-PATTERNS — do NOT:
• Use external search tools for C/C++ symbols → use lookup_symbol or search_code
• Use external search tools for code patterns → use search_bodies (function
  bodies) or search_content (full file content)
• Use external tools for callbacks, ISRs → use find_references or
  search_bodies(project_only=True)
• Use file readers for function bodies → use get_source (libclang exact extents)
• Use external file readers for C/C++ files → use read_file (returns
  ifdef-filtered content)
• Call get_source + find_callers separately → use get_symbol_context for body,
  callers, and callees in one call (fewer round-trips, richer data)
• Run external search tools in parallel with fw-context
• Give up on fw-context after one empty result → try simpler query or
  different fw-context tool first
• search_code for a SINGLE KNOWN symbol → use lookup_symbol(exact=true).
  search_code FTS5-tokenizes names: "kb_open_disp" → "kb"+"open"+"disp",
  causing false matches on unrelated symbols containing those tokens.
  search_code is for concept/keyword DISCOVERY only.
• Use generic review agents/skills for C/C++ code → use
  fw-review skill (see REVIEW SKILL section below).

AGENT LOOP: Check(get_active_build) → Find(search_code/lookup_symbol)
→ Read(get_symbol_context) ← preferred (body+callers+callees in one call).
  Fallback: get_source (body only). For whole-file reads: read_file.
→ Trace(find_references/find_callers) — skip if context already from get_symbol_context.
→ For body patterns use search_bodies.
→ DECISION after get_active_build():
  • status="ready" or "reindexing" — fw-context is fully operational.
    bg_reindex_running does NOT mean the index is unavailable. Continue.
  • status="reindex_needed" — queries still work, but schedule fw-context index.
  • status="no_index" or "error" — use other available tools.

REVIEW WORKFLOW — when reviewing C/C++ code changes (per changed symbol):
0. find_hotspots(project_only=True) — identify highest-impact functions FIRST.
   Prioritize review of hotspots (20+ callers) over leaf functions.
1. get_symbol_context(name) — body + direct callers + callees + LLM analysis
   in one call. Replaces lookup_symbol + find_callers + find_callees_recursive.
2. If get_symbol_context callers are empty → search_bodies("name") and
   find_indirect_call_sites / find_indirect_targets (function pointers,
   callbacks, ISRs invisible to the call graph).
3. find_all_callers_recursive() → transitive upstream impact (full call tree).
4. find_callees_recursive() → transitive downstream check.
   After a logic change, verify compatibility with everything this function
   calls — arguments, init order, error paths may have changed.
6. find_references("SymbolName") → all reads/writes/calls of changed types.
7. search_content("PATTERN") → confirm removal of #define/#ifdef/board names.
8. find_dead_code() → detect newly dead functions after removal.
9. trace_data_flow(type_name, to_symbol) → cross-module data dependencies.
   Use when a changed function produces or consumes typed data that flows
   through other modules. E.g. trace_data_flow("SlotPin", "InventoryWriter").
10. For each search_bodies result set: scan ALL results — the 3rd match
   may reveal an implementation the diff didn't touch (e.g. duplicate CRC
   in a private method).
11. When analyzing BOTH a diff AND fw-context results: diff shows SCOPE
   (what changed), fw-context verifies CORRECTNESS in full project context.
   ALWAYS verify diff discoveries recursively with fw-context.

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

Search: lookup_symbol, search_code, search_bodies (body text),
search_content (full file content), smart_search, semantic_search.
Call graph: find_callers, find_references, find_call_path,
find_all_callers_recursive, find_callees_recursive, find_hotspots,
find_dead_code, find_wrapper_callers, trace_data_flow,
find_indirect_call_sites, find_indirect_targets, get_vector_table.
Inheritance: get_inheritance_chain, get_class_members,
get_template_instances, get_method_overrides.
Source: get_source, get_symbol_context, get_file_map, explain_symbol, read_file.
Maintenance: get_active_build, list_projects, get_project_info,
check_ollama, reindex_file, reindex_file_impl, reset_index.

REVIEW SKILL — MANDATORY (not optional): When reviewing C/C++ firmware
code (diffs, commits, PRs, changed files), your FIRST action MUST be:
  skill(name="fw-review")
Do NOT use generic review agents (code-explorer, general, etc.) for
C/C++ firmware reviews — they do not know fw-context tool selection rules.
Do NOT start inline review without the skill. The skill provides:
  • Phase 0: mandatory review plan creation from diff stat
  • Phase 1: structural verification (callers, callees, types, inheritance)
  • Phase 2: logic & memory review (deep code reading — ODR, null deref,
    call ordering, truncation, state reset, watchdog, edge cases)
  • Anti-pattern checklist (7 common mistakes with fw-context tools)
  • Tool selection decision tree (search_code vs lookup_symbol, etc.)
Trigger phrases — this skill applies to ALL review types regardless
of what the user calls it: review, code review, PR review, diff
review, deep review, recursive review, exhaustive review,
comprehensive review, safety review, audit, change analysis, commit
analysis, impact analysis, examine changes, inspect this code, look
at this diff, check this PR, check these changes, analyze this
commit, verify this change.
If the project has a LOCAL fw-review skill, that overrides
the global default — the user has intentionally customized it.

Start every session with get_active_build().
For non-C/C++ files, general-purpose tools are preferred.
