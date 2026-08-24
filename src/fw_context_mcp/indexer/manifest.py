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

from fw_context_mcp.indexer.compile_commands import _DROP_WITH_ARG, _SOURCE_EXTS
from fw_context_mcp.indexer.config_hash import _TRANSIENT_DROP, compute_flags_hash
from fw_context_mcp.utils import compute_source_hash

# Object / dependency outputs.  A token with one of these suffixes is a build
# product, never a flag that shapes the dialect.
_OUTPUT_EXTS = frozenset({".o", ".obj", ".d", ".map", ".elf", ".bin", ".hex"})

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
    if macros:
        manifest["macros"] = macros
    if build_dir_patterns:
        manifest["build_dir_patterns"] = build_dir_patterns

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
_BUILD_PATTERNS_CACHE: dict[tuple[str, int], list[str]] = {}


def load_build_dir_patterns(db_dir: Path, config_hash: str) -> list[str]:
    """Return just ``build_dir_patterns``, cached across calls.

    WHY this exists rather than ``load(...)["build_dir_patterns"]``: the
    staleness helpers on the MCP query path need nothing else from the
    manifest, and parsing the whole file to reach one short list is the most
    expensive thing they do.  Measured on zbox-ecb-fw (876 TUs), the manifest
    is 52 MB and takes 109 ms to read and parse — paid on EVERY query routed
    through ``_with_stale_recovery``, to obtain a list of two or three
    strings.

    The first call after an index still parses once; every later one is a
    dict lookup.  Only the list is kept, so the cost is bytes rather than the
    hundreds of megabytes a parsed manifest occupies.
    """
    path = _manifest_path(db_dir, config_hash)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return []
    cached = _BUILD_PATTERNS_CACHE.get(key)
    if cached is not None:
        return cached
    manifest = load(db_dir, config_hash)
    patterns = list(manifest.get("build_dir_patterns", [])) if manifest else []
    # One project has one active manifest; bound the dict so a long-running
    # server that reindexes repeatedly cannot accumulate entries.
    if len(_BUILD_PATTERNS_CACHE) > 32:
        _BUILD_PATTERNS_CACHE.clear()
    _BUILD_PATTERNS_CACHE[key] = patterns
    return patterns


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
    - **Include search paths** (``-I``, ``-isystem``, ``-include``, …).  This
      is where per-directory variance lives — HA_Boiler has 14 distinct
      flag-sets but 208 distinct include paths.  A changed path is caught by
      that TU's ``files.flags_hash`` (which expands response files, so it sees
      real paths) and reparses the one TU.
    - **Build-output paths**, whether they arrive as a path flag or embedded
      in another flag, detected via *build_dir_patterns*.
    - **Transient ``-D`` macros** (timestamps, build counters) — they change
      every build without changing semantics.

    Flag order and TU order do not matter: both accumulate into sets.

    What detects real change is therefore split by scope: this hash for the
    dialect, ``files.source_hash`` for content, ``files.flags_hash`` for a
    TU's own flags, and the manifest entry list for which files belong to the
    build at all.

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

    def _is_dialect_token(token: str) -> bool:
        """Does *token* belong to the build's compilation dialect?

        Callers hand over ``unit.clang_args``, which ``normalize_args``
        already stripped of the source file and the output path.  This check
        does not trust that: a filename slipping through would be neither a
        ``-D`` nor a path-bearing flag, so it would land in *dialect* and put
        the translation-unit list back into the hash — the exact coupling
        this function exists to remove.  Keeping the guard local means the
        property holds no matter what the caller passes.
        """
        if token in _TRANSIENT_DROP or token in _DROP_WITH_ARG:
            return False
        if _is_build_output(token):
            # A build-output path can ride inside a non-path flag
            # (-Wl,-Map=BUILD/x.map, -fprofile-dir=BUILD/...).  Those names
            # change per build without changing semantics.
            return False
        suffix = Path(token).suffix.lower()
        return suffix not in _SOURCE_EXTS and suffix not in _OUTPUT_EXTS

    # Accumulated across ALL translation units, deduplicated: a macro or
    # dialect flag anywhere in the build is part of that build's identity,
    # and it counts once no matter how many TUs carry it.  (Measured: FM has
    # an identical -D set on all 216 TUs; HA_Boiler has exactly one macro
    # that varies, ARDUINO_CORE_BUILD on 46 of 114.)
    defines: set[str] = set()
    dialect: set[str] = set()
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

            # ── Path-bearing flags are consumed and DROPPED ──
            # The include search path is per-TU state, not build identity.
            # Measured: HA_Boiler has 14 distinct flag-sets but 208 distinct
            # include paths — the per-directory variance lives here.  A path
            # change is caught by that TU's ``files.flags_hash``, which
            # reparses the one TU instead of minting a new build.
            #
            # ``-include`` / ``-imacros`` are dropped too, even though the
            # injected header defines macros: its CONTENT reaches every TU as
            # an include, so the per-TU manifest header hash already covers a
            # change to it.  Only its path is dropped, and a path alone is not
            # a semantic difference.
            for prefix in _PATH_PREFIXES:
                if arg == prefix and i + 1 < len(args):
                    i += 2
                    handled = True
                    break
                elif arg.startswith(prefix) and len(arg) > len(prefix):
                    # Concatenated form: -I/path/to/include
                    i += 1
                    handled = True
                    break

            if handled:
                continue

            for prefix in _PATH_EQ_PREFIXES:
                if arg.startswith(prefix):
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


