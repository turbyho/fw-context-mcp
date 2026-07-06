---
name: fw-context-embedded-review
description: Review embedded C/C++ firmware changes using fw-context MCP tools. Three-phase workflow: (0) create a review plan from the diff, (1) structural verification via callers/callees/types, (2) logic review across 9 embedded-specific domains. Takes priority over generic review skills for C/C++ code. If project has a local copy, that overrides this global default.
---

## Scope

- Review of diffs, commits, or specific changed files in embedded C/C++ projects
- Bugs, behavioral regressions, runtime risks, concurrency, memory, and platform rule violations
- Changes outside firmware code (docs, CI, scripts, plans) — skip fw-context, review manually

---

## CRITICAL — MANDATORY

For C/C++ source code, use ONLY fw-context tools for reading, searching, and browsing files.
MUST NOT use any other tool to read or search C/C++ source. fw-context provides
ifdef-filtered code — only what actually compiles for the active build configuration.
Other tools show unprocessed preprocessor noise including dead `#ifdef` branches.

MUST NOT skip Phase 0. The review plan is the single most effective guard against missed checks.
Every review MUST begin by creating and writing out the plan.

---

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

---

## Prerequisites

- `fw-context index` must have been run on the project
- Call `get_active_build()` first — if status is not "ready" or "reindexing",
  fix the index or note the limitation in the review

---

## Phase 0: Create a review plan (MANDATORY)

BEFORE any detailed analysis, create a review plan from the diff. This prevents the most common
review failure: missing entire categories of checks because analysis jumps straight into per-symbol
inspection.

### Step 1: Get the diff overview

```
git diff --stat <base>..<head>
git log --oneline <base>..<head>
```

Note: total commit count, changed files, lines added/removed, modules touched.

### Step 2: Classify every change

Scan the diff and classify EVERY changed file into one or more categories. Use this table
to determine which fw-context checks are required:

| Category | Trigger | Required fw-context checks |
|---|---|---|
| Changed function/method | Modified `.cpp`/`.c` with function body changes | `lookup_symbol`, `find_callers`, `find_all_callers_recursive`, `find_callees_recursive` |
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

### Step 3: Write the plan

For each changed file and symbol from the diff, use the classification table above to determine
what checks are needed. Write the resulting checklist as your review plan. Every plan item
MUST name a specific fw-context tool and a specific symbol or pattern from the diff.

Before executing: verify the plan covers EVERY changed file from the diff stat. If a changed
file has no corresponding plan items, add them now.

### Step 4: Pre-review prioritization

```
find_hotspots(project_only=True)
get_active_build()
```

Rank functions by caller count. Prioritize review of hotspots (20+ callers) — a logic change
in a hotspot touches dozens of call sites.

### Step 5: Execute and extend

Work through the plan systematically. The plan is a STARTING POINT — as you discover new
symbols, patterns, or risks during review, ADD them to the plan. The final plan in the
review output MUST show ALL items completed, including those added dynamically.

---

## Phase 1: Structural verification

For each symbol identified in the plan, verify impact through the call graph and type system.

### Per function/method

```
1. lookup_symbol(exact=True)
   → Verify symbol existence, signature, parent class.

2. find_callers()
   → Direct callers — who is affected?

3. IF find_callers returns empty:
   → search_bodies("<function_name>")
   → search_content("<function_name>")
   → Also try find_indirect_call_sites and find_indirect_targets — function pointers,
     callbacks, ISR registrations are invisible to find_callers.

4. find_all_callers_recursive()
   → Transitive upstream impact — full call tree.

5. find_callees_recursive()
   → Transitive downstream check — is the new behavior still compatible with
     everything this function depends on?

6. find_references("<SymbolName>")
   → All reads, writes, calls of changed types/globals.

7. For each search_bodies result set: scan ALL results, not just the first few.
```

### Per class/struct

```
1. get_inheritance_chain("<ClassName>", transitive=True)
   → A base class change affects every derived class.

2. get_method_overrides("<ClassName>::<method>")
   → Virtual dispatch impact — what overrides this, what does this override?

3. get_class_members("<ClassName>")
   → New/removed fields, changed methods.

4. find_wrapper_callers("<ClassName>")
   → Adapter/wrapper classes that delegate to this class.

5. get_template_instances("<TemplateClass>")
   → For templates: all concrete instantiations need compatibility check.
```

