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

from fw_context_mcp.indexer.compile_commands import _DROP_WITH_ARG
from fw_context_mcp.indexer.config_hash import _TRANSIENT_DROP, compute_flags_hash
from fw_context_mcp.utils import compute_source_hash

log = logging.getLogger(__name__)

# Bumped from /1, which repeated every header's hash and every TU's argument
# list inside each entry.  On zbox that was 86 686 header records for 1 037
# distinct files and 876 argument lists for 2 distinct ones — 52.45 MB, of
# which 74% was duplication.  /2 keeps one ``headers`` map and one
# ``arg_sets`` table and has each entry reference them.
MANIFEST_FORMAT = "fw-context-manifest/2"

def resolve_headers(entry: dict, header_table: dict[str, dict] | None) -> list[dict]:
    """Resolve one entry's header paths into ``{path, hash, generated}`` dicts.

    This is the only place that knows how the ``headers`` map and the per-TU
    path list fit together.  Everything downstream keeps working with the
    record shape it always had.

    A path the map does not cover gets an empty hash, which every staleness
    check reads as "changed".  The alternative — dropping it — would take the
    file out of the coverage set and let the purge delete rows for a header
    that is still included.  Erring towards one extra re-parse is cheap;
    erring towards deletion is not.

    Takes the table rather than the manifest so callers that carry the table
    alone — the indexing loop passes it beside its ``{path: entry}`` lookup —
    do not have to fake a manifest dict around it.
    """
    table = header_table or {}
    resolved: list[dict] = []
    for path in entry.get("headers") or ():
        record = table.get(path)
        if record is None:
            resolved.append({"path": path, "hash": "", "generated": False})
        else:
            resolved.append({"path": path, **record})
    return resolved


def tu_headers(manifest: dict, entry: dict) -> list[dict]:
    """Resolve one entry's headers against *manifest*'s shared table."""
    return resolve_headers(entry, manifest.get("headers"))


def fold_headers(
    headers: list[dict] | list[str],
    header_table: dict[str, dict],
) -> list[str]:
    """Fold header records into *header_table*, return the entry's path list.

    The indexing loop collects ``{path, hash, generated}`` records per TU,
    because that is what a single parse naturally produces.  The manifest
    stores one record per distinct path.  This is the one place that converts
    between the two.

    Accepts a list of plain path strings unchanged, so an entry reused from a
    previous manifest passes through without a special case at the call site.
    A record whose path is already in the table wins over the stored one: it
    was read during this run and is the newer of the two.
    """
    paths: list[str] = []
    for item in headers:
        if isinstance(item, str):
            paths.append(item)
            continue
        path = item["path"]
        paths.append(path)
        header_table[path] = {
            "hash": item.get("hash", ""),
            "generated": item.get("generated", False),
        }
    return paths


def tu_arguments(manifest: dict, entry: dict) -> list[str]:
    """Resolve one entry's compiler arguments from the shared ``arg_sets``.

    Returns an empty list when the index is out of range, which can only
    happen for a hand-edited manifest — the writer and the reader are the same
    module.
    """
    arg_sets = manifest.get("arg_sets") or []
    index = entry.get("arg_set")
    if isinstance(index, int) and 0 <= index < len(arg_sets):
        return list(arg_sets[index])
    return []


def _intern_arguments(arguments: list[str], arg_sets: list[list[str]]) -> int:
    """Return the index of *arguments* in *arg_sets*, appending it if new.

    Linear search is deliberate: a project has a handful of distinct argument
    lists (2 on zbox, 14 on HA_Boiler), so the scan is shorter than the cost
    of hashing a 410-token list to key a dict.
    """
    for index, existing in enumerate(arg_sets):
        if existing == arguments:
            return index
    arg_sets.append(arguments)
    return len(arg_sets) - 1


