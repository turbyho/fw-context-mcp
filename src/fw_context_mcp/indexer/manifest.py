"""Build manifest — deterministic snapshot of compile_commands + header hashes + macros.

Replaces ``.d`` dependency files with a single ``manifest.json`` in the index
directory.

WHY a manifest: ``.d`` dependency files are fragile — they depend on build
output paths that change between builds and are scattered across the build
tree.  A centralised manifest with SHA-256 hashes provides uniform,
content-addressable staleness detection: compare stored hashes against
current on-disk content.  This is faster (one JSON read vs. thousands of
``.d`` file reads) and more reliable (no missing or stale ``.d`` files).

The manifest records every translation unit's source hash, included
header hashes (collected from the libclang token stream), and preprocessor
macro definitions (from ``clang -dM -E``).  Staleness detection is uniform:
compare the stored hashes against current on-disk content.

``config_hash`` is ``SHA-256`` of the **structural** part of the manifest
(file, directory, arguments, macros — excluding source_hash, headers, and
build_dir_patterns).  This means a header content change triggers per-TU
reparsing (Tier 2) but NOT a full reindex (config_hash stays the same).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from fw_context_mcp.indexer.config_hash import compute_flags_hash
from fw_context_mcp.utils import compute_source_hash

log = logging.getLogger(__name__)

MANIFEST_FORMAT = "fw-context-manifest/1"

def _is_generated_header(header_path: str, build_dir_patterns: list[str] | None = None) -> bool:
    """Return True when *header_path* looks like a build-generated file.

    Uses *build_dir_patterns* passed from the caller (detected per build
    system).  When no patterns are provided, no header is treated as generated.
    """
    if not build_dir_patterns:
        return False
    return any(pat in header_path for pat in build_dir_patterns)



_HEADER_EXTS = frozenset({".h", ".hpp", ".hxx", ".hh", ".inl"})
# Public alias — used by ops.py for header extension filtering
HEADER_EXTS = _HEADER_EXTS


def _collect_headers_from_tokens(tu, project_root: Path, build_dir_patterns: list[str] | None = None) -> list[dict]:
    """Collect included header paths and their SHA-256 hashes from libclang includes.

    WHY libclang token stream: libclang's ``get_includes()`` returns the
    ACTUAL resolved header set after preprocessing — including files pulled
    in via nested ``#include`` chains, ``-include`` forced headers, and
    platform-conditional ``#ifdef`` branches.  This is more accurate than
    parsing ``.d`` files, which can miss headers from different build configs
    or become stale after incremental builds.

    Paths inside *project_root* are stored relative so the manifest is
    portable across machines.  Paths outside (SDK, framework, system headers)
    are stored absolute.

    *build_dir_patterns* are passed through to ``_is_generated_header``
    so that the builder's actual build-output directories (not just the
    hardcoded fallback set) are used to mark generated headers.
    """
    from clang import cindex as cx

    from fw_context_mcp.indexer.symbols import _get_index

    try:
        translation_unit = _get_index().parse(
            str(tu.file),
            args=tu.clang_args,
            options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
        )
        includes = list(translation_unit.get_includes())
    except (ValueError, TypeError, RuntimeError, AttributeError):
        log.debug("_collect_headers_from_tokens: parse failed for %s", tu.file.name)
        return []

    seen: set[str] = set()
    headers: list[dict] = []

    for inc in includes:
        abs_path = str(inc.include.name)
        if abs_path in seen:
            continue
        seen.add(abs_path)

        resolved = Path(abs_path).resolve()
        # Only collect files with header-like extensions
        if resolved.suffix.lower() not in _HEADER_EXTS:
            continue

        try:
            rel = str(resolved.relative_to(project_root))
        except ValueError:
            rel = str(resolved)  # absolute path for files outside project tree

        h = compute_source_hash(resolved)
        headers.append(
            {
                "path": rel,
                "hash": h,
                "generated": _is_generated_header(rel, build_dir_patterns),
            }
        )

    return headers


def generate(
    compile_commands_path: Path,
    db_dir: Path,
    project_root: Path,
    units: list,
    macros: dict[str, str] | None = None,
    build_dir_patterns: list[str] | None = None,
    config_hash: str = "",
) -> str:
    """Generate ``manifest.json`` from compile_commands + libclang token stream.

    Args:
        compile_commands_path: Path to ``compile_commands.json`` (for recording only).
        db_dir: Index directory where ``manifest.json`` will be written.
        project_root: Project root for relative path resolution.
        units: List of ``CompilationUnit`` objects (from ``parse_compile_commands``).
        macros: Optional pre-computed macro dictionary from ``clang -dM -E``.
        build_dir_patterns: Optional patterns for build-output directories
            (e.g. ``["BUILD/", ".pio/"]``).  Used to mark generated headers.
        config_hash: Optional pre-computed config_hash.  When empty, the hash
            is computed from the manifest dict (backward-compatible fallback).

    Returns:
        The ``config_hash`` — SHA-256 of the structural part of the manifest.
    """
    from fw_context_mcp.config.settings import derive_project_id

    entries: list[dict] = []
    t0 = time.monotonic()

    for unit in units:
        source_file = str(unit.file.resolve())
        try:
            source_rel = str(unit.file.resolve().relative_to(project_root))
        except ValueError:
            source_rel = source_file

        source_hash = compute_source_hash(unit.file.resolve())
        headers = _collect_headers_from_tokens(unit, project_root, build_dir_patterns)

        entry: dict = {
            "file": source_rel,
            "directory": str(unit.directory) if unit.directory else str(project_root),
            "arguments": unit.clang_args,
            "source_hash": source_hash,
            "headers": headers,
            "flags_hash": compute_flags_hash(unit.raw_entry) if unit.raw_entry else "",
        }
        entries.append(entry)

    manifest: dict = {
        "_format": MANIFEST_FORMAT,
        "compile_commands_path": str(compile_commands_path),
        "project_root": str(project_root),
        "entries": entries,
    }
    if macros:
        manifest["macros"] = macros
    if build_dir_patterns:
        manifest["build_dir_patterns"] = build_dir_patterns

    if not config_hash:
        # Backward-compatible fallback — compute hash from manifest dict.
        # New callers should pass config_hash computed from the normalized
        # compile_commands.json instead.
        project_id = derive_project_id(project_root)
        config_hash = compute_config_hash(units, project_root, project_id, build_dir_patterns)

    manifest["config_hash"] = config_hash
    manifest_json = json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False)
    manifest_path = db_dir / "manifest.json"
    manifest_path.write_text(manifest_json, encoding="utf-8")

    elapsed = time.monotonic() - t0
    header_count = sum(len(e.get("headers", [])) for e in entries)
    log.info(
        "manifest.json written: %d TUs, %d headers, config_hash=%s  %s",
        len(entries),
        header_count,
        config_hash[:12],
        f"{elapsed:.1f}s",
    )

    return config_hash


def load(db_dir: Path) -> dict | None:
    """Load ``manifest.json`` from *db_dir*, returning the parsed dict or None."""
    manifest_path = db_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load manifest.json: %s", exc)
        return None


def compute_structural_hash(
    compile_commands_path: Path,
    project_root: Path,
    units: list,
    build_dir_patterns: list[str] | None = None,
    project_id: str = "",
) -> str:
    """Compute the config_hash from structural build identity — no I/O, no libclang.

    Uses the normalized compile_commands.json hashing via
    :func:`compute_config_hash`.  Does **not** write ``manifest.json``
    to disk.  Useful for comparing against a stored manifest to detect
    structural changes.

    Returns the *config_hash* string.
    """
    from fw_context_mcp.config.settings import derive_project_id

    if not project_id:
        project_id = derive_project_id(project_root)
    return compute_config_hash(units, project_root, project_id, build_dir_patterns)


def build_preliminary(
    compile_commands_path: Path,
    db_dir: Path,
    project_root: Path,
    units: list,
    build_dir_patterns: list[str] | None = None,
    project_id: str = "",
) -> str:
    """Build a preliminary ``manifest.json`` from structural data only — no libclang.

    WHY preliminary: full manifest generation (with ``_collect_headers_from_tokens``)
    requires parsing every TU with libclang, which can take minutes on large projects.
    This function captures the structural identity (files, directories, compiler flags)
    immediately so staleness checks (the config_hash comparison) work BEFORE the full
    parse phase.  If the config_hash matches the stored manifest, we know the build
    structure hasn't changed and can proceed to incremental header updates.

    Creates a manifest with ``file``, ``directory``, and ``arguments`` for each
    translation unit, leaving ``source_hash`` and ``headers`` empty.  The
    config_hash is computed from the normalized compile_commands.json via
    :func:`compute_config_hash`, so it stays stable when the real manifest
    (with headers) is generated later.

    This is cheap — no file I/O beyond reading ``compile_commands.json``,
    no libclang parsing.
    """
    from fw_context_mcp.config.settings import derive_project_id

    if not project_id:
        project_id = derive_project_id(project_root)

    config_hash = compute_config_hash(units, project_root, project_id, build_dir_patterns)

    # Build entries for the manifest file
    entries: list[dict] = []
    for unit in units:
        source_file = str(unit.file.resolve())
        try:
            source_rel = str(unit.file.resolve().relative_to(project_root))
        except ValueError:
            source_rel = source_file

        entries.append({
            "file": source_rel,
            "directory": str(unit.directory) if unit.directory else str(project_root),
            "arguments": unit.clang_args,
            "source_hash": "",
            "headers": [],
        })

    manifest: dict = {
        "_format": MANIFEST_FORMAT,
        "compile_commands_path": str(compile_commands_path),
        "project_root": str(project_root),
        "entries": entries,
    }
    if build_dir_patterns:
        manifest["build_dir_patterns"] = build_dir_patterns

    manifest["config_hash"] = config_hash
    manifest_json = json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False)
    db_dir.mkdir(parents=True, exist_ok=True)

    # Don't overwrite an existing manifest that already has full header-hash
    # data just because the structural config_hash changed (e.g. a new
    # -Wno-unused flag was added to compile_commands.json).  The preliminary
    # manifest has empty source_hash/headers — writing it would degrade the
    # on-disk manifest.  _update_manifest_after_index will handle the
    # regeneration when it detects the degraded entries.
    manifest_path = db_dir / "manifest.json"
    existing = load(db_dir)
    if existing is not None:
        existing_entries = existing.get("entries", [])
        if existing_entries and existing_entries[0].get("source_hash"):
            # Existing manifest has real data — keep it, don't overwrite
            log.info(
                "manifest.json (preliminary): skipped — existing manifest has %d TUs with full hashes, "
                "config_hash=%s (was %s)",
                len(existing_entries),
                config_hash[:12],
                existing.get("config_hash", "")[:12],
            )
            return config_hash

    manifest_path.write_text(manifest_json, encoding="utf-8")
    log.info("manifest.json (preliminary): %d TUs, config_hash=%s", len(entries), config_hash[:12])
    return config_hash


def compute_config_hash(
    units: list,
    project_root: Path,
    project_id: str,
    build_dir_patterns: list[str] | None = None,
) -> str:
    """Return SHA-256 of the **normalized** compile_commands.json.

    WHY normalization: raw compile_commands.json is unstable — flag order,
    absolute vs. relative paths, build-output directory names, and transient
    ``-D`` macros (timestamps, build counters) change between builds even
    when compilation semantics are identical.  Hashing the raw file would
    trigger unnecessary full reindexes.  This function normalizes away
    all non-semantic differences.

    Instead of hashing the manifest dict (which created a circular dependency
    and caused unnecessary config_hash churn when flag order changed), this
    function normalizes the raw TU list directly:

    1. Sort entries by *file* — removes TU order dependence.
    2. Sort *arguments* alphabetically per entry — removes flag order dependence.
    3. Normalize paths in path-bearing arguments (``-I``, ``-isystem``, etc.)
       to be relative to *project_root*; paths outside the project are
       resolved through ``Path.resolve()`` for symlink/collapse stability.
    4. Drop arguments pointing into build-output directories
       (detected via *build_dir_patterns* from the build system) so that
       per-build temporary directories don't destabilize the hash.
    5. Drop transient ``-D`` macros that vary per build (timestamps, build
       IDs injected by the build system).

    The canonical JSON is written to
    ``~/.fw-context/index/<project_id>/compile_commands.<hash>.json``
    as a debug artifact — ``diff`` between two versions shows only
    semantic differences, without flag-ordering or path-format noise.

    Returns the *config_hash* hex string.
    """
    # Path arguments whose value should be normalized
    _PATH_PREFIXES = ("-I", "-isystem", "-idirafter", "-iquote", "-include", "-imacros")
    _PATH_EQ_PREFIXES = ("--sysroot=",)

    # Build-output directory patterns from the build system.
    # When no patterns are provided, no arguments are filtered.
    _markers: tuple[str, ...] = tuple(build_dir_patterns) if build_dir_patterns else ()

    # ``-D`` macros that contain per-build transient values (timestamps,
    # build counter, etc.) — their presence destabilizes the config_hash
    # without changing compilation semantics.  Drop them during normalization.
    _TRANSIENT_DEFINES: frozenset[str] = frozenset({
        "MBED_BUILD_TIMESTAMP",  # Mbed OS — Python time.time() float
        "BUILD_TIMESTAMP",       # generic timestamp
        "BUILD_TIME",            # generic build time
        "BUILD_DATE",            # generic build date
        "BUILD_ID",              # CI build number
        "BUILD_NUMBER",          # CI build counter
    })

    def _is_build_output(arg_value: str) -> bool:
        return any(marker in arg_value for marker in _markers)

    def _normalize_path(arg_value: str) -> str:
        """Resolve a path argument value relative to *project_root*.

        Paths inside *project_root* become relative; paths outside are
        resolved via ``Path.resolve()`` for symlink/``..`` collapse.
        """
        p = Path(arg_value)
        if not p.is_absolute():
            p = (project_root / p).resolve()
        else:
            p = p.resolve()
        try:
            return str(p.relative_to(project_root))
        except ValueError:
            return str(p)

    entries: list[dict] = []
    for unit in units:
        # ── Pre-pass: collapse space-separated -D NAME=VALUE → -DNAME=VALUE ──
        # Sorting alphabetically (next step) would separate "-D" from its value
        # token — join them first so the sort is semantically safe.  Also
        # drops transient -D macros in the same pass.
        raw_args = unit.clang_args
        collapsed_args: list[str] = []
        j = 0
        while j < len(raw_args):
            a = raw_args[j]
            if a == "-D" and j + 1 < len(raw_args):
                val = raw_args[j + 1]
                eq_idx = val.find("=")
                name = val[:eq_idx] if eq_idx != -1 else val
                if name in _TRANSIENT_DEFINES:
                    j += 2
                    continue
                collapsed_args.append(f"-D{val}")
                j += 2
            elif a.startswith("-D") and len(a) > 2:
                name_part = a[2:]
                if "=" in name_part:
                    name = name_part.split("=", 1)[0]
                else:
                    name = name_part
                if name in _TRANSIENT_DEFINES:
                    j += 1
                    continue
                collapsed_args.append(a)
                j += 1
            else:
                collapsed_args.append(a)
                j += 1

        # Normalize arguments: sort, then process path-bearing flags
        args = sorted(collapsed_args)
        normalized_args: list[str] = []

        i = 0
        while i < len(args):
            arg = args[i]
            handled = False

            for prefix in _PATH_PREFIXES:
                if arg == prefix and i + 1 < len(args):
                    val = args[i + 1]
                    if not _is_build_output(val):
                        normalized_args.append(prefix)
                        normalized_args.append(_normalize_path(val))
                    i += 2
                    handled = True
                    break
                elif arg.startswith(prefix) and len(arg) > len(prefix):
                    # Concatenated form: -I/path/to/include
                    val = arg[len(prefix):]
                    if not _is_build_output(val):
                        normalized_args.append(prefix + _normalize_path(val))
                    i += 1
                    handled = True
                    break

            if handled:
                continue

            for prefix in _PATH_EQ_PREFIXES:
                if arg.startswith(prefix):
                    val = arg[len(prefix):]
                    if not _is_build_output(val):
                        normalized_args.append(prefix + _normalize_path(val))
                    i += 1
                    handled = True
                    break

            if handled:
                continue

            # Non-path argument — keep as-is
            normalized_args.append(arg)
            i += 1

        try:
            file_rel = str(unit.file.resolve().relative_to(project_root))
        except ValueError:
            file_rel = str(unit.file.resolve())

        entries.append({"file": file_rel, "arguments": normalized_args})

    # Sort entries by file path for deterministic ordering
    entries.sort(key=lambda e: e["file"])

    canonical: dict = {
        "_format": "fw-context-cc/1",
        "project_root": str(project_root),
        "entries": entries,
    }

    canonical_json = json.dumps(canonical, sort_keys=True, indent=2, ensure_ascii=False)
    config_hash = hashlib.sha256(canonical_json.encode()).hexdigest()

    # Write canonical JSON as a debug artifact
    cc_dir = Path.home() / ".fw-context" / "index" / project_id
    cc_dir.mkdir(parents=True, exist_ok=True)
    out_path = cc_dir / f"compile_commands.{config_hash}.json"
    try:
        out_path.write_text(canonical_json, encoding="utf-8")
    except OSError:
        pass  # best-effort — hash is already computed

    return config_hash


def check_tu_staleness(
    entry: dict,
    project_root: Path,
    vendor_patterns: list[str],
) -> tuple[bool, str | None]:
    """Check whether a translation unit's source or project headers have changed.

    WHY per-TU staleness: re-parsing every TU on every index run is wasteful
    when only a few source files changed.  This function enables incremental
    indexing — only TUs whose source or project headers changed are re-parsed.

    Vendor/SDK headers and headers outside *project_root* are trusted from
    the manifest — re-hashing thousands of SDK headers on every run would
    dominate index time.  Only project headers inside *project_root* are
    re-hashed.

    Compares the stored ``source_hash`` and ``headers[].hash`` from *entry*
    against the current on-disk content.

    Returns:
        ``(stale, new_source_hash)`` — *stale* is True when the TU needs
        re-parsing.  *new_source_hash* is the current SHA-256 of the source
        file (for updating the manifest after reparse), or None when stale
        is False.
    """
    from .sdk_detect import _path_matches

    # ── Check source file hash ──
    source_file = Path(entry["file"])
    if not source_file.is_absolute():
        source_file = (project_root / entry["file"]).resolve()

    current_source_hash = compute_source_hash(source_file)
    if current_source_hash != entry.get("source_hash", ""):
        return True, current_source_hash

    # ── Check project header hashes ──
    # Vendor/SDK headers and headers outside project_root are trusted from
    # the manifest.  Only project headers inside project_root are re-hashed.
    for h in entry.get("headers", []):
        if h.get("generated"):
            continue  # build-generated headers are skipped

        header_path = h["path"]
        p = Path(header_path)
        if not p.is_absolute():
            p = (project_root / header_path).resolve()

        # Outside project_root → trust manifest (system/toolchain/external SDK)
        try:
            rel_path = str(p.relative_to(project_root))
        except ValueError:
            continue

        # Matches vendor pattern → trust manifest
        if any(_path_matches(rel_path, pat) for pat in vendor_patterns):
            continue

        current_hash = compute_source_hash(p)
        if current_hash != h.get("hash", ""):
            return True, current_source_hash

    return False, None


def update_entry(
    manifest: dict,
    entry_index: int,
    source_hash: str,
    headers: list[dict],
) -> None:
    """Update a single translation unit's entry in the manifest in-place.

    Called after a TU has been re-parsed — updates ``source_hash`` and
    ``headers`` for the given entry index.
    """
    if entry_index < len(manifest.get("entries", [])):
        manifest["entries"][entry_index]["source_hash"] = source_hash
        manifest["entries"][entry_index]["headers"] = headers


def save(manifest: dict, db_dir: Path, config_hash: str) -> str:
    """Save manifest.json with the given *config_hash*, return it.

    The caller is responsible for computing *config_hash* — save() no
    longer calls ``compute_config_hash()`` internally.  This removes the
    circular dependency between manifest content and config_hash.
    """
    manifest["config_hash"] = config_hash
    manifest_json = json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False)
    (db_dir / "manifest.json").write_text(manifest_json, encoding="utf-8")
    return config_hash


def get_manifest_entry_hash(entry: dict) -> str:
    """Return SHA-256 hash of a single manifest entry (source + headers).

    Used as the per-TU staleness hash (replaces the old deps_hash-based
    ``content_hash`` for Tier 2 checks).
    """
    header_hashes = "".join(
        h.get("hash", "")
        for h in sorted(
            entry.get("headers", []),
            key=lambda x: x.get("path", ""),
        )
    )
    source = entry.get("source_hash", "")
    return hashlib.sha256(f"{source}|{header_hashes}".encode()).hexdigest()


def compute_current_entry_hash(
    entry: dict,
    project_root: Path,
    vendor_patterns: list[str],
    *,
    new_source_hash: str | None = None,
) -> str:
    """Return the manifest entry hash computed from CURRENT disk content.

    When *entry* is stale (headers or source changed), this function reads
    the actual on-disk hashes rather than trusting the stored values.
    Headers matching *vendor_patterns* or outside *project_root* keep their
    stored hashes — only project headers are re-hashed.

    *new_source_hash* overrides ``entry["source_hash"]`` when the source
    file content has also changed.
    """
    from .sdk_detect import _path_matches

    source = new_source_hash if new_source_hash else entry.get("source_hash", "")

    current_headers: list[dict] = []
    for h in entry.get("headers", []):
        if h.get("generated"):
            current_headers.append(dict(h))
            continue

        header_path = h["path"]
        p = Path(header_path)
        if not p.is_absolute():
            p = (project_root / header_path).resolve()

        # Outside project_root → keep stored hash
        try:
            rel_path = str(p.relative_to(project_root))
        except ValueError:
            current_headers.append(dict(h))
            continue

        # Matches vendor pattern → keep stored hash
        if any(_path_matches(rel_path, pat) for pat in vendor_patterns):
            current_headers.append(dict(h))
        else:
            current_hash = compute_source_hash(p)
            current_headers.append({"path": h["path"], "hash": current_hash, "generated": h.get("generated", False)})

    header_hashes = "".join(
        h.get("hash", "")
        for h in sorted(current_headers, key=lambda x: x.get("path", ""))
    )
    return hashlib.sha256(f"{source}|{header_hashes}".encode()).hexdigest()


