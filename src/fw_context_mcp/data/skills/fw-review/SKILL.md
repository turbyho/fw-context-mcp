---
name: fw-review
description: Review embedded C/C++ firmware changes using fw-context MCP tools. Three-phase workflow: (0) create a review plan from the diff, (1) structural verification via callers/callees/types, (2) logic review across 9 embedded-specific domains. Takes priority over generic review skills for C/C++ code. If project has a local copy, that overrides this global default.
---

## Scope
- Review of diffs, commits, or specific changed files in embedded C/C++ projects
- Bugs, behavioral regressions, runtime risks, concurrency, memory, and platform rule violations
- Changes outside firmware code (docs, CI, scripts, plans) — skip fw-context, review manually

## CRITICAL — MANDATORY
For C/C++ source code, use ONLY fw-context tools for reading, searching, and browsing files.
MUST NOT use any other tool to read or search C/C++ source. fw-context provides
ifdef-filtered code — only what actually compiles for the active build configuration.
Other tools show unprocessed preprocessor noise including dead `#ifdef` branches.

MUST NOT skip Phase 0. Every review MUST begin by creating and writing out the plan.

## Core principle — verify in full project context
Every code change MUST be verified recursively against the entire project, not just the local diff
fragment. A change that looks correct in isolation can be wrong in the broader context.
For every changed symbol (function, type, macro):

- **Callers** — who calls this? Were they all updated? Use `find_callers` then
  `find_all_callers_recursive` for the full call tree.
- **Callees** — what does this call? Is the new behavior still compatible with downstream
  dependencies? Use `get_symbol_context` or `find_callees_recursive`.
- **Users** — who reads/writes this type or variable? Use `find_references`.
- **Duplicates** — does a similar implementation exist elsewhere? Use `search_bodies` and scan
  ALL results.
- **Removals** — is the removed symbol truly gone from every file? Use `search_content` to
  confirm zero results project-wide.
- **Dead code** — did the removal leave orphaned functions? Use `find_dead_code`.

**The diff shows SCOPE. fw-context verifies CORRECTNESS.**
Never conclude a review from the diff alone.

## Phase 0: Create a review plan (MANDATORY)
BEFORE any detailed analysis, classify EVERY changed file and build a checklist of fw-context
tools to run against each changed symbol. This is the single most effective guard against
missed checks.

### Step 1: Classify every change
Scan the diff and classify EVERY changed file into one or more categories. Use this table
to determine which fw-context checks are required:

| Category | Trigger | Required fw-context checks |
|---|---|---|
| Changed function/method | Modified `.cpp`/`.c` with function body changes | `get_symbol_context` (replaces `lookup_symbol`+`find_callers`+`find_callees_recursive`), `find_all_callers_recursive` |
| Changed class/struct | Modified `.h` class definition | `get_inheritance_chain(transitive=true)`, `get_class_members`, `find_wrapper_callers` |
| Changed template | Template function/class modified | `get_template_instances`, `find_callers` for each instantiation |
| Changed enum | Enum definition changed (added/removed/reordered values) | `find_references` for each removed value, `search_content` for removed value names |
| Changed type/sizeof | Struct layout change, field added/removed | `find_references`, `search_bodies("sizeof.*<TypeName>")` |
| Changed signature | Function signature changed (params, return type) | `find_callers` — all call sites MUST match new signature |
| Changed virtual method | Virtual method signature or behavior changed | `get_method_overrides`, `find_callers` on base-class method |
| New class/function | New `.h`/`.cpp` file or new symbol in existing file | `lookup_symbol(exact=true)`, `get_inheritance_chain`, `find_callers` |
| New serialization/protocol | Serialization, protocol, or persisted storage layout change | `find_references` for affected types |
| Removed file/module | File deleted | `search_content` for every removed symbol, `find_dead_code` |
| Removed macro/`#ifdef` | `#ifdef` or `#define` removed | `search_content` — confirm zero results project-wide |
| Removed enum value | Enum constant deleted | `search_content` — confirm zero results |
| Changed preprocessor | `#if`/`#ifdef`/`#define` logic changed | `search_content` — find all sites affected by the change |

### Step 2: Write the plan
For each changed file and symbol from the diff, use the classification table to determine
required checks. Write the checklist as your review plan. Every item MUST name a specific
fw-context tool and symbol/pattern from the diff.

Before executing: verify the plan covers EVERY changed file from the diff stat. If a changed
file has no corresponding plan items, add them now.

### Step 3: Pre-review prioritization
Run `find_hotspots(project_only=True)`. Prioritize review of hotspots (20+ callers).

### Step 4: Execute and extend
Work through the plan systematically. The plan is a STARTING POINT — add new symbols,
patterns, or risks as you discover them. The final plan MUST show ALL items completed,
including those added dynamically.