def derive_extension_sets(
    compile_commands_path: Path, header_table
) -> tuple[list[str], list[str]]:
    """Return ``(tu_extensions, header_extensions)`` of THIS project.

    The compiler rules in ``utils`` say which suffix means C or C++.  This
    says something else: which suffixes this particular build actually
    touches.  No list can answer that in advance — measured across the test
    projects, five of seven compile ``.S`` units, and their headers include
    ``.tcc`` and extension-less libstdc++ ones that no hand-written set had.

    Taken from the manifest itself, so it cannot drift from what was
    indexed.  Stored rather than derived on read, because a reader on the
    query path must not parse a 52 MB manifest to learn two short lists —
    see ``load_build_dir_patterns`` for the same reasoning.

    An empty suffix is kept.  ``<string>`` and ``<vector>`` have none, and
    dropping them would make the watcher blind to a header the project
    really includes.

    The units come from compile_commands.json and NOT from the manifest
    entries, which is the whole point: by the time entries exist the runner
    has already dropped every unit libclang cannot read as C or C++.
    Deriving from them would say a project never compiles assembly, when
    the truth is that its assembly is skipped — and the new-file scan would
    then stay blind to exactly the files that need reporting.

    Reading the file again costs a plain json.load of a few hundred
    kilobytes, once per index run.  It is not on the query path.
    """
    try:
        raw = json.loads(compile_commands_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = []
    tu = {
        Path(e["file"]).suffix
        for e in raw
        if isinstance(e, dict) and e.get("file")
    }
    headers = {Path(h).suffix for h in (header_table or ())}
    return sorted(tu), sorted(headers)


def _is_generated_header(header_path: str, build_dir_patterns: list[str] | None = None) -> bool:
    """Return True when *header_path* looks like a build-generated file.

    Uses *build_dir_patterns* passed from the caller (detected per build
    system).  When no patterns are provided, no header is treated as generated.
    """
    if not build_dir_patterns:
        return False
    return any(pat in header_path for pat in build_dir_patterns)





def _collect_headers_from_tokens(
    tu,
    project_root: Path,
    build_dir_patterns: list[str] | None = None,
    header_table: dict[str, dict] | None = None,
) -> list[str]:
    """Collect the paths of the headers this TU includes, hashing each once.

    Returns the paths as stored in the manifest.  The hash and the
    ``generated`` flag go into *header_table*, keyed by path — a file included
    by 300 TUs is hashed on the first TU that reaches it and looked up by the
    other 299.  On zbox that is 1 037 hashes instead of 86 686.

    Pass a fresh dict per manifest, not per TU; the table is the manifest's
    ``headers`` section and must span every entry.  When *header_table* is
    ``None`` a throwaway table is used, which only makes sense for a caller
    that wants the path list alone.

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

    if header_table is None:
        header_table = {}
    seen: set[str] = set()
    headers: list[str] = []

    for inc in includes:
        abs_path = str(inc.include.name)
        if abs_path in seen:
            continue
        seen.add(abs_path)

        resolved = Path(abs_path).resolve()
        # Everything get_includes() returns was reached by an #include, so it
        # IS a header — whatever it is called.  There used to be an extension
        # whitelist here ({.h .hpp .hxx .hh .inl}), and it silently dropped
        # every extensionless C++ standard header (<algorithm>, <bit>) and
        # every .tcc template body.  Two consequences, both measured on
        # HA_Boiler: the coverage purge deleted 29 such files and 1810 symbols
        # because the manifest did not list them, and a toolchain upgrade
        # could change any of them without marking a single TU stale, because
        # no hash was recorded to compare.  A whitelist of "what counts as a
        # header" cannot be kept complete; not filtering can.
        try:
            rel = str(resolved.relative_to(project_root))
        except ValueError:
            rel = str(resolved)  # absolute path for files outside project tree

        headers.append(rel)
        if rel not in header_table:
            header_table[rel] = {
                "hash": compute_source_hash(resolved),
                "generated": _is_generated_header(rel, build_dir_patterns),
            }

    return headers


def generate(
    compile_commands_path: Path,
    db_dir: Path,
    project_root: Path,
    units: list,
    macros: dict[str, str] | None = None,
    build_dir_patterns: list[str] | None = None,
    config_hash: str = "",
    scope: list[str] | None = None,
    vendor_patterns: list[str] | None = None,
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
        vendor_patterns: The effective vendor/SDK set this build used.  Stored
            so the query layer reads the same boundary the indexer applied
            instead of deriving its own.

    Returns:
        The ``config_hash`` — SHA-256 of the structural part of the manifest.
    """
    from fw_context_mcp.config.settings import derive_project_id

    entries: list[dict] = []
    # Shared across every entry — see MANIFEST_FORMAT for why.
    header_table: dict[str, dict] = {}
    arg_sets: list[list[str]] = []
    t0 = time.monotonic()

    for unit in units:
        source_file = str(unit.file.resolve())
        try:
            source_rel = str(unit.file.resolve().relative_to(project_root))
        except ValueError:
            source_rel = source_file

        source_hash = compute_source_hash(unit.file.resolve())
        headers = _collect_headers_from_tokens(
            unit, project_root, build_dir_patterns, header_table
        )

        entry: dict = {
            "file": source_rel,
            "directory": str(unit.directory) if unit.directory else str(project_root),
            "arg_set": _intern_arguments(unit.clang_args, arg_sets),
            "source_hash": source_hash,
            "headers": headers,
            "flags_hash": compute_flags_hash(unit.raw_entry) if unit.raw_entry else "",
        }
        entries.append(entry)

    manifest: dict = {
        "_format": MANIFEST_FORMAT,
        "compile_commands_path": str(compile_commands_path),
        "project_root": str(project_root),
        "arg_sets": arg_sets,
        "headers": header_table,
        "entries": entries,
    }
    tu_exts, header_exts = derive_extension_sets(compile_commands_path, header_table)
    manifest["tu_extensions"] = tu_exts
    manifest["header_extensions"] = header_exts
    if macros:
        manifest["macros"] = macros
    if build_dir_patterns:
        manifest["build_dir_patterns"] = build_dir_patterns
    # `is not None`: an empty set is the correct answer for a project whose
    # SDK lives outside it, and a missing key means something else entirely.
    if vendor_patterns is not None:
        manifest["vendor_patterns"] = vendor_patterns

    if not config_hash:
        # Backward-compatible fallback — compute hash from manifest dict.
        # New callers should pass config_hash computed from the normalized
        # compile_commands.json instead.
        project_id = derive_project_id(project_root)
        config_hash = compute_config_hash(units, project_root, project_id, build_dir_patterns, scope=scope, db_dir=db_dir)

    manifest["config_hash"] = config_hash
    manifest_json = json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False)
    manifest_path = _manifest_path(db_dir, config_hash)
    manifest_path.write_text(manifest_json, encoding="utf-8")

    elapsed = time.monotonic() - t0
    # Both numbers, because their ratio is the whole point of the /2 format:
    # references are what /1 stored, distinct files are what /2 stores.
    reference_count = sum(len(e.get("headers", [])) for e in entries)
    log.info(
        "manifest.json written: %d TUs, %d distinct headers (%d references), "
        "%d arg sets, config_hash=%s  %s",
        len(entries),
        len(header_table),
        reference_count,
        len(arg_sets),
        config_hash[:12],
        f"{elapsed:.1f}s",
    )

    return config_hash


def _manifest_path(db_dir: Path, config_hash: str) -> Path:
    """Return the per-``config_hash`` manifest path.

    WHY per-hash: multi-variant builds produce one manifest per (variant,
    image) — a single ``manifest.json`` would be overwritten by each build.
    The file is keyed by ``config_hash`` (content-addressable, stable).
    """
    return db_dir / f"manifest.{config_hash}.json"


