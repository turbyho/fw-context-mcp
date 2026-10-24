# Call graph fixtures with a known truth

Each directory is one case: the source files and `case.json`. The test
`tests/test_callgraph_truth.py` indexes the case as a project, with the full
pipeline (skip set of headers, post-processing), and compares the call edges
with the truth.

`case.json`:

| Key | Meaning |
|---|---|
| `description` | What the case shows |
| `units` | The translation units of `compile_commands.json` (the runner sorts them by path) |
| `compiler` | `cc` or `c++` |
| `flags` | Extra compiler flags (optional) |
| `present` | Edges that must be in the index: `[caller, callee, line, ref_kind]` |
| `absent` | Edges that must not be in the index |
| `exact` | When true, the edges between symbols of the case equal `present` (as a multiset) |
| `xfail` | Optional. The phase of the implementation plan that repairs the case. The test is then `xfail(strict=True)`: the commit of that phase removes the key |

A caller is the qualified name of the enclosing function, or `null` when the
index records no caller. Only the `call`, `indirect` and `implicit_construct`
edges (the kinds that `find_callers` answers with) between symbols of the case
are compared. A symbol of the case is one that the index stores with a file of
the case; a call into the C library is not part of the truth.
