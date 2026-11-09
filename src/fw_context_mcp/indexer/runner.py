"""Index runner: parse compile_commands.json, extract symbols, store to SQLite.

WHY a separate runner: the indexing pipeline has four distinct phases
(preparation, parsing, post-processing, optional analysis) that are
orchestrated in sequence.  Keeping them in one module allows the
write-lock and heartbeat mechanisms to span the entire run.

Uses ``indexer/ops.py`` for the shared "parse TU → store symbols" loop so
that runner, reindex_file, and auto-reindex all use the same code path.
"""

from __future__ import annotations

import bisect
import dataclasses
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Collection
from pathlib import Path
from typing import TYPE_CHECKING

from ..config.settings import derive_project_id

# Re-exported so an existing `from .runner import EXIT_SUPERSEDED` keeps
# working.  The constants live in fw_context_mcp.exit_codes because
# mcp/daemon.py needs them and must not pull the indexer — see that module.
from ..exit_codes import (  # noqa: F401 — re-exported
    EXIT_ALREADY_RUNNING,
    EXIT_SUPERSEDED,
    EXIT_TERMINATED,
)
from ..mcp.shared.pid_file import PidFile
from ..path_key import ABSOLUTE_KEY_SQL, canonical_path, index_key
from ..utils import TU_EXTENSIONS, StopRequested
from . import _run_marker
from ._embedding import (
    _build_embeddings,
    _chunk_body,
    _cleanup_orphaned_cc_artifacts,
    _fmt_dur,
)
from ._llm_analysis import ContextChoice, _fetch_callees, _fetch_referencers
from ._postprocess import (
    _build_hotspot_cache,
    _build_pagerank,
    _run_postprocess,
)
from ._unit_processor import (
    UnitDecision,
    _handle_unchanged,
    _parse_unit,
    _process_unit,
    decide_units,
)
from .compile_commands import parse as parse_compile_commands
from .compile_commands import validate_include_files
from .config_hash import DEFAULT_TRANSIENT_DEFINES
from .db import (
    Ownership,
    UnitKey,
    UnitPlaces,
    add_pending_units,
    claim_order,
    clear_embedding_stash,
    clear_pending_units,
    delete_slot_facts_of_unit,
    delete_unit_places,
    drop_fts_triggers,
    files_claimed_before,
    files_without_owner,
    forget_unit,
    get_file_hashes,
    load_ownership,
    load_pending_units,
    load_type_places,
    load_unit_places,
    mark_stale_files,
    open_db,
    rebuild_fts,
    release_files,
    release_ref_producer,
    replace_file_data,
    settle_stale_files,
    skip_form,
    slot_unit_key,
    store_merged_types,
    store_template_places,
    transaction,
    unclaimed,
    upsert_build_config,
    upsert_project,
    write_lock,
)
from .ops import _normalize_file_path

if TYPE_CHECKING:
    from ._usr import TypeDecisions

log = logging.getLogger(__name__)

__all__ = [
    "_build_embeddings",
    "_build_hotspot_cache",
    "_build_pagerank",
    "_chunk_body",
    "_cleanup_orphaned_cc_artifacts",
    "_fetch_callees",
    "_fetch_referencers",
    "_fmt_dur",
    "_run_postprocess",
    "run",
]



# ═══════════════════════════════════════════════════════════════
# SECTION: Embedding building (→ llm_analysis.py)
# ═══════════════════════════════════════════════════════════════


class IndexStopped(StopRequested):
    """This run stopped before its end, and it did not fail.

    The base of the two reasons.  A handler that must unwind a stopped run
    catches this class, and ``exit_code`` gives the status of the process.
    ``label`` starts the line that the CLI prints for it.

    Deliberately outside :data:`SAFE_EXCEPT` — a handler that swallowed this
    as "a step failed, carry on" would resume exactly the run this exists to
    stop.
    """

    exit_code: int = 1
    label: str = "Stopped"


class IndexSuperseded(IndexStopped):
    """Another process took over the index, so this run gave up.

    Not a failure: nothing is wrong with the index, and nothing was written
    that has to be undone.  The run simply stopped because continuing would
    have applied decisions made from a snapshot the other process has since
    invalidated.

    Callers report it as superseded and retry the work from the start.
    """

    exit_code = EXIT_SUPERSEDED
    label = "Superseded"


class IndexTerminated(IndexStopped):
    """A SIGTERM stopped this run, and no other index run sent it.

    A CI timeout, a ``kill`` or the shutdown of the daemon.  Nothing took
    the index over, thus the work is not to be retried: somebody wanted it
    to stop.
    """

    exit_code = EXIT_TERMINATED
    label = "Terminated"


# How often the run writes the manifest while it parses.  The manifest is
# written at the end of the run, and a run that stopped before (a watchdog,
# a manual operation, a crash) left the preliminary manifest of a new build,
# whose entries have no hashes: the next run parsed every unit again, also
# the units that the stopped run had stored.  A checkpoint costs one build of
# the manifest from the units and one write; one each minute is small next to
# a run that parses for minutes or hours, and a stop loses at most one minute.
MANIFEST_CHECKPOINT_S: float = 60.0


def _in_claim_order(
    units: list, project_root: Path, transient_defines: Collection[str],
) -> list:
    """Return *units* in the order in which they claim the shared headers.

    The first unit that walks a header owns its rows (see ``skip_files`` in
    :func:`run`).  A header that a C unit and a C++ unit include, or that two
    units read with different defines, has different rows for each unit.
    Thus the order of the units decides the content of the index.

    WHY a sort and not the order of compile_commands.json: ``bear`` writes
    the units in the order in which a parallel build compiles them.
    Measured on two Mbed projects: 36 of 321 and 44 of 449 units moved
    between two builds.  Two full runs then gave the headers different
    owners, and an incremental run could not repeat a full run.

    The key is ``db.claim_order``: the C++ units first (the reason is in
    ``db/_owners.py``), then the normalized path and the flags hash of the
    listing.  The flags hash keeps the listings of one file in a fixed
    order too: the index holds the rows of the listing that the run parses
    last (see :func:`decide_units`).  The sort is stable, thus two listings
    with the same key keep their order, and those two give the same rows.
    """
    from .config_hash import compute_flags_hash

    def key(unit) -> tuple[int, str, str]:
        path = _normalize_file_path(str(unit.file.resolve()), project_root)
        flags_hash = (
            compute_flags_hash(unit.raw_entry, transient_defines=transient_defines)
            if unit.raw_entry is not None else ""
        )
        return claim_order((path, flags_hash), unit.language)

    return sorted(units, key=key)


def _hand_over(
    decisions: list[UnitDecision],
    start: int,
    paths: Collection[str],
    conn: sqlite3.Connection,
    config_hash: str,
    db_dir: Path,
) -> tuple[set[str], dict[int, set[str]]]:
    """Give each of *paths* to the next listing that claims it in a full run.

    *paths* lost their owner in this run: the owner no longer claims them,
    or it left the build.  In a full run, the first listing at or after
    *start* that includes the file and has a cursor in it claims it.  A
    listing before *start* that includes it does not: in a full run it came
    first, and it did not claim the file, thus it has no cursor in it.

    * The next includer is parsed in this run: it claims the file itself,
      because no other listing holds the file in ``skip_files``.
    * The next includer is unchanged: it must be parsed, thus this function
      sets it, and each listing of its file, to parsed (see the last rule of
      ``decide_units``).
    * No listing includes the file: it has no owner in a full run, thus its
      rows go.

    The includer can have no cursor in the file (an inactive ``#if`` around
    all of it).  Then it does not claim the file, and the listing after it
    must.  Thus the caller gets ``{index of the includer: paths}``, and
    after it parsed that listing, it calls this function again from the
    next index with each path that the listing did not claim.

    Returns the files of the listings set to parsed, and the assignments.
    A run that stops before the heir leaves the file without an owner, and
    the next run gives it to its first includer (``files_without_owner``).
    """
    promoted: set[str] = set()
    assigned: dict[int, set[str]] = {}
    orphans: list[str] = []
    for path in sorted(paths):
        heir = next(
            (j for j in range(start, len(decisions)) if path in decisions[j].includes),
            None,
        )
        if heir is None:
            orphans.append(path)
            continue
        assigned.setdefault(heir, set()).add(path)
        if not decisions[heir].unchanged:
            continue
        # Each listing of the heir file that the loop did not reach yet: a
        # listing before *start* was handled already, and setting it now
        # changes nothing but the record of the decision.
        heir_file = decisions[heir].key[0]
        for k in range(start, len(decisions)):
            if decisions[k].key[0] == heir_file and decisions[k].unchanged:
                decisions[k] = dataclasses.replace(decisions[k], unchanged=False)
        promoted.add(heir_file)
        log.debug("%s lost its owner; %s parses again to claim it", path, heir_file)
    if orphans:
        with write_lock(db_dir, timeout=120.0), transaction(conn):
            ids = [
                row[0]
                for path in orphans
                for row in conn.execute(
                    "SELECT id FROM files WHERE config_hash = ? AND path = ?", (config_hash, path),
                )
            ]
            replace_file_data(conn, config_hash, ids)
            release_files(conn, config_hash, orphans)
        log.info("%d file(s) lost their owner and no later unit includes them: rows removed", len(orphans))
    return promoted, assigned


