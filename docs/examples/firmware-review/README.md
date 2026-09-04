# Case Study: Firmware Code Review with fw-context

This is a real-world demonstration of **fw-context**, applied to a
comprehensive code review of an embedded C/C++ firmware project. This case
study shows what fw-context enables, how to use each tool effectively, and
the token savings compared with traditional `grep` and `read` workflows.

## Project Profile

| Attribute | Value |
|-----------|-------|
| **Project type** | Embedded firmware (nRF52, Mbed OS) |
| **Codebase size** | ~67 000 lines of C/C++ (281 indexed files) |
| **Review scope** | 115 changed files, 10 239 insertions, 5 236 deletions |
| **Index size** | 52 646 symbols, 1 335 934 references |
| **Review method** | 8 parallel LLM subagents, each using fw-context |

## Benefits Overview

### Quality — what fw-context made possible

| Finding | Tool | Why traditional tools would miss it |
|---------|------|-------------------------------------|
| Memory leak in BLE RX handler | `get_source` | Exact function extents showed the missing `free()` — `grep` cannot find missing code |
| `error` vs `err` typo | `get_source` + `lookup_symbol` | Proved the variable does not exist in scope — `grep` returns hundreds of false positives |
| Cross-module race condition | `find_callers` on both functions | Traced two call chains to different thread contexts — invisible in single-file reads |
| Dead code after migration | `find_callers` | Zero callers confirmed — `grep` would find the definition and make it look used |
| Missing frame length validation | `get_source` | Absence of bounds check visible in exact function body — `grep` only shows where things **are** |
| Complete BOARD_V1 target removal | `search_content` ×4 | FTS5 index searched 2 562 files in seconds, ifdef-filtered — `grep -r` takes minutes |

Traditional `grep` and `read` workflows could not find **9 of the 19 findings**.

### Token savings — 108× less context

| | fw-context | Traditional (grep + read) |
|---|---|---|
| **Total tokens** | **~54 000** | **~5 818 000** |
| Context window impact | ~27% of a 200K window | **29× over** a 200K window |
| Sessions required | **1** (8 parallel subagents) | **30+** separate conversations |

Without fw-context, the traditional approach would produce **5.8 million
tokens**. The review would be impossible in a single session. The biggest
savings come from tools that have no manual equivalent:
`find_all_callers_recursive` (428× savings), `find_dead_code` (400×),
and `find_references` (320×).

See the full breakdown in [`token-savings.md`](token-savings.md).

---

## What Is Inside

### [`review.md`](review.md) — The Review Output

This is the actual code review deliverable: 19 findings across 10 review
areas, annotated with **💡 fw-context insight** callouts that explain
which tool the reviewer used, and why. See what a deep,
fw-context-powered review looks like in practice.

**Highlights:**
- How `get_source` with libclang exact extents caught a memory leak that
  manual `read` would likely miss (Finding #1)
- How `lookup_symbol` proved that a variable does not exist in
  application scope, something that `grep` cannot do (Finding #2)
- How `find_callers` on two functions from different thread contexts
  revealed a race condition invisible to single-file reading (Finding #8)

### [`methodology.md`](methodology.md) — How It Was Done

This document is the complete methodology behind the review: a tool
selection guide, a per-agent breakdown, the anti-patterns that the review
avoided, and recommendations for getting the most out of fw-context.

**Highlights:**
- **Tool selection matrix** (§2) — when to use `search_bodies` vs
  `search_content` vs `find_callers`
- **Per-agent breakdown** (§3) — exactly which tools each of the 8 subagents
  used and what they found
- **Findings fw-context enabled** (§4) — 9 findings that would be impossible
  with traditional tools
- **Anti-patterns** (§5) — grep-for-callers, cat-for-C++, and other traps
  successfully avoided

### [`token-savings.md`](token-savings.md) — Quantified Results

This document is the quantitative analysis of the token savings: a
per-tool breakdown from 10× (`get_source`) to 428× (`find_all_callers_recursive`),
the hidden savings from false-positive elimination and ifdef-filtered
content, and the bottom-line conclusion: **108× fewer tokens, a 99.1%
effective savings**.

**Highlights:**
- **Detailed breakdown** (§3) — per-tool comparison tables showing exactly
  what fw-context returns vs. what `grep` + `read` would produce
- **Hidden savings** (§6) — false positive elimination, exact libclang
  extents, ifdef-filtered content, cross-agent deduplication
- **Total balance** (§4) — ~54K tokens with fw-context vs. ~5.8M without

## Learning Takeaways

1. **Always start with `get_active_build()`** — verify index health before
   any analysis. A stale index produces wrong results.

2. **Use `find_hotspots` to prioritize** — focus review effort on the most
   heavily-called functions first.

3. **Use `get_symbol_context` instead of `get_source`** —
   `get_symbol_context` returns the body, callers, and callees in one
   call. This saves about 30% of round-trips.

4. **`find_callers` → `find_all_callers_recursive` pipeline** — start with
   direct callers, then go transitive for cross-module impact analysis.

5. **`search_bodies` for implementations, `search_content` for file scope** —
   macros, `#ifdef`, `extern "C"`, and type declarations only exist in
   `search_content`.

6. **fw-context is for C/C++ only** — JSON, YAML, Python, and git operations
   still use standard tools. Do not force fw-context on other languages.

---

*You can find the raw source files for this case study in
[`docs/examples/raw/`](../raw/).*