## Phase 1: Structural verification
For each symbol, verify impact through the call graph and type system.

**Per function/method:** `get_symbol_context(name)` → body, callers, callees, LLM analysis in one call. Then `find_all_callers_recursive()` for the full upstream tree. If empty, fall back: `search_bodies` + `search_content` + `find_indirect_call_sites` + `find_indirect_targets` (function pointers, callbacks, ISRs invisible to call graph).
**Per class/struct:** `get_inheritance_chain(transitive=True)` → `get_method_overrides` → `get_class_members` → `find_wrapper_callers` → `get_template_instances`.
**Per enum:** `find_references` on each changed/removed constant → `lookup_symbol` to verify new values.
**Per preprocessor change:** `search_content` (NOT `search_bodies` — preprocessor is file-scope).
**Per type change:** `find_references` + `search_bodies` on the type name.
**Post-removal:** `find_dead_code(project_only=True)` → verify `possibly_dead` with `find_indirect_targets` → `search_content` on removed symbols (must return empty).


## Phase 2: Logic review — correctness of changed behavior

Apply to ALL changed C/C++ functions. Use `get_source` or `get_symbol_context` to read
function bodies — do NOT rely on the diff alone.

### Domain 1: Memory and resources
- Buffer boundaries — every `memcpy`/`strcpy`/array access has bounds check? Destination large enough?
- Null dereference — every pointer/array dereference has null/bounds check BEFORE access?
- Allocation/deallocation — every `malloc`/`new` has matching `free`/`delete` on ALL paths (including error paths)?
- Resource ownership — mutex, file handles, sockets: acquired → used → released on all paths? Released in reverse acquisition order?
- Mutex held across blocking ops — lock held during queue receive, sleep, or other blocking call?

### Domain 2: Types and data
- Integer overflow/underflow — arithmetic on potentially large or untrusted values?
- Truncation — wider type assigned to narrower variable?
- Sign extension — implicit signed/unsigned conversions?
- sizeof impact — struct layout change → persisted data, serialized frames, shared memory?
- Serialization/deserialization — endianness, padding, field ordering, versioning?
- Checksum/CRC — verified before and after data transformation?

### Domain 3: Concurrency and interrupts
- Race conditions — shared data accessed from multiple threads/ISRs without protection?
- Volatile correctness — variables accessed from ISR declared `volatile`?
- Atomicity — read-modify-write operations on shared variables protected?
- ISR nesting and priority — does the ISR tolerate being preempted by a higher-priority ISR?
- Critical sections — kept as short as possible? No blocking calls inside?

### Domain 4: Control flow and timing
- State machine transitions — all transitions valid from every state? Can the machine get stuck (no timeout → infinite wait)?
- State reset — do error paths reset state the same as success paths?
- Blocking in time-critical paths — long operations without yield/wait in RTOS context?
- Watchdog starvation — loops with potentially large iteration count: is there a watchdog feed inside?
- Deadline guarantees — if real-time, are measured worst-case paths within limits?

### Domain 5: Initialization and cleanup
- Uninitialized variables — local variables, stack buffers, struct fields?
- Static initialization order — dependencies between static constructors?
- Peripheral init sequence — clock enable → pin configuration → mode set → interrupt enable?
- Cleanup ordering — disconnect/cleanup BEFORE or AFTER data flush/send?
- ODR violations — declaration in `.h` vs definition in `.cpp` match EXACTLY (same type, same size, same qualifiers)?

### Domain 6: Input validation and error handling
- Untrusted input — all external input (network, serial, sensor) validated before use?
- Buffer size validation — before `memcpy`/`strcpy`/message parsing?
- Integer range validation — before use as array index?
- Return value propagation — ALL callee return values checked? Error codes propagated, logged, or explicitly ignored?
- Enum completeness — switch/case on enum handles ALL defined values? No `default: break` on untrusted input?
- Error recovery — after a non-fatal error, does the system return to a known-safe state? Error counters/timers reset on success?

### Domain 7: Peripheral access
- Init sequence — clock → pin mux → configure → enable, correct order?
- Deinit — disable → clear interrupts → release resources?
- Register access — read-modify-write atomicity? Reserved bits preserved? Volatile correctness for memory-mapped registers?
- Interrupt flags — cleared before/after ISR body? Unhandled flags don't cause interrupt loop?
- DMA — buffer alignment, cache coherency, double-buffer switch atomicity?
- Timeouts — every wait on a peripheral flag has a timeout (hardware can fail)?
- Error states — overrun, underrun, framing error handled?
- Shared peripherals — mutex/exclusive access between tasks?
- Low-power transitions — peripheral state restored correctly after sleep/wake?