### Per enum

```
1. find_references("<ENUM_CONSTANT>")
   → Every site using changed or removed enum values.

2. lookup_symbol("<ENUM_NAME>")
   → Verify new values, check for gaps or renumbering.
```

### Per preprocessor change

```
search_content("<PATTERN>")
→ NOT search_bodies — preprocessor directives are file-scope.
→ Confirm removal of #define, #ifdef, board names.
→ Verify no lingering references to removed macros.
```

### Per type change

```
1. find_references("<TypeName>")
   → All allocations, reads, writes of the changed type.

2. search_bodies("<TypeName>")
   → Functions that reference the type in their bodies.
```

### Post-removal cleanup

```
1. find_dead_code(project_only=True)
   → Functions now dead after removals.
   → Verify "possibly_dead" hits with find_indirect_targets.

2. search_content("<REMOVED_SYMBOL>")
   → Confirm zero results project-wide.
```

---

## Phase 2: Logic review — correctness of changed behavior

After structural verification confirms the IMPACT, this phase verifies the CORRECTNESS of the
logic itself. Apply to functions meeting ANY of these criteria:

| Criterion | Why |
|---|---|
| New function >20 lines | No test history, no usage patterns to lean on |
| Changed function with >30% diff | Significant rewrite — old assumptions may be broken |
| Hotspot (>10 callers) | Bug here propagates widely |
| State machine / protocol handler | Complex state transitions, hard to verify by inspection |
| Data migration / persistence code | Power-loss or partial-write risk |
| Encryption / auth / token handling | Security-critical — subtle bugs are catastrophic |
| Memory layout change (sizeof, field order) | Can corrupt persisted data silently |
| Changed algorithm or data flow | Old invariants may no longer hold |

Skip logic review for: simple getters/setters, logging changes, formatting changes, trivial
refactors with no behavioral change, 1-3 line fixes with obvious correctness.

### How to read code in logic review

Use `get_source("<FunctionName>")` or `get_symbol_context("<FunctionName>")` to read the
actual function body. Do NOT rely on the diff alone — the diff shows what changed, not what
the resulting code does. Read every line of the changed function body.

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

For this domain, set `project_only=False` on fw-context tools — vendor code MUST be searched
to verify API contracts.

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

### How to report logic findings

Logic review findings are the highest-severity category. Report each finding as:

```
#### N. <Title> (severity)

- **File:line** — where the issue is
- **Category** — domain name (Memory, Concurrency, State machine, …)
- **Root cause** — why it's a logic bug
- **Runtime impact** — what happens at runtime (crash, data loss, undefined behavior)
- **Evidence** — fw-context get_source / find_callers / search_bodies / diff
- **Fix** — concrete suggestion
```

### Residual risks — explicitly list what was NOT checked

- Async/ISR interactions beyond static analysis
- Hardware-specific behavior (cannot simulate peripheral states)
- Compiler optimizations that change instruction ordering
- Business logic correctness relative to external specifications unless provided

---

## Anti-pattern checklist

| Anti-pattern | Why it's wrong | Correct approach |
|---|---|---|
| Skipping the Phase 0 review plan | Without a plan, entire categories of checks are consistently missed | Always create and write the plan before any analysis |
| Using any tool other than fw-context for C/C++ code | Other tools show unprocessed preprocessor noise, dead `#ifdef` branches | fw-context tools ONLY |
| `search_code("<name>")` for a single known symbol | FTS5 tokenizes names, matching unrelated symbols | `lookup_symbol(exact=True)` |
| Giving up when `find_callers` returns empty | Callers exist through member-field access or base-class pointers | Fall back to `search_bodies` then `search_content` |
| Using `search_bodies` for preprocessor directives | `search_bodies` only searches function bodies `{ }` — preprocessor directives are file-scope | `search_content` |
| Treating diff as the only source of truth | Diff shows what changed, not cross-file impact, duplicates, or dead code | Verify every diff finding with fw-context |
| Using non-fw-context file readers for function bodies | Generic readers don't know where a function ends and show preprocessor noise | `get_source` or `get_symbol_context` |
| Manually reconstructing call chains from the diff | Diff only shows changed lines, not the full call tree | `find_all_callers_recursive` |
| Skipping logic review on large changed functions | Structural checks verify impact, not correctness | Apply Phase 2 logic review domains |
| Looking only at the first few `search_bodies` results | Important matches may be deeper in the list | Scan ALL results |

