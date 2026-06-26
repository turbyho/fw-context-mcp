# Your AI Assistant Is Blind to Your Firmware. Here's How to Fix It.

**fw-context gives Claude, OpenCode, Cursor, and other AI assistants a
compiler-accurate understanding of your embedded C/C++ codebase. No cloud. No
guesswork. Just answers that match what actually compiles.**

---

> *"I asked Claude who calls `uart_init`. It read four wrong files, missed the
> callback, and burned 8 000 tokens on a half-right answer. With fw-context, the
> same question took one tool call and 200 tokens — and the answer was
> compiler-verified."*
> — The experience that built this tool

---

## The 10-Second Pitch

Your AI assistant is smart enough to understand your firmware. It just can't
**see** it properly.

Give Claude or OpenCode a Python question, and it reads three files, understands
the imports, and gives you a precise answer. Give it the same question about
your Zephyr project, and it greps through headers it shouldn't read, misses the
callback hidden behind a function pointer, and burns thousands of tokens on the
wrong `#ifdef` branch.

**The model isn't the problem. The information it can access is.**

fw-context is an MCP server that sits between your AI assistant and your
codebase. Instead of the assistant reading source files blindly, it queries a
**compiler-accurate index** built by parsing your project with the exact flags
from `compile_commands.json`. The same `-I` paths. The same `-D` defines. The
same `CONFIG_*` symbols as your actual build.

Your AI goes from grepping text to querying a database of **what actually
compiles**.