def _settle_left(
    index: int,
    left_at: dict[int, list[UnitKey]],
    ownership: Ownership,
    decisions: list[UnitDecision],
    awaiting: dict[int, set[str]],
    skip_files: set[str],
    conn: sqlite3.Connection,
    config_hash: str,
    db_dir: Path,
    project_root: Path,
) -> None:
    """At loop position *index*, give away the files of the listings that left before it.

    A file that an earlier listing claimed in this run has an owner.  A
    listing that replaces the one that left (the same file with new flags)
    can sort before or after the old position: before, it claimed the files
    already; after, :func:`_hand_over` finds it as the next includer,
    because the includes of a listing hold its own source file.

    The claims of a listing that left are removed here, and not at the
    start of the run: a run that stops before this position leaves them,
    and the next run gives the files away.
    """
    left = left_at.pop(index, [])
    if not left:
        return
    with write_lock(db_dir, timeout=120.0), transaction(conn):
        for unit_key in left:
            forget_unit(conn, config_hash, unit_key)
            # The files of the places of its rows (_settle_odr_merge).
            delete_unit_places(conn, config_hash, unit_key)
            # The edges of template instances that only this listing found.
            release_ref_producer(conn, config_hash, unit_key)
            # The slot facts of the template instances of this listing.
            delete_slot_facts_of_unit(conn, config_hash, slot_unit_key(unit_key))
    paths = unclaimed(
        (path for unit_key in left for path in ownership.files_of(unit_key)), skip_files, project_root,
    )
    if not paths:
        return
    _, assigned = _hand_over(decisions, index, paths, conn, config_hash, db_dir)
    for heir, heir_paths in assigned.items():
        awaiting.setdefault(heir, set()).update(heir_paths)


def _settle_claims(
    index: int,
    decisions: list[UnitDecision],
    claimed: set[str] | None,
    ownership: Ownership,
    awaiting: dict[int, set[str]],
    skip_files: set[str],
    conn: sqlite3.Connection,
    config_hash: str,
    db_dir: Path,
    project_root: Path,
) -> None:
    """After the parse of listing *index*, find an owner for each file that has none.

    Two kinds of files need one:

    * The files that the listing claimed in the last run and did not claim
      now (*claimed* holds the normalized paths of this parse).  A listing
      whose parse failed keeps its rows, thus it loses nothing:
      *claimed* is None.
    * The files that :func:`_hand_over` gave to this listing and that it did
      not claim (``awaiting[index]``).

    A file that an earlier listing claimed in this run has an owner, thus
    it is not given away again.

    A listing whose parse failed (*claimed* is None) gives nothing away, also
    not its ``awaiting`` files: a later listing could not claim them, and
    when none includes them, :func:`_hand_over` deletes their rows.  A parse
    can fail for a moment (a file that cannot be read now).  The files keep
    their rows without an owner, and the next run, which parses the listing
    again, gives them to their first includer (``files_without_owner``).
    """
    due = awaiting.pop(index, set())
    if claimed is None:
        return
    lost = set(ownership.files_of(decisions[index].key))
    open_paths = unclaimed((lost | due) - claimed, skip_files, project_root)
    if not open_paths:
        return
    _, assigned = _hand_over(decisions, index + 1, open_paths, conn, config_hash, db_dir)
    for heir, heir_paths in assigned.items():
        awaiting.setdefault(heir, set()).update(heir_paths)


def _checkpoint_manifest(
    *,
    manifest: dict | None,
    units: list,
    project_root: Path,
    db_dir: Path,
    compile_commands: Path,
    updated: int,
    tu_headers: dict[str, list[dict]],
    build_dir_patterns: list[str] | None,
    vendor_patterns: list[str],
    config_hash: str,
    scope: list[str] | None,
    reparsed_tus: dict[str, str],
    transient_defines: Collection[str],
    reached: int,
    flags_hashes: list[str],
) -> None:
    """Write the manifest with the entries of the units that the run parsed so far.

    The entries are the ones that the end of the run writes for those
    units (the same updater, in checkpoint mode).  A unit that the run did
    not parse keeps its old entry, thus the next run parses it as this run
    would.  The rows of the parsed units are in the database: each unit is
    one transaction, and the checkpoint runs after the commit.  The
    post-processing is not done for them; the run marker
    (:mod:`_run_marker`) tells the next run.

    *reached* is the count of units that the loop reached: a later listing
    of a file whose earlier listing was parsed keeps its old entry.

    A write that fails is reported and the run goes on: the checkpoint only
    saves work for a run that stops, and the next checkpoint writes again.
    On Windows the rename fails while the MCP server reads the manifest.
    """
    from ._manifest_updater import _update_manifest_after_index

    t0 = time.monotonic()
    try:
        _update_manifest_after_index(
            manifest=manifest,
            units=units,
            project_root=project_root,
            db_dir=db_dir,
            compile_commands=compile_commands,
            updated_count=updated,
            tu_headers=tu_headers,
            build_dir_patterns=build_dir_patterns,
            vendor_patterns=vendor_patterns,
            config_hash=config_hash,
            scope=scope,
            reparsed_tus=reparsed_tus,
            transient_defines=transient_defines,
            checkpoint_reached=reached,
            flags_hashes=flags_hashes,
        )
    except OSError as exc:
        log.warning("manifest checkpoint not written: %s", exc)
        return
    log.info("manifest checkpoint: %d file(s) parsed so far, %.1fs", len(reparsed_tus), time.monotonic() - t0)


def raise_if_superseded(db_dir: Path) -> None:
    """Raise :class:`IndexSuperseded` when another process owns the index.

    Each writer of the project now holds the index lock
    (``indexer._index_owner``), thus while a run holds it no other process
    writes ``reindex.pause``, and a takeover stops the run through SIGTERM.
    The check stays for a marker that a process without the lock wrote: an
    older fw-context during an upgrade.  The MCP server once wrote
    ``<pid>`` to ``reindex.pause`` before a manual ``reindex_file`` or
    ``reset_index``, and an indexing run blocked here until the marker
    cleared and then carried on from the same translation unit — which is
    not safe.  Everything the loop decides with was captured before the
    pause: ``existing_files`` (the snapshot of the rows), the
    decision for each unit, the manifest lookup, the stale-header set, and
    the header ownership built up TU by TU.  After a manual operation those
    all describe an index that no longer exists.

    The damage is concrete.  ``reset_index`` empties the database; a resumed
    run then reads its pre-pause decisions, keeps as unchanged the
    translation units whose rows are gone, and finishes by
    stamping a manifest over a half-built index.  A ``reindex_file`` is
    subtler but the same shape: the manual parse re-assigned header
    ownership, and the resumed run's ``replace_file_data`` deletes rows it
    just wrote.

    So the run gives up instead.  The caller reports it as superseded rather
    than as a failure, and the work is retried from the start — with a fresh
    snapshot — once the marker clears.

    A marker holding our own PID is ignored: a foreground index writes it to
    stop the background reindex, not to stop itself.  A marker whose process
    is gone is ignored too — nothing owns the index any more.
    """
    pause_file = db_dir / "reindex.pause"
    if not PidFile.is_active(pause_file):
        return
    requester_pid = PidFile.read_pid(pause_file)
    if requester_pid is None or requester_pid == os.getpid():
        return
    raise IndexSuperseded(
        f"another process (pid {requester_pid}) took over the index; "
        f"abandoning this run so it can be redone from a fresh snapshot"
    )





def _tus_to_requeue(
    manifest: dict,
    stale_headers: set[str],
    project_root: Path,
) -> frozenset[str]:
    """Return the normalized TU paths that are parsed again whatever their entry says.

    Two sources, and the union of them:

    - a TU that includes a header whose content changed, and
    - a TU whose entry carries ``needs_reparse``.

    The second source must be read even when *stale_headers* is empty.  That
    is the whole point of the mark: another unit's re-parse already wrote the
    current hash into the shared header table, so the difference the header
    pass looks for is gone.  Built inside an ``if stale_headers:`` the set
    stays empty and the mark reaches nothing.
    """
    from .manifest import tus_affected_by_headers

    flagged = {
        e["file"] for e in manifest.get("entries", []) if e.get("needs_reparse")
    }
    from_headers = tus_affected_by_headers(manifest, stale_headers)
    affected = from_headers | flagged
    if not affected:
        return frozenset()

    log.info(
        "%d changed header(s) -> %d TU(s) queued for reparse, "
        "%d more queued by needs_reparse",
        len(stale_headers), len(from_headers), len(flagged - from_headers),
    )
    return frozenset(
        _normalize_file_path(str((project_root / tu_rel).resolve()), project_root)
        for tu_rel in affected
    )