---

## Tool selection decision tree

```
What are you looking for?

├── A single KNOWN symbol (function, method, class)?
│   → lookup_symbol(exact=True)
│   → NEVER search_code for this.

├── Symbols by concept or keyword (not exact name)?
│   → search_code("<concept words>")
│   → Use 1-3 words, no underscores.

├── Patterns in function IMPLEMENTATIONS (what code DOES)?
│   → search_bodies("<pattern>")
│   → Searches inside { } of function bodies.

├── Patterns at FILE SCOPE (preprocessor, types, globals)?
│   → search_content("<pattern>")
│   → Searches full file content, not limited to function bodies.

├── Natural-language concept (don't know keywords)?
│   → smart_search("<description>")
│   → Slow — uses Ollama.

├── Who calls this function?
│   → find_callers("<name>")
│   → If empty → search_bodies → search_content

├── All references (reads, writes, calls, member access)?
│   → find_references("<name>")

├── Full call tree (transitive impact)?
│   → find_all_callers_recursive("<name>")
│   → find_callees_recursive("<name>")

├── Path between two functions?
│   → find_call_path("<from>", "<to>")

├── Dead code?
│   → find_dead_code(project_only=True)

├── Is this symbol completely gone?
│   → search_content("<SYMBOL>") — should return empty.

└── Read a function body?
    → get_source("<name>") — libclang exact extents, ifdef-filtered.
    → get_symbol_context("<name>") — body + callers + callees in one call.
```

---

## Reporting

### Review structure

Every review MUST follow this structure:

```
## Review: <branch/tag/PR>

**Scope:** X files, Y firmware (rest docs/tooling)

### Review plan

<Phase 0 plan with all items completed. MUST be the FIRST section.>

### Structural analysis

<For each changed firmware file or function:>

- **Change:** what changed (signature, logic, data)
- **Callers** (fw-context): who calls this, how many, any breaking?
- **Callees** (fw-context): what does the changed function call downstream?
- **Data flow:** does the struct/contract change → who reads/writes it?
- **Memory/layout:** does it affect sizeof, persistence, serialization?

### Logic review

<For each function that qualified for Phase 2, a section per domain that found issues:>

- **Domain:** which of the 9 domains
- **Intent** (explain_symbol): what does this function claim to do?
- **Input validation:** are all inputs validated?
- **Return values:** are callee return values checked and propagated?
- **State machine:** are all transitions valid? Stuck-state risk?
- **Resources:** leaks on error paths? Lock held across blocking ops?
- **Edge cases:** empty/max input, overflow, timeout, race condition?
- **Error recovery:** does the system return to a safe state after errors?

### Findings

<Sorted from most to least severe:>

#### N. <Title> (severity: critical / warning / info)

- **File:line** — where the issue is
- **Root cause** — why it's a problem
- **Runtime impact** — what happens (misbehavior, crash, data loss)
- **Evidence** — fw-context find_callers / get_symbol_context / search_bodies / diff
- **Fix** — concrete suggestion

### Assumptions and residual risks

- What the reviewer assumed but could not verify (stale index, lambdas in event queue,
  callback registrations through macros)
- Residual risks after the fix
```

### Finding severity

| Level | Criteria |
|-------|----------|
| critical | Crash, data loss, security breach, bricked device, OTA break |
| warning | Incorrect edge-case behavior, race condition, memory leak, unvalidated input |
| info | Dead code, misleading naming, missing log, minor inefficiency |

### Impact evidence

Every finding involving a function/method/field MUST include fw-context evidence:

- `find_callers` — who is affected
- `find_callees_recursive` — what breaks further in the chain
- `get_symbol_context` — body + immediate surroundings

If fw-context returns "no callers found", mention it as a limitation (lambdas, event queue,
interrupt handlers, base-class pointer dispatch).

### No findings

When the review finds no issues, state explicitly:

```
### Findings — none

### Structural impact summary
- <concise justification for each change, why it's safe>
```