# Keyed by (manifest path, mtime_ns) so a rewritten manifest misses.  Only the
# small list is retained — never the parsed manifest, which is 150 MB+ of
# Python objects for a large project and would sit in the MCP server for its
# whole life.
_BUILD_PATTERNS_CACHE: dict[tuple[str, int], dict] = {}

# The top-level keys a reader on the query path may need.  All short, all
# read from one parse — adding a second cache would mean a second parse of
# the same 52 MB file.
_CHEAP_KEYS = ("build_dir_patterns", "tu_extensions", "header_extensions")


def _load_cheap_keys(db_dir: Path, config_hash: str) -> dict:
    """Return the short top-level manifest keys, cached across calls.

    WHY this exists rather than ``load(...)[key]``: the staleness helpers on
    the MCP query path need nothing else from the manifest, and parsing the
    whole file to reach one short list is the most expensive thing they do.
    Measured on zbox-ecb-fw (876 TUs), the manifest is 52 MB and takes 109 ms
    to read and parse — paid on EVERY query routed through
    ``_with_stale_recovery``.

    The first call after an index still parses once; every later one is a
    dict lookup.  Only the short lists are kept, so the cost is bytes rather
    than the hundreds of megabytes a parsed manifest occupies.
    """
    path = _manifest_path(db_dir, config_hash)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {}
    cached = _BUILD_PATTERNS_CACHE.get(key)
    if cached is not None:
        return cached
    manifest = load(db_dir, config_hash)
    cheap = {k: list(manifest.get(k, [])) for k in _CHEAP_KEYS} if manifest else {}
    # One project has one active manifest; bound the dict so a long-running
    # server that reindexes repeatedly cannot accumulate entries.
    if len(_BUILD_PATTERNS_CACHE) > 32:
        _BUILD_PATTERNS_CACHE.clear()
    _BUILD_PATTERNS_CACHE[key] = cheap
    return cheap


def load_tu_extensions(db_dir: Path, config_hash: str) -> frozenset[str] | None:
    """Return the suffixes this build compiles, or None when unknown.

    None means the manifest predates the key or does not exist.  The caller
    then falls back to the compiler rules in ``utils.TU_EXTENSIONS``, which
    is what a project without a manifest — manual mode, a first run — needs
    anyway.
    """
    exts = _load_cheap_keys(db_dir, config_hash).get("tu_extensions")
    return frozenset(exts) if exts else None


def load_header_extensions(db_dir: Path, config_hash: str) -> frozenset[str] | None:
    """Return the suffixes of the headers this build includes, or None."""
    exts = _load_cheap_keys(db_dir, config_hash).get("header_extensions")
    return frozenset(exts) if exts else None


def load_build_dir_patterns(db_dir: Path, config_hash: str) -> list[str]:
    """Return just ``build_dir_patterns``, cached across calls.

    WHY this exists rather than ``load(...)["build_dir_patterns"]``: the
    staleness helpers on the MCP query path need nothing else from the
    manifest, and parsing the whole file to reach one short list is the most
    expensive thing they do.  Measured on zbox-ecb-fw (876 TUs), the manifest
    is 52 MB and takes 109 ms to read and parse — paid on EVERY query routed
    through ``_with_stale_recovery``, to obtain a list of two or three
    strings.

    Shares one parse with the other cheap keys — see ``_load_cheap_keys``.
    """
    return _load_cheap_keys(db_dir, config_hash).get("build_dir_patterns", [])


def _read_manifest_file(manifest_path: Path) -> dict | None:
    """Parse one manifest file, or return None when it is unusable.

    A manifest written by an older format is rejected rather than migrated.
    Its entries carry per-TU header records and argument lists that the /2
    readers do not understand, and a silent misread would look like "this TU
    has no headers" — which marks nothing stale and freezes the index.
    Returning None makes the caller treat the build as un-indexed, and the
    reindex that follows writes the current format.
    """
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load %s: %s", manifest_path.name, exc)
        return None
    if not isinstance(data, dict):
        log.warning("Ignoring %s: not a JSON object", manifest_path.name)
        return None
    stored_format = data.get("_format")
    if stored_format != MANIFEST_FORMAT:
        log.info(
            "Ignoring %s: format %r, this build reads %r",
            manifest_path.name, stored_format, MANIFEST_FORMAT,
        )
        return None
    return data


def load_build_dir_patterns_any(db_dir: Path) -> list[str]:
    """Return ``build_dir_patterns`` for the newest manifest in *db_dir*.

    Same purpose as :func:`load_build_dir_patterns`, for the caller that does
    not know the active config_hash yet — the file-watch daemon reads the
    patterns before it has resolved a build.
    """
    try:
        newest = max(
            db_dir.glob("manifest.*.json"), key=lambda p: p.stat().st_mtime, default=None
        )
    except OSError:
        return []
    if newest is None:
        return []
    config_hash = newest.name.removeprefix("manifest.").removesuffix(".json")
    return load_build_dir_patterns(db_dir, config_hash)