def _files_read_by_c_units(
    units: list,
    unit_headers: dict[str, set[str]],
    manifest_lookup: dict[str, list[dict]] | None,
    project_root: Path,
) -> frozenset[Path]:
    """Give the resolved path of each file that a C or C++ unit of the build reads.

    The assembly pass writes no text for such a file: the C path owns it
    (see ``asm.store_units``).  A unit whose headers this run collected
    gives them in *unit_headers*, over all listings of its file — a parse,
    or the content pass of an unchanged unit.  Another unit gives the
    headers of its manifest entries: its rows and its text are the ones of
    that earlier parse.

    A header path is relative to *project_root* for a file of the project,
    and absolute for a file outside it.  Both forms are resolved here.
    """
    from .manifest import path_resolver

    resolve = path_resolver(project_root)
    read: set[Path] = set()
    for unit in units:
        read.update(_files_read_by_unit(unit, unit_headers, manifest_lookup, project_root, resolve))
    return frozenset(read)


def _tu_key(unit, project_root: Path) -> str:
    """The key of *unit* in ``tu_headers`` and ``reparsed_tus``: its path relative to *project_root*, else absolute."""
    return index_key(unit.file, project_root)


def _files_read_by_unit(
    unit,
    unit_headers: dict[str, set[str]],
    manifest_lookup: dict[str, list[dict]] | None,
    project_root: Path,
    resolve: Callable[[str], Path],
) -> frozenset[Path]:
    """Give the resolved path of the source file of *unit* and of each header that it reads.

    The sources and the path forms are the ones of ``_files_read_by_c_units``.
    """
    source = canonical_path(unit.file)
    key = index_key(source, project_root)
    if key in unit_headers:
        names = list(unit_headers[key])
    else:
        # The manifest keys an entry with the same relative-or-absolute
        # form (``_manifest_updater``, ``"file": tu_rel``).
        entries = (manifest_lookup or {}).get(key, [])
        names = [name for entry in entries for name in entry.get("headers", [])]
    return frozenset({source, *(resolve(name) for name in names)})


def _units_of_changed_places(
    changed_files: frozenset[Path],
    read: list[frozenset[Path]],
    decisions: list,
    unit_places: dict[UnitKey, UnitPlaces],
) -> list[int]:
    """Give the index of each unit whose rows hold a place in *changed_files*, the files of a change of the merge.

    *unit_places* gives the file keys of the places that the rows of each
    listing hold (``odr_unit_places``): only these rows change.  Measured on
    an Mbed build: 6 of the 65 units that read a file of a merged type hold
    a place of it.  A listing with no row has rows of an index without the
    table: each such unit that reads a file of the change (*read*) is parsed
    again, because a unit can hold a place only of a file that it reads.
    """
    from ._usr import file_key

    changed_keys = {file_key(str(path)) for path in changed_files}
    affected: list[int] = []
    for index, files in enumerate(read):
        places = unit_places.get(decisions[index].key)
        if places is None:
            if files & changed_files:
                affected.append(index)
        elif places.files & changed_keys:
            affected.append(index)
    return affected


def _units_of_changed_templates(
    changed: set[str], decisions: list, unit_places: dict[UnitKey, UnitPlaces],
) -> list[int]:
    """Give the index of each unit whose rows took the place of a template of *changed* from the build.

    Such a unit does not read the definition of the template, thus the files
    that it reads do not tell it.  A listing with no row has rows of an
    index without the table: it is parsed again when a place changed.
    """
    if not changed:
        return []
    affected: list[int] = []
    for index, decision in enumerate(decisions):
        places = unit_places.get(decision.key)
        if places is None or places.templates & changed:
            affected.append(index)
    return affected


def _changed_templates(before: TypeDecisions, after: TypeDecisions) -> set[str]:
    """Give the merge key of each template whose place in the build differs between *before* and *after*."""
    return {
        key for key in before.templates.keys() | after.templates.keys()
        if before.templates.get(key) != after.templates.get(key)
    }


def _settle_odr_merge(
    *,
    conn,
    config_hash: str,
    units: list,
    decisions: list,
    previous: TypeDecisions,
    pending: frozenset[str],
    failed: set[str],
    unit_headers: dict[str, set[str]],
    tu_headers: dict[str, list[dict]],
    reparsed_tus: dict[str, str],
    manifest_lookup: dict[str, list[dict]] | None,
    project_root: Path,
    db_path: Path,
    index_refs: bool,
    vendor_patterns: list[str],
    project_patterns: list[str],
    build_dir_patterns: list[str] | None,
) -> int:
    """Decide the places of the C++ types from the definitions of all units, and parse again each unit whose rows change.

    The loop parsed each unit with *previous*, the decisions of the last
    run.  The definitions of this run can give other decisions: a copy of a
    header came or went, or a unit of the other language reads it now
    (``_odr.merged_types_of_build``), or the definition of a class template
    that some units do not define came, went or moved
    (``_odr.template_places_of_build``).  Each unit whose rows hold a place
    in a file of the difference of the merge
    (``_units_of_changed_places``), or took the place of a changed template
    from the build (``_units_of_changed_templates``), is parsed and stored
    again, in the claim order, with the files that the listings before it
    claimed as its skip set: the claims of the run stay as they are.

    The new decisions and the units to parse are written first, in one
    transaction.  A run that stops before it stored each unit leaves them in
    ``odr_pending_units``, and the next run parses them with the new
    decisions (``run``).  A unit that fails stays pending, and so does a
    unit whose parse failed in the loop (*failed*): a later listing took its
    claims of this run, and a parse now would take them back.

    *pending* holds the units that an earlier run left; the loop parsed
    them with *previous*, and each one that did not fail is done when the
    decisions do not change.

    A unit that this function stores gives its headers to *tu_headers* and
    its source hash to *reparsed_tus*, as a parse of the loop does: the
    manifest then holds the hashes of the text of its rows.

    Returns the number of units that this function stored: their rows
    changed, and the post-processing must know it (``_needs_macros``).
    """
    from ._odr import StoredPlace, decide_types, files_of_keys
    from .manifest import path_resolver

    resolve = path_resolver(project_root)

    def places() -> list[StoredPlace]:
        return [
            StoredPlace(resolve(path), key, line, column)
            for path, key, line, column in load_type_places(conn, config_hash)
        ]

    def path_of(index: int) -> str:
        return _normalize_file_path(str(units[index].file.resolve()), project_root)

    read = [_files_read_by_unit(unit, unit_headers, manifest_lookup, project_root, resolve) for unit in units]
    cxx_files = frozenset().union(*(files for unit, files in zip(units, read, strict=True) if unit.language == "cpp"))
    main_files = frozenset(unit.file.resolve() for unit in units)

    def decide(stored: list[StoredPlace], unit_places: dict[UnitKey, UnitPlaces]) -> TypeDecisions:
        keys = frozenset().union(*(places.templates for places in unit_places.values()))
        return decide_types(stored, cxx_files, main_files, keys)

    def units_of_change(
        stored: list[StoredPlace], before: TypeDecisions, after: TypeDecisions,
        unit_places: dict[UnitKey, UnitPlaces],
    ) -> list[int]:
        changed_keys = {key for key, _ in before.merged ^ after.merged}
        by_files = _units_of_changed_places(files_of_keys(stored, changed_keys), read, decisions, unit_places)
        by_templates = _units_of_changed_templates(_changed_templates(before, after), decisions, unit_places)
        return sorted({*by_files, *by_templates})

    stored = places()
    unit_places = load_unit_places(conn, config_hash)
    new = decide(stored, unit_places)
    if new == previous:
        if pending:
            with write_lock(db_path.parent, timeout=120.0), transaction(conn):
                clear_pending_units(conn, config_hash, pending - failed)
        return 0

    affected = units_of_change(stored, previous, new, unit_places)
    log.info(
        "places of C++ types: %d definition(s) merged (was %d), %d template place(s) changed; "
        "%d unit(s) parsed again",
        len(new.merged), len(previous.merged), len(_changed_templates(previous, new)), len(affected),
    )
    with write_lock(db_path.parent, timeout=120.0), transaction(conn):
        store_merged_types(conn, config_hash, new.merged)
        store_template_places(conn, config_hash, new.templates)
        add_pending_units(conn, config_hash, {path_of(index) for index in affected})

    ownership = load_ownership(conn, config_hash)
    existing_files = get_file_hashes(conn, config_hash)
    stored_paths: set[str] = set()
    failed_paths = {path_of(index) for index in affected} & failed
    for index in affected:
        unit, decision, path = units[index], decisions[index], path_of(index)
        if path in failed:
            continue
        skip = frozenset(skip_form(files_claimed_before(ownership, decision.key, unit.language), project_root))
        status, parsed, timing, _ = _parse_unit(unit, index_refs, set(skip), decision.hashes, new)
        headers = None
        if status == "updated":
            with write_lock(db_path.parent, timeout=120.0):
                status, _, _, _, headers = _process_unit(
                    unit, config_hash, project_root, vendor_patterns, project_patterns, index_refs,
                    db_path, existing_files, conn=conn, pre_parsed=parsed, parse_timing=timing,
                    hashes=decision.hashes, build_dir_patterns=build_dir_patterns,
                    skip_files=skip or None, listing=decision.key,
                )
        if status == "updated":
            stored_paths.add(path)
            # The rows come from this parse now, thus the manifest takes the
            # headers and the hashes of the text that it read, as the loop
            # does: a header that changed after the loop and came back must
            # read as a change.
            if headers is not None:
                tu_key = _tu_key(unit, project_root)
                tu_headers[tu_key] = headers
                reparsed_tus[tu_key] = decision.hashes[0]
        else:
            failed_paths.add(path)
    for path in sorted(failed_paths):
        log.warning("places of C++ types: %s stays pending, its parse or store failed", path)
    with write_lock(db_path.parent, timeout=120.0), transaction(conn):
        clear_pending_units(conn, config_hash, (stored_paths | (pending - failed)) - failed_paths)

    # The definitions are the ones of the parse and not of the decisions
    # (``_usr.odr_place``), and a parse looks for the same templates with
    # each decision, thus the second parse gives the same decisions, unless
    # a file changed on the disk after the parse of the loop, or a unit with
    # no row in ``odr_unit_places`` (an index without the table) stored the
    # templates that it looked for.  Then the units of the difference go to
    # the next run: that run parses them with the decisions of the new rows.
    # The changed file alone does not make the index stale (the second
    # parse stored the hash of the new text), thus ``run`` gives the pending
    # units ``needs_reparse`` in the manifest.  The other units read nothing
    # of the difference.
    again_places = places()
    again_unit_places = load_unit_places(conn, config_hash)
    again = decide(again_places, again_unit_places)
    if again != new:
        late = {path_of(index) for index in units_of_change(again_places, new, again, again_unit_places)}
        log.warning(
            "places of C++ types: the second parse changed the decisions (a file changed during the run, "
            "or units of an older index stored their templates); %d unit(s) parsed in the next run", len(late),
        )
        with write_lock(db_path.parent, timeout=120.0), transaction(conn):
            store_merged_types(conn, config_hash, again.merged)
            store_template_places(conn, config_hash, again.templates)
            add_pending_units(conn, config_hash, late)
    return len(stored_paths)


