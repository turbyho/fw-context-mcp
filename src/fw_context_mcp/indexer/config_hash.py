"""Compute a deterministic config_hash from compile_commands.json.

Same build configuration always produces the same hash, regardless of
timestamps, output paths, or dependency file names.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

# Arguments that vary per-build but don't affect compilation semantics
_TRANSIENT_DROP = frozenset({"-MD", "-MP", "-MMD", "-MG"})
_TRANSIENT_DROP_WITH_ARG = frozenset({"-o", "-MF", "-MT", "-MQ"})


def _normalize_entry(entry: dict) -> dict:
    """Normalize a single compile_commands.json entry for stable hashing.

    Strips the compiler binary, expands response files (@rsp), removes
    transient flags (-MD, -o, -MF, -MT, -MQ and their arguments), drops
    the source file argument (keyed by the "file" field already), and
    sorts remaining arguments.  The result is a deterministic
    ``{file, args}`` dict that produces the same hash for the same
    build configuration regardless of timestamps or output paths.
    """
    raw_args: list[str] = entry.get("arguments") or shlex.split(
        entry.get("command", "")
    )

    entry_file = entry.get("file", "")
    source_basename = Path(entry_file).name

    # Drop compiler binary (first non-flag token)
    if raw_args and not raw_args[0].startswith("-"):
        raw_args = raw_args[1:]

    # Expand response files inline so hash is stable across build dirs
    expanded: list[str] = []
    for token in raw_args:
        if token.startswith("@"):
            rsp = Path(token[1:])
            if rsp.exists():
                try:
                    expanded.extend(shlex.split(rsp.read_text()))
                except (OSError, ValueError):
                    pass  # unreadable: skip; hash will differ — intentional
            # If missing: skip; hash will differ — intentional (build dir gone)
        else:
            expanded.append(token)

    result: list[str] = []
    skip_next = False
    for token in expanded:
        if skip_next:
            skip_next = False
            continue
        if token in _TRANSIENT_DROP_WITH_ARG:
            skip_next = True
            continue
        if token in _TRANSIENT_DROP:
            continue
        # Drop source file argument — keyed by "file" field already
        if source_basename and Path(token).name == source_basename:
            continue
        result.append(token)

    # Normalize file path: strip leading ./
    file = entry_file.lstrip("./")

    return {"file": file, "args": sorted(result)}


def compute(compile_commands_path: Path) -> str:
    """Return lowercase hex sha256 of the normalized compile_commands content."""
    entries = json.loads(compile_commands_path.read_text())
    normalized = sorted(
        (_normalize_entry(e) for e in entries),
        key=lambda e: e["file"],
    )
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# Public alias for use by the runner for per-TU flags hashing.
normalize_entry = _normalize_entry


def compute_flags_hash(entry: dict) -> str:
    """Return SHA-256 hash of the normalized flags for a single compile_commands entry.

    Used by the runner to detect whether compiler flags changed for a
    specific translation unit — flag changes (e.g. new ``-D`` defines,
    different include paths) invalidate the index for that TU.
    """
    norm = _normalize_entry(entry)
    args = " ".join(norm["args"])
    return hashlib.sha256(args.encode()).hexdigest()


def compute_tu_content_hash(source_hash: str, flags_hash: str, deps_hash: str) -> str:
    """Return combined SHA-256 of the three per-TU component hashes.

    This is the value stored in ``files.content_hash`` — when it matches
    the stored hash, the TU can be skipped even if mtime has changed.
    """
    return hashlib.sha256(f"{source_hash}|{flags_hash}|{deps_hash}".encode()).hexdigest()