def check_tu_staleness(
    entry: dict,
    project_root: Path,
    vendor_patterns: list[str],
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
    for h in headers or ():
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

        if _hash_with_cache(p, hash_cache) != h.get("hash", ""):
            return True, current_source_hash

    return False, None


def collect_stale_headers(
    manifest: dict,
    project_root: Path,
    vendor_patterns: list[str],
    *,
    hash_cache: dict[str, str] | None = None,
) -> set[str]:
    """Return the manifest header paths whose on-disk content has changed.

    WHY this pre-pass exists: a header is not a translation unit, so editing
    one never moves the mtime of any TU.  The per-TU mtime fast-path would
    therefore report every dependent TU as unchanged and the header's symbols
    would stay frozen at their first-index state.  Collecting the changed
    headers up front lets the runner mark the dependent TUs for re-parsing.

    Trust rules match :func:`check_tu_staleness` exactly — generated headers,
    headers outside *project_root*, and headers matching *vendor_patterns*
    keep their stored hash and are never re-read.  Re-hashing thousands of
    SDK headers on every run would dominate index time.

    Reads the manifest's ``headers`` map, which already holds each path once,
    so a header shared by 300 TUs is checked once.  Pass *hash_cache* to share
    those hashes with the per-TU staleness checks that follow.

    Returns the paths exactly as stored in the manifest, so the result can be
    fed straight into :func:`tus_affected_by_headers`.
    """
    from .sdk_detect import _path_matches

    stale: set[str] = set()

    for header_path, record in (manifest.get("headers") or {}).items():
        if record.get("generated"):
            continue

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

    Records already in the map are overwritten, because the re-parse just read
    the file and its hash is the newer one.
    """
    entries = manifest.get("entries", [])
    if entry_index >= len(entries):
        return
    entries[entry_index]["source_hash"] = source_hash
    entries[entry_index]["headers"] = headers
    if header_records:
        manifest.setdefault("headers", {}).update(header_records)


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
    vendor_patterns: list[str],
    *,
    new_source_hash: str | None = None,
    hash_cache: dict[str, str] | None = None,
    headers: list[dict] | None = None,
) -> str:
    """Return the manifest entry hash computed from CURRENT disk content.

    When *entry* is stale (headers or source changed), this function reads
    the actual on-disk hashes rather than trusting the stored values.
    Headers matching *vendor_patterns* or outside *project_root* keep their
    stored hashes — only project headers are re-hashed.

    *new_source_hash* overrides ``entry["source_hash"]`` when the source
    file content has also changed.

    *headers* are the entry's resolved records from :func:`tu_headers`.
    """
    from .sdk_detect import _path_matches

    source = new_source_hash if new_source_hash else entry.get("source_hash", "")

    current_headers: list[dict] = []
    for h in headers or ():
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
            current_hash = _hash_with_cache(p, hash_cache)
            current_headers.append({"path": h["path"], "hash": current_hash, "generated": h.get("generated", False)})

    header_hashes = "".join(
        h.get("hash", "")
        for h in sorted(current_headers, key=lambda x: x.get("path", ""))
    )
    return hashlib.sha256(f"{source}|{header_hashes}".encode()).hexdigest()