def _store_linker_scripts(
    *,
    conn,
    config_hash: str,
    project_root: Path,
    db_dir: Path,
    compile_commands: Path,
    build_system: str | None,
    variant: str,
    units: list,
    vendor_patterns: list[str],
    project_patterns: list[str],
    force: bool = False,
):
    """Read the linker scripts of this build and store what they define.

    Returns a ``linker_script.LinkerResult``, or None when the build names
    no script.

    **The position of this pass is between the C units and the assembly**,
    and both neighbours depend on it:

    * AFTER C, so ``store_scripts`` can see a definition the compiled code
      makes and leave that name alone.  A linker script and a C file can
      name the same symbol, and the compiled definition is the real one.
    * BEFORE assembly, so ``asm._declare_referenced_only`` finds the name
      already in the index and adds no ``kind="undefined"`` row for it.
      That is what closes slot 0 of a vector table: measured before this,
      the STM32 project held ``_estack`` and the Mbed project held ``__StackTop`` with that kind, one
      per project.

    *build_system* is the ``[build] system`` config key, which wins over
    marker detection for the same reason it does for the vendor patterns: a
    freestanding NCS application reads as a plain CMake project by its
    markers alone.

    The backend answers in one of three ways, see ``_linker_pass``: it does
    not know the link (the rows of an earlier run stay), the link names no
    script (the rows go, because an old map is a wrong map), or the link
    names scripts.  Reading a script the build did not name would be a
    guess, and a wrong memory map is worse than none — see
    ``builders._linker``.

    A run whose inputs did not change since the last full run stores
    nothing: a store deletes and inserts each `ld:` row (the embedding step
    takes their vectors back from the stash, see ``_linker_pass``).
    *force* (``fw-context index --force``) always stores.  That run is the
    documented repair of an old index, and it computes every embedding
    again anyway, thus the skip would save nothing there.
    """
    from . import _linker_pass
    from .build import detect_build_system
    from .builders import link_record, registry

    key = build_system or detect_build_system(project_root)
    builder_cls = registry.get(key) if key else None
    record = link_record(
        builder_cls() if builder_cls else None,
        project_root,
        compile_commands=compile_commands,
        variant=variant,
        units=units,
    )
    state_file = _linker_pass.state_path(db_dir, config_hash)
    state = _linker_pass.read_state(state_file)

    if record is None:
        log.info("linker script: this build does not state its link, thus the stored rows stay")
        # The coverage purge needs the paths of the scripts that the rows
        # came from, or it removes those rows as files no unit covers.
        if state is not None and _linker_pass.rows_match(conn, config_hash, state):
            return _linker_pass.result_from_state(state)
        return None

    if not record.scripts:
        log.info("linker script: the link of this build names no script")
        with write_lock(db_dir, timeout=120.0):
            with transaction(conn, checkpoint=False):
                _linker_pass.store(conn, config_hash, [], project_root, [], [], {})
        _linker_pass.remove_state(state_file)
        return None

    scripts = _linker_pass.parse_scripts(record)
    fingerprint = _linker_pass.fingerprint(
        conn, config_hash, scripts, project_root,
        vendor_patterns, project_patterns, record.defsyms,
    )
    if (
        not force
        and fingerprint is not None
        and state is not None
        and state.fingerprint == fingerprint
        and _linker_pass.rows_match(conn, config_hash, state)
    ):
        log.info(
            "linker script: %d file(s) unchanged since the last run, thus not stored again",
            state.files,
        )
        return _linker_pass.result_from_state(state)

    with write_lock(db_dir, timeout=120.0):
        with transaction(conn, checkpoint=False):
            result = _linker_pass.store(
                conn, config_hash, scripts, project_root,
                vendor_patterns, project_patterns, record.defsyms,
            )
    # After the commit, so that a stop before it gives a full run next time.
    # With no fingerprint the state gets "", which no digest matches: the
    # next run cannot skip, and a "not known" run still gets the paths for
    # the coverage purge, which without them deletes the rows of the scripts.
    _linker_pass.write_state(state_file, _linker_pass.PassState(
        fingerprint=fingerprint or "",
        files=result.files,
        symbols=result.symbols,
        regions=result.regions,
        entry=result.entry,
        paths=sorted(result.paths),
    ))
    log.info(
        "linker script: %d file(s) -> %d symbol(s), %d region(s), entry %s%s%s",
        result.files, result.symbols, result.regions,
        result.entry or "(none)",
        f", {result.skipped_defined} name(s) already defined"
        if result.skipped_defined else "",
        f", {result.defsym_values} value(s) from --defsym"
        if result.defsym_values else "",
    )
    # A refusal nobody can see looks exactly like a script that held nothing
    # worth indexing.
    summary = result.report.summary()
    if summary:
        log.info("linker script refusals: %s", summary)
    return result