[Get started in 60 seconds →](#getting-started-in-60-seconds)

---

## LLM Alone vs LLM + fw-context — The Difference in One Table

Let's be concrete. Here's what happens when you ask real embedded development
questions — first with an LLM alone, then with the same LLM backed by
fw-context.

| Question | LLM Alone | LLM + fw-context |
|----------|-----------|-----------------|
| *"Who calls `spi_transfer`?"* | Grep for `spi_transfer` → 47 matches in vendor HAL, declarations, and comments → manually filter → ~10 min, still uncertain about callbacks | `find_callers("spi_transfer")` → 4 direct callers at exact file:line → 2 seconds, verified against compiled AST |
| *"What would break if I change this signature?"* | Read every caller manually → grep for function name in each file → hope you didn't miss the callback registration → ~30 min, low confidence | `find_all_callers_recursive("spi_transfer", max_depth=5)` → transitive tree of 12 callers across 8 files → 3 seconds, exhaustive |
| *"What does this function do?"* | Read the file → trace includes to understand types → look up macros → read callees manually → ~15 min | `get_symbol_context("boiler_control")` → body + callers + callees + pre-computed plain-English analysis → 1 call, instant |
| *"Is there dead code?"* | Generate function list → grep for each → filter entry points/ISRs/constructors → verify candidates → **days** for a 10k-function codebase | `find_dead_code(project_only=true)` → 3 candidates at exact file:line → 5 seconds, then verify manually |
| *"How does battery data reach the heating logic?"* | Read modbus_poll → read handleData → search for batt_soc references → trace through boiler_control → piece together mentally → ~40 min | `find_references("rt_data")` → 25 references in 5 functions across 4 files → complete data flow map → 2 seconds |
| *"Find all code related to power management"* | Grep `power`, `sleep`, `low_power`, `pm_`, `suspend`, `resume` → 300+ hits, 90% in vendor headers → overload | `semantic_search("power management low power sleep modes")` → top 10 conceptually related symbols ranked by similarity → 1 second |
| *"Give me an overview of this 4 300-line file"* | `Read file` → 4 300 lines → ~12 000 tokens burned on one file → still don't know the structure | `get_file_map("modem_msg.cpp")` → 426 symbols grouped by kind (methods, fields, enums, nested types) → ~800 tokens |
| *"What's the full dependency tree of main()?"* | Read main → find each called function → read each of those → find their callees → exponential explosion of reading → **hours** | `find_callees_recursive("loop", max_depth=3)` → 30 callees at depths 1-3, project + framework → 5 seconds |

### The Token Economics

This isn't just about time. It's about **what questions are even practical to
ask**.

| Metric | LLM Alone | LLM + fw-context |
|--------|-----------|-----------------|
| Tokens to understand one function | 3 000–8 000 (read file + includes) | 200–500 (one `get_symbol_context` call) |
| Tokens to trace callers (3 levels) | 15 000–40 000 (exponential file reading) | 300–800 (one `find_all_callers_recursive` call) |
| Tokens to find all references to a variable | 5 000–15 000 (grep + read each file) | 200–400 (one `find_references` call) |
| Questions per conversation before context fills up | 3–5 | 15–25 |
| **Cost per deep investigation** | **$0.50–$2.00** (burning context on file reads) | **$0.05–$0.20** (structured queries, minimal reads) |

The AI with fw-context doesn't just answer faster — it can answer questions
you'd never ask without it, because the token cost of asking would exhaust your
context window before you got to the interesting part.

---

## Why LLMs Struggle With Embedded C/C++

The problem isn't model capability. Claude, GPT-4, Gemini — they can all read
and understand C code. The problem is that **reading source files is the wrong
way to understand compiled firmware**.

### 1. The LLM reads text. The compiler reads translation units.

When your AI opens `main.c`, it sees 200 lines. When your compiler compiles
`main.c`, it sees 15 000+ lines — every `#include` resolved, every macro
expanded, every `#ifdef` branch collapsed to exactly one path per board.

Your AI answers based on the 200 lines it read. Your board runs the 15 000-line
reality it didn't see.

**Symptom:** AI explains how `gpio_pin_set()` works in the generic HAL. But on
*your* board, with *your* device tree, that function resolves to
`nct38xx_gpio_set()` — a specific driver with timing characteristics that
matter if you're toggling in a tight loop. The AI's answer is correct for the
generic case. It's wrong for your board.

**With fw-context:** `get_source("gpio_pin_set")` returns the body as compiled —
the specific driver your board uses. `explain_symbol("gpio_pin_set")` describes
what actually runs.

### 2. The LLM sees all `#ifdef` branches. Your board runs one.

```c
void uart_send(const uint8_t *data, size_t len) {
#if CONFIG_UART_ASYNC_API
    return uart_async_send(data, len);       // nRF52, STM32H7
#elif defined(CONFIG_UART_INTERRUPT_DRIVEN)
    return uart_int_send(data, len);         // STM32F4, some NXP
#elif defined(SOC_FAMILY_NRF)
    return nrf_uarte_send(data, len);        // nRF91 fallback
#else
    return uart_poll_send(data, len);        // everything else
#endif
}
```

The LLM sees all four implementations as dead text. It can't know which one is
compiled. It might describe the polling implementation because it's at the
bottom, or the async one because it's at the top — either way, it's guessing.

**With fw-context:** libclang applies your `-DCONFIG_UART_ASYNC_API=1` from
`compile_commands.json`. `get_source("uart_send")` returns ONLY the
`uart_async_send` branch — because that's what compiled. No guessing.

### 3. The LLM can't see callbacks, ISRs, or function pointers

```cpp
// This is how embedded code actually connects:
MB.onDataHandler(&handleData);           // Modbus callback
k_work_init(&work, sensor_handler);      // RTOS work queue
IRQ_DIRECT_CONNECT(UART0_IRQn, isr);     // interrupt vector
dev->api->send(data, len);               // driver virtual table

// None of these are visible to grep.
// None appear when the LLM searches for "who calls handleData?"
```

**With fw-context:** `find_references("handleData")` finds the registration at
`modbus_setup:6` — even though it's not a direct call, it IS a reference.
`find_callers("handleData")` returns empty — which is itself the answer: "this
is invoked through a callback mechanism, not a direct call."

The empty result is data, not failure.

### 4. Every LLM question starts from zero

Ask *"who calls `spi_transfer`?"* → LLM greps the codebase.
Then *"what does `spi_transfer` call?"* → LLM greps again. Same headers. Same
includes. Same tokens.

This is like asking a colleague a follow-up question and having them forget the
entire previous conversation. The LLM has no index, so it has no memory of the
codebase structure.

**With fw-context:** The index persists across queries. `search_code` →
`get_symbol_context` → `find_callees_recursive` — each query builds on the
index, not on re-reading files. The SQLite database is the shared memory.

### 5. Vendor code drowns application code

In a typical Zephyr project, 90%+ of compiled code is framework, HAL, and
RTOS. Grep for `gpio_set` and you get 300 hits — 290 in vendor directories.

The LLM reads ESP-IDF's GPIO driver (400 lines) before it finds your one call
in `src/buttons.c:45`. It doesn't know what's "your code" and what's "the
platform" — they're all just files.

**With fw-context:** `find_callers("gpio_set", project_only=true)` returns only
calls from your `src/` and `lib/` directories. `search_code` boosts project
paths (1.2×) over vendor paths (0.85×) in result scoring. Your code surfaces
first.

---

## How fw-context Supercharges Your AI

### The Architecture

```
Your project build (west / pio / bear)
    │
    ▼
compile_commands.json          ← exact compiler flags for every .c file
    │
    ▼
libclang parses every file     ← same -I, -D, --target as your compiler
    │
    ▼
SQLite index on disk           ← symbols + call graph + FTS5 + embeddings
    │                              + pre-computed LLM analysis
    ▼
29 MCP tools                   ← your AI queries the index, not files
    │
    ▼
Your AI assistant              ← Claude, OpenCode, Cursor, Codex, Kilo Code
```

**The key design choice:** The index is built from `compile_commands.json` using
libclang — the same parser that powers clangd and Xcode. It applies your exact
compiler flags. The `#ifdef` branches your board uses are resolved. The macros
your `-D` flags define are expanded. The include paths your `-I` flags specify
are followed.

When your AI asks "show me `uart_init`", it sees what your compiler compiled.
Not the text on disk.

### What the Index Contains

| Layer | Technology | What it stores |
|-------|-----------|---------------|
| **Symbols** | SQLite | name, kind, file, line, signature, docstring for every function/method/class/enum/variable |
| **Full-text** | FTS5 | inverted index over names, qualified names, tokens, summaries — prefix and phrase search |
| **Call graph** | refs table | every call edge: who calls whom, who reads which variable |
| **Vectors** | sqlite-vec | 1024-dimensional embeddings for semantic/concept search |
| **Analysis** | llm_analysis table | pre-computed plain-English explanations of every function |

### The 27 Tools

Your AI assistant gets these capabilities — not by reading files, but by
querying the index:

**🔍 Search & Discovery** — Find anything without grepping
- `search_code` — FTS5 full-text with progressive fallback (5 strategies tried automatically)
- `lookup_symbol` — by name or prefix, definitions prioritized
- `smart_search` — natural language → FTS5 via Ollama (*"how does the modem connect?"*)
- `semantic_search` — find code by concept, not keyword (*"power saving modes"*)

**📖 Code Understanding** — Read code with compiler precision
- `get_source` — exact function body via libclang extent (not line number guesses)
- `get_symbol_context` — body + callers + callees + LLM analysis in ONE call
- `get_file_map` — symbol table of contents (4 300-line file → 426 symbols grouped by kind)
- `explain_symbol` — plain-English description, pre-computed during indexing
- `get_file_analysis` — per-file summary (*"Implements control logic for the boiler system..."*)

**🕸️ Call Graph & Dependencies** — Understand what depends on what
- `find_callers` — direct callers with exact file:line
- `find_all_callers_recursive` — transitive callers (BFS, max_depth 5)
- `find_callees_recursive` — what does this depend on, directly and transitively?
- `find_call_path` — how does A reach B? shortest paths through the call graph
- `find_references` — every read, write, and call of a symbol
- `find_dead_code` — defined but never called (with project/vendor filtering)
- `find_hotspots` — most-called functions ranked by caller count
- `find_wrapper_callers` — which wrapper classes call which driver methods?
- `find_indirect_call_sites` — where is a function pointer field/variable actually invoked?
- `find_indirect_targets` — which functions are assigned to a callback field/param?
- `trace_data_flow` — follow a type through function signatures to a target

**🏗️ C++ Analysis** — Navigate classes and templates
- `get_inheritance_chain` — base classes, derived classes, transitive hierarchy
- `get_class_members` — full API surface grouped by kind
- `get_method_overrides` — virtual dispatch chain
- `get_template_instances` — all concrete instantiations of a template

**🔧 Maintenance** — Keep the index healthy
- `get_active_build` — health check: is the index stale?
- `reindex_file` — update one file (2 seconds after editing)
- `reset_index` — delete and rebuild
- `list_projects` — what's indexed on this machine?
- `check_ollama` — is Ollama available for smart_search?

---

## Real Prompts, Real Results

Here's what actually happens when you use an AI backed by fw-context. These
are real prompts, real tool calls, and real responses from the HA_Boiler
project (ESP32 heating controller, ~700 lines of C++).

### Prompt: "Show me boiler_control and explain how it fits in the system"

**LLM alone:** Reads `control.cpp` (63 lines). Doesn't know who calls it.
Doesn't know what it calls. Needs to search for `boiler_control` references
manually. Burns ~3 000 tokens.

**LLM + fw-context:**
```
→ get_symbol_context("boiler_control")

Returns in ONE call:
  ✓ Function body (42 lines, libclang-exact extent)
  ✓ Callers: [ha_update at ha_mqtt.cpp:109]
  ✓ Callees: [modbus_is_stale at modbus.cpp:99, digitalRead, digitalWrite]
  ✓ LLM Analysis: "Controls the boiler system based on battery state,
    temperature readings, and time. Adjusts the relay state (BOILER2_PIN)
    to control heating based on battery fullness, temperature thresholds,
    and time-based rules. Side effects: updates batFull flag, reads relay
    state, determines new state, toggles BOILER2_PIN, logs to Serial."

Tokens: ~500. All the context the AI needs. One call.
```

### Prompt: "What would break if I change the signature of modbus_poll?"

**LLM alone:** Greps for `modbus_poll` → finds the definition and the call in
`loop()`. Thinks that's everything. Doesn't know about the timing dependencies
or the pending request check. Answer: "Only `loop()` calls it, should be safe."
**Wrong** — changing the timing behavior would affect Modbus polling cadence.

**LLM + fw-context:**
```
→ find_callers("modbus_poll")
  → loop at main.cpp:134

→ get_symbol_context("modbus_poll")
  → Body shows: internal 2s timing (MODBUS_INTERVAL_MS), WiFi check,
    MODBUS_MAX_PENDING guard, two addRequest calls with specific tokens

→ find_references("MODBUS_INTERVAL_MS")
  → Used only in modbus_poll (internal timing)

→ find_references("MODBUS_REG_SOC")
  → Used in modbus_poll (request token) and handleData (response routing)
  → Changing the polling signature could break the token-based response routing

AI's answer: "modbus_poll is called only from loop(). However, changing its
signature would affect: (a) the internal 2-second polling cadence, (b) the
token values used to route Modbus responses in handleData (MODBUS_REG_SOC
and MODBUS_REG_VOLTAGE), (c) the pending request guard. The token constants
are used in both modbus_poll (sender) and handleData (receiver) — they form
a protocol. Changing one side without the other breaks the response routing."
```

Tokens: ~1 200 total across 4 tool calls. Answer: complete and correct.

### Prompt: "Is there dead code in this project?"

**LLM alone:** Has no practical way to answer this. Would need to read every
file, enumerate every function, grep for each function name, filter out false
positives — impossible within a single conversation. Best it can do: "I don't
have a way to exhaustively check for dead code."

**LLM + fw-context:**
```
→ find_dead_code(project_only=true)

Returns: "No dead code found — every defined function has at least one caller."

Tokens: ~150. Answer: exhaustive, compiler-verified.
```

This result is genuine — the HA_Boiler project is 700 lines, well-maintained,
and has no dead functions. That's valuable information. In a larger or older
project, you'd get a list of candidates to investigate.

### Prompt: "Trace how battery SOC data flows from the Modbus register to the boiler relay decision"

**LLM alone:** Reads `modbus.cpp` → reads `control.cpp` → pieces together the
flow manually. Might miss the stale detection (`modbus_is_stale`) because it's
a separate function. Might miss the hysteresis constants in `config.h`.
Answer after ~20 minutes and 15 000 tokens.

**LLM + fw-context:**
```
→ find_references("rt_data")
  → 25 references across 5 functions in 4 files

→ get_symbol_context("handleData")
  → Body + note: no callers (it's a callback)

→ explain_symbol("modbus_is_stale")
  → "Returns true when more than MODBUS_STALE_MS has passed since lastModbusSuccess"

→ get_source("boiler_control")
  → Shows modbus_is_stale() is the FIRST check — stale data → batFull = false

AI's data flow answer:
  1. modbus_poll() sends request for register 266 (SOC) every 2s
  2. eModbus library receives TCP response → calls handleData() callback
  3. handleData() validates range (0–1000), stores to rt_data.batt_soc
  4. boiler_control() reads rt_data.batt_soc (lines 28, 31)
  5. First check: modbus_is_stale() — if >5 min without response → batFull=false
  6. Battery full check: batt_soc >= BATT_SOC_THRESHOLD (80.0%)
  7. Hysteresis: drops below BATT_SOC_THRESHOLD - BATT_SOC_HYSTERESIS (78.0%)
     before batFull transitions to false
  8. batFull determines which temperature sensor and logic branch is used
  9. Final output: digitalWrite(BOILER2_PIN, ...) with 30s minimum toggle guard
```

Tokens: ~2 000. Answer: complete data flow with hysteresis, stale detection,
and relay guard. Every step traceable to specific file:line.

---

## Before and After: A Real Investigation

Here's the same task — understanding an unfamiliar embedded codebase — done
two ways.

### Without fw-context: The Manual Detective

```
08:00  Open main.cpp. Find loop(). See ha_update() called every 5 seconds.
       → Read file: 163 lines, ~800 tokens
08:05  Search for ha_update. Open ha_mqtt.cpp. See it calls boiler_control().
       → Read file: 113 lines, ~600 tokens
08:10  Open control.cpp. Read boiler_control() — 42 lines, 5 conditions.
       → Read file: 63 lines, ~400 tokens
08:15  Search for BATT_VOLTAGE_THRESHOLD — config.h — 5380 = 53.80V.
       → Another file read
08:20  Search for TEMP_SENTINEL — also config.h — 125.0 = disconnected sensor.
08:25  Search for modbus_is_stale — modbus.cpp — returns true after 5 min.
08:30  Read modbus_poll() — sends SOC and voltage requests every 2 seconds.
08:35  Read handleData() — validates and stores Modbus responses.
08:40  Read getTemp() — DS18B20 with RunningMedian filter.
08:50  Try to piece together: sensor → rt_data → boiler_control → relay.
       But who calls handleData? Grep finds nothing. Must be a callback.
       Search for onDataHandler — found in modbus_setup(). The callback
       is registered there. The actual caller is inside eModbus library.
09:10  Finally have a mental model of the data flow.

Total tokens burned: ~15 000 (reading 8 files, multiple searches)
Questions answered: "What does boiler_control do and how does the data flow?"
Time: ~70 minutes (if you're thorough)
Confidence: Moderate — might have missed a reference or a macro
```

### With fw-context: Query the Index

```
get_symbol_context("boiler_control")
→ Body + callers + callees + LLM analysis. One call. 500 tokens.
→ Answers "what does this do and where does it fit?"

find_references("rt_data")
→ 25 references across 5 functions in 4 files. 300 tokens.
→ Complete data flow map.

explain_symbol("modbus_poll")
→ Pre-computed plain-English. 200 tokens.
→ "Polls the Modbus server for battery SOC and voltage..."

find_callers("handleData")
→ Empty — it's a callback. 150 tokens.
→ This IS the answer: callback-based, invoked by Modbus library.

find_call_path("loop", "handleData")
→ No path found. 150 tokens.
→ Confirms the callback boundary.

get_file_analysis("src/control.cpp")
→ "Implements control logic for the boiler system..." 100 tokens.
→ Context for the whole module.

Total tokens burned: ~1 400 (6 tool calls, zero file reads)
Questions answered: Full data flow, architecture, timing, edge cases
Time: ~5 minutes
Confidence: High — every reference is compiler-verified, every edge is from the AST
```

### What Changed

| | LLM Alone | LLM + fw-context |
|---|---|---|
| Files read | 8 (entire files) | 0 (index queries only) |
| Token cost | ~15 000 | ~1 400 |
| Time invested | ~70 minutes | ~5 minutes |
| Callback discovery | Manual grep + luck | `find_callers` returning empty — explicit signal |
| Macro resolution | Manual config.h reading | libclang already resolved them |
| Data flow confidence | Mental model, could miss references | 25 references enumerated, every one accounted for |
| **Questions you can ask** | Limited by token budget | Limited by tool coverage (27 tools, wide) |

---

## For AI Assistant Users: What Changes Day-to-Day

If you use Claude Code, OpenCode, Cursor, or another MCP-capable assistant,
adding fw-context changes what's practical:

### Questions that become practical

- *"Before I refactor this driver, show me every caller, including transitive ones."*
- *"Are there any functions in this codebase that nothing calls anymore?"*
- *"Where exactly is this global variable written to? I need every write site."*
- *"What's the call path from the main loop to the lowest-level UART byte send?"*
- *"Find everything in the project related to battery management — across all files."*
- *"What are the top 10 most-called functions? I want to focus testing there."*
- *"Give me a structural overview of this 5 000-line file before I read it."*

### Questions that become faster

- "What does this function do?" → 1 call, not 1 file read
- "Who calls this?" → 1 call, not a grep + manual filter
- "Where is this constant used?" → 1 call, not a grep across vendor headers
- "How does X reach Y?" → 1 call, not reading every intermediate function

### Questions that become *correct*

The AI with fw-context answers based on what compiles. Not what's in the text
file. The `#ifdef` branch your board uses. The include path your build
specifies. The macro your `-D` flag defines.

When your AI says *"`spi_transfer` is called by `flash_write` at
`drivers/flash.c:142`"*, that's not a grep result. It's a compiler-verified
edge from the AST.

---

## Getting Started in 60 Seconds

### 1. Install

```bash
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
cd ~/.fw-context/src && make install
fw-context init          # registers with Claude Code, OpenCode, Cursor, etc.
```

**Prerequisites:** Python 3.11+, `bear`, `libclang-dev`. Ollama optional.
[Full installation guide →](https://github.com/turbyho/fw-context-mcp/blob/main/docs/installation.md)

### 2. Index Your Project

```bash
cd your-firmware-project
fw-context index
```

Auto-detects Zephyr, PlatformIO, Mbed OS, or wraps any build with `bear`.
First indexing: ~90 seconds for 15 000 symbols. Subsequent: incremental,
seconds.

### 3. Start Asking Real Questions

```
"Show me boiler_control — body, callers, callees, and what it does."

"Find every function that reads or writes the battery voltage."

"If I change spi_transfer's signature, what's the full impact?"

"Is there any dead code in this project? Project code only."

"How does data get from the Modbus register to the relay output?"

"What are the most-called functions? I want to prioritize testing."
```

Your AI now queries the index instead of grepping files.

### 4. Stay Fresh

```bash
fw-context reindex src/control.cpp    # after editing — 2 seconds
fw-context index                       # after a big merge
# Or just work — auto-reindex detects stale files on query
```

### Supported AI Assistants

| Assistant | Registration |
|-----------|-------------|
| **Claude Code** | `~/.claude/mcp.json` |
| **OpenCode** | `~/.config/opencode/mcp.json` |
| **Cursor** | `.cursor/mcp.json` |
| **Codex** | `~/.codex/mcp.json` |
| **Kilo Code** | Inherits from Claude Code |

### Supported Build Systems

| Ecosystem | How |
|-----------|-----|
| **PlatformIO** | `pio run --target compiledb` |
| **Zephyr RTOS** | `west build` (CMake-based, auto) |
| **Mbed OS** | `bear -- mbed compile` |
| **ESP-IDF** | `idf.py build` + bear |
| **Arduino** | Via PlatformIO wrapper |
| **CMake** | `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` |
| **Bare-metal / Make** | `bear -- make` |

---

## Honest Limitations

Every tool has boundaries. Here are fw-context's — clearly stated.

### Callbacks and function pointers

fw-context tracks function pointers end-to-end across three phases:

1. **Registration** — `find_callers` and `find_references` detect function
   pointers in assignments (`driver.onData = &handler`), call arguments
   (`register_cb(&handler)`), variable initializers (`void (*fp)(int) = &handler`),
   and designated struct initializers (`.onReady = &handler`). These appear as
   `"indirect"` call edges.

2. **Invocation** — `find_indirect_call_sites` finds where function pointers are
   actually called, whether through fields (`driver.onData(buf, len)`),
   variables (`stored_callback(42)`), or parameters (`cb(args)`).

3. **Linking** — `find_indirect_targets` connects assignments to call sites
   via the field's USR, answering "which functions can be called through
   `onData`?" with precise, compiler-verified results.

Registration through stored pointers in libraries (e.g. inside eModbus) where
the actual call site is in precompiled code still has no call-graph edge
because the invocation side is not indexed.

### ISRs, RTOS primitives, message queues

Interrupt vectors, `k_work_init()`, `k_msgq_put()/get()` — these connect
functions without appearing in the call graph. Data that flows through a
message queue has no static call path.

### Precompiled libraries

Nordic SoftDevice, TI BLE stack, precompiled WiFi blobs — no source to index.
Calls into them will have missing callee edges.

### Proprietary IDEs without compile_commands.json

IAR, Keil MDK, some Segger configurations — these don't generate
`compile_commands.json` natively. Workaround: wrap the build with `bear`, which
intercepts compiler invocations from any IDE that calls a command-line compiler.

### Macro-heavy code

If macros generate function names via token pasting (X-Macros), the generated
symbols are indexed, but the connection to the macro that produced them is
lost. The expanded form is there; the meta-programming is not.

### Indirect call sites (invocation side)

The invocation side is now covered — `find_indirect_call_sites` detects when
function pointers are called through fields, variables, or parameters, and
`find_indirect_targets` links them to their assignments. Dynamically-indexed
arrays (`handlers[i](args)`), `void*`-erased callbacks, and multi-hop pointer
chains through intermediate variables remain outside the reach of static analysis.

---

## When To Use What

fw-context isn't meant to replace your existing tools. It fills the gap that
none of them cover: **giving AI assistants a compiler-accurate understanding of
your codebase.**

| Tool | Best for | AI-native? |
|------|----------|------------|
| **clangd** | Code completion, go-to-def, inline diagnostics in your editor | ❌ (LSP) |
| **grep/rg** | Quick text searches, finding string literals | ❌ |
| **ctags/cscope** | Legacy index-based navigation in Vim/Emacs | ❌ |
| **Sourcegraph** | Organization-wide code search with server infra | ❌ (REST API) |
| **fw-context** | **AI-assisted code exploration, understanding, and impact analysis** | ✅ (MCP) |

**The workflow:** clangd in your editor for editing. fw-context in your AI
assistant for exploration and understanding. Both use libclang. Both see the
same compiled reality. They serve different moments in the development cycle.

---

## FAQ

### Does this send my firmware to the cloud?

**Never.** Everything runs locally: libclang, SQLite, Ollama (if used). No
telemetry. No API keys. No network required after installation. Your
proprietary firmware never leaves your machine.

### Do I need a GPU or special hardware?

No. Ollama runs on CPU if no GPU is available. For the embedding model
(`mxbai-embed-large`, ~670 MB) and the analysis model (`qwen2.5-coder:14b`,
~9 GB), CPU inference is slower but works fine for indexing (done once) and
occasional smart_search queries.

### Can I use it without Ollama at all?

Yes. All 27 tools work without Ollama. `smart_search` falls back to word-split
FTS5. `semantic_search` falls back to `search_code`. `explain_symbol` returns
source + prompt for your AI to process itself. The index and call graph are
fully functional without any LLM dependency.

### What if my index gets corrupted?

```bash
fw-context reset   # delete
fw-context index   # rebuild — ~90 seconds for 15 000 symbols
```

The index is just a SQLite file. No permanent state.

### Does it work with C++ templates and virtual methods?

Yes. Template instantiations are tracked (`get_template_instances`), virtual
method overrides are mapped (`get_method_overrides`), and inheritance chains
are traversable (`get_inheritance_chain`). All require the reference index
(enabled by default).

### How large a project can it handle?

Tested with projects up to 100 000+ symbols. The reference index has been
verified with 1.2 million reference edges. Initial indexing time scales roughly
linearly with translation unit count. Incremental re-indexing is always fast.

---

## The Bottom Line

Your AI assistant is smart enough to understand your firmware. It
fundamentally cannot do so by reading source files — because source files are
an incomplete, configuration-dependent, macro-obscured shadow of what actually
compiles and runs on your board.

fw-context fixes the information problem. It gives your AI the same view of
your code that your compiler has. From that foundation, everything changes:
answers are precise, conversations are shorter, token costs drop by an order of
magnitude, and questions you'd never ask because they'd exhaust your context
window become practical.

**The model is fine. Give it better data.**

---

## Documentation

| Document | Covers |
|----------|--------|
| **[Technical Overview](docs/README.md)** | Architecture diagrams, directory layout, features, supported ecosystems — the original README |
| **[Installation Guide](docs/installation.md)** | Prerequisites, install, upgrade, Ollama setup, AI assistant integration |
| **[Tools Reference](docs/tools.md)** | All 29 MCP tools, 10 CLI commands, search pipeline internals |
| **[Configuration](docs/configuration.md)** | `.fw-context/config.toml` + `local.toml` — shared project config and local developer overrides |
| **[MCP Server](README-MCP.md)** | JSON-RPC protocol, tool schemas, error handling, debugging |

---

<div align="center">

**[⭐ Star on GitHub](https://github.com/turbyho/fw-context-mcp)**
&nbsp;|&nbsp;
**[📖 Docs](https://github.com/turbyho/fw-context-mcp/blob/main/docs/)**
&nbsp;|&nbsp;
**[🐛 Issues](https://github.com/turbyho/fw-context-mcp/issues)**

**Open source (MIT). No cloud. No daemon. No API keys.**
*Your firmware never leaves your machine.*

</div>
