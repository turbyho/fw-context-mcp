"""Scoring functions shared across search phases.

Centralised so that search_code, smart_search, and any future search tools
apply the same scoring rules.
"""

from __future__ import annotations

# Bonus for definition-level symbol kinds (functions, methods, classes, etc.)
# vs lightweight kinds (variables, fields, namespaces).
KIND_WEIGHT: dict[str, int] = {
    "function": 2,
    "method": 2,
    "constructor": 2,
    "destructor": 2,
    "class": 2,
    "struct": 2,
    "enum": 2,
    "typedef": 2,
    "enum_constant": 1,
    "namespace": 1,
    "variable": 0,
    "field": 0,
}


def _is_project_local(fpath: str, source_roots: list[str] | None) -> bool:
    """Check whether *fpath* is under one of the project source roots.

    Uses case-sensitive comparison on Linux (correct for the filesystem);
    on case-insensitive filesystems the paths will already match in the
    correct case from ``compile_commands.json``.
    """
    if not source_roots or not fpath:
        return False
    fpath_normalized = fpath.rstrip("/")
    for root in source_roots:
        root_normalized = root.rstrip("/")
        if fpath_normalized.startswith(root_normalized + "/") or fpath_normalized == root_normalized:
            return True
    return False


def score_result(
    row: dict,
    query_stems: list[str],
    source_roots: list[str] | None = None,
) -> int:
    """Score a single result row by how well it matches the query stems.

    Points:
        name / name_tokens match  → +3
        qualified_name match      → +2
        file_path match           → +1
        project-local code        → +1 (under any source_root)
        kind weight               → KIND_WEIGHT bonus

    ``query_stems`` are lowercased query terms with trailing ``*`` stripped
    so ``modem_init*`` becomes ``modem_init``.
    """
    name = (row.get("name") or "").lower()
    ntoks = (row.get("name_tokens") or "").lower()
    qname = (row.get("qualified_name") or "").lower()
    fpath = (row.get("file_path") or "").lower()
    kind = row.get("kind") or ""

    s = 0
    for stem in query_stems:
        if len(stem) < 2:  # skip single-char stems — too noisy
            continue
        if stem in name or stem in ntoks:
            s += 3
        elif stem in qname:
            s += 2
        elif stem in fpath:
            s += 1

    # Bonus for symbols under project source roots.
    # Prefer the indexed is_project flag (set during indexing with the
    # exact source_roots at that time).  Fall back to local path check
    # for older indexes built before is_project was populated — the
    # current config's source_roots may differ, so this is best-effort.
    if row.get("is_project"):
        s += 1
    elif source_roots and _is_project_local(fpath, source_roots):
        s += 1

    s += KIND_WEIGHT.get(kind, 0)
    return s


def stems_from_queries(queries: list[str]) -> list[str]:
    """Extract lowercased stems from query strings (strip trailing wildcards)."""
    return [q.rstrip("*").lower() for q in queries if q]