def run(
    compile_commands: Path,
    db_path: Path,
    vendor_paths: list[str] | None = None,
    project_paths: list[str] | None = None,
    project_name: str | None = None,
    index_refs: bool = False,
    index_embeddings: bool = False,
    analyze_symbols: bool = False,
    project_root: Path | None = None,
    project_id: str | None = None,
    llm_config=None,
    cache_server_config=None,
    force: bool = False,
    force_llm_since: str | None = None,
    context_choice: ContextChoice | None = None,
    index_macros_expanded: bool = True,
    config_header: str = "",
    build_dir_patterns: list[str] | None = None,
    build_system: str | None = None,
    analyze_vendor: bool = False,
    purge_max_missing_percent: int = 20,
    variant: str = "",
    image: str = "",
    board: str = "",
    build_env: dict[str, str] | None = None,
    defer_fts: bool = False,
    defer_cleanup: bool = False,
    transient_defines: Collection[str] = DEFAULT_TRANSIENT_DEFINES,
    record_git: bool = True,
) -> str:
    """Index a project: parse translation units, extract symbols, and store to SQLite.

    This is the main entry point for indexing a firmware project.  It reads
    ``compile_commands.json``, parses every translation unit with libclang,
    deduplicates symbols across files, and persists the result to the SQLite
    database at ``db_path``.  Optionally builds embeddings, LLM-based symbol
    analysis, file-level summaries, and the method override graph.

    All files from all included headers are indexed unconditionally.
    ``is_project`` is computed per-symbol from *vendor_paths* (auto-detected
    SDK dirs + user-configured) and *project_paths* (user-configured overrides).

    Args:
        compile_commands: Path to ``compile_commands.json`` (generated by
            ``bear``, ``compiledb``, or ``CMAKE_EXPORT_COMPILE_COMMANDS``).
        db_path: Path to the SQLite database file that will store the index.
        vendor_paths: Additional vendor/SDK directory patterns (additive to
            auto-detection).  Paths matching these get ``is_project=0``.
        project_paths: Manual project directory patterns that override
            auto-detection.  Paths matching these get ``is_project=1``.
            Use absolute paths for directories outside the project root.
        project_name: Human-readable name for the project (defaults to the
            directory name of ``project_root``).
        index_refs: When True, extract call-graph references (call, ref,
            member, and indirect edges) during AST traversal.
        index_embeddings: When True, generate vector embeddings for all
            definition symbols using Ollama after indexing.
        analyze_symbols: When True, generate structured LLM analysis
            (summary, inputs, outputs) for project symbols via Ollama.
        project_root: Root directory of the project.  Used to resolve relative
            paths and derive defaults.  Defaults to the parent of
            ``compile_commands``.
        build_system: The ``[build] system`` config key of this project.  It
            decides WHICH builder answers for the vendor patterns.  Pass it
            whenever the config has it: the config and the markers can
            disagree, and a freestanding NCS application reads as a CMake
            project by its markers alone.  With None the markers decide.
        project_id: Unique project identifier (auto-derived from
            ``project_root`` when not provided).
        llm_config: Configuration dataclass for Ollama connection (URL,
            model names, enabled flag).  Required when any ``index_*`` or
            ``analyze_*`` option is enabled.
        force_llm_since: ``fw-context index --force-llm``: the time of the
            command (UTC, ``YYYY-MM-DD HH:MM:SS``).  The LLM analysis of
            each symbol is made again, and an analysis of the caches from
            before it does not apply.  ``force`` alone keeps the analyses.
        context_choice: The context of the LLM analysis from the measured
            speed, one for the builds of a run
            (``_llm_analysis.ContextChoice``).  None: ``num_ctx``.
        transient_defines: ``[index] transient_defines`` — the ``-D`` macros
            that the config_hash and the flags hash of each unit ignore.  The
            CLI passes the value of the config.  The default is the default
            of the config, for a caller without a config.
        record_git: Whether this run records the git context of the tree
            (``description`` and HEAD) on its builds.  False keeps the
            recorded values.  The CLI passes False after a checkout that the
            run did not build: compile_commands.json then still belongs to
            the old checkout, and the recorded HEAD is what tells each
            answer that the index is of the old tree.

    Returns:
        The ``config_hash`` string — a content-addressable fingerprint of the
        ``compile_commands.json`` used for staleness detection.
    """
    # The canonical root (path_key): each key of the run is relative to it.
    if project_root is None:
        project_root = canonical_path(compile_commands.parent)
    else:
        project_root = canonical_path(project_root)
    # One set for the whole run: each unit tests each argument against it.
    transient_defines = frozenset(transient_defines)

    # Normalize patterns without % wildcard to match subdirectories.  The
    # vendor set waits for the translation units — see below.
    from .sdk_detect import _build_sdk_excludes, _normalize_patterns

    project_patterns_list = _normalize_patterns(list(project_paths), project_root) if project_paths else []

    if project_id is None:
        project_id = derive_project_id(project_root)
    name = project_name or project_root.name
    # Collect git context for build description (branch + last tag) and
    # HEAD, at the start: the content comes from the tree of this moment.
    from .git_context import get_git_description, read_head

    git_description: str | None = get_git_description(project_root) if record_git else None
    git_head: str | None = read_head(project_root) if record_git else None
    # Clean up compile_commands.<hash>.json artifacts left by a previous
    # interrupted run before computing a new config_hash.
    _cleanup_orphaned_cc_artifacts(db_path, project_id)
    # Parse compile_commands.json to discover translation units.  Must
    # happen before config_hash computation so the manifest can be built
    # from the actual TU list.
    all_units = list(parse_compile_commands(compile_commands))

    # libclang reads a translation unit as C or C++.  Given an assembly
    # unit, it parses that unit as C and gets no cursor and one error
    # diagnostic per file — measured on a Zephyr startup.S: 0 cursors,
    # "error: expected identifier or '('".  So libclang never gets them.
    #
    # They are NOT dropped.  The assembly pass further down preprocesses
    # them, stores their symbols, and logs its own "assembly:" summary.
    # This line reports only the split, and it must say so: the wording
    # was "skipping %d of %d TUs", which reads as data loss to anyone who
    # watches the run — the units are indexed, by a different reader.
    #
    # The line stays, because no other line reports these units before the
    # assembly pass ends.  The new-file scan cannot report them either: a
    # suffix outside TU_EXTENSIONS is not even a candidate there, so the
    # units are invisible from that direction.  Five of the seven test
    # projects compile assembly.
    units = _in_claim_order(
        [u for u in all_units if u.file.suffix in TU_EXTENSIONS],
        project_root, transient_defines,
    )
    asm_units = [u for u in all_units if u.file.suffix not in TU_EXTENSIONS]
    if asm_units:
        from collections import Counter
        by_ext = Counter(u.file.suffix for u in asm_units)
        log.info(
            "%d of %d TUs go to the assembly indexer "
            "(libclang cannot read them as C or C++): %s",
            len(asm_units), len(all_units),
            ", ".join(f"{n}x {ext or '(no suffix)'}" for ext, n in by_ext.most_common()),
        )
    log.info("TUs to index: %d", len(units))

    # ── The effective vendor set, computed HERE and not earlier ──
    # A builder may read the compiler flags: Zephyr takes ZEPHYR_BASE and
    # WEST_TOPDIR from -fmacro-prefix-map, which names the roots of the build
    # that is indexed rather than the shell that runs fw-context.  The units
    # carry those flags, so the set cannot be built before they are parsed.
    # Its first use is the header staleness pre-pass further down.
    vendor_patterns = list(
        _build_sdk_excludes(project_root, build_system, units=units)
    )
    if vendor_paths:
        vendor_patterns.extend(_normalize_patterns(vendor_paths, project_root))
    log.info(
        "vendor patterns for %s: %s",
        variant or project_root.name,
        ", ".join(vendor_patterns) or "none",
    )

    # Determine config_hash from manifest.json.  The manifest captures the
    # full structural build identity (files, directories, compiler flags) —
    # more comprehensive than hashing compile_commands.json alone.
    #
    # When a manifest exists, compute the expected structural hash from the
    # current units and compare.  If they match, reuse the stored hash so
    # _update_manifest_after_index() can do an incremental header update.
    # If they differ (compile_commands.json changed), rebuild the preliminary
    # manifest.  When no manifest exists yet (first index), build one.
    # Inject user-configured config header when the build system doesn't emit
    # -include flags (custom builds, legacy Makefiles, etc.).
    if config_header:
        ch = project_root / config_header
        if not ch.exists():
            raise RuntimeError(
                f"Configured config_header not found: {ch}\nCheck [index] config_header in .fw-context/config.toml"
            )
        ch_abs = ch.resolve()
        for unit in units:
            unit.clang_args.extend(("-include", str(ch_abs)))

    from .manifest import build_preliminary, build_scope, compute_structural_hash
    from .manifest import load as load_manifest

    scope = build_scope(variant, image, build_env)

    expected_hash = compute_structural_hash(
        compile_commands,
        project_root,
        units,
        build_dir_patterns,
        project_id=project_id,
        scope=scope,
        transient_defines=transient_defines,
    )
    manifest = load_manifest(db_path.parent, expected_hash)
    if manifest is not None:
        config_hash = manifest.get("config_hash", expected_hash)
    else:
        config_hash = build_preliminary(
            compile_commands,
            db_path.parent,
            project_root,
            units,
            build_dir_patterns,
            project_id=project_id,
            scope=scope,
            transient_defines=transient_defines,
        )
        # Reload manifest from disk — build_preliminary may have written a
        # preliminary (empty source_hash) manifest.  The in-memory manifest
        # must reflect what _update_manifest_after_index will find on disk
        # (degraded or fresh), so the early-return guard can detect
        # preliminary entries and fall through to regeneration.
        manifest = load_manifest(db_path.parent, config_hash)

    # WHY heartbeat: the index subprocess can deadlock (libclang hang, disk
    # full, NFS stall).  Without a heartbeat, the watchdog has no way to
    # distinguish "slow but alive" from "deadlocked."  This daemon thread
    # writes a timestamp to a known file every 30s — if the file stops
    # updating, the watchdog kills and restarts the process.
    _hb_log = os.environ.get("FW_CONTEXT_HEARTBEAT_LOG")
    if _hb_log:
        _hb_stop = threading.Event()

        def _heartbeat() -> None:
            while not _hb_stop.wait(30.0):
                try:
                    with open(_hb_log, "a", encoding="utf-8") as f:
                        f.write(f"{time.strftime('%H:%M:%S')} heartbeat\n")
                except (OSError, ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error):
                    pass  # non-fatal — continue

        _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
        _hb_thread.start()

    log.info("", extra={"phase": f"Indexing {name} ({config_hash[:12]})"})
    log.info("project=%s project_id=%s config_hash=%s", name, project_id, config_hash[:12])

    conn = open_db(db_path)
    # Determine initial manifest verification for this config.
    # New config_hash → 'indexing' (hidden from MCP queries until complete).
    # Existing config → preserve the previous value (in-place update).
    old_row = conn.execute(
        "SELECT manifest_verification FROM build_configs WHERE config_hash=?",
        (config_hash,),
    ).fetchone()
    if old_row is None:
        initial_manifest_verification = "indexing"
    else:
        initial_manifest_verification = old_row["manifest_verification"]

    # Resolve analyze_vendor: CLI flag takes precedence, config falls back.
    # Only meaningful when analyze_symbols is True — otherwise analysis doesn't
    # run and the flag is irrelevant (storing True would mislead get_project_status).
    if analyze_symbols:
        _analyze_vendor = analyze_vendor or (llm_config is not None and llm_config.analyze_vendor)
    else:
        _analyze_vendor = False

    # Serialize with any concurrent writer (background reindex, daemon)
    with write_lock(db_path.parent, timeout=120.0):
        with transaction(conn):
            upsert_project(conn, project_id, name, str(project_root))
            upsert_build_config(
                conn,
                config_hash,
                project_id,
                str(compile_commands),
                # None keeps the recorded git context of the CONTENT.  This
                # write happens before a single translation unit is read, so
                # stamping the current branch here would mark the index as
                # coming from a tree it has not been built from — and a run
                # that fails leaves exactly that.  `_run_postprocess` writes
                # the real value at the end, when the content matches it.
                description=None,
                manifest_verification=initial_manifest_verification,
                analyze_vendor=int(_analyze_vendor),
                variant=variant,
                image=image,
                board=board,
            )

    # Validate that all -include/-imacros referenced files exist BEFORE
    # starting any libclang parsing.  A missing build-generated config
    # header (e.g. BUILD/.../mbed_config.h) would otherwise cause libclang
    # to fail for every TU that includes the SDK, producing a partial index
    # and wasting ~seconds per TU on doomed parse attempts.
    for unit in units:
        validate_include_files(unit.clang_args)

    existing_files = get_file_hashes(conn, config_hash)

    # The entries of the manifest by file, for the decision about each unit.
    # A list, because compile_commands.json can list one file more than once
    # with other flags, and each listing has its own entry.
    # *manifest* was loaded above (before config_hash computation) — reuse it.
    manifest_lookup: dict[str, list[dict]] | None = None
    if manifest is not None:
        manifest_lookup = {}
        for e in manifest.get("entries", []):
            manifest_lookup.setdefault(e.get("file", ""), []).append(e)
    # The entries hold header paths; their hashes live in this shared map.
    # Both travel together into the staleness checks.
    manifest_header_table: dict[str, dict] = (
        dict(manifest.get("headers") or {}) if manifest is not None else {}
    )

    # ── Header staleness pre-pass ──
    # A header is not a translation unit, so editing one leaves the source
    # hash of every dependent TU untouched.  Hash the headers once here and
    # mark every TU that includes a changed one for re-parsing.  The check of
    # each entry in decide_units reads the same hashes (one cache), and gives
    # the same answer for each listing.  This pass logs how many units the
    # headers sent.
    #
    # ALL dependent TUs are marked, not a subset: symbols from a shared header
    # are claimed by whichever TU stores them first, and a TU that keeps its
    # old rows would block the fresh ones via the ON CONFLICT(config_hash,
    # usr) guard in insert_symbols_batch.
    #
    # header_hash_cache is shared with the per-TU staleness checks below, so
    # each project header is read and hashed exactly once per run.
    header_hash_cache: dict[str, str] = {}
    header_stale_tus: frozenset[str] = frozenset()
    if manifest is not None:
        from .manifest import collect_stale_headers

        stale_headers = collect_stale_headers(
            manifest, project_root, hash_cache=header_hash_cache
        )
        header_stale_tus = _tus_to_requeue(manifest, stale_headers, project_root)

    # ── The places of C++ types (indexer/_odr.py) ──
    # The decisions that the rows of the build follow (the merged
    # definitions, the places of the class templates): each unit of this
    # run parses with them, thus its rows agree with the rows of the units
    # that the run does not parse.  A run that stopped after a change of the
    # decisions left units to parse again (_settle_odr_merge): they are
    # parsed now.
    from ._odr import load_type_decisions

    odr_decisions = load_type_decisions(conn, config_hash)
    odr_pending = load_pending_units(conn, config_hash)
    if odr_pending:
        log.info("%d unit(s) parsed again for the places of C++ types", len(odr_pending))
        header_stale_tus = header_stale_tus | odr_pending

    # Drop FTS5 content-sync triggers before bulk indexing — each symbol
    # INSERT/DELETE/UPDATE would otherwise pay per-row FTS index overhead
    # (~2× write I/O).  The FTS table is rebuilt from scratch in one pass
    # after all TUs are stored.
    # If a previous run crashed after drop_fts_triggers() but before
    # rebuild_fts(), the triggers are missing — repair now so FTS stays
    # consistent during this run.
    missing_triggers = False
    for trigger_name in ("symbols_ai", "files_ai", "macros_ai"):
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger_name,)
        )
        if cur.fetchone() is None:
            missing_triggers = True
    if missing_triggers and not defer_fts:
        log.info("FTS5 triggers missing (possibly from a crashed run) — rebuilding FTS first")
        rebuild_fts(conn)
    drop_fts_triggers(conn)

    total_syms = 0
    total_refs = 0
    skipped = 0
    unchanged = 0
    updated = 0
    acc_parse = 0.0
    acc_lock = 0.0
    acc_write = 0.0
    content_filled = 0
    # Collect headers during tokenization for incremental manifest update.
    # Maps file_path → list of {path, hash, generated} header dicts.
    tu_headers: dict[str, list[dict]] = {}
    # The header paths that each C or C++ unit of this run read (a unit of
    # libclang, not of the assembly pass), over ALL listings of its file.
    # `tu_headers` keeps the list of the last listing only.  Two readers
    # must know each header that such a unit reads: the assembly pass, and
    # the merge of the types (`_settle_odr_merge`), which takes the files
    # that the C++ units read from it.
    c_unit_headers: dict[str, set[str]] = {}
    # TUs actually re-parsed in this run, with the source hash that the
    # decision read before the parse.  The manifest may only take fresh
    # header hashes from these — an entry refreshed for a TU that kept its
    # previous symbols would erase the evidence that the index is behind —
    # and it takes this source hash, not one read at the end of the run.
    reparsed_tus: dict[str, str] = {}
    # The normalized path of each unit whose parse or store failed in this
    # run: it keeps its old rows, thus a unit of odr_pending stays pending.
    failed_tus: set[str] = set()
    t0 = time.monotonic()

    # Is any PROJECT file still missing its ifdef-filtered content?  One query
    # per run instead of one per TU.  While a backfill is pending, unchanged
    # TUs keep running the content pass so it can complete; once every project
    # file has content they skip it, along with the libclang parse behind it.
    #
    # WHY project files only (relative key = inside project_root, see
    # path_key.index_key; ABSOLUTE_KEY_SQL also knows the Windows form C:/): out-of-tree system headers routinely end up with
    # an empty content column — e.g. libstdc++ headers whose cursors were all
    # claimed by an earlier TU.  Counting those would pin the flag to True
    # forever on any C++ project and the pass would never be skipped.
    content_backfill_needed = (
        conn.execute(
            "SELECT 1 FROM files WHERE config_hash = ? AND content = '' "
            f"AND NOT {ABSOLUTE_KEY_SQL} LIMIT 1",
            (config_hash,),
        ).fetchone()
        is not None
    )

    # skip_files accumulates resolved paths of the files that earlier
    # listings claimed in this run.  A parsed listing adds what its parse
    # walked; an unchanged listing adds the files that it claimed in the
    # last run (db/_owners.py), at its own position in the loop.  Thus the
    # owner of a file is the owner of a full run.  A changed header is not
    # kept from its includers by this: each listing that includes it is
    # parsed (header_stale_tus), and the first one claims it again.
    skip_files: set[str] = set()

    # An earlier run of this build that stored rows and stopped: its units
    # read as unchanged now (the checkpoint below), and the post-processing
    # must still run for them — see _run_marker.
    pending_run = _run_marker.is_pending(db_path.parent, config_hash)

    # Every unit is decided before the first parse: hashes only, no write.
    # A file whose listing changed is parsed in each of its listings, and
    # that needs the answer for all of them — see decide_units.
    #
    # The files that each listing claimed in the last runs (db/_owners.py).
    # A listing that the run does not parse adds its files to skip_files at
    # its position in the loop, thus a parsed listing claims only what it
    # claims in a full run.
    ownership = load_ownership(conn, config_hash)
    # A stash of embeddings that a stopped run left (see _run_postprocess):
    # this run stashes the vectors of its own re-parse.
    with write_lock(db_path.parent, timeout=120.0), transaction(conn):
        clear_embedding_stash(conn)
    decisions = decide_units(
        units, project_root, existing_files, manifest_lookup,
        header_stale_tus,
        force=force, hash_cache=header_hash_cache, header_table=manifest_header_table,
        transient_defines=transient_defines, claims_recorded=ownership.recorded,
    )
    flags_hashes = [decision.hashes[1] for decision in decisions]

    # A listing that left the build gives its files away at its old position
    # in the loop: a listing before that position that the run parses can
    # claim them first, as in a full run.  The decisions are in the order of
    # _in_claim_order, thus a bisect of their claim_order finds the position.
    # A listing that left has no unit: its recorded language gives its key.
    # {index of a listing: files that it must claim in this run}
    awaiting: dict[int, set[str]] = {}
    # {index of the loop: the listings that left the build before it}
    left_at: dict[int, list[UnitKey]] = {}
    left = sorted(
        ownership.recorded - {decision.key for decision in decisions},
        key=lambda unit_key: claim_order(unit_key, ownership.languages[unit_key]),
    )
    if left:
        order = [claim_order(decision.key, unit.language) for decision, unit in zip(decisions, units, strict=True)]
        for unit_key in left:
            position = bisect.bisect_left(order, claim_order(unit_key, ownership.languages[unit_key]))
            left_at.setdefault(position, []).append(unit_key)
        log.info("%d listing(s) left the build; their files go to the next includer", len(left))

    # A file with rows and no owner: a run that stopped after a listing lost
    # the file and before the next includer claimed it, or rows from a
    # version without the owner tables.  The first includer gets it, as in
    # a full run.  Only files that a listing includes: the rows of the
    # assembly pass and of the linker scripts have no owner by design.
    included = frozenset().union(*(decision.includes for decision in decisions))
    without_owner = files_without_owner(conn, config_hash)
    unowned = without_owner & included
    if unowned:
        _, assigned = _hand_over(decisions, 0, unowned, conn, config_hash, db_path.parent)
        for heir, heir_paths in assigned.items():
            awaiting.setdefault(heir, set()).update(heir_paths)
        log.info("%d file(s) had rows and no owner; their first includer claims them", len(unowned))

    # The rows that this run replaces or removes give way to the new rows of
    # the run (db._symbols.mark_stale_files): the files that the last run
    # gave to each listing that this run parses again, and to each listing
    # that left the build, and the files that no listing owns (an index of
    # a version without the claims, a run that stopped before the heir).  A
    # definition that moved to a file of an other unit was lost before, by
    # the order of the units.
    mark_stale_files(conn, config_hash, sorted(
        {path for decision in decisions if not decision.unchanged for path in ownership.files_of(decision.key)}
        | {path for unit_key in left for path in ownership.files_of(unit_key)}
        | without_owner
    ))
    conn.commit()

    # This run writes the marker before its first row, and removes it after
    # its own post-processing.
    #
    # A listing that left the build can set a later listing to parsed in the
    # loop (_settle_left), thus its presence writes the marker too.  The
    # other hand-overs happen after a parse, and the marker is there then.
    marker_written = pending_run or bool(left_at) or any(not decision.unchanged for decision in decisions)
    if marker_written:
        _run_marker.mark_pending(db_path.parent, config_hash)
    last_checkpoint = time.monotonic()

    log.info("", extra={"phase": f"Parsing ({len(units)} TUs)"})

    # Single sequential loop with per-TU write lock for responsiveness.
    # The lock is acquired and released for each translation unit, thus
    # each store is short.  The manual operations of the project
    # (reindex_file, reset_index, analyze) hold the index lock, thus they
    # do not interleave with the run: they take a background run over or
    # wait for it.
    #
    # Libclang parsing runs OUTSIDE the write lock — a single TU can take
    # seconds to minutes on large codebases (mbed-os, Zephyr).  Holding the
    # lock during parsing starves other indexers (bg reindex, concurrent
    # ``fw-context index --force``) and causes WriteLockTimeout errors.

    for i, unit in enumerate(units):
        raise_if_superseded(db_path.parent)  # Give up if another process took over
        fname = unit.file.name
        processed = i + 1
        _settle_left(
            i, left_at, ownership, decisions, awaiting, skip_files,
            conn, config_hash, db_path.parent, project_root,
        )

        # ── Phase 1: libclang parse of a changed unit (no lock) ──
        # WHY outside lock: libclang parsing can take seconds to minutes per
        # TU on large codebases (mbed-os, Zephyr).  Holding the write lock
        # during parsing starves other indexers (bg reindex, concurrent
        # ``fw-context index --force``) and causes WriteLockTimeout errors.
        decision = decisions[i]
        hashes = decision.hashes
        if decision.unchanged:
            check_status, parsed_data, parse_timing = "unchanged", None, None
        else:
            check_status, parsed_data, parse_timing, _ = _parse_unit(
                unit, index_refs, skip_files, hashes, odr_decisions,
            )

        # Snapshot the skip set BEFORE folding in this TU's own files.
        # Content fill must skip only headers already processed by EARLIER
        # TUs.  This TU's headers are filled now — their active lines come
        # from the AST walk, because get_tokens() returns tokens only for
        # the main file.  A post-update skip set would skip this TU's
        # headers entirely and leave files.content empty.
        skip_before = frozenset(skip_files) if skip_files else None

        if parsed_data is not None and hasattr(parsed_data, 'newly_seen_files'):
            skip_files.update(parsed_data.newly_seen_files)
        elif decision.unchanged:
            # Not parsed: the files that it claimed in the last run stay its
            # own, at the same position as in a full run.
            skip_files.update(skip_form(ownership.files_of(decision.key), project_root))

        if check_status == "unchanged":
            result = _handle_unchanged(
                unit, hashes, decision.mtime, conn, config_hash, project_root,
                build_dir_patterns, db_path, existing_files,
                skip_files=skip_before,
                content_backfill_needed=content_backfill_needed,
            )
            unchanged += 1
            content_filled += result["content_filled"]
            if result["headers"]:
                tu_headers.update(result["headers"])
                for key, headers in result["headers"].items():
                    c_unit_headers.setdefault(key, set()).update(h["path"] for h in headers)
            log.info("[%d/%d] %s: %s", processed, len(units), fname, result["status"])
            continue

        if check_status == "skipped":
            skipped += 1
            failed_tus.add(_normalize_file_path(str(unit.file.resolve()), project_root))
            log.info("[%d/%d] %s: skipped", processed, len(units), fname)
            # The unit keeps its rows, and the files that it got from a listing
            # that lost them keep theirs: the marks end for the rest of the run
            # (db._symbols.mark_stale_files).
            settle_stale_files(conn, config_hash)
            conn.commit()
            _settle_claims(
                i, decisions, None, ownership, awaiting, skip_files,
                conn, config_hash, db_path.parent, project_root,
            )
            continue

        # ── Phase 2: DB store (inside lock) ──
        with write_lock(db_path.parent, timeout=120.0):
            status, syms, refs, timing, tu_headers_list = _process_unit(
                unit,
                config_hash,
                project_root,
                vendor_patterns,
                project_patterns_list,
                index_refs,
                db_path,
                existing_files,
                conn=conn,
                pre_parsed=parsed_data,
                parse_timing=parse_timing,
                hashes=hashes,
                build_dir_patterns=build_dir_patterns,
                skip_files=skip_before,
                listing=decision.key,
            )
            if status == "updated":
                updated += 1
                total_syms += syms
                total_refs += refs
                acc_parse += timing[0]
                acc_lock += timing[1]
                acc_write += timing[2]
                tu_key = _tu_key(unit, project_root)
                # Only a re-parsed TU may refresh its manifest entry — its
                # symbols now match the headers it just read.
                reparsed_tus[tu_key] = hashes[0]
                # An empty list too: a TU with no recorded header still gets
                # its entry, with its source hash.  Without one, the entry of
                # the preliminary manifest stayed, with an empty source hash,
                # and the TU read as stale after each run.  Measured on one
                # project: 63 of 215 TUs.  None is a TU that failed to parse.
                if tu_headers_list is not None:
                    tu_headers[tu_key] = tu_headers_list
                    c_unit_headers.setdefault(tu_key, set()).update(h["path"] for h in tu_headers_list)
                log.info(
                    "[%d/%d] %s: %d syms, %d refs, %.1fs",
                    processed,
                    len(units),
                    fname,
                    syms,
                    refs,
                    sum(timing),
                )
            else:
                skipped += 1
                failed_tus.add(_normalize_file_path(str(unit.file.resolve()), project_root))
                log.info("[%d/%d] %s: skipped", processed, len(units), fname)
                # The store failed and rolled back: the unit keeps its rows,
                # also the rows of the files that it claimed or got in this
                # run.  The marks end for the rest of the run.
                settle_stale_files(conn, config_hash)
                conn.commit()

        claimed = (
            {_normalize_file_path(path, project_root) for path in parsed_data.newly_seen_files}
            if status == "updated" and parsed_data is not None else None
        )
        _settle_claims(
            i, decisions, claimed, ownership, awaiting, skip_files,
            conn, config_hash, db_path.parent, project_root,
        )

        if updated and time.monotonic() - last_checkpoint >= MANIFEST_CHECKPOINT_S:
            # A manual operation that took the index wrote its own manifest;
            # a checkpoint after it would overwrite that.  Give up first.
            raise_if_superseded(db_path.parent)
            _checkpoint_manifest(
                manifest=manifest, units=units, project_root=project_root,
                db_dir=db_path.parent, compile_commands=compile_commands, updated=updated,
                tu_headers=tu_headers, build_dir_patterns=build_dir_patterns,
                vendor_patterns=vendor_patterns, config_hash=config_hash, scope=scope,
                reparsed_tus=reparsed_tus, transient_defines=transient_defines,
                reached=processed, flags_hashes=flags_hashes,
            )
            last_checkpoint = time.monotonic()


    # The listings that left after the last listing: no listing comes after
    # them, thus their files that nobody claimed in this run lose their rows.
    _settle_left(
        len(units), left_at, ownership, decisions, awaiting, skip_files,
        conn, config_hash, db_path.parent, project_root,
    )
    # The loop of the C and C++ units is over: a row of a file that no unit
    # parsed again is the row that the post-processing keeps or removes, as
    # before.  The marks are for the C and C++ units only, thus they end
    # before the linker scripts and the assembly.  These passes clear their
    # own rows first, and they must keep the usual order of the upsert:
    # two assembly units that read one shared file give one USR, and a
    # `.weak` row must not replace a `.global` row.
    settle_stale_files(conn, config_hash)
    conn.commit()

    # ── The places of C++ types, after each C and C++ row (indexer/_odr.py) ──
    # Before the linker scripts, the assembly and the post-processing: they
    # read the rows of the C and C++ units.  A unit that it stores again
    # counts as updated: the post-processing must see that rows changed,
    # also in a run whose loop changed none.
    updated += _settle_odr_merge(
        conn=conn, config_hash=config_hash, units=units, decisions=decisions,
        previous=odr_decisions, pending=odr_pending, failed=failed_tus,
        unit_headers=c_unit_headers, tu_headers=tu_headers, reparsed_tus=reparsed_tus,
        manifest_lookup=manifest_lookup, project_root=project_root,
        db_path=db_path, index_refs=index_refs, vendor_patterns=vendor_patterns,
        project_patterns=project_patterns_list, build_dir_patterns=build_dir_patterns,
    )
    # A unit that stays pending holds rows that do not follow the decisions,
    # and its entry can be fresh: the loop parsed it.  Only the manifest
    # tells the status and the daemon that a run must come, thus the update
    # of the manifest marks it (``_postprocess._step_update_manifest``).
    odr_left = load_pending_units(conn, config_hash)

    # ── Linker scripts, between the C units and the assembly ──
    # The position is deliberate, and both neighbours depend on it — see
    # _store_linker_scripts.
    linker = _store_linker_scripts(
        conn=conn,
        config_hash=config_hash,
        project_root=project_root,
        db_dir=db_path.parent,
        compile_commands=compile_commands,
        build_system=build_system,
        variant=variant,
        units=all_units,
        vendor_patterns=vendor_patterns,
        project_patterns=project_patterns_list,
        force=force,
    )

    asm = None

    # ── Assembly, after every other unit ──
    # The ordering is deliberate and the steps built on this one depend on
    # it: deciding whether a vector slot is handled means asking whether a
    # strong definition exists, and nothing can answer that until the C and
    # C++ units are stored.
    if asm_units:
        from .asm import store_units as _store_asm

        c_read = _files_read_by_c_units(units, c_unit_headers, manifest_lookup, project_root)
        with write_lock(db_path.parent, timeout=120.0):
            with transaction(conn, checkpoint=False):
                asm = _store_asm(
                    conn, config_hash, asm_units, project_root, build_dir_patterns,
                    vendor_patterns, project_patterns_list, c_read=c_read,
                )
        log.info(
            "assembly: %d unit(s) -> %d file(s), %d symbol(s), "
            "%d vector edge(s), %d alias edge(s), %d call edge(s), %d unresolved vector(s)%s",
            len(asm_units), asm.files, asm.symbols, asm.vectors, asm.aliases,
            asm.calls, asm.unresolved,
            f", {asm.failed} could not be preprocessed" if asm.failed else "",
        )
        # A separate line, and only when a unit held a `.macro` at all.
        # A refused macro leaves its symbols out of the index, and a
        # refusal nobody can see looks exactly like a file that held
        # nothing worth indexing.
        macro_summary = asm.macros.summary()
        if macro_summary:
            log.info("assembly macros: %s", macro_summary)

    # ── Post-processing ──
    # Every path no libclang unit covers travels in one set.  The coverage
    # purge counts a file it cannot see in the units as missing, and the
    # assembly pass lost every row it wrote to exactly that purge before
    # its paths were threaded through.  A linker script is invisible the
    # same way — it is an input to the linker, not a compilation unit.
    uncovered_paths = set(asm.paths) if asm is not None else set()
    if linker is not None:
        uncovered_paths |= linker.paths
    _run_postprocess(
        asm_paths=uncovered_paths,
        conn=conn,
        config_hash=config_hash,
        project_root=project_root,
        db_dir=db_path.parent,
        units=units,
        tu_headers=tu_headers,
        manifest=manifest,
        compile_commands=compile_commands,
        updated=updated,
        build_dir_patterns=build_dir_patterns,
        vendor_patterns=vendor_patterns,
        project_patterns_list=project_patterns_list,
        project_id=project_id,
        git_description=git_description,
        git_head=git_head,
        index_refs=index_refs,
        index_embeddings=index_embeddings,
        index_macros_expanded=index_macros_expanded,
        analyze_symbols=analyze_symbols,
        analyze_vendor=_analyze_vendor,
        llm_config=llm_config,
        cache_server_config=cache_server_config,
        force=force,
        force_llm_since=force_llm_since,
        context_choice=context_choice,
        purge_max_missing_percent=purge_max_missing_percent,
        variant=variant,
        image=image,
        board=board,
        scope=scope,
        defer_fts=defer_fts,
        defer_cleanup=defer_cleanup,
        header_hash_cache=header_hash_cache,
        reparsed_tus=reparsed_tus,
        transient_defines=transient_defines,
        pending_run=pending_run,
        flags_hashes=flags_hashes,
        odr_pending=odr_left,
        failed_units=frozenset(failed_tus),
    )
    _run_marker.clear_pending(db_path.parent, config_hash)

    elapsed = time.monotonic() - t0
    log.info("", extra={"phase": f"Done — {total_syms} symbols, {total_refs} refs, {_fmt_dur(elapsed)}"})
    log.info("%d updated, %d unchanged, %d skipped  config_hash=%s", updated, unchanged, skipped, config_hash[:12])
    return config_hash