def load(db_dir: Path, config_hash: str | None = None) -> dict | None:
    """Load the manifest for *config_hash* from *db_dir*, or None.

    With *config_hash*, reads ``manifest.<config_hash>.json``.  Without it,
    returns the most recently modified ``manifest.*.json`` — a best-effort
    fallback for callers that only need ``build_dir_patterns`` and do not
    know the active build yet (e.g. the file-watch daemon).
    """
    if config_hash:
        manifest_path = _manifest_path(db_dir, config_hash)
        if not manifest_path.exists():
            return None
        return _read_manifest_file(manifest_path)

    candidates: list[Path] = []
    try:
        candidates = sorted(
            db_dir.glob("manifest.*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for manifest_path in candidates:
        data = _read_manifest_file(manifest_path)
        if data is not None:
            return data
    return None


def compute_structural_hash(
    compile_commands_path: Path,
    project_root: Path,
    units: list,
    build_dir_patterns: list[str] | None = None,
    project_id: str = "",
    scope: list[str] | None = None,
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
    return compute_config_hash(units, project_root, project_id, build_dir_patterns, scope=scope)


def build_preliminary(
    compile_commands_path: Path,
    db_dir: Path,
    project_root: Path,
    units: list,
    build_dir_patterns: list[str] | None = None,
    project_id: str = "",
    scope: list[str] | None = None,
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

    config_hash = compute_config_hash(units, project_root, project_id, build_dir_patterns, scope=scope, db_dir=db_dir)

    # Build entries for the manifest file
    entries: list[dict] = []
    arg_sets: list[list[str]] = []
    for unit in units:
        source_file = str(unit.file.resolve())
        try:
            source_rel = str(unit.file.resolve().relative_to(project_root))
        except ValueError:
            source_rel = source_file

        entries.append({
            "file": source_rel,
            "directory": str(unit.directory) if unit.directory else str(project_root),
            "arg_set": _intern_arguments(unit.clang_args, arg_sets),
            "source_hash": "",
            "headers": [],
        })

    manifest: dict = {
        "_format": MANIFEST_FORMAT,
        "compile_commands_path": str(compile_commands_path),
        "project_root": str(project_root),
        "arg_sets": arg_sets,
        # Empty until generate() runs — a preliminary manifest has no hashes.
        "headers": {},
        "entries": entries,
    }
    if build_dir_patterns:
        manifest["build_dir_patterns"] = build_dir_patterns
    # The units are known even in a preliminary manifest; the headers are
    # not, and an empty list there is the honest answer rather than a guess.
    tu_exts, _ = derive_extension_sets(compile_commands_path, None)
    manifest["tu_extensions"] = tu_exts
    manifest["header_extensions"] = []

    manifest["config_hash"] = config_hash
    manifest_json = json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False)
    db_dir.mkdir(parents=True, exist_ok=True)

    # Don't overwrite an existing manifest that already has full header-hash
    # data just because the structural config_hash changed (e.g. a new
    # -Wno-unused flag was added to compile_commands.json).  The preliminary
    # manifest has empty source_hash/headers — writing it would degrade the
    # on-disk manifest.  _update_manifest_after_index will handle the
    # regeneration when it detects the degraded entries.
    manifest_path = _manifest_path(db_dir, config_hash)
    existing = load(db_dir, config_hash)
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


def build_scope(
    variant: str = "",
    image: str = "",
    env: dict[str, str] | None = None,
) -> list[str]:
    """Build the canonical scope token list that enters ``config_hash``.

    WHY scope: two (variant, image) builds may compile the same shared source
    (``proj/common/*.c``) with identical normalized flags after build-dir paths
    are stripped — the raw compile_commands hash could collide.  Folding
    variant/image/env into the hash disambiguates them explicitly.  Empty parts
    are omitted so single-project builds (variant='' image='' env={}) produce
    the exact same hash as before this feature.  ``env`` is serialized with
    sorted keys so the hash is deterministic across dict ordering.
    """
    tokens: list[str] = []
    if variant:
        tokens.append(variant)
    if image:
        tokens.append(image)
    if env:
        tokens.append("env:" + json.dumps(dict(sorted(env.items()))))
    return tokens


def compute_config_hash(
    units: list,
    project_root: Path,
    project_id: str,
    build_dir_patterns: list[str] | None = None,
    scope: list[str] | None = None,
    db_dir: Path | None = None,
) -> str:
    """Return SHA-256 of the build's **compilation dialect**.

    The hash answers exactly one question: *could the same source text compile
    to something different now?*  Only two kinds of flag can change that
    answer, so only those are hashed:

    1. ``-D`` macros — they flip ``#ifdef``, so they change which code exists.
    2. Dialect / target flags (``-std``, ``-mcpu``, ``-f*``, ``-O*``, …) —
       they change what the parser accepts and how it behaves.

    Everything else is deliberately excluded:

    - **The translation-unit list.**  Which files exist is not build identity.
      Including it meant adding one ``.c`` file minted a new identity for
      every unchanged TU, and their rows then had to be migrated to it.  That
      migration was the "reuse" tier, and it was incomplete: rows owned by a
      header stayed under the old hash and retention deleted them.
    - **Include search paths INSIDE the project** (``-I``, ``-isystem``,
      ``-include``, …).  This is where per-directory variance lives —
      HA_Boiler has 14 distinct flag-sets but 208 distinct include paths, and
      zbox-ecb-fw has 268 in-project ones.  A directory that moves inside the
      project does not change what the compiler reads.

    An include path OUTSIDE the project is KEPT, because it names the
    toolchain and the SDK.  Two toolchains must map to two config_hash
    values: rows parsed against different system headers cannot share one
    build.  Those paths go in as the INTERSECTION over the units, so a path
    that only some units carry stays out — otherwise the translation-unit
    list comes back into the hash through the side door.
    - **Build-output paths**, whether they arrive as a path flag or embedded
      in another flag, detected via *build_dir_patterns*.
    - **Transient ``-D`` macros** (timestamps, build counters) — they change
      every build without changing semantics.

    Flag order and TU order do not matter: both accumulate into sets.

    What detects real change is therefore split by scope: this hash for the
    dialect, ``files.source_hash`` for content, ``files.flags_hash`` for a
    TU's own flags, and the manifest entry list for which files belong to the
    build at all.

    ``files.flags_hash`` only counts for a TU that already got past Tier 1.
    Tier 1 compares the source file mtime and stops there, and a change of
    flags alone moves no mtime.  A statement that flags_hash catches a
    changed include path is therefore wrong on its own, which is why the
    out-of-project paths belong in THIS hash.

    The canonical JSON is written to
    ``<db_dir>/<project_id>/compile_commands.<hash>.json``
    as a debug artifact — ``diff`` between two versions shows only
    semantic differences.

    Returns the *config_hash* hex string.
    """
    # Path-bearing flags — consumed and dropped, see the docstring.
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

    def _is_outside_project(path_value: str) -> bool:
        """Is *path_value* outside the project tree and outside the build tree?

        Does no I/O: compute_structural_hash() documents this code path as
        "no I/O, no libclang", so this function does not call Path.resolve().
        normalize_args() already resolved the relative paths, and an absolute
        path arrives as the build system wrote it, which is stable between
        runs.
        """
        if _is_build_output(path_value):
            return False
        p = Path(path_value)
        if not p.is_absolute():
            # normalize_args resolves an include path against the translation
            # unit's directory, so a relative path here is not a search path.
            return False
        try:
            p.relative_to(project_root)
        except ValueError:
            return True
        return False

    def _is_dialect_token(token: str) -> bool:
        """Does *token* belong to the build's compilation dialect?

        A dialect flag always starts with '-'.  Anything else is a filename or
        a stray value.  The pre-pass joins every separated flag with its
        value, so a bare token that arrives here belongs to no flag.

        This test compared the token SUFFIX against a source-extension
        whitelist and an output-extension one before.  A whitelist of "what counts as a source file"
        cannot stay complete, and a suffix outside the list put a filename
        into the hash — the translation-unit coupling that this function
        exists to remove.  The rule above needs no list.

        The rule does NOT mean the hash holds no project path.  A path can
        ride inside a non-path flag, and -fmacro-prefix-map=<project>=NAME
        stays.  That flag has one value per build, not one per translation
        unit, so it makes no churn.
        """
        if not token.startswith("-"):
            return False
        if token in _TRANSIENT_DROP or token in _DROP_WITH_ARG:
            return False
        # A build-output path can ride inside a non-path flag
        # (-Wl,-Map=BUILD/x.map, -fprofile-dir=BUILD/...).  Those names
        # change per build without changing semantics.
        return not _is_build_output(token)

    # Accumulated across ALL translation units, deduplicated: a macro or
    # dialect flag anywhere in the build is part of that build's identity,
    # and it counts once no matter how many TUs carry it.  (Measured: FM has
    # an identical -D set on all 216 TUs; HA_Boiler has exactly one macro
    # that varies, ARDUINO_CORE_BUILD on 46 of 114.)
    defines: set[str] = set()
    dialect: set[str] = set()
    # Out-of-project include paths go in as the INTERSECTION over translation
    # units, not as the union.  A path that only some units carry is per-unit
    # state, and to put it in would put the translation-unit list back into
    # the hash: on zbox-ecb-fw-v5 one generated unit,
    # validate_binding_headers.c, carries 11 devicetree binding headers, so
    # the union moves each time the board overlay changes.  The intersection
    # does not move.  Measured over 2 052 translation units in 7 real builds:
    # no single unit changes it, and it still holds all 16 toolchain
    # directories.
    #
    # A running intersection needs no list of per-unit sets, and `None` for
    # "no unit seen yet" removes the empty-build special case.
    external: set[str] | None = None
    for unit in units:
        # ── Pre-pass: collapse space-separated -D NAME=VALUE → -DNAME=VALUE ──
        # Sorting alphabetically (next step) would separate "-D" from its value
        # token — join them first so the sort is semantically safe.  Also
        # drops transient -D macros in the same pass.
        raw_args = unit.clang_args
        # The unit's own source file, by basename.  The join below must not
        # take it for the value of the flag in front of it: "-c main.c" would
        # become "-cmain.c" and put the translation-unit list back into the
        # hash.  normalize_args() strips the positional tokens on the
        # production path, but this function does not trust its caller — see
        # _is_dialect_token().  Read without resolve(): this code path is
        # documented as "no I/O".
        source_name = Path(str(unit.file)).name
        collapsed_args: list[str] = []
        j = 0
        while j < len(raw_args):
            a = raw_args[j]
            if a in _DROP_WITH_ARG:
                # -o, -MF, -MT, -MQ: the flag AND its value go.  Without this
                # the join below would attach the value and hide it from
                # _is_dialect_token(), which only recognises the bare flag.
                j += 2 if (j + 1 < len(raw_args) and not raw_args[j + 1].startswith("-")) else 1
                continue
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
            # Join ANY flag with a following non-flag token, before the sort.
            # The path pass below pairs a flag with its neighbour BY POSITION,
            # and the sort has already moved each value away from its flag.
            # Measured on zbox-ecb-fw-v5, one build of 257 translation units:
            # a sorted -isystem consumed -mabi=aapcs 230 times, and it left
            # its own directory in the set as a token with no flag.
            #
            # WHY no list of "flags that take a value": such a list cannot
            # stay complete, and _is_dialect_token() drops what it forgets.
            # Measured on FM, the forgotten ones were
            # "--param max-inline-insns-single=500" and the separated
            # --sysroot form, which carries the toolchain root.
            #
            # Two structural guards keep a stray token out, so the join
            # needs no list of flag names either.  A flag that already holds
            # an '=' carries its own value and takes no second one
            # (-std=c11, -mcpu=cortex-m4, -Wl,-Map=x.map), and the unit's own
            # source file is never a value.  normalize_args() removes the
            # positional tokens on the production path, but this function
            # does not trust its caller — see _is_dialect_token().
            elif (
                a.startswith("-")
                and "=" not in a
                and j + 1 < len(raw_args)
                and not raw_args[j + 1].startswith("-")
                and Path(raw_args[j + 1]).name != source_name
            ):
                collapsed_args.append(f"{a}{raw_args[j + 1]}")
                j += 2
            else:
                collapsed_args.append(a)
                j += 1

        # Normalize arguments: sort, then process path-bearing flags
        args = sorted(collapsed_args)
        normalized_args: list[str] = []
        # The out-of-project paths of THIS unit.  _is_build_output is tested
        # while they are collected, because the running intersection below
        # bypasses _is_dialect_token().
        tu_external: set[str] = set()

        i = 0
        while i < len(args):
            arg = args[i]
            handled = False

            # ── Path-bearing flags ──
            # Include search paths INSIDE the project are dropped.  Paths
            # outside it are kept.  The two answer different questions.
            #
            # An in-project path is per-unit state.  zbox-ecb-fw has 268 of
            # them, and they move when a directory moves.  A moved directory
            # does not change what the compiler reads.
            #
            # An out-of-project path names the toolchain and the SDK.  Those
            # are the system headers that the parse reads.  Measured: 16
            # toolchain directories on every translation unit of a Zephyr
            # build and of a PlatformIO build.
            #
            # A toolchain update creates a new build IDENTITY.  Two
            # toolchains must map to two config_hash values: rows parsed
            # against different system headers cannot share one build, and a
            # variant project would mix them under a single hash.  That is
            # what this path pass buys, and the full reindex it causes is
            # intended.
            #
            # Detection is a separate question, answered elsewhere: the
            # staleness pass hashes every header the units include, so it
            # sees a content change at an unchanged path.  Tier 1 sees
            # neither — it compares the source file mtime, and a toolchain
            # update moves no source file.
            for prefix in _PATH_PREFIXES:
                if arg == prefix:
                    # Dangling: the pre-pass joined every real pair, so a
                    # bare prefix here has no value.  Consume the flag alone.
                    # When this branch consumed args[i + 1], it removed a
                    # sorted neighbour that belongs to no flag.
                    i += 1
                    handled = True
                    break
                elif arg.startswith(prefix) and len(arg) > len(prefix):
                    # Concatenated form: -I/path/to/include
                    if _is_outside_project(arg[len(prefix):]):
                        tu_external.add(arg)
                    i += 1
                    handled = True
                    break

            if handled:
                continue

            for prefix in _PATH_EQ_PREFIXES:
                if arg.startswith(prefix):
                    # --sysroot= is a toolchain root by definition, so it is
                    # kept whenever it points outside the project.
                    if _is_outside_project(arg[len(prefix):]):
                        tu_external.add(arg)
                    i += 1
                    handled = True
                    break

            if handled:
                continue

            # Non-path argument — keep as-is
            normalized_args.append(arg)
            i += 1

        # Split this TU's surviving flags into the two things that define
        # the compilation dialect.  The TU's own identity is deliberately
        # NOT recorded — see the canonical dict below.
        for token in normalized_args:
            if token.startswith("-D"):
                defines.add(token)
            elif _is_dialect_token(token):
                dialect.add(token)

        # The attached token goes into the intersection, not the bare path:
        # it keeps the invariant that a dialect token starts with '-', and it
        # records WHICH flag carried the path.
        external = tu_external if external is None else external & tu_external

    if external:
        dialect |= external

    canonical: dict = {
        # Bumped from /1, which keyed the hash on the per-TU {file, arguments}
        # list.  Every existing index therefore gets one final reindex, which
        # is intended: the old hashes describe a different question.
        "_format": "fw-context-cc/2",
        "project_root": str(project_root),
        # WHY only these two: config_hash answers "could the same source text
        # compile to something different now?"  Macros flip #ifdef, and the
        # standard / target / dialect flags change what the parser accepts.
        # Nothing else can change the meaning of unchanged source.
        #
        # WHY the translation-unit list is absent: it made adding one .c file
        # mint a new build identity for every unchanged TU, whose rows then
        # had to be migrated to it.  That migration was the reuse tier, and it
        # was incomplete — rows owned by a header stayed behind under the old
        # hash and retention deleted them.  Which files exist is recorded in
        # the manifest entries; whether one changed is answered by
        # ``files.source_hash`` and ``files.flags_hash``; whether one left the
        # build is answered by comparing the manifest to the files table.
        "defines": sorted(defines),
        "dialect": sorted(dialect),
    }
    if scope:
        canonical["scope"] = scope

    canonical_json = json.dumps(canonical, sort_keys=True, indent=2, ensure_ascii=False)
    config_hash = hashlib.sha256(canonical_json.encode()).hexdigest()

    # Write canonical JSON as a debug artifact under the configured index dir
    # (db_dir), never ~/.fw-context/index.  db_dir is None only on the no-I/O
    # path (compute_structural_hash) — skip the artifact there so no stray
    # file escapes test isolation (FW_CONTEXT_INDEX_DIR).
    if db_dir is not None:
        cc_dir = db_dir / project_id
        cc_dir.mkdir(parents=True, exist_ok=True)
        out_path = cc_dir / f"compile_commands.{config_hash}.json"
        try:
            out_path.write_text(canonical_json, encoding="utf-8")
        except OSError:
            pass  # best-effort — hash is already computed

    return config_hash


def _hash_with_cache(path: Path, hash_cache: dict[str, str] | None) -> str:
    """Return the SHA-256 of *path*, memoized in *hash_cache* by resolved path.

    WHY a cache: a project header included by 300 TUs is asked for by the
    staleness pre-pass and again by every per-TU check.  Without memoization
    each of those reads and hashes the same file, which dominates the cost of
    an otherwise no-op index run on large codebases.

    Callers that pass ``None`` get the uncached behaviour.
    """
    if hash_cache is None:
        return compute_source_hash(path)
    key = str(path)
    cached = hash_cache.get(key)
    if cached is None:
        cached = compute_source_hash(path)
        hash_cache[key] = cached
    return cached


def header_is_trusted(record: dict) -> bool:
    """May the pipeline keep the stored hash of this header without a re-read?

    ONE definition, FIVE callers: collect_stale_headers(),
    check_tu_staleness(), compute_current_entry_hash(),
    _mtime_bump_is_safe() and _headers_moved_on().  What the pipeline trusts
    is one decision, and five copies of it move apart.  A caller that
    repeats the rule instead of a call to this function is a defect.

    compute_current_entry_hash() was the copy that four rounds of review did
    not see: it answers a different question ("what IS the hash now") and so
    it reads as a hash function, not as a trust rule.  It carries the same
    three conditions, and a copy that keeps the STORED hash for a header the
    run just re-read writes a files.content_hash that describes no text at
    all.  grep for the rule, not for the word "trust".

    Only a build-generated header is trusted.  Its content changes with every
    build without a change of meaning, and config_hash plus flags_hash carry
    what does change.

    A vendor header and a header outside the project are NOT trusted any
    more.  External code reaches the index THROUGH the project's own units:
    an inline function, a macro, a constexpr, a C++ template, or a struct
    layout expands from the header INTO every unit that includes it.  A
    changed external header therefore changes the indexed symbols of PROJECT
    files — their signatures, their lines, and which code survives the
    #ifdefs.  A full reparse is the correct answer, not a conservative one.

    Cost measured: to hash every header in the manifest takes 45 ms, against
    an index run of 42 minutes.
    """
    return bool(record.get("generated"))


def merge_header_records(
    manifest: dict,
    header_records: dict[str, dict],
) -> None:
    """Merge fresh header records into the manifest's shared ``headers`` map.

    Merge, do not replace: the hash is newer and wins, but ``generated`` is a
    property of the PATH, not of this parse.  A caller with no
    build_dir_patterns computes False for every header, and ``generated`` is
    the only trust rule the pipeline has left — to let False overwrite True
    disables it in silence.

    ONE definition, and every writer of that map calls it.  A second copy of
    this merge is how the defect it fixes came back the first time.
    """
    table = manifest.setdefault("headers", {})
    for path, record in header_records.items():
        existing = table.get(path)
        if existing is not None and existing.get("generated"):
            record = {**record, "generated": True}
        table[path] = record


def check_tu_staleness(
    entry: dict,
    project_root: Path,
    *,
    hash_cache: dict[str, str] | None = None,
    headers: list[dict] | None = None,
) -> tuple[bool, str | None]:
    """Check whether a translation unit's source or project headers have changed.

    *headers* are the entry's resolved header records from
    :func:`tu_headers`.  It is a required argument in practice: the entry
    itself only stores paths, so omitting it checks the source hash alone and
    silently stops tracking headers.  It stays optional so a caller that
    genuinely wants the source-only check can ask for it.

    WHY per-TU staleness: re-parsing every TU on every index run is wasteful
    when only a few source files changed.  This function enables incremental
    indexing — only TUs whose source or project headers changed are re-parsed.

    Every header is re-hashed except a build-generated one — see
    :func:`header_is_trusted` for the one rule and for why the vendor and
    out-of-tree exceptions are gone.

    An entry marked ``needs_reparse`` is stale whatever its hashes say.  The
    manifest keeps ONE hash per header path and every entry shares it, so a
    unit that another unit's re-parse left behind cannot be told apart by
    the hashes alone.  See ``_headers_moved_on``.

    Compares the stored ``source_hash`` and ``headers[].hash`` from *entry*
    against the current on-disk content.

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

    if entry.get("needs_reparse"):
        # Another unit refreshed a header that this entry shares.  The shared
        # hash proves nothing about THIS entry's rows any more.
        return True, current_source_hash

    # ── Check header hashes ──
    for h in headers or ():
        if header_is_trusted(h):
            continue

        header_path = h["path"]
        p = Path(header_path)
        if not p.is_absolute():
            p = (project_root / header_path).resolve()

        if _hash_with_cache(p, hash_cache) != h.get("hash", ""):
            return True, current_source_hash

    return False, None


def collect_stale_headers(
    manifest: dict,
    project_root: Path,
    *,
    hash_cache: dict[str, str] | None = None,
) -> set[str]:
    """Return the manifest header paths whose on-disk content has changed.

    WHY this pre-pass exists: a header is not a translation unit, so editing
    one never moves the mtime of any TU.  The per-TU mtime fast-path would
    therefore report every dependent TU as unchanged and the header's symbols
    would stay frozen at their first-index state.  Collecting the changed
    headers up front lets the runner mark the dependent TUs for re-parsing.

    Trust rules match :func:`check_tu_staleness` exactly, because both call
    :func:`header_is_trusted`.  A build-generated header keeps its stored
    hash; every other header is re-read.

    Reads the manifest's ``headers`` map, which already holds each path once,
    so a header shared by 300 TUs is checked once.  Pass *hash_cache* to share
    those hashes with the per-TU staleness checks that follow.

    Returns the paths exactly as stored in the manifest, so the result can be
    fed straight into :func:`tus_affected_by_headers`.
    """
    stale: set[str] = set()

    for header_path, record in (manifest.get("headers") or {}).items():
        if header_is_trusted(record):
            continue

        p = Path(header_path)
        if not p.is_absolute():
            p = (project_root / header_path).resolve()

        if _hash_with_cache(p, hash_cache) != record.get("hash", ""):
            stale.add(header_path)

    return stale


def tus_affected_by_headers(manifest: dict, stale_headers: set[str]) -> set[str]:
    """Return ``entry["file"]`` for every TU that includes one of *stale_headers*.

    WHY every dependent TU and not just one: symbols from a shared header are
    stored by whichever TU claims them first in a run.  A TU that keeps its
    old rows would hold the fresh ones out through the
    ``ON CONFLICT(config_hash, usr)`` guard in ``insert_symbols_batch``, so a
    partial re-parse leaves stale line numbers and signatures behind.
    """
    if not stale_headers:
        return set()
    affected: set[str] = set()
    for entry in manifest.get("entries", []):
        # The per-TU list holds paths, so this is a plain membership test.
        if any(path in stale_headers for path in entry.get("headers") or ()):
            affected.add(entry["file"])
    return affected


def update_entry(
    manifest: dict,
    entry_index: int,
    source_hash: str,
    headers: list[str],
    header_records: dict[str, dict] | None = None,
) -> None:
    """Update a single translation unit's entry in the manifest in-place.

    Called after a TU has been re-parsed — updates ``source_hash`` and the
    entry's header path list.  *header_records* carries the ``{hash,
    generated}`` records for those paths and is merged into the manifest's
    shared ``headers`` map; a re-parse that discovered a header no other TU
    has seen must contribute its record, or the path would resolve with an
    empty hash and stay permanently stale.

    The hash of a record already in the map is overwritten, because the
    re-parse just read the file.  ``generated`` is NOT — see
    :func:`merge_header_records`.
    """
    entries = manifest.get("entries", [])
    if entry_index >= len(entries):
        return
    entries[entry_index]["source_hash"] = source_hash
    entries[entry_index]["headers"] = headers
    if header_records:
        merge_header_records(manifest, header_records)


def mark_entries_behind(
    manifest: dict,
    changed_paths: set[str],
    keep: set[str],
) -> int:
    """Flag each entry that depends on a header another unit refreshed.

    Trusts what :func:`header_is_trusted` trusts, and nothing else — the same
    one decision that collect_stale_headers() and _headers_moved_on() read.

    *changed_paths* is the difference of the shared header table, taken
    BEFORE the loop over the re-parsed units, not one time per unit.  A
    per-unit difference makes the second unit read what the first one just
    wrote as somebody else's change.

    *keep* is the set of ``tu_rel`` paths this run really re-parsed.  Those
    entries hold current rows and must not be flagged.

    Returns the number of entries flagged.
    """
    if not changed_paths:
        return 0
    flagged = 0
    for entry in manifest.get("entries", []):
        if entry.get("file") in keep or entry.get("needs_reparse"):
            continue
        if any(path in changed_paths for path in entry.get("headers") or ()):
            entry["needs_reparse"] = True
            flagged += 1
    return flagged


def prune_header_table(manifest: dict) -> int:
    """Drop ``headers`` records no entry references, return how many.

    A re-parse that stops including a header leaves its record behind.  The
    coverage set is built from the per-TU lists, never from this map, so an
    orphan cannot keep a removed file inside the build — but it would still
    make the artifact claim a dependency that no longer exists, and it would
    grow the map without bound across incremental runs.
    """
    table = manifest.get("headers")
    if not table:
        return 0
    referenced: set[str] = set()
    for entry in manifest.get("entries", []):
        referenced.update(entry.get("headers") or ())
    orphans = [path for path in table if path not in referenced]
    for path in orphans:
        del table[path]
    return len(orphans)


def save(manifest: dict, db_dir: Path, config_hash: str) -> str:
    """Save the per-hash manifest (``manifest.<config_hash>.json``), return it.

    The caller is responsible for computing *config_hash* — save() no
    longer calls ``compute_config_hash()`` internally.  This removes the
    circular dependency between manifest content and config_hash.
    """
    prune_header_table(manifest)
    manifest["config_hash"] = config_hash
    manifest_json = json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False)
    _manifest_path(db_dir, config_hash).write_text(manifest_json, encoding="utf-8")
    return config_hash


def get_manifest_entry_hash(entry: dict, headers: list[dict] | None = None) -> str:
    """Return SHA-256 hash of a single manifest entry (source + headers).

    Used as the per-TU staleness hash (replaces the old deps_hash-based
    ``content_hash`` for Tier 2 checks).

    *headers* are the entry's resolved records from :func:`tu_headers`.
    Passing None hashes the source alone, which no longer detects a header
    change — the entry stores paths, not hashes.
    """
    header_hashes = "".join(
        h.get("hash", "")
        for h in sorted(headers or (), key=lambda x: x.get("path", ""))
    )
    source = entry.get("source_hash", "")
    return hashlib.sha256(f"{source}|{header_hashes}".encode()).hexdigest()


def compute_current_entry_hash(
    entry: dict,
    project_root: Path,
    *,
    new_source_hash: str | None = None,
    hash_cache: dict[str, str] | None = None,
    headers: list[dict] | None = None,
) -> str:
    """Return the manifest entry hash computed from CURRENT disk content.

    When *entry* is stale (headers or source changed), this function reads
    the actual on-disk hashes rather than trusting the stored values.
    Trusts what :func:`header_is_trusted` trusts and nothing else.  This
    function reads as a hash function rather than as a trust rule, which is
    why its copy of the rules survived four rounds of review: a copy that
    keeps the STORED hash for a header the run just re-read writes a
    files.content_hash that describes no text at all.

    *new_source_hash* overrides ``entry["source_hash"]`` when the source
    file content has also changed.

    *headers* are the entry's resolved records from :func:`tu_headers`.
    """
    source = new_source_hash if new_source_hash else entry.get("source_hash", "")

    current_headers: list[dict] = []
    for h in headers or ():
        if header_is_trusted(h):
            current_headers.append(dict(h))
            continue

        header_path = h["path"]
        p = Path(header_path)
        if not p.is_absolute():
            p = (project_root / header_path).resolve()

        current_hash = _hash_with_cache(p, hash_cache)
        current_headers.append(
            {"path": h["path"], "hash": current_hash, "generated": h.get("generated", False)}
        )

    header_hashes = "".join(
        h.get("hash", "")
        for h in sorted(current_headers, key=lambda x: x.get("path", ""))
    )
    return hashlib.sha256(f"{source}|{header_hashes}".encode()).hexdigest()