### Domain 8: Platform/SDK API usage
Set `project_only=False` — vendor code MUST be searched.
- Lifecycle — init → use → deinit pairing? Acquire/release calls matched?
- Calling context — ISR-safe vs thread-only APIs called from correct context? Blocking vs non-blocking variants?
- Memory ownership — who allocates, who frees? SDK conventions for caller-owned vs callee-owned buffers?
- Return values — checked per platform conventions (error codes, negative values)?
- Callback registration — register/unregister paired? Callback parameter lifetime survives async operation?
- Configuration structures — must persist for duration of async operation? Stack-allocated config passed to async API is a bug.
- Deprecated API — using current API versions, not deprecated ones?
- Reentrance — is the API thread-safe? Called from concurrent contexts without protection?
- Dependency ordering — dependent subsystems enabled/disabled in correct order?

### Domain 9: RTOS and multi-threading
- Mutex/semaphore — take/give paired on ALL paths (including error paths)? Recursive vs normal mutex used correctly? Priority inversion risk?
- Deadlock — consistent lock acquisition order across all tasks? Lock held across blocking call (queue receive, delay)?
- Queue/mailbox — send/receive buffer ownership? ISR-safe variants used in ISR? Timeout values: `0` (no wait), `N` (timeout), `INFINITE` (permanent block)?
- Task/thread — stack size sufficient (including worst-case nesting + ISR overhead)? Stack overflow detection enabled? `alloca`/VLA on small-memory systems? Recursion depth bounded?
- Timer — callback context (timer task vs ISR)? One-shot vs periodic correctly handled? Start/stop paired?
- Event flags/signals — wait/set atomic? Race between simultaneous set+wait?
- Memory pools — alloc/free from correct pool? ISR-safe alloc? Fragmentation risk?
- Task notifications — correct API variant for ISR vs task context?
- Thread safety — all shared data protected? ISR→task communication uses correct primitives (not mutex)?
- Priority — lower-priority tasks not starved? Priority inheritance configured? Idle task not blocked?
- Critical sections — minimal duration? No blocking calls inside? Nesting-safe?

## Anti-pattern checklist
| Anti-pattern | Correct approach |
|---|---|
| Skipping the Phase 0 review plan | Always create and write the plan before any analysis |
| Using non-fw-context tools for C/C++ | fw-context tools ONLY |
| Calling `get_source` + `find_callers` separately | Use `get_symbol_context` — body, callers, callees in one call |
| Not checking function pointer assignments | Use `find_indirect_call_sites` / `find_indirect_targets` for callbacks, ISRs, driver registrations |
| `find_callers` returns empty | Fall back: `search_bodies` → `search_content` |
| Using `search_bodies` for preprocessor directives | Use `search_content` — preprocessor directives are file-scope |
| Treating diff as the only source of truth | Verify every diff finding with fw-context |
| Looking only at the first few `search_bodies` results | Scan ALL results |
| Skipping logic review on trivial-looking changes | Apply Phase 2 to ALL changed C/C++ functions |

## Tool selection
| Goal | Tool |
|---|---|
| Known symbol by name | `lookup_symbol(exact=True)` |
| Symbols by concept | `search_code` |
| Patterns in function bodies | `search_bodies` |
| Patterns at file scope | `search_content` |
| Natural language | `smart_search` (slow, uses Ollama) |
| Full context (body+callers+callees) | `get_symbol_context(name)` — preferred over `get_source`+`find_callers` |
| Who calls this? | `find_callers` → if empty: `search_bodies` |
| Function pointer assignments/calls | `find_indirect_call_sites(field)` / `find_indirect_targets(field)` |
| Full call tree | `find_all_callers_recursive` / `find_callees_recursive` |
| All references | `find_references` |
| Cross-module data flow | `trace_data_flow(type, target)` |
| Dead code | `find_dead_code(project_only=True)` |
| Read function body only | `get_source` |
| Confirm symbol gone | `search_content` — should be empty |

## Reporting
### Review structure
Every review MUST follow this structure:

## Review: <branch/tag/PR>
**Scope:** X files, Y firmware

### Review plan
<Phase 0 plan with all items completed. MUST be the FIRST section.>

### Structural analysis
<For each changed firmware file/function: change, callers, callees, data flow, memory/layout>

### Logic review
<For each changed function: domain, intent (explain_symbol), input validation,
return values, state machine, resources, edge cases, error recovery>

### Findings
#### N. <Title> (severity: critical / warning / info)
- **File:line** — where the issue is
- **Root cause** — why it's a problem
- **Runtime impact** — what happens (misbehavior, crash, data loss)
- **Evidence** — fw-context find_callers / get_symbol_context / search_bodies / diff
- **Fix** — concrete suggestion

### Assumptions and residual risks