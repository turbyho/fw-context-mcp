"""Compute a deterministic config_hash from compile_commands.json.

WHY a deterministic hash: the same build configuration should always produce
the same hash, regardless of timestamps, output paths, or dependency file
names.  This allows comparing two compile_commands.json files to answer
"has the build configuration changed?" without manual inspection.

Same build configuration always produces the same hash, regardless of
timestamps, output paths, or dependency file names.
"""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path

from .compile_commands import _DROP_WITH_ARG, expand_response_file

# Arguments that vary per-build but don't affect compilation semantics
_TRANSIENT_DROP = frozenset({"-MD", "-MP", "-MMD", "-MG"})


def _normalize_entry(entry: dict) -> dict:
    """Normalize a single compile_commands.json entry for stable hashing.

    WHY strip transient flags: ``-MD``, ``-MP``, ``-o``, ``-MF`` vary per
    build but do not affect compilation semantics.  Including them in the
    hash would cause false config_hash changes (and unnecessary full reindexes)
    every build.

    WHY sort arguments: build tools may reorder flags between consecutive
    builds (e.g. ``-I/path -DFOO`` vs ``-DFOO -I/path``).  Sorting ensures
    the same set of flags always produces the same hash.

    Strips the compiler binary, expands response files (@rsp), removes
    transient flags (-MD, -o, -MF, -MT, -MQ and their arguments), drops
    the source file argument (keyed by the "file" field already), and
    sorts remaining arguments.  The result is a deterministic
    ``{file, args}`` dict that produces the same hash for the same
    build configuration regardless of timestamps or output paths.
    """
    raw_args: list[str] = entry.get("arguments") or shlex.split(entry.get("command", ""))

    entry_file = entry.get("file", "")
    source_basename = Path(entry_file).name

    # Drop compiler binary (first non-flag token)
    if raw_args and not raw_args[0].startswith("-"):
        raw_args = raw_args[1:]

    # Expand response files inline so hash is stable across build dirs.
    # A relative @rsp resolves against the entry's OWN directory, not the
    # directory fw-context happens to run from.  Measured on the Mbed project: all
    # 873 entries carry a relative @./BUILD/... response file with 269 -I
    # tokens inside, and expand_response_file returns [] for a file it cannot
    # find, with no error and no log.  flags_hash therefore depended on the
    # process CWD, and the whole build read as changed after a `cd`.
    cwd = Path(entry["directory"]) if entry.get("directory") else None
    expanded: list[str] = []
    for token in raw_args:
        if token.startswith("@"):
            rsp_args = expand_response_file(token, cwd)
            expanded.extend(rsp_args)
        else:
            expanded.append(token)

    result: list[str] = []
    skip_next = False
    for token in expanded:
        if skip_next:
            skip_next = False
            continue
        if token in _DROP_WITH_ARG:
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



def compute_flags_hash(entry: dict) -> str:
    """Return SHA-256 hash of the normalized flags for a single compile_commands entry.

    Used by the runner to detect whether compiler flags changed for a
    specific translation unit — flag changes (e.g. new ``-D`` defines,
    different include paths) invalidate the index for that TU.
    """
    norm = _normalize_entry(entry)
    args = " ".join(norm["args"])
    return hashlib.sha256(args.encode()).hexdigest()


def compute_tu_content_hash(source_hash: str, flags_hash: str, manifest_entry_hash: str) -> str:
    """Return combined SHA-256 of the three per-TU component hashes.

    WHY three components: a translation unit's index validity depends on
    three independent factors:
    1. Source file content (source_hash)
    2. Compiler flags (flags_hash)
    3. Included headers (manifest_entry_hash, replacing old deps_hash)

    Any one of these changing invalidates the TU's indexed symbols.
    This hash is stored in ``files.content_hash`` — when it matches the
    stored hash, the TU can be skipped even if mtime has changed (mtime
    false-positives from git checkout, touch, etc.).

    Tier 1 compares the source file mtime only, so nothing reads this hash
    when the mtime is unchanged.  A change of flags_hash alone therefore
    invalidates nothing through this path — config_hash must hold what a
    toolchain change moves.  See compute_config_hash.

    *manifest_entry_hash* is the hash of the TU's manifest entry
    (source + headers), replacing the old ``deps_hash`` from ``.d`` files.
    """
    return hashlib.sha256(f"{source_hash}|{flags_hash}|{manifest_entry_hash}".encode()).hexdigest()
