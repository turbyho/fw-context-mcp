"""Build manifest — deterministic snapshot of compile_commands + header hashes + macros.

Replaces ``.d`` dependency files with a single ``manifest.json`` in the index
directory.  The manifest records every translation unit's source hash, included
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

from fw_context_mcp.utils import compute_source_hash

log = logging.getLogger(__name__)

MANIFEST_FORMAT = "fw-context-manifest/1"

# Build output directories whose headers should be marked as ``generated``.
# Changes to generated headers do NOT trigger a reparse — they are
# rebuild artifacts, not source code the developer edits.
_BUILD_DIR_PATTERNS = ("BUILD/", "build/", ".pio/", "cmake-build-", "_build/")


def _is_generated_header(header_path: str, build_dir_patterns: list[str] | None = None) -> bool:
    """Return True when *header_path* looks like a build-generated file.

    Uses *build_dir_patterns* from the manifest when available (dynamic detection
    per build system), falling back to ``_BUILD_DIR_PATTERNS`` for backward
    compatibility with manifests that predate the ``build_dir_patterns`` field.
    """
    patterns: tuple[str, ...] = (
        tuple(build_dir_patterns) if build_dir_patterns else _BUILD_DIR_PATTERNS
    )
    return any(pat in header_path for pat in patterns)



_HEADER_EXTS = frozenset({".h", ".hpp", ".hxx", ".hh", ".inl"})
# Public alias — used by ops.py for header extension filtering
HEADER_EXTS = _HEADER_EXTS


def _collect_headers_from_tokens(tu, project_root: Path, build_dir_patterns: list[str] | None = None) -> list[dict]:
    """Collect included header paths and their SHA-256 hashes from libclang includes.

    Parses *tu* (a ``CompilationUnit``) with libclang, uses
    ``TranslationUnit.get_includes()`` to enumerate every included file
    (recursively, at all nesting depths), and returns a list of
    ``{path, hash, generated}`` dicts for files with header-like extensions.

    Paths inside *project_root* are stored relative; paths outside (SDK,
    framework) are stored absolute.

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
    except Exception:
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

    Returns:
        The ``config_hash`` — SHA-256 of the structural part of the manifest.
    """
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

    # Compute config_hash from manifest content (without config_hash itself),
    # then save with the hash embedded.
    config_hash = compute_config_hash(manifest)
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
) -> str:
    """Compute the config_hash from structural build identity — no I/O, no libclang.

    Builds the same structural manifest dict that :func:`build_preliminary`
    produces, computes its hash via :func:`compute_config_hash`, but does
    **not** write ``manifest.json`` to disk.  Useful for comparing against a
    stored manifest to detect structural changes without overwriting headers.

    Returns the *config_hash* string.
    """
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

    return compute_config_hash(manifest)


def build_preliminary(
    compile_commands_path: Path,
    db_dir: Path,
    project_root: Path,
    units: list,
    build_dir_patterns: list[str] | None = None,
) -> str:
    """Build a preliminary ``manifest.json`` from structural data only — no libclang.

    Creates a manifest with ``file``, ``directory``, and ``arguments`` for each
    translation unit, leaving ``source_hash`` and ``headers`` empty.  The
    config_hash is computed from these structural fields via
    :func:`compute_config_hash`, so it stays stable when the real manifest
    (with headers) is generated later.

    This is cheap — no file I/O beyond reading ``compile_commands.json``,
    no libclang parsing.

    Returns the *config_hash* string.
    """
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

    config_hash = compute_config_hash(manifest)
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


def compute_config_hash(manifest: dict) -> str:
    """Return the SHA-256 hash of the **structural** part of the manifest.

    Only build-configuration fields are hashed — file, directory, arguments,
    macros, format, project_root, compile_commands_path.  Per-TU content
    fields (source_hash, headers) and build_dir_patterns are EXCLUDED so
    that header/content changes do not trigger a full reindex.

    The manifest's own ``config_hash`` key is also excluded to avoid a
    circular dependency.
    """
    excluded = {"config_hash", "build_dir_patterns"}
    stripped: dict = {}
    for k, v in manifest.items():
        if k in excluded:
            continue
        if k == "entries":
            # Exclude per-TU content fields — only keep structural identity
            stripped[k] = [
                {
                    ek: ev
                    for ek, ev in e.items()
                    if ek not in ("source_hash", "headers")
                }
                for e in v
            ]
        else:
            stripped[k] = v
    canonical = json.dumps(stripped, sort_keys=True, indent=2, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def check_tu_staleness(
    entry: dict,
    project_root: Path,
    source_roots: list[Path],
) -> tuple[bool, str | None]:
    """Check whether a translation unit's source or project headers have changed.

    Compares the stored ``source_hash`` and ``headers[].hash`` from *entry*
    against the current on-disk content.

    Args:
        entry: A single entry from the manifest's ``"entries"`` list.
        project_root: Project root for resolving relative paths.
        source_roots: Directories considered project code — only headers
            inside these roots are checked.  SDK/vendor headers are trusted.

    Returns:
        ``(stale, new_source_hash)`` — *stale* is True when the TU needs
        re-parsing.  *new_source_hash* is the current SHA-256 of the source
        file (for updating the manifest after reparse), or None when stale
        is False.
    """
    # ── Check source file hash ──
    source_file = Path(entry["file"])
    if not source_file.is_absolute():
        source_file = (project_root / entry["file"]).resolve()

    current_source_hash = compute_source_hash(source_file)
    if current_source_hash != entry.get("source_hash", ""):
        return True, current_source_hash

    # ── Check project header hashes ──
    # Only project headers (inside source_roots) are checked — SDK headers
    # are trusted from the manifest.
    source_root_strs = [str(r) for r in source_roots]
    for h in entry.get("headers", []):
        if h.get("generated"):
            continue  # build-generated headers are skipped

        header_path = h["path"]
        # Resolve header path to absolute for reliable source root comparison
        p = Path(header_path)
        if not p.is_absolute():
            p = (project_root / header_path).resolve()
        resolved_str = str(p)

        # Check if this header is inside a source root
        in_project = any(resolved_str.startswith(sr + "/") or resolved_str == sr for sr in source_root_strs)
        if not in_project:
            continue  # SDK/vendor header — trust the manifest

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


def save(manifest: dict, db_dir: Path) -> str:
    """Recompute config_hash, save manifest.json, return the new config_hash."""
    config_hash = compute_config_hash(manifest)
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
    source_roots: list[Path],
    *,
    new_source_hash: str | None = None,
) -> str:
    """Return the manifest entry hash computed from CURRENT disk content.

    When *entry* is stale (headers or source changed), this function reads
    the actual on-disk hashes rather than trusting the stored values.
    Headers outside *source_roots* (SDK/vendor) keep their stored hashes
    — only project headers are re-hashed.

    *new_source_hash* overrides ``entry["source_hash"]`` when the source
    file content has also changed.
    """
    source = new_source_hash if new_source_hash else entry.get("source_hash", "")
    source_root_strs = [str(r) for r in source_roots]

    current_headers: list[dict] = []
    for h in entry.get("headers", []):
        if h.get("generated"):
            current_headers.append(dict(h))
            continue

        header_path = h["path"]
        p = Path(header_path)
        if not p.is_absolute():
            p = (project_root / header_path).resolve()
        resolved_str = str(p)

        # Only re-hash project headers — SDK/vendor headers keep stored hash
        in_project = any(
            resolved_str.startswith(sr + "/") or resolved_str == sr
            for sr in source_root_strs
        )
        if in_project:
            current_hash = compute_source_hash(p)
            current_headers.append({"path": h["path"], "hash": current_hash, "generated": h.get("generated", False)})
        else:
            current_headers.append(dict(h))

    header_hashes = "".join(
        h.get("hash", "")
        for h in sorted(current_headers, key=lambda x: x.get("path", ""))
    )
    return hashlib.sha256(f"{source}|{header_hashes}".encode()).hexdigest()


