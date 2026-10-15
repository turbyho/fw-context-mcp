"""Maintenance MCP tools — index health checks, LLM configuration,
project listing, index reset, and single-file reindexing.

These are diagnostic and administrative tools distinct from the
search and code-navigation tools in other handler modules.  They
do not query the symbol index for code-analysis purposes; instead
they inspect index metadata, configure runtime behavior, and
manage the index lifecycle.

Public MCP handlers in this module:
    get_project_status       — mandatory first call: index health and the builds
    get_build_info           — the detail of one build (memory map, defines)
    check_ollama             — LLM backend availability and model status
    check_dependencies       — read-only dependency audit (structured results)
    configure_llm            — write per-developer LLM settings to local.toml
    reindex_file             — re-parse a single source file into the index
    reindex_file_impl        — same as above with an ``--analysis`` equivalent
    reset_index              — delete the symbol database
    list_projects            — enumerate all indexed projects
    get_project_info         — look up a project by UUID4 in the global registry

Each handler is read-only except where noted in its docstring.
The ``Field(description=…)`` strings on public signatures are tool
registration metadata — they must not be modified casually."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...config import derive_project_id
from ...config import load as load_config
from ...config.settings import Config
from ...indexer.autobuild import AutobuildState, build_advice, build_missing_reason
from ...indexer.autobuild import clear_problem as clear_build_problem
from ...indexer.autobuild import project_state as project_autobuild_state
from ...indexer.autobuild import read_problem as read_build_problem
from ...indexer.build import checked_compile_commands, user_database
from ...indexer.compile_commands import command_line_defines
from ...indexer.compile_commands import parse as parse_cc
from ...indexer.db import (
    CURRENT_ROW_FORMAT,
    CURRENT_SCHEMA_VERSION,
    DatabaseCorruptionError,
    WriteLockTimeout,
    compute_analysis_coverage,
    count_refs,
    get_active_config,
    get_all_projects,
    get_db_schema_version,
    get_entry_point,
    get_memory_regions,
    make_analysis_summary,
    open_db,
    row_format_effect,
    row_format_is_newer,
    row_format_is_older,
    transaction,
)
from ...indexer.git_context import head_moved, stored_head
from ...llm.ollama import check_setup
from ...utils import HEADER_EXTENSIONS, resolve_project_root
from ..background import read_reindex_progress, read_reindex_state
from ..shared.context import (
    _db_path,
    _detect_build_system,
    _is_stale,
    _quick_open_readonly,
    _resolve_context,
    get_executor,
    invalidate_executor,
)
from ..shared.index_state import RUN_STATES, WAIT_ADVICE, index_state, manifest_reason
from ..shared.stale import (
    _check_header_staleness,
    _count_modified_files,
    build_label,
    find_unindexed_sources,
    latest_builds,
    stored_row_format,
)
from ..shared.variants import IMAGE_DESCRIPTION, VARIANT_DESCRIPTION

log = logging.getLogger(__name__)


#: The command of the degraded `_warning` when `reindex_needed` is true and
#: no index run is in progress or scheduled: each reason then names its own
#: action.  The message of a project of several builds looks for it.
_REASON_COMMAND = "the command that reindex_reasons names"


def _after_the_run(action: str) -> str:
    """Return the advice to wait for the index run, and then to do *action*.

    The text starts after the article ("an"), thus a caller puts it in the
    middle of a sentence or at its start.  WHY a reason gives this text when
    an index run is in progress or scheduled: the command of the reason is
    often the command that runs, and a caller that follows it asks for the
    same work a second time.  The run can also leave the reason standing,
    for example a run that does not build, thus the action stays in the
    text.

    The text holds no dash and no colon, because a reason can put it after
    either.
    """
    return f"index run is in progress or scheduled; wait for it, and when this reason stays after the run, {action}"


def _new_source_advice(auto_state: AutobuildState, bg_running: bool, rebuild: str) -> str:
    """Return the advice for a source file that compile_commands.json does not cover.

    *rebuild* is the action that makes compile_commands.json again
    (``autobuild.build_advice``).  A build that failed is not an outcome
    here: the failed run writes its own text (``autobuild.read_problem``),
    which comes first.

    WHY *bg_running*: "fw-context builds them on its own" is true only when
    an index run comes.  The daemon schedules one for a new file
    (`daemon._staleness_check`), and without a daemon nothing does.  For
    another backend a run in progress gets the same advice as a checkout:
    wait, and act when the reason stays.  The state of a query also asks to
    wait during a run.

    The text comes after a dash in the reason, and after a colon in the
    message, thus it holds no dash and no colon of its own.
    """
    if auto_state is AutobuildState.WILL_BUILD:
        if bg_running:
            return "fw-context builds them on its own; no command is needed"
        return (
            "fw-context builds them in the next index run, and no index run is in "
            "progress or scheduled; run `fw-context index`"
        )
    run = _after_the_run(rebuild)
    if auto_state is AutobuildState.USER_DATABASE:
        # `rebuild` says that fw-context does not build that file.
        return f"an {run}" if bg_running else rebuild
    if bg_running:
        # The fact comes first: the condition of the action is the run.
        return f"fw-context does not build them on its own. An {run}"
    return f"fw-context does not build them on its own; {rebuild}"


def _autobuild_state(root: Path, proj_cfg, new_sources: list[str]) -> AutobuildState:
    """Say what will happen to *new_sources*, for the message and the status.

    Answers UNSUPPORTED when the build system cannot be determined: without
    a backend nothing can build on its own, and that is the answer that
    tells the caller to act rather than to wait.
    """
    if not new_sources:
        return AutobuildState.WILL_BUILD  # unused — no file to report
    return project_autobuild_state(root, proj_cfg)


def _with_action(fact: str, action: str, bg_running: bool) -> str:
    """Return the sentence *fact* and then *action*, or the advice to wait for the run.

    *action* is an instruction without a subject, in lower case, as
    ``autobuild.build_advice`` gives it.  See :func:`_after_the_run` for the
    reason to wait.
    """
    if bg_running:
        return f"{fact}. An {_after_the_run(action)}."
    return f"{fact}. {action[0].upper()}{action[1:]}."


def _build_missing_advice(reason: str, root: Path, proj_cfg, bg_running: bool) -> str:
    """Give *reason* with the command that brings the build back.

    The same two cases as the refusal of ``fw-context index``: a build that
    fw-context can run, and a build that only the IDE can run (a stub, see
    ``build.can_run_build``).  ``--build`` and not a plain reindex: a plain
    run builds the missing build too (``_build_if_missing`` in the CLI), but
    ``--build`` says what happens, and it is the same command that the
    refusal of a run names.

    WHY *bg_running*: an index run with ``--build`` removes the database of
    the build before the build writes it again.  Thus during the run of the
    daemon after a checkout, the build is missing, and the command of this
    reason is the command that runs.
    """
    from ...indexer.build import can_run_build

    system = proj_cfg.build.system or _detect_build_system(root)
    if can_run_build(proj_cfg.build, system):
        return _with_action(reason, "run `fw-context index --build`", bg_running)
    return _with_action(
        f"{reason}, and fw-context cannot run the build of this project",
        "run the build outside of fw-context (for example in the IDE), then run `fw-context index`",
        bg_running,
    )


def _compile_commands_reason(stale_reason: str | None, cfg, root: Path, proj_cfg, bg_running: bool) -> str:
    """Give the reason of a compile_commands.json that changed after the index of the build *cfg*.

    *stale_reason* is the code of ``readiness._is_stale``.  For a missing
    file the caller gives the reason only when the build check
    (``_build_missing_advice``) does not cover the file, for example for
    the file of a build variant.  WHY a sentence and not the code: each
    other reason is a sentence that names its action, and a caller cannot
    act on a bare code.

    A missing file needs the action that makes it again
    (``autobuild.build_advice``): a plain run reads the same missing file.
    A newer file needs only a plain run, which reads it.
    """
    if stale_reason == "compile_commands_missing":
        return _with_action(
            f"compile_commands.json {cfg['compile_commands_path']} does not exist",
            build_advice(root, proj_cfg),
            bg_running,
        )
    if stale_reason == "compile_commands_newer":
        fact = "compile_commands.json changed after the last index run"
    elif stale_reason:
        # `is_compile_commands_stale` could not compare the two times, and
        # its code holds the error.  A run repairs a time of the index that
        # does not parse, because it writes the time again.  A run does not
        # repair an error of the file system (a file that this process
        # cannot read), and the error in the text names that cause.
        fact = f"fw-context cannot compare compile_commands.json with the index ({stale_reason})"
    else:
        fact = "compile_commands.json changed"
    return _with_action(fact, "run `fw-context index`", bg_running)


def _background_build_allowed(root: Path, proj_cfg: Config) -> bool:
    """Say whether fw-context may run a build of this project on its own.

    The same question the background index run asks
    (`cli._index._may_build_in_background`), and the same answer:
    `background_build_safe` refuses where a build of fw-context would reach
    the object files of the build of the user, and a `[build] command` never
    builds on its own.  Asked here only to word the advice — a project the
    daemon will handle should not read like one that needs a command typed.

    Any failure to decide answers no, which keeps the command in the advice.
    """
    from ...indexer.build import detect_build_system
    from ...indexer.builders import background_build_safe, registry
    from ...utils import SAFE_EXCEPT

    if proj_cfg.build.command or user_database(root, proj_cfg) is not None:
        return False
    try:
        key = proj_cfg.build.system or detect_build_system(root)
        builder_cls = registry.get(key) if key else None
        if builder_cls is None:
            return False
        return background_build_safe(builder_cls(), proj_cfg.build)
    except SAFE_EXCEPT:
        return False


def _with_version_state(result: dict) -> dict:
    """Add the version facts that do not depend on the index to *result*.

    One wrapper around every return path of :func:`_project_state`,
    because an upgrade of the package concerns the process and not the
    project: the caller must read it also when the project has no index.
    A restart reason of the index (a newer row format) and a restart reason
    of the package can apply together, thus the second one joins the text
    and does not replace it.
    """
    from ...config.settings import load_update_settings
    from ...version_check import restart_reason, update_notice

    reason = restart_reason()
    if reason:
        prior = result.get("client_restart_reason")
        result["client_restart_required"] = True
        result["client_restart_reason"] = f"{prior} {reason}" if prior else reason
        # A row-format reason already put the same action at the front of
        # index_message.  One action needs one sentence there.
        if "index_message" in result and not prior:
            result["index_message"] = (
                "Restart the LLM client — this server runs a different fw-context than "
                f"the installed one. Queries work meanwhile. {result['index_message']}"
            )
    notice = update_notice(load_update_settings().check)
    if notice:
        result["update_notice"] = notice
    return result


@dataclass(frozen=True)
class _IndexTarget:
    """The index of one project, as the two status tools find it."""

    root: Path
    cfg: Config
    project_id: str
    db_path: Path
    # "reindexing" for an index run of the daemon or of the operator, and
    # for a `reindex_file` or a `reset_index` that holds `write.lock`.
    # "pending" for work of the daemon that is not complete.  See
    # `read_reindex_state`.
    # Read once for each call: a run is a fact of the project, and not of a
    # build.
    reindex_state: str | None

    @property
    def bg_running(self) -> bool:
        """Return True when an index run is active or the daemon has work that comes.

        WHY "pending" counts: the daemon starts its run, and the run holds
        the index only after the interpreter started and the config loaded.
        For a PlatformIO project, the build system also lists the variants
        before the run holds the index.  That can take some seconds.  A
        status that said "no run" in that time sent the caller to start a
        second index run of the same work.
        """
        return self.reindex_state is not None


def _index_target(project_root: str | None) -> _IndexTarget | dict:
    """Find the index of the project, or give the answer for a project without one.

    WHY the project ID comes from the config here and not from
    ``derive_project_id``: that call raises for a project that is not
    initialized, and these two tools report that state instead.  The other
    tools fail fast in that case, and these diagnostic tools explain it.
    """
    root = resolve_project_root(project_root)
    fw_cfg = load_config(root)
    if not fw_cfg.project.id:
        return {
            "status": "not_initialized",
            "project_root": str(root),
            "index_message": (
                f"Project at {root} is not initialized. "
                "Run `fw-context init` via bash to create a project ID and config, "
                "then call get_project_status() again."
            ),
        }
    db_path = fw_cfg.index.db_dir / fw_cfg.project.id / "index.db"
    if not db_path.exists():
        return {
            "status": "no_index",
            "project_root": str(root),
            "project_id": fw_cfg.project.id,
            "index_message": (
                f"No symbol index found for {root}. "
                "Run `fw-context index <path/to/compile_commands.json>` via bash "
                "to build the index, then call get_project_status() again."
            ),
        }
    # Read BEFORE the database: while a run is in progress the cache of
    # modified files can hold the counts from before it started.
    return _IndexTarget(root, fw_cfg, derive_project_id(root), db_path, read_reindex_state(root))


@dataclass(frozen=True)
class _ProjectFacts:
    """The facts of the project that the health of each build reads, computed one time for a call."""

    # Source files that no build covers, and what happens to them.
    new_sources: list[str]
    auto_state: AutobuildState
    # The text of the marker of the last index run that stopped without an
    # index, after the check below; "" without one.
    build_problem: str
    # True when that marker says that the build is missing: a missing
    # database of a build is then the same fact, and the marker names it
    # with the command of the run.
    marker_build_missing: bool
    # The text of the file HEAD of the tree (`git_context.read_head`): a
    # branch, or the commit of a detached HEAD; "" without git.
    live_head: str


def _project_facts(conn, target: _IndexTarget) -> _ProjectFacts:
    """Return the facts of the project, from the build that ``fw-context index`` checks.

    WHY one time and against that build, and not in the health of each
    build:

    * The marker of a failed run is one file for the project, and the index
      run writes it for the build that it checks (``active_build``: the
      application of a build of several images).  A check of another build
      found that build present and removed the marker while the
      application was still missing, and every query answer then lost its
      ``build_warning``.
    * ``find_unindexed_sources`` reads the TU extensions and the build
      directories of the build that it starts from.  From the bootloader, a
      new ``.cpp`` of the application was no candidate.
    * HEAD is the same for each build.
    """
    from ...indexer.git_context import read_head
    from ..shared.variants import checked_build

    checked = checked_build(conn, target.project_id, target.cfg, target.root)
    if checked is None:
        # Both callers read a completed build first, thus only a reindex
        # that removed it in between gets here.  No build, no facts.
        return _ProjectFacts([], AutobuildState.WILL_BUILD, "", False, "")
    db_dir = target.db_path.parent
    cc_path = Path(checked["compile_commands_path"])
    problem = read_build_problem(db_dir)
    if problem is not None and problem.build_missing:
        checked_cc = checked_compile_commands(target.root, target.cfg, cc_path)
        if checked_cc is not None and not build_missing_reason(checked_cc):
            # The check ran and found the build (built outside of
            # fw-context): the text is out of date for every answer, not
            # only here.  Without a check the marker stays.
            clear_build_problem(db_dir)
            problem = None
    new_sources = find_unindexed_sources(conn, checked["config_hash"], target.root, cc_path)
    return _ProjectFacts(
        new_sources=new_sources,
        auto_state=_autobuild_state(target.root, target.cfg, new_sources),
        build_problem=problem.text if problem is not None else "",
        marker_build_missing=problem is not None and problem.build_missing,
        live_head=read_head(target.root),
    )


def _analysis_of(conn, config_hash: str) -> tuple[dict, bool]:
    """Return the LLM-analysis coverage of a build, and the stored ``analyze_vendor``.

    Split by project vs vendor, so the LLM can tell intentionally skipped
    vendor symbols apart from project symbols that still need analysis.
    """
    coverage = compute_analysis_coverage(conn, config_hash)
    # Exclude the skip:* sentinels (skip:toolarge, skip:unparseable) —
    # they are not model names and must never surface as analysis.model.
    analysis_model_row = conn.execute(
        """SELECT a.model FROM llm_analysis a
           JOIN symbols s ON s.id = a.symbol_id
           WHERE s.config_hash = ?
             AND a.model NOT LIKE 'skip:%' LIMIT 1""",
        (config_hash,),
    ).fetchone()
    model = analysis_model_row["model"] if analysis_model_row else None
    # The stored analyze_vendor: what was actually indexed.  False for a
    # database from before the column.
    try:
        stored_analyze_vendor = bool(
            conn.execute(
                "SELECT analyze_vendor FROM build_configs WHERE config_hash = ?",
                (config_hash,),
            ).fetchone()["analyze_vendor"]
        )
    except sqlite3.OperationalError:
        stored_analyze_vendor = False
    return make_analysis_summary(coverage, stored_analyze_vendor, model), stored_analyze_vendor


def _build_health(
    conn,
    target: _IndexTarget,
    cfg,
    *,
    fast: bool,
    hash_cache: dict[str, str],
    facts: _ProjectFacts,
    detail: bool = True,
) -> dict:
    """Return the health of the build *cfg* (a ``build_configs`` row).

    Runs under the executor lock on the single shared connection, and must
    not open its own connection.

    *detail* False leaves out what only a report of this one build gives:
    ``reference_count`` and ``analysis`` (None), and the analysis part of
    ``index_message``.  WHY: the list of the builds of a project shows
    neither, and each costs queries over every symbol of the build.

    The result holds the fields of the build that the two status tools
    report, and two internal ones: ``_restart_reason`` (the index holds a
    NEWER row format than this process reads) and ``_restart_format``.
    ``index_message`` comes without the two leading notes that concern the
    project, a run in progress and a restart of the client:
    :func:`_with_project_notes` adds them one time.

    *hash_cache* is one cache of file hashes for the whole call: the count,
    the header check and the other builds read the same files of the SDK.
    Measured on a project of 7 builds, the header check took 3.6 s with one
    cache for each build.
    """
    root = target.root
    proj_cfg = target.cfg
    bg_running = target.bg_running
    config_hash = cfg["config_hash"]
    sym_count = conn.execute("SELECT COUNT(*) FROM symbols WHERE config_hash=?", (config_hash,)).fetchone()[0]
    file_count = conn.execute("SELECT COUNT(*) FROM files WHERE config_hash=?", (config_hash,)).fetchone()[0]
    ref_count = count_refs(conn, config_hash) if detail else None
    manifest_verification = cfg["manifest_verification"] or "none"
    # The scan runs in fast mode too.  Without it the status tool answered
    # "ready" while search_code on the same index warned about the same
    # file, and the caller had no way to tell which reading held.  It costs
    # about 41 ms on a project of 1900 files, against the 749 ms that the
    # header check below already spends.
    #
    # use_cache=False on purpose: the cache validates itself against
    # MAX(mtime) of the files table, which only a reindex moves, thus a
    # cached answer would miss the edit that just happened.
    modified_count = _count_modified_files(conn, config_hash, root, use_cache=False, hash_cache=hash_cache)
    # The header check is NOT cheap: it hashes every header of every
    # translation unit, which is 22,693 files and 749 ms on the Mbed
    # project.  `fast` only gives it a cache, and does not skip it.
    if manifest_verification == "full":
        header_affected_tus, _ = _check_header_staleness(
            conn, config_hash, root, use_cache=fast, hash_cache=hash_cache,
        )
    else:
        header_affected_tus = 0
    db_schema_ver = get_db_schema_version(conn)

    analysis, stored_analyze_vendor = _analysis_of(conn, config_hash) if detail else (None, False)

    cc_changed, stale_reason = _is_stale(cfg, cfg["compile_commands_path"])
    schema_old = db_schema_ver < CURRENT_SCHEMA_VERSION
    # Does the stored text still mean what this version reads it to mean?
    # The schema version above cannot answer that: it hashes the column
    # SET, thus it moves only when a column appears or goes, while the
    # content of a column can change under an unchanged name.  See
    # CURRENT_ROW_FORMAT.
    # The test is ordinal, and not a plain inequality, because the two
    # directions need opposite answers.  An OLDER format asks for a
    # reindex, and the run repairs it.  A NEWER one cannot be repaired by
    # any command: the index is right and THIS PROCESS is the old reader,
    # thus the indexer writes the same new format again.
    stored_format = stored_row_format(cfg)
    row_format_old = row_format_is_older(stored_format)
    row_format_newer = row_format_is_newer(stored_format)

    # ── The build: what the last run found, and what is there now ──
    # The marker is the text of the last index run that stopped without
    # an index (``autobuild.record_problem``); each query tool answer
    # carries it.  `_project_facts` reads it one time for the project.
    # The check of the build runs here too, because the status tool is
    # the first call of a session: a build that went away after the last
    # run has no marker yet.
    #
    # The check reads the file that an index run checks
    # (``checked_compile_commands``), and only when the index came from
    # it: an index of an explicit file, or of build variants, gets no
    # check, as `fw-context index` does none, and its advice `--build`
    # would replace the explicit file.  A build of fw-context keeps its
    # database in its output directory, thus a missing database is a
    # missing build, and it gets the advice of one.
    checked_cc = checked_compile_commands(root, proj_cfg, Path(cfg["compile_commands_path"]))
    build_missing = build_missing_reason(checked_cc) if checked_cc is not None else ""
    if facts.marker_build_missing:
        # The marker says the same, with the command of the run.
        build_missing = ""
    build_problem = facts.build_problem
    new_sources, auto_state = facts.new_sources, facts.auto_state

    # A source file that the build system never saw has no translation
    # unit, and only a build adds it.  It forces a reindex only when
    # fw-context does not build it on its own.  When it does, the next
    # index run builds it, thus the status below is "reindexing" when no
    # other reason forces a reindex, and no reason names `--build`.
    blocked_sources = bool(new_sources) and auto_state is not AutobuildState.WILL_BUILD

    # ── The tree has another checkout than this index ──
    # A plain reindex cannot fix it.
    # compile_commands.json is a build artifact of the checkout it was
    # generated on: it carries its file list AND its compiler flags.
    # Measured on the Mbed project, a switch from 4.15.3 to 4.15.1 left two
    # generated zcbor sources listed in that file and absent from the
    # tree, and a reindex reads the same file again.  A branch, a tag and a
    # commit all count (`git_context.head_moved`), with HEAD read one time
    # for the project.
    indexed_checkout, live_checkout = head_moved(
        stored_head(cfg),
        str(cfg["description"] or "") if "description" in cfg.keys() else "",
        facts.live_head,
        root,
    )
    checkout_moved = bool(indexed_checkout)
    # Modified source files are handled per query and do NOT force a
    # reindex.
    needs_reindex = bool(
        cc_changed or schema_old or row_format_old or blocked_sources or checkout_moved
        or build_problem or build_missing
    )
    # A missing compile_commands.json needs the action that makes it again
    # (`_compile_commands_reason`), and a plain run reads the same missing
    # file.
    cc_missing = cc_changed and stale_reason == "compile_commands_missing"
    # Asked once, because the call reaches the build backend and is not
    # free.  Only the checkout reason below reads it.
    bg_build_allowed = checkout_moved and _background_build_allowed(root, proj_cfg)
    # The action that makes compile_commands.json again: one function for
    # the status tools and the query answers, thus the two name the same
    # command.  The function reads the config and the markers of the build
    # system, thus the code asks it only when a reason needs it.
    rebuild = build_advice(root, proj_cfg) if checkout_moved or new_sources else ""

    reindex_reasons: list[str] = []
    if build_problem:
        reindex_reasons.append(build_problem)
    if build_missing:
        reindex_reasons.append(_build_missing_advice(build_missing, root, proj_cfg, bg_running))
    checkout_text = ""
    if checkout_moved:
        # Never a plain reindex: that would reuse compile_commands.json,
        # which belongs to the checkout the index was built on, with its file
        # list and its compiler flags.  `rebuild` names the build that
        # makes it again: `--build`, or the build of the user.
        #
        # The daemon runs it on its own where the backend allows a
        # background build, so the advice says both — a checkout on a
        # project nobody is watching gets no burst, and then the manual
        # command is the only thing that fixes it.  A run already under way
        # changes the advice: the command named here is the command that
        # runs, thus naming it asks for the same work a second time.
        #
        # WHY the wording stays careful: `bg_running` says that AN INDEX
        # RUN exists, and nothing more.  The run belongs to the daemon or
        # to the operator, and its arguments are not readable from here.
        # A run that does not build leaves this reason standing.
        #
        # A database of the user, or a build that fw-context cannot run (a
        # stub such as STM32CubeIDE): the user makes the file again, and the
        # next run records the checkout when the file is newer than the
        # checkout (`cli._index._records_checkout`).
        if user_database(root, proj_cfg) is not None:
            branch_advice = f"; {rebuild}"
        elif bg_running:
            branch_advice = (
                "; an index run is in progress or scheduled — wait for it rather than "
                f"start a second one, and when this reason stays after the run, {rebuild}"
            )
        elif bg_build_allowed:
            branch_advice = (
                "; a background reindex regenerates it with --build when "
                "the watcher sees a change, or run "
                "`fw-context index --build` now"
            )
        else:
            branch_advice = f"; {rebuild}"
        checkout_text = (
            f"checkout changed: indexed at {indexed_checkout!r}, "
            f"now at {live_checkout!r} — compile_commands.json belongs to "
            f"the old checkout" + branch_advice
        )
        reindex_reasons.append(checkout_text)
    # The schema and the row format name their command as each other
    # reason does, thus a caller that reads only `reindex_reasons` finds the
    # action of each one.  During a run the two tell the caller to wait:
    # each index run stamps the schema, and an index run of this build
    # writes its rows again.  A run of another build leaves the row format
    # old, and then the reason stays after the run with its command.
    if schema_old:
        reindex_reasons.append(_with_action(
            f"schema_mismatch: {db_schema_ver} < {CURRENT_SCHEMA_VERSION}",
            "run `fw-context index`",
            bg_running,
        ))
    if row_format_old:
        # Names the effect, not only the value: the caller has to know
        # what the answers it gets now are worth.  `row_format_effect`
        # picks the wording by the gap — the faults of the generations are
        # opposite, thus one fixed sentence would be false for every gap
        # but one.
        reindex_reasons.append(_with_action(
            f"row_format_mismatch: {stored_format or '(none)'} != "
            f"{CURRENT_ROW_FORMAT} — {row_format_effect(stored_format)}",
            "run `fw-context index`",
            bg_running,
        ))
    # A missing database of a build of fw-context is the missing build,
    # and the build reason above (or the marker of the run that found it)
    # already names it with its command.
    database_gone = checked_cc is not None and not checked_cc.exists()
    compile_commands_text = ""
    if cc_changed and not database_gone:
        compile_commands_text = _compile_commands_reason(stale_reason, cfg, root, proj_cfg, bg_running)
        reindex_reasons.append(compile_commands_text)
    if new_sources:
        listed = ", ".join(new_sources[:3])
        more = f" and {len(new_sources) - 3} more" if len(new_sources) > 3 else ""
        # "No command is needed" is false while the last run failed: the
        # build that adds them is the one that must be repaired first.
        advice = (
            "repair the build first, see the first reason"
            if build_problem or build_missing else _new_source_advice(auto_state, bg_running, rebuild)
        )
        reindex_reasons.append(
            f"{len(new_sources)} source file(s) not in compile_commands.json "
            f"({listed}{more}) — {advice}"
        )

    # One status that drives the decision of the caller.
    # Priority: reindex_needed > reindexing > ready.
    #
    # A file that fw-context builds on its own counts as "reindexing" and
    # not as "reindex_needed": the next index run builds it, and no other
    # command is necessary.  `reindex_state` below tells if that run comes:
    # without a run it is "not_scheduled", and the advice names
    # `fw-context index`.
    if needs_reindex:
        status = "reindex_needed"
    elif bg_running or (new_sources and auto_state is AutobuildState.WILL_BUILD):
        status = "reindexing"
    else:
        status = "ready"

    # ── Is the index of THIS build current, and what updates it ──
    # `status` mixes the build with the run of the project: "reindexing"
    # also when the run concerns another build.  `reindex_state` answers
    # for this build alone, see `index_state`.  A header that changed and
    # a unit that failed to parse count too: the rows of those units are
    # older than the files.  So does an index without full header
    # tracking: it cannot see a changed header, and the warning below asks
    # for a run.
    behind = bool(
        needs_reindex or modified_count or header_affected_tus or new_sources
        or manifest_verification != "full"
    )
    reindex_state = index_state(behind, run_state=target.reindex_state, failed=bool(build_problem))

    if status == "ready" and modified_count:
        # The index is structurally sound, thus the status stays "ready"
        # and every query keeps working.  But files on disk have moved past
        # it, and the per-query warnings will say so — this message must
        # not contradict them by claiming full freshness.
        index_message = (
            f"Index is usable ({sym_count} symbols), but {modified_count} file(s) "
            f"changed since the last index run. Results about those files can be "
            f"out of date — run `fw-context index`."
        )
    elif status == "ready" and reindex_state is not None:
        # Stale headers, or an index without full header tracking: the
        # notes below name them.  "Fully up to date" would contradict
        # `reindex_state`.
        index_message = f"Index is usable ({sym_count} symbols), but it is not current"
    elif status == "ready":
        index_message = f"Index is fully up to date ({sym_count} symbols)"
    elif status == "reindexing":
        if new_sources and auto_state is AutobuildState.WILL_BUILD:
            index_message = (
                f"{len(new_sources)} source file(s) are not in compile_commands.json "
                f"(first: {new_sources[0]}). {_new_source_advice(auto_state, bg_running, rebuild)}. "
                f"Queries work on existing data meanwhile."
            )
        elif modified_count:
            # Not "accurate": the answers about these files come from the
            # last run until the run ends.  `_with_project_notes` puts the
            # advice to wait in front.
            index_message = (
                f"Index is usable ({sym_count} symbols), but {modified_count} file(s) "
                f"changed since the last index run, and an index run updates them."
            )
        elif reindex_state is None:
            index_message = (
                "Index is usable — background reindex in progress or scheduled, and the "
                "index of this build is current. All queries return accurate results."
            )
        else:
            # Stale headers, or an index without full header tracking: the
            # run updates them, and the advice to wait comes from
            # `_with_project_notes`.
            index_message = "Index is usable — background reindex in progress or scheduled."
    elif build_problem or build_missing:
        # First among the reasons: without the build each unit gets no
        # answer of its compiler, and no other repair helps before it.
        index_message = (
            (build_problem or _build_missing_advice(build_missing, root, proj_cfg, bg_running))
            + " Queries still work on the data of the last finished index run."
        )
    elif blocked_sources:
        # Checked before the other reindex reasons: only a build repairs
        # this one, and a message that says plain `index` would send the
        # caller in a circle.  Only files that fw-context does not build on
        # its own: the next run builds the others, and a checkout next to
        # them needs its own message below.
        index_message = (
            f"{len(new_sources)} source file(s) are not in compile_commands.json "
            f"(first: {new_sources[0]}). A plain reindex cannot see them: "
            f"{_new_source_advice(auto_state, bg_running, rebuild)}. Queries still work on existing data."
        )
    elif checkout_moved:
        # Before the schema and the database: a plain `fw-context index`
        # reads compile_commands.json of the old checkout again.  The
        # reason names the build that makes it again.
        index_message = f"The {checkout_text}. Queries still work on existing data."
    elif cc_missing and compile_commands_text:
        # Before the schema and the row format: their plain
        # `fw-context index` reads the same missing file.  The reason names
        # the action that makes it again.
        index_message = f"{compile_commands_text} Queries still work on existing data."
    elif schema_old and cc_changed:
        index_message = (
            f"Schema version mismatch ({db_schema_ver} < {CURRENT_SCHEMA_VERSION}) "
            f"and compile_commands.json changed. Run fw-context index. "
            f"Queries still work on existing data."
        )
    elif schema_old:
        index_message = (
            f"Schema version mismatch ({db_schema_ver} < {CURRENT_SCHEMA_VERSION}). "
            f"Run fw-context index. Queries still work on existing data."
        )
    elif row_format_old:
        index_message = (
            "The rows of the index have an older meaning than this version reads "
            "(row_format_mismatch) — run fw-context index. Queries still work on existing data."
        )
    elif compile_commands_text:
        # The reason text, thus the message and the reason name one action.
        index_message = f"{compile_commands_text} Queries still work on existing data."
    else:
        index_message = "Compile commands changed — run fw-context index. Queries still work on existing data."

    # When manifest verification is not full, warn the LLM.
    warning = None
    if manifest_verification != "full":
        if bg_running:
            # The same advice as `reindex_state`: the build is not current,
            # and a run comes.  A text that said "continue" here gave the
            # caller two opposite actions in one answer.
            warning = (
                f"The index is not complete: {manifest_reason(manifest_verification)}. "
                "An index run is in progress or scheduled, and it updates the "
                f"index when it ends. {WAIT_ADVICE}"
            )
            index_message += (
                f" — manifest verification: {manifest_verification} (reindex in progress or scheduled — wait for completion)"
            )
        else:
            # Each reason that sets `reindex_needed` names its own action,
            # and a plain 'fw-context index' next to it is not always
            # sufficient: a reason can name what to repair before the run,
            # or that the run needs `--build`.  The run that the reason
            # names also makes the index complete.
            if needs_reindex:
                command = _REASON_COMMAND
                tracking = "the run that reindex_reasons names makes the index complete"
            else:
                command = "'fw-context index'"
                tracking = "run 'fw-context index' to make the index complete"
            warning = (
                "⚠️ INDEX DEGRADED — STOP ALL C/C++ ANALYSIS "
                "IMMEDIATELY.\n\n"
                f"The index is not complete: {manifest_reason(manifest_verification)}. "
                "The index can hold STALE data, and an analysis with a "
                "stale index WILL give incorrect results.\n\n"
                f"REQUIRED: Tell the user to run {command} "
                "to make the index complete. Do NOT continue any C/C++ "
                "analysis until the user confirms that the index run ended."
            )
            index_message += f" — manifest verification: {manifest_verification} ({tracking})"

    if header_affected_tus:
        # A TU that failed to parse keeps its needs_reparse mark, so it
        # counts here run after run.  Without the second half of this
        # sentence the user is told to run 'fw-context index', and the
        # index does not clear it.
        index_message += (
            f" | {header_affected_tus} source file(s) have stale header dependencies "
            "— header changes since last index, or a TU that failed to "
            "parse (see the index log)"
        )
        if not bg_running and not needs_reindex:
            # No reason names this, thus without the command the caller of
            # a project of one build got "not_scheduled" and no action.  The
            # message of several builds names the same command.  A reason
            # names its own command, and a plain `fw-context index` next to
            # it is not sufficient: the reason names what to repair before
            # the run, or that the run needs `--build`.
            index_message += " — run `fw-context index` for a changed header"

    # LLM-analysis coverage, split by project/vendor, so the LLM reads
    # unanalyzed vendor symbols as expected (analyze_vendor=false), not as
    # a defect.
    if analysis is not None:
        index_message += _analysis_message(analysis, stored_analyze_vendor)

    # ── This process is older than the index it reads ──
    # No command repairs this, thus it is a warning and not a reindex
    # reason.
    restart_reason = ""
    if row_format_newer:
        restart_reason = (
            f"This session reads row format {CURRENT_ROW_FORMAT} and the index holds "
            f"{stored_format}. The index is the newer one and it is correct — do "
            f"NOT reindex, because the indexer writes {stored_format} again and "
            f"this message comes back. This server process runs older code. Tell the "
            f"operator to restart the LLM client (Claude Code, opencode, or the client "
            f"in use). The MCP server is a child process of that client, thus nobody "
            f"can restart the server alone."
        )

    health: dict = {
        "indexed_at": cfg["created_at"],
        "symbol_count": sym_count,
        "file_count": file_count,
        "reference_count": ref_count,
        "modified_files_count": modified_count,
        "header_affected_tus": header_affected_tus,
        "analysis": analysis,
        "manifest_verification": manifest_verification,
        "description": cfg["description"] if "description" in cfg.keys() else "",
        "first_indexed_at": cfg["first_indexed_at"] if "first_indexed_at" in cfg.keys() else "",
        "status": status,
        "reindex_state": reindex_state,
        "reindex_needed": needs_reindex,
        "reindex_reasons": reindex_reasons,
        "stale": needs_reindex or header_affected_tus > 0,
        "index_message": index_message,
        "_restart_reason": restart_reason,
        "_restart_format": stored_format,
    }
    if warning is not None:
        health["_warning"] = warning
    return health


def _with_project_notes(result: dict, target: _IndexTarget, restart_reason: str, restart_format: str) -> dict:
    """Put the two notes of the project in front of ``index_message``, and add the project fields.

    The front of ``index_message`` is what a caller acts on, thus a note
    that changes the action goes there, one time for the call.

    * A run in progress or scheduled, while the index of a build is not
      current (``reindex_state`` "reindexing" or "pending").  A message
      can name a command, and a caller that follows it asks the operator
      for work that is being done.  ``status`` stays as it is on purpose:
      the answers of THIS moment come from the rows of the last finished
      run.  The wording promises nothing about the outcome: the run belongs
      to the daemon or to the operator, and its arguments cannot be read
      from here.
    * This process is older than the index it reads (a NEWER row format).
      No command repairs that, and a "fully up to date" message alone would
      hide the one action that helps.
    """
    if any(state in RUN_STATES for state in _build_states(result)):
        result["index_message"] = (
            "An index run is in progress or scheduled — wait for it rather than start "
            "a second one, and ask the operator for no command yet. It "
            "can clear the reasons below, but a reason that needs "
            "`--build` clears only if the run uses it. Answers meanwhile "
            f"come from the last finished run. {WAIT_ADVICE} " + result["index_message"]
        )
    if restart_reason:
        result["index_message"] = (
            f"Restart the LLM client — this session reads row format "
            f"{CURRENT_ROW_FORMAT} and the index holds {restart_format}. "
            f"Queries work meanwhile. {result['index_message']}"
        )
        # No leading underscore: this is an answer to act on, and not a
        # note about where an answer came from.
        result["client_restart_required"] = True
        result["client_restart_reason"] = restart_reason
    result["bg_reindex_running"] = target.bg_running
    # Only for "reindexing".  For "pending", the log holds the lines of the
    # run before until the daemon starts the next run, and the start
    # empties the log.  Thus a line in that time can come from the old run
    # or from the next run.
    if target.reindex_state == "reindexing":
        result["reindex_progress"] = read_reindex_progress(target.db_path.parent)
    # sqlite-vec is an optional C extension for vector embeddings.  Report
    # its availability so semantic_search can degrade gracefully.
    from ...deps import _vec_available

    vec_ok, vec_err = _vec_available()
    result["vec_available"] = vec_ok
    if vec_err is not None:
        result["vec_error"] = vec_err
    return result


def _build_states(result: dict) -> list[str | None]:
    """Return the ``reindex_state`` of each build of a status answer.

    ``get_build_info`` answers for one build, and a project of several
    builds holds them in ``builds``.  A project of one build has both, with
    the same value.  WHY a function: `_with_project_notes` serves the two
    tools, and it must read the state from the field that each one has.
    """
    if "reindex_state" in result:
        return [result["reindex_state"]]
    return [row["reindex_state"] for row in result.get("builds", [])]


def _public(health: dict) -> dict:
    """Return the fields of *health* that a tool gives, without the internal ones."""
    return {key: value for key, value in health.items() if not key.startswith("_restart")}


# The fields of one build in the `builds` list of get_project_status.
_BUILD_ROW_FIELDS = (
    "symbol_count", "file_count", "indexed_at", "status", "reindex_state", "reindex_needed", "stale",
    "modified_files_count", "header_affected_tus",
)


def _build_row(build, health: dict) -> dict:
    """Return the entry of one build in the ``builds`` list of :func:`get_project_status`."""
    row = {
        "variant": build["variant"] or "",
        "image": build["image"] or "",
        "board": build["board"] or "",
        **{key: health[key] for key in _BUILD_ROW_FIELDS},
        "reasons": health["reindex_reasons"],
    }
    if health.get("_warning"):
        row["manifest_verification"] = health["manifest_verification"]
    return row


def _build_state_words(health: dict) -> str:
    """Return the state of one build in a few words, for the message of a project of several builds.

    "" for a build that is current, else its ``reindex_state`` and the
    counts that the reasons do not give.  WHY not ``status``: it is
    "reindexing" while a run of the project is active, also for a build
    that the run does not change, and for a new source file when no run
    comes.  A message that said "reindexing" then made the caller wait for
    a run that did not exist.
    """
    state = health["reindex_state"]
    if state is None:
        return ""
    parts = []
    if health.get("_warning"):
        # A build without full header tracking is not up to date: the
        # warning of the project says so, and the message must agree.
        parts.append(f"manifest verification {health['manifest_verification']}")
    if health["modified_files_count"]:
        parts.append(f"{health['modified_files_count']} modified file(s)")
    if health["header_affected_tus"]:
        parts.append(f"{health['header_affected_tus']} file(s) with stale headers")
    return f"{state} ({', '.join(parts)})" if parts else state


def _project_health(builds: list, healths: list[dict]) -> dict:
    """Join the health of several builds into the health of the project.

    No sums: a file that two images compile is in the index of each, thus
    a sum counts shared code once for each build.  The counts are in the
    ``builds`` list.

    A reason that every build gives is a reason of the project, such as an
    old schema or a failed build, and it comes one time and without a
    label.  Any other reason gets the label of its build.
    """
    labels = [build_label(build) or "(no name)" for build in builds]
    common = set.intersection(*(set(health["reindex_reasons"]) for health in healths))
    reasons = [reason for reason in healths[0]["reindex_reasons"] if reason in common]
    for label, health in zip(labels, healths, strict=True):
        reasons += [f"build {label}: {reason}" for reason in health["reindex_reasons"] if reason not in common]
    if any(health["status"] == "reindex_needed" for health in healths):
        status = "reindex_needed"
    elif any(health["status"] == "reindexing" for health in healths):
        status = "reindexing"
    else:
        status = "ready"
    behind = [(label, words) for label, health in zip(labels, healths, strict=True)
              if (words := _build_state_words(health))]
    listing = "; ".join(f"{label}: {words}" for label, words in behind)
    if not behind and status == "reindexing":
        # The words of a project of one build, see `_build_health`.
        message = (
            f"Index is usable — background reindex in progress or scheduled, and the "
            f"index of each build is current ({len(builds)} builds). All queries "
            f"return accurate results."
        )
    elif not behind:
        message = f"Index is fully up to date ({len(builds)} builds)."
    elif not reasons and status == "reindexing":
        # No reason to read, and nothing for the caller to do but to wait
        # for the run: `_with_project_notes` puts that advice in front.
        message = f"Index is usable — background reindex in progress or scheduled ({listing})."
    elif not reasons:
        # `_warning` only when a build has one: a pointer to an absent field
        # sends the caller to look for nothing.
        read = "builds and _warning" if any(health.get("_warning") for health in healths) else "builds"
        message = (
            f"{len(behind)} of {len(builds)} builds are not up to date — {listing}. "
            f"Queries work, and their answers can be out of date — run `fw-context index`. "
            f"Read {read} for each build."
        )
    else:
        message = (
            f"{len(behind)} of {len(builds)} builds are not up to date — {listing}. "
            f"Read reindex_reasons for the action, and builds for each build."
        )
    result = {
        "status": status,
        "reindex_needed": status == "reindex_needed",
        "reindex_reasons": reasons,
        "stale": any(health["stale"] for health in healths),
        "index_message": message,
    }
    degraded = [label for label, health in zip(labels, healths, strict=True) if health.get("_warning")]
    if degraded:
        # One text for the project.  The text that sends the caller to
        # `reindex_reasons` wins: a plain 'fw-context index' is wrong for a
        # build whose reason names its own command, and `reindex_reasons`
        # of the project holds the reasons of each build.
        warnings = [health["_warning"] for health in healths if health.get("_warning")]
        chosen = next((text for text in warnings if _REASON_COMMAND in text), warnings[0])
        result["_warning"] = f"Builds without full header tracking: {', '.join(degraded)}. {chosen}"
    return result


def get_project_status(
    project_root: Annotated[
        str | None, Field(description="Project root directory. Auto-detected from CWD if omitted.")
    ] = None,
    fast: Annotated[
        bool, Field(description="When True (default), reuse the cached manifest "
        "hashes for the header check. Both modes count modified files. Pass "
        "False to recompute the header hashes, which is far slower.")
    ] = True,
) -> dict:
    """MANDATORY FIRST CALL for C/C++ projects. Return the health of the
    index of the project, and the list of its builds — check it before you
    use any other fw-context tool, and call it again while you wait for a
    reindex.

    Read-only with two exceptions, and it spawns no subprocess — the
    startup daemon thread and the file watcher own the background reindex.
    The exceptions: the last index run can leave a marker that says that
    the build is not there, and when the tool finds that build again, it
    removes the marker.  It also removes a ``reindex.pid`` or a
    ``reindex.pending`` of a process that no longer runs.  It creates no
    ``.fw-context`` file in a project that is not initialized.  It takes no
    ``variant`` and no ``image``: it reports every build.  For the detail
    of one build (its ``config_hash``, memory map, defines), call
    ``get_build_info``.

    ``builds`` lists every build of the index.  A project of more than one
    build has no active build: every query tool must get the parts of one
    entry that are not empty, its ``variant``, its ``image``, or the two.
    Select the build from the task, and
    ask the user when the task does not identify one.

    Act on ``reindex_state`` of the build that the task needs, in
    ``builds`` (a project of one build has it at the top level too):

    * ``null`` — the index of the build is current. Continue.
    * ``"reindexing"`` or ``"pending"`` — the index of the build is not
      current, and an index run is active or scheduled. Wait, and call
      this tool again after about 30 s. Continue with the old index only
      when the user tells you to.
    * ``"not_scheduled"`` — the index of the build is not current, and no
      run updates it. Tell the user the command that the reasons name.
    * ``"failed"`` — the last index run failed. Give its text to the user:
      it names the command that repairs it.

    Each query answer carries the state of its build as ``index_state``,
    from quick checks only. A query thus can miss a changed header, a
    changed file that its answer does not name, or a new source file when
    its answer is not empty. This tool does the full check.

    ``status`` gives the state of the project:

    * ``"ready"`` — up to date. Continue.
    * ``"reindexing"`` — background reindex running or scheduled, or fw-context builds
      new source files on its own. ``reindex_state`` of a build tells
      whether that build waits for the run.
      ``reindex_progress`` holds the last log line of a running reindex.
    * ``"reindex_needed"`` — the last index run failed, the build is not
      there, schema mismatch, an old row format, a changed or missing
      compile_commands.json, a checkout of another branch, tag or commit
      since the index, or a source file that compile_commands.json does
      not cover and fw-context will not build itself. A changed
      compile_commands.json includes one that fw-context cannot compare
      with the index. Queries still work on existing data. Read
      ``reindex_reasons``: each reason names the action that it needs.

      Read ``bg_reindex_running`` before you name any command here.  A run
      already under way does the work, thus a second one only waits on the
      same lock, and ``index_message`` and the checkout reason both say so.
      That field covers a run of the daemon AND a run the operator started,
      and also work that the daemon has scheduled and not yet started
      — the arguments of no run are readable, thus a reason that needs
      ``--build`` can outlive a run without it.  This status wins over
      ``"reindexing"`` deliberately: the answers of this moment come from
      the rows of the last FINISHED run.
    * ``"no_index"`` — initialized, never indexed. Run ``fw-context index``.
    * ``"not_initialized"`` — run ``fw-context init``.

    A project of several builds gets the worst status of its builds.  A
    reason that every build gives comes one time, and any other reason
    names its build: ``build <variant>/<image>: <reason>``.

    A failure sets NO ``status``.  An index with no build config gives a
    dict that holds ``error`` alone.  Read that key first: a reader that
    waits for ``status == "error"`` waits for a value this tool does not
    produce.  A database that fw-context cannot read (damage, no access)
    makes the call fail with the error of SQLite.  The message of the
    check ``db-integrity`` of ``check_dependencies`` tells if it is
    damaged.

    Seven conditions set ``reindex_needed``: a failed last index run, a
    build that is not there, an outdated schema, an outdated ROW FORMAT, a
    changed or missing compile_commands.json, a checkout of another
    branch, tag or commit since the index, and a source file that is on
    disk but absent from compile_commands.json when fw-context will not
    build it on its own.  A missing build, a missing
    compile_commands.json, a checkout and such a source file need the
    action that makes compile_commands.json again: a build, or for a
    database of the user (``[index] compile_commands``) a new file from the
    user.  A plain reindex reads the same file again, and it has no
    translation unit for a new source file and skips it without a word.  A
    failed run needs the repair that its reason names first.  Modified
    source files are something else: they are handled per query, and
    never set it.

    ``row_format_mismatch`` means that the same columns hold text with an
    older meaning.  Take it seriously: an index written before
    ``fw-context-rows/1`` keeps every inactive ``#ifdef`` branch, thus a
    body or a file from it can show code that the compiler never sees.  An
    index written before ``fw-context-rows/3`` is wrong the other way: it
    ANSWERS LESS than it should.  A definition that begins and ends on one
    line has no stored body there, thus ``search_bodies`` cannot reach an
    inline accessor; no macro is marked function-like; and no instance links
    to its template, thus ``get_template_instances`` gives an empty list for
    every template.  Each of those looks like a legitimate empty answer.
    The ``value`` of a macro there also holds its parameter list glued to
    its replacement text, thus neither can be read out of it.  An index
    written before ``fw-context-rows/4`` parsed a GCC build with guessed
    toolchain headers and the macros of clang: an ESP32 or AVR index there
    holds host headers from ``/usr/include`` and takes the ``#if`` branches
    of the host, for example ``#if __XTENSA__`` as false, and an ARM index
    saw ``__GNUC__`` 4.  A build whose GCC driver does not answer the query
    keeps that fault also after ``/4``; the index run names such a compiler
    in a warning.

    ``client_restart_required`` is the OPPOSITE case, and no command repairs
    it.  This server process does not run the code that it must run.  Two
    causes set it, and ``client_restart_reason`` names each one that
    applies:

    * The index carries a NEWER row format than this process reads, thus
      the index is the correct one and this process is the old reader.  A
      reindex makes it worse than useless: the indexer writes the same new
      format again, and the message comes back over an index that had
      nothing wrong with it.
    * The operator installed a different fw-context after the LLM client
      started this process.  This cause does not need an index, thus it
      shows also for ``not_initialized`` and ``no_index``.

    ``status`` therefore stays as it is and ``reindex_needed`` stays False
    — every query keeps working.  Do NOT run a reindex for this field.
    Tell the operator to restart the LLM client — Claude Code, opencode, or
    whichever one is in use.  The MCP server is a child process of that
    client, thus nobody can restart the server by itself.
    ``client_restart_reason`` holds the wording, and ``index_message``
    opens with it.

    ``update_notice`` is not a status either.  It shows when PyPI has a
    newer fw-context than the installed one.  Tell the operator one time,
    and do NOT upgrade fw-context yourself: the operator knows the tool
    that installed it.  After a request that succeeded, the server asks
    PyPI again only after 24 hours.  The request runs in a background
    thread, and no tool waits for it.  ``[updates] check = false``
    in the global config, or ``FW_CONTEXT_NO_UPDATE_CHECK=1``, stops it.

    ``indexed_at`` and ``first_indexed_at`` are UTC; file mtimes are local
    time.  Never compare the two directly — in UTC+2 a correctly indexed
    file looks 2 hours newer than ``indexed_at``.  Read
    ``modified_files_count`` to find modified files.

    ``analysis`` splits the LLM-analysis coverage into project and vendor
    symbols:

    * ``model`` — the model of the analysis, or None. One model only, even
      when several were used.
    * ``analyze_vendor`` — the value at index time, not the current config.
    * ``project`` / ``vendor`` — ``{analyzed, skipped, total}``.
      ``skipped`` = tried, but not analyzable (body larger than the model
      context, an unparseable answer, or a body that was not readable).
    * ``complete`` — no work left: every project symbol is analyzed or
      skipped, and every vendor symbol too when ``analyze_vendor`` is True.
      Vendor symbols excluded by ``analyze_vendor=False`` never block it,
      thus ``vendor.total`` large with ``vendor.analyzed=0`` is expected,
      not a defect.  ``index_message`` ends with the same counts as text.

    Args:
        project_root: Project root directory. Auto-detected from CWD if
            omitted.
        fast: When True (default), the header check reuses the cached
            manifest hashes.  Both modes run the per-file scan, thus
            ``modified_files_count`` is accurate either way — a tool that
            reported "ready" while the search tools warned about the same
            file gave the caller two readings and no way to choose.
            False recomputes the header hashes and costs several times more.

    Returns:
        dict: {project_id, project_root, build_system, schema_version (int
        — DB schema version), current_schema (int — code expects),
        vendor_paths (list[str] — config index.vendor_paths),
        project_paths (list[str] — config index.project_paths),
        status (str — "ready"|"reindexing"|"reindex_needed"|"no_index"|
        "not_initialized"; a failure sets no status and gives ``error``
        alone), reindex_needed (bool), reindex_reasons (list[str] — why a
        reindex is needed, empty when False, except that a new source file
        that fw-context builds on its own is listed while
        ``reindex_needed`` is False.  Some of them ask for a build rather
        than a plain reindex.  For example, after a checkout of another
        branch, tag or commit, compile_commands.json belongs to the OLD
        checkout and carries its file list and its compiler flags, so only
        a build regenerates it.  Read the reason text — it names the
        command it needs), stale (bool — True when a
        build needs a reindex or has stale header dependencies),
        index_message (str — human-readable summary of index state),
        builds (list[dict] — every build: {variant, image, board,
        symbol_count, file_count, indexed_at, status, reindex_state (str
        or None — "reindexing", "pending", "not_scheduled", "failed", or
        None for a current index), reindex_needed, stale,
        modified_files_count, header_affected_tus, reasons}, and
        ``manifest_verification`` on a build without full header
        tracking), declared_not_indexed (list[dict], optional — each
        variant that the config declares and the index does not hold:
        {variant, command}; no query reaches it until the command ran),
        bg_reindex_running (bool), reindex_progress (str or
        None — last log line when reindex is running), _warning (str,
        optional — when the manifest verification of a build is not
        "full"), vec_available (bool), vec_error (str, optional),
        client_restart_required (bool, optional — only when True),
        client_restart_reason (str, optional — with the field above),
        update_notice (str, optional — a newer release is on PyPI)}

        A project of ONE build also gets the fields of that build at the
        top level: indexed_at (str — "YYYY-MM-DD HH:MM:SS" in UTC, the
        completion time of the last full index), first_indexed_at,
        symbol_count, file_count, reference_count, modified_files_count
        (int — files whose content no longer matches the index; counted in
        both modes), header_affected_tus (int — number of source files with
        stale header dependencies; a file that compile_commands.json lists
        more than once counts once), manifest_verification (str — "full"
        when manifest.json exists, "none" otherwise), analysis (dict — LLM
        analysis coverage split by project/vendor: {model, analyze_vendor,
        project: {analyzed, skipped, total}, vendor: {analyzed, skipped,
        total}, complete}), description (str).  A project of several
        builds gets no sums: the counts of each build are in ``builds``,
        and ``reference_count``, ``analysis``, ``description`` and
        ``first_indexed_at`` of a build are in ``get_build_info``.

        For a project that is not initialized, the result holds only
        ``status``, ``project_root``, and ``index_message``.  When no index
        exists, the result adds ``project_id``.  The two version fields
        (``client_restart_*`` for an upgrade, ``update_notice``) can come
        with every result, these two included.
    """
    return _with_version_state(_project_state(project_root, fast))


def _project_state(project_root: str | None, fast: bool) -> dict:
    """Return the state of :func:`get_project_status`, before the version fields."""
    target = _index_target(project_root)
    if isinstance(target, dict):
        return target

    def _query(conn, _config_hash):
        builds = sorted(latest_builds(conn, target.project_id), key=build_label)
        if not builds:
            return {"error": f"No build config indexed for project at {target.root}."}
        hash_cache: dict[str, str] = {}
        facts = _project_facts(conn, target)
        # One build gives its whole health at the top level, as get_build_info
        # does.  The list of several builds shows no analysis and no count of
        # references, thus it does not pay for them.
        detail = len(builds) == 1
        healths = [
            _build_health(conn, target, build, fast=fast, hash_cache=hash_cache, facts=facts, detail=detail)
            for build in builds
        ]
        result = _public(healths[0]) if len(builds) == 1 else _project_health(builds, healths)
        result["builds"] = [_build_row(build, health) for build, health in zip(builds, healths, strict=True)]
        # A variant that the config declares and the index does not hold: no
        # query reaches it, and nothing else says that it exists.
        indexed = {build["variant"] or "" for build in builds}
        missing = [v.name for v in target.cfg.build.variants if v.name not in indexed]
        if missing:
            result["declared_not_indexed"] = [
                {"variant": name, "command": f"fw-context index --build --variant {name}"} for name in missing
            ]
        restart = next((health for health in healths if health["_restart_reason"]), None)
        return result, restart, get_db_schema_version(conn)

    answer = get_executor(target.db_path).execute_sync(_query, "")
    if isinstance(answer, dict):
        return answer
    result, restart, schema_version = answer
    result.update({
        "project_id": target.project_id,
        "project_root": str(target.root),
        "build_system": target.cfg.build.system or _detect_build_system(target.root),
        "schema_version": schema_version,
        "current_schema": CURRENT_SCHEMA_VERSION,
        "vendor_paths": target.cfg.index.vendor_paths,
        "project_paths": target.cfg.index.project_paths,
    })
    return _with_project_notes(
        result, target,
        restart["_restart_reason"] if restart else "",
        restart["_restart_format"] if restart else "",
    )


def get_build_info(
    project_root: Annotated[
        str | None, Field(description="Project root directory. Auto-detected from CWD if omitted.")
    ] = None,
    variant: Annotated[str | None, Field(description=VARIANT_DESCRIPTION)] = None,
    image: Annotated[str | None, Field(description=IMAGE_DESCRIPTION)] = None,
    fast: Annotated[
        bool, Field(description="When True (default), reuse the cached manifest "
        "hashes for the header check. Pass False to recompute them, which is far slower.")
    ] = True,
) -> dict:
    """Return the detail of ONE build: its identity, its health, its memory
    map and its defines.

    Read-only with the two exceptions that ``get_project_status`` also
    has: it removes the marker of a missing build when it finds that build
    again, and a ``reindex.pid`` or a ``reindex.pending`` of a process that
    no longer runs.  It spawns no subprocess.  On a project of more than one
    build, give the arguments of one build as the refusal lists them: the
    ``variant``, the ``image``, or the two.  ``get_project_status`` lists
    the builds in ``builds``.  Without the arguments the answer is an
    error that lists the builds.  A project of one build
    needs no selector.

    The health fields (``status``, ``reindex_state``, ``reindex_needed``, ``reindex_reasons``,
    ``index_message``, ``modified_files_count`` and the others) have the
    meaning that ``get_project_status`` gives them, for this build only.
    The version fields (``update_notice``, a restart after an upgrade) are
    in ``get_project_status``.

    About ``memory``: ``origin`` and ``length`` hold the expression the
    script writes, thus they differ by platform — an mbed script writes
    `0xefe00` and a Zephyr script writes `((673792) - 0xe6)`.
    ``origin_value`` and ``length_value`` hold the number, and both are
    None for an expression that names a symbol, such as
    `ORIGIN(RAM) + LENGTH(RAM)`.  A name that the link command defines
    with ``--defsym`` counts as a number: the STM32 Arduino core writes
    `LENGTH = LD_MAX_DATA_SIZE` and gets ``length_value`` 98304.  The end
    of a region is ``origin_value + length_value``.

    ``memory`` is empty for a build system that records no linker script,
    such as Keil or IAR.  A PlatformIO project records its link only in
    ``fw-context index --build``: SCons writes no file with the link
    command, thus the build asks ``pio run -t envdump``.  An empty list for
    PlatformIO means that no build recorded the link yet, and
    ``fw-context index --build`` is the step.  An empty list means "not
    recorded", never "no memory".

    ``defines`` holds only the names that EVERY translation unit of the
    build carries with the same value, so the tool never shows the defines
    of one file as the defines of the build.  ``defines_varying`` counts
    the names left out, thus a name absent from ``defines`` is either not
    defined at all or not defined everywhere.  Measured on the Mbed
    project, 27 names are in all 881 units and 59 in only some, where the
    three assembly files get a shorter set.

    This is the configuration the BUILD states, not every macro the
    preprocessor saw.  The second is three orders of magnitude larger —
    27800 distinct names on the STM32 project — and almost all of it comes
    from the headers and the compiler.  A Zephyr build keeps its real
    configuration in ``autoconf.h`` (740 `CONFIG_*` names) and passes few
    `-D` flags, so ``defines`` says little there and a great deal on an
    mbed build, where it holds `APPLICATION_ADDR`, `APPLICATION_SIZE`, and
    `CMSIS_VECTAB_VIRTUAL`.

    Args:
        project_root: Project root directory. Auto-detected from CWD if
            omitted.
        variant: Variant of the build (a board, or a PlatformIO
            environment). On a project of more than one build, give the
            arguments of one build as the refusal lists them: the
            variant, the image, or the two. fw-context does not select a
            build. One query answers for ONE build.
        image: Image of the build: a separate program, such as the
            application or the bootloader. Give it when the build has an
            image name.
        fast: When True (default), the header check reuses the cached
            manifest hashes.  False recomputes them.

    Returns:
        dict: {variant, image, board, config_hash, project_id, project_root,
        build_system, compile_commands, indexed_at (str — UTC),
        first_indexed_at, symbol_count, file_count, reference_count,
        modified_files_count, header_affected_tus, manifest_verification,
        analysis, description, vendor_paths, project_paths,
        effective_vendor_patterns (list[str] — the SQL LIKE patterns that
        this build really used to mark vendor code, for example
        ``["mbed-os/%"]``; empty when the manifest cannot be read.  The two
        lists above hold what the config asks for, this one what the index
        did), status, reindex_state (str or None — see get_project_status),
        reindex_needed, reindex_reasons, stale,
        index_message, entry_point (str — the `ENTRY()` of the linker
        script of the build, empty when no script names one), memory
        (list[dict] — the `MEMORY` regions of the build: {name, attributes,
        origin, length, origin_value, length_value, file_path, line}),
        defines (dict — the `-D` flags of the build), defines_varying
        (int), bg_reindex_running (bool), reindex_progress (str or None),
        vec_available (bool), vec_error (str, optional), _warning (str,
        optional), client_restart_required (bool, optional),
        client_restart_reason (str, optional)}

        A refusal of the selector, or an index with no build config, gives
        a dict that holds ``error`` alone.  A project that is not
        initialized or has no index gets the answer of
        ``get_project_status``.
    """
    target = _index_target(project_root)
    if isinstance(target, dict):
        return target
    from ..shared.variants import resolve_build

    conn = _quick_open_readonly(target.db_path)
    try:
        config_hash, refusal = resolve_build(conn, target.project_id, target.cfg, variant or "", image or "")
    finally:
        conn.close()
    if refusal:
        return {"error": refusal}
    if config_hash is None:
        return {"error": f"No build config indexed for project at {target.root}."}

    def _query(conn, config_hash):
        cfg = conn.execute(
            "SELECT * FROM build_configs WHERE project_id = ? AND config_hash = ?",
            (target.project_id, config_hash),
        ).fetchone()
        if cfg is None:
            # A reindex removed the build between the two reads.
            return {"error": f"No build config indexed for project at {target.root}."}
        health = _build_health(conn, target, cfg, fast=fast, hash_cache={}, facts=_project_facts(conn, target))
        compile_commands = cfg["compile_commands_path"]
        # The `-D` flags of this build, read from ITS compile_commands.json.
        # Computed here and not stored: the read costs 19 ms on the Mbed
        # project with 881 units, measured, so a column would buy nothing
        # and a stale one would mislead.
        defines, varying = command_line_defines(Path(compile_commands) if compile_commands else Path())
        result = {
            "variant": cfg["variant"] or "",
            "image": cfg["image"] or "",
            "board": cfg["board"] or "",
            "config_hash": config_hash,
            "project_id": target.project_id,
            "project_root": str(target.root),
            "build_system": target.cfg.build.system or _detect_build_system(target.root),
            "compile_commands": compile_commands,
            **_public(health),
            "vendor_paths": target.cfg.index.vendor_paths,
            "project_paths": target.cfg.index.project_paths,
            # What the INDEX RUN applied, which is not always what the
            # config says today: the builder derives part of it, a variant
            # adds to it, and the config can have changed since.
            "effective_vendor_patterns": _effective_vendor_patterns(
                target.db_path.parent, config_hash, target.root
            ),
            "entry_point": get_entry_point(conn, config_hash),
            "memory": get_memory_regions(conn, config_hash),
            "defines": defines,
            "defines_varying": varying,
        }
        return result, health["_restart_reason"], health["_restart_format"]

    answer = get_executor(target.db_path).execute_sync(_query, config_hash)
    if isinstance(answer, dict):
        return answer
    result, restart_reason, restart_format = answer
    return _with_project_notes(result, target, restart_reason, restart_format)


# ── moved from server.py ──
def _analysis_message(analysis: dict, analyze_vendor: bool) -> str:
    """Return the LLM-analysis part of ``index_message``.

    Takes the ``analysis`` dict from ``make_analysis_summary`` and gives one
    line for a human reader.  The line always names the skipped symbols,
    because the pipeline cannot analyze them — the gap between ``analyzed``
    and ``total`` is not a defect.

    With ``analyze_vendor=False`` the line reports the project counts, and
    says that fw-context skipped the vendor symbols by design.  With
    ``analyze_vendor=True`` it reports both sides.

    Args:
        analysis: The ``analysis`` dict — {project, vendor} each with
            ``analyzed``, ``skipped``, and ``total``.
        analyze_vendor: The stored value, thus the message describes the
            build that fw-context indexed, not the current config.

    Returns:
        str: the message part, with a leading ``" | "`` separator.
    """
    p = analysis["project"]
    v = analysis["vendor"]
    if analyze_vendor:
        skipped_note = ""
        if p["skipped"] or v["skipped"]:
            skipped_note = (
                f" ({p['skipped']} project, {v['skipped']} vendor symbols skipped)"
            )
        return (
            f" | LLM analysis: project {p['analyzed']}/{p['total']}, "
            f"vendor {v['analyzed']}/{v['total']}{skipped_note}"
        )
    skipped_note = f", {p['skipped']} project symbols skipped" if p["skipped"] else ""
    return (
        f" | LLM analysis: project {p['analyzed']}/{p['total']}{skipped_note} "
        "(vendor skipped — analyze_vendor=false)"
    )


def _list_status(db_schema_ver: int, cc_stale: bool, row_format_old: bool) -> str:
    """Map schema version, compile-commands staleness and row format to a status label.

    Returns ``"reindex_needed"`` when the DB schema is outdated, when the
    compile_commands.json of a build changed or is missing, or when a build
    holds text of an older row format.  Returns ``"ready"`` otherwise.  Used
    by ``list_projects`` to summarize each indexed project.

    WHY the row format is here: the schema version hashes the column set
    alone, thus it does not move when the meaning of the text in a column
    changes.  Without this check ``list_projects`` reported "ready" over an
    index for which ``get_project_status`` and the daemon ask for a reindex.
    """
    needs = (db_schema_ver < CURRENT_SCHEMA_VERSION) or cc_stale or row_format_old
    return "reindex_needed" if needs else "ready"


def list_projects(
    project_root: Annotated[
        str | None,
        Field(
            description="Project root whose config names the index directory; pass it to distinguish multiple "
            "indexed projects that keep separate index directories. Omitted = the global config. Every "
            "project in that directory is listed."
        ),
    ] = None,
) -> list[dict]:
    """List all indexed firmware projects with their statistics.

    Read-only. No side effects. Use it to find another project; call
    ``get_project_status`` for the builds and the health of one project.

    ``indexed_at`` and ``first_indexed_at`` are UTC, in ``"YYYY-MM-DD
    HH:MM:SS"`` format — the same format that ``get_project_status`` returns.
    ``indexed_at`` is the time of the newest build.

    A project of several builds gets no counts here (``symbol_count``,
    ``file_count``, ``description`` and ``analysis`` are None): a file that
    two images compile is in the index of each, thus a sum counts shared
    code once for each build.  ``get_project_status`` gives the counts of
    each build.

    ``analysis`` holds the ``project`` and ``vendor`` counts only.  For the
    ``model``, ``analyze_vendor``, and ``complete`` fields, call
    ``get_build_info`` for that build.

    Args:
        project_root: Project root whose config names the index
            directory (``index.db_dir``).  Omitted, the global config does.

    Returns:
        list of dicts, each with: project_id, name, root_path, build_system,
        symbol_count, file_count, indexed_at (str — UTC), description (str),
        first_indexed_at (str — UTC), schema_version, current_schema,
        reindex_needed (bool — the schema is outdated, the
        compile_commands.json of a build changed or is missing, or a build
        holds rows of an older row format; each (variant, image) build
        counts), status (str — "ready" or "reindex_needed"),
        db (path to SQLite database file),
        build_count (int — number of builds, each a (variant, image) pair),
        analysis (dict — LLM-analysis coverage
        {project: {analyzed, skipped, total},
        vendor: {analyzed, skipped, total}}, or None when no build is
        indexed).

        When no project has an index, the result is a single dict with an
        ``info`` key.  When fw-context cannot read a database, the result
        holds a dict with ``db`` and ``error`` keys for that file.
    """
    # resolve_project_root, not Path().resolve(): project_root can hold a
    # project name or a project_id, and only the shared resolver reads the
    # global registry that maps those to a root directory.
    cfg = load_config(project_root=resolve_project_root(project_root) if project_root else None)
    index_dir = cfg.index.db_dir
    # Glob for subdirectories containing index.db — each subdirectory
    # is named after a project_id UUID4.
    db_files = list(index_dir.glob("*/index.db")) if index_dir.exists() else []
    if not db_files:
        return [{"info": f"No indexed projects found under {index_dir}."}]
    results: list[dict] = []
    for db_path in sorted(db_files):
        try:
            executor = get_executor(db_path)

            def _query(conn, _config_hash):
                # Runs under the executor lock on the single shared
                # connection; must not open its own connection.
                rows = get_all_projects(conn)
                schema_ver = get_db_schema_version(conn)
                coverage_map = {
                    row["config_hash"]: compute_analysis_coverage(conn, row["config_hash"])
                    for row in rows
                    if row["config_hash"]
                }
                # The newest build of each (variant, image), the builds that
                # get_project_status and the daemon check.  The newest build
                # of the project alone is not enough: the builds of one
                # project are not always indexed together, thus another build
                # can be stale while the newest is current.
                builds = {row["project_id"]: latest_builds(conn, row["project_id"]) for row in rows}
                return rows, schema_ver, coverage_map, builds

            rows, db_schema_ver, coverage_map, builds = executor.execute_sync(_query, "")
            for r in rows:
                project_builds = builds[r["project_id"]]
                # A changed or missing compile_commands.json of a build: the
                # check that get_project_status makes, by mtime against the
                # time of the index.  Does not require _wrap_tool —
                # list_projects is informational only.
                cc_stale = any(
                    _is_stale(build, build["compile_commands_path"])[0]
                    for build in project_builds
                    if build["compile_commands_path"]
                )
                # A project with no build has no row here, and an absent
                # build is not an OLD format.
                row_format_old = any(
                    row_format_is_older(stored_row_format(build)) for build in project_builds
                )
                root = Path(r["root_path"]) if r["root_path"] else None
                # Report the CONFIGURED build system first — projects without
                # markers (e.g. an NCS workspace) resolve system from config,
                # not from marker detection, so _detect_build_system would
                # wrongly report "unknown".
                try:
                    _pc = load_config(project_root=root) if root else None
                    _bs = (_pc.build.system if _pc else None) or (_detect_build_system(root) if root else "unknown")
                except Exception:
                    _bs = _detect_build_system(root) if root else "unknown"
                status = _list_status(db_schema_ver, cc_stale, row_format_old)
                # The counts of the newest build describe the project only
                # when it is the one build.  See the docstring.
                single = len(project_builds) <= 1
                cov = coverage_map.get(r["config_hash"]) if single and r["config_hash"] else None
                results.append(
                    {
                        "project_id": r["project_id"],
                        "name": r["name"],
                        "root_path": r["root_path"],
                        "build_system": _bs,
                        "symbol_count": r["symbol_count"] if single else None,
                        "file_count": r["file_count"] if single else None,
                        "indexed_at": r["created_at"],
                        "description": (r["description"] if "description" in r.keys() else "") if single else None,
                        "first_indexed_at": r["first_indexed_at"] if "first_indexed_at" in r.keys() else "",
                        "schema_version": db_schema_ver,
                        "current_schema": CURRENT_SCHEMA_VERSION,
                        "reindex_needed": status == "reindex_needed",
                        "status": status,
                        "db": str(db_path),
                        "build_count": len(project_builds),
                        "analysis": (
                            {"project": cov["project"], "vendor": cov["vendor"]}
                            if cov else None
                        ),
                    }
                )
        except (sqlite3.Error, OSError) as e:
            results.append({"db": str(db_path), "error": str(e)})
    return results


# SQLite result codes (the low byte of an extended code) that do not show
# damage of the database file:
#   - a fault of the host: SQLITE_PERM, SQLITE_NOMEM, SQLITE_READONLY,
#     SQLITE_IOERR, SQLITE_FULL, SQLITE_CANTOPEN, SQLITE_NOLFS
#   - a different connection or a stop: SQLITE_ABORT, SQLITE_BUSY,
#     SQLITE_LOCKED, SQLITE_INTERRUPT, SQLITE_PROTOCOL, SQLITE_SCHEMA
#     (a run changed the schema), SQLITE_AUTH
#   - a fault of the code: SQLITE_MISUSE, SQLITE_RANGE
_FAULT_CODES = frozenset({3, 4, 5, 6, 7, 8, 9, 10, 13, 14, 15, 17, 21, 22, 23, 25})


def _shows_damage(exc: sqlite3.DatabaseError) -> bool:
    """Tell if *exc* shows a damaged database, and not a lock or a fault of the disk.

    WHY: a lock, a full disk, an I/O error or a missing permission does
    not damage the database.  If the tool calls such a database damaged,
    the user can delete a good index.  Each other error of SQLite shows a
    database that the tool must let the user delete: damaged pages, and
    also a schema that open_db cannot repair.

    open_db wraps most errors of SQLite in `DatabaseCorruptionError`, and
    keeps the error of SQLite as the cause.  The other errors of open_db
    and of the reads come without a wrap.  A report of the integrity check
    has no cause: that is damage.  ``locked=True`` tells that open_db could
    not get the lock.  An error without a result code, for example a
    `sqlite3.ProgrammingError`, is a fault of the code, not damage.
    """
    error: sqlite3.Error = exc
    if isinstance(exc, DatabaseCorruptionError):
        if exc.locked:
            return False
        if not isinstance(exc.__cause__, sqlite3.Error):
            return True
        error = exc.__cause__
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and (code & 0xFF) not in _FAULT_CODES


# ── moved from server.py ──
def reset_index(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    confirm: Annotated[
        bool, Field(description="Must be True to execute. Call without confirm first as dry-run.")
    ] = False,
) -> dict:
    """Delete the entire symbol index for a project.

    Not read-only — permanently deletes the SQLite database and WAL files.
    Call with ``confirm=False`` first (dry-run) to see what would be deleted.
    Re-index with ``fw-context index`` afterwards.

    Handles corrupt databases gracefully — you can delete a corrupt index
    without needing to open it first.

    Args:
        project_root: Project root directory. Auto-detected if omitted.
        confirm: Must be True to execute. Call without first as dry-run.

    Returns:
        dict: {project_root, db, project_id, action: "dry_run"|"deleted",
        message, build_count, symbol_count, indexed_at}.  ``build_count``
        counts the completed builds of the project, as
        ``get_project_status`` lists them.  ``symbol_count`` counts every
        symbol row in the database, the rows that the reset deletes: also
        the rows of a build that an index run writes now, and of an older
        build of a pair that an interrupted run left behind.
        ``indexed_at`` is the time of the newest build.  The three are
        present only when a build config is readable.

        A ``warning`` key means that the database is corrupt — the
        integrity check or a read of the database failed, thus the tool
        counts nothing and the three count fields are absent.

        On failure the dict holds only ``error`` with the reason.
    """
    root = resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Nothing to reset."}
    project_id = derive_project_id(root)
    cfg_data = None
    sym_count = 0
    build_count = 0
    corrupt = False
    conn = None
    try:
        conn = open_db(db_path)
        with conn:
            # The newest build gives the time of the last run.  The counts
            # are of the whole database, because the reset deletes all of
            # it: the count of the newest build alone was the size of the
            # one program that the last run did.
            cfg_data = get_active_config(conn, project_id)
            if cfg_data:
                sym_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
                build_count = len(latest_builds(conn, project_id))
    except sqlite3.DatabaseError as exc:
        if not _shows_damage(exc):
            raise
        # The reset can still delete a damaged database, thus the tool
        # records the flag and gives no counts.
        corrupt = True
        cfg_data = None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    info: dict[str, object] = {
        "project_root": str(root),
        "db": str(db_path),
        "project_id": project_id,
    }
    if cfg_data:
        info["build_count"] = build_count
        info["symbol_count"] = sym_count
        info["indexed_at"] = cfg_data["created_at"]
    elif corrupt:
        info["warning"] = "Database is corrupt — integrity check or read failed."
    if not confirm:
        info["action"] = "dry_run"
        if corrupt:
            info["message"] = (
                f"Database at {db_path} is corrupt. "
                "Call reset_index(confirm=True) to delete it anyway, "
                "then run 'fw-context index' to rebuild."
            )
        else:
            info["message"] = f"Would delete {db_path}. Call reset_index(confirm=True) to proceed."
        return info
    from ..background import bg_reindex_pause

    with bg_reindex_pause(root):
        # Mandatory ordering:
        # 1. The dry-run connection above is already closed (finally block).
        # 2. Invalidate the executor BEFORE deleting the DB file —
        #    otherwise it keeps a connection to an unlinked file and
        #    every subsequent query silently reads stale data forever.
        # 3. Only then delete the files.
        invalidate_executor(str(db_path.resolve()))
        db_path.unlink()
        # WAL and SHM files are sidecar files managed by SQLite in WAL
        # journal mode.  They must be deleted alongside the main DB file
        # to prevent a zombie WAL from being replayed into a fresh index.
        for suffix in ("-wal", "-shm", "-journal"):
            p = db_path.with_name(db_path.name + suffix)
            p.unlink(missing_ok=True)
        # Both autobuild markers describe the index that just went away: why
        # its last run stopped, and the files a build ran for and did not
        # cover.  Left behind they would warn about, or hide, work on a
        # fresh index that knows nothing about either.
        from ...indexer.autobuild import clear_excluded, clear_problem
        clear_problem(db_path.parent)
        clear_excluded(db_path.parent)
        from ...mcp.shared.stale import _invalidate_modified_cache
        _invalidate_modified_cache()  # clear all entries — DB is gone
        info["action"] = "deleted"
        info["message"] = f"Index deleted. Run 'fw-context index' in {root} to rebuild."
    return info


# ── moved from server.py ──
def _reindex_cleanup_deleted_file(
    conn: sqlite3.Connection,
    cfg_data: sqlite3.Row,
    target: Path,
    root: Path,
    db_path: Path,
) -> dict:
    """Remove index records for a file that no longer exists on disk.

    Called when ``reindex_file`` is invoked for a path that the user
    deleted after indexing.  Fetches the file's old ID from the index,
    counts deleted symbols for reporting, and purges all records
    (symbols, references, analysis) via ``purge_file_records``.

    Pauses background reindex to avoid a race: the bg thread might
    also detect the deletion and attempt its own cleanup.
    """
    config_hash = cfg_data["config_hash"]
    from ...indexer.db import get_file_mtimes, purge_file_records
    from ...indexer.db._locking import WriteLockTimeout
    from ...indexer.ops import _normalize_file_path
    from ..background import bg_reindex_pause

    known = get_file_mtimes(conn, config_hash)
    file_path_str = _normalize_file_path(str(target), root)
    if file_path_str not in known:
        return {"error": f"File not found on disk or in index: {target}"}

    file_id_old, _ = known[file_path_str]
    symbol_count = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE file_id = ?", (file_id_old,)
    ).fetchone()[0]

    from ...mcp.shared.stale import _invalidate_modified_cache
    _invalidate_modified_cache(config_hash)

    with bg_reindex_pause(root):
        try:
            # write_lock_held=False, transaction_held=False — this caller
            # does NOT hold either; purge_file_records acquires its own.
            purge_file_records(
                conn, config_hash, file_id_old, file_path_str,
                db_dir=db_path.parent,
            )
            return {
                "file": str(target),
                "symbols_removed": symbol_count,
                "action": "deleted",
            }
        except WriteLockTimeout as e:
            return {"error": f"Could not acquire write lock for cleanup of {target}: {e}", "action": "timeout"}


def _reindex_parse_and_store(
    conn: sqlite3.Connection,
    matching: list,
    cfg_data: sqlite3.Row,
    cfg_refs: bool,
    root: Path,
    db_path: Path,
    target: Path,
    vendor_patterns: list[str],
    project_patterns_list: list[str],
    transient_defines: Collection[str],
) -> tuple[int, dict]:
    """Parse one or more translation units with libclang and store their symbols.

    Steps:
    1. Call ``extract_all`` for each TU — this invokes libclang to parse
       the file with the compiler flags from compile_commands.json.
    2. Within a single write-lock transaction, call ``store_symbols_for_unit``
       for each successfully parsed TU to upsert symbols into the database.
    3. Run macro resolution (best-effort — silently skipped on failure).
    4. Delete orphan file records (files that disappeared from the index).
    5. Update the manifest.json header-dependency tracking file.

    Returns ``(total_symbols, result_dict)``.  The result dict includes
    ``symbols_updated``, ``elapsed_s``, and optionally ``skipped_tus``.
    """
    from ...indexer.compile_commands import CompilationUnit as TU
    from ...indexer.db import write_lock as db_write_lock
    from ...indexer.ops import store_symbols_for_unit
    from ...indexer.symbols import (
        ExtractionResult,
        extract_all,
    )
    from ..background import bg_reindex_pause

    config_hash = cfg_data["config_hash"]

    # Phase 1: parse all matching TUs before acquiring the write lock.
    # Parsing is CPU-bound and does not touch the database — doing it
    # outside the lock minimises contention.
    #
    # return_tu: the store takes from the TU the lines that the preprocessor
    # skipped and the text that the parse read, with its hash.  Without it,
    # the stored bodies kept their inactive #if branches, and each row kept
    # the hash of the text before the edit.  The TUs of one call are few:
    # the listings of one source file, or one unit for a header.
    parsed_units: list[tuple[TU, ExtractionResult]] = []
    skipped_tus: list[str] = []
    for unit in matching:
        try:
            parsed = extract_all(unit, with_refs=cfg_refs, return_tu=True)
            parsed_units.append((unit, parsed))
        except sqlite3.Error as exc:
            return 0, {"error": f"DB error during parse of {unit.file.name}: {exc}"}
        except RuntimeError as exc:
            # Clang parse failures (missing headers, syntax errors) are
            # non-fatal — skip the TU and continue with others.
            log.warning("skip TU %s during reindex: %s", unit.file.name, exc)
            skipped_tus.append(str(unit.file.name))

    from ...mcp.shared.stale import _invalidate_modified_cache
    _invalidate_modified_cache(config_hash)

    with bg_reindex_pause(root):
        t0 = time.monotonic()
        total_symbols = 0

        try:
            # Phase 2: acquire the write lock and store all parsed symbols
            # in a single transaction per TU.  Each TU gets its own
            # transaction so partial failures don't roll back all work.
            with db_write_lock(db_path.parent, timeout=60.0):
                # The header records of each store, for the manifest below:
                # their hashes are the ones of the text that the parse read.
                unit_headers: list[list[dict] | None] = []
                for unit, parsed in parsed_units:
                    with transaction(conn):
                        syms_added, _, headers = store_symbols_for_unit(
                            conn, unit, config_hash, root,
                            vendor_patterns=vendor_patterns,
                            project_patterns=project_patterns_list,
                            index_refs=cfg_refs,
                            pre_parsed=parsed,
                        )
                        total_symbols += syms_added
                    unit_headers.append(headers)

                # Phase 3: resolve macro definitions after all TUs are stored.
                # Macro resolution needs the clang flags from one TU (they
                # are identical for TUs of the same file) and runs a
                # best-effort pass — failure is silently ignored.
                if parsed_units:
                    try:
                        from ...indexer.macros import resolve_and_update
                        first_unit = parsed_units[0][0]
                        resolve_and_update(conn, config_hash, first_unit.clang_args, first_unit.file.resolve(), cwd=root)
                    except Exception:  # nosec B110 — macro expansion is best-effort
                        pass

                # Phase 4: clean up orphan files — files that existed in
                # the previous index but are now absent from the TU list.
                from ...indexer.db import delete_orphan_files
                deleted_orphans = delete_orphan_files(conn, config_hash, root)
                if deleted_orphans:
                    log.debug("Orphan files cleaned up: %d", deleted_orphans)

                _update_manifest_after_reindex(
                    parsed_units, unit_headers, root, db_path.parent, config_hash,
                    transient_defines=transient_defines,
                )

                elapsed = round(time.monotonic() - t0, 2)
                result = {
                    "file": str(target),
                    "translation_units": len(matching),
                    "symbols_updated": total_symbols,
                    "elapsed_s": elapsed,
                }
                if skipped_tus:
                    result["skipped_tus"] = skipped_tus
                    result["skipped_count"] = len(skipped_tus)
                if not parsed_units and skipped_tus:
                    return 0, {"error": f"All {len(matching)} translation unit(s) failed to parse", "skipped_tus": skipped_tus}
            return total_symbols, result
        except (sqlite3.Error, WriteLockTimeout) as exc:
            return 0, {"error": f"DB error during reindex: {exc}"}

def _entries_for_listings(entries: list[dict], listings: list[tuple[str, str]]) -> list[int | None]:
    """Return the manifest entry that each parsed listing refreshes.

    *listings* holds ``(file, flags_hash)`` for each parsed unit, in order.
    The result holds the index of an entry for each listing, or None when
    the file has no entry left for it, and the caller adds a new entry.

    Two passes, and each entry goes to one listing only:

    1. A listing takes an entry of its file with the same flags hash.
    2. A listing that found none takes an entry of its file that no listing
       took, thus a listing whose flags changed refreshes the entry of the
       flags that the file had before.

    The first pass runs for all listings before the second.  In one pass, a
    listing with new flags could take the entry that a later listing
    matches exactly.

    The previous code refreshed the first entry of the file for each
    listing.  With three listings, the first entry was written three times,
    and the other two kept the old hashes: the staleness check reported
    the file stale after the reindex.
    """
    taken: set[int] = set()
    slots: list[int | None] = [None] * len(listings)
    by_file: dict[str, list[int]] = {}
    for idx, entry in enumerate(entries):
        by_file.setdefault(entry.get("file", ""), []).append(idx)
    for pos, (file, flags_hash) in enumerate(listings):
        for idx in by_file.get(file, []):
            if idx not in taken and entries[idx].get("flags_hash") == flags_hash:
                slots[pos] = idx
                taken.add(idx)
                break
    for pos, (file, _flags_hash) in enumerate(listings):
        if slots[pos] is not None:
            continue
        for idx in by_file.get(file, []):
            if idx not in taken:
                slots[pos] = idx
                taken.add(idx)
                break
    return slots


def _update_manifest_after_reindex(
    parsed_units: list,
    unit_headers: list[list[dict] | None],
    root: Path,
    db_dir: Path,
    config_hash: str,
    *,
    transient_defines: Collection[str],
) -> None:
    """Update manifest.json entries for reindexed translation units.

    The manifest tracks each TU's source hash and its set of included
    headers.  After re-parsing a file, the manifest entry must be
    refreshed so header-staleness detection (used by ``get_project_status``)
    reflects the file's current dependencies.

    compile_commands.json can list one file more than once, each time with
    other flags, and the manifest holds one entry for each listing.  Each
    parsed listing refreshes its own entry: see :func:`_entries_for_listings`.
    The entry gets the flags hash of the listing, with *transient_defines*
    as the index run computes it: the carry-over of the index run gives a
    listing the entry with its own flags hash, and a hash of another list
    matches no listing.

    When the manifest does not exist (first index, or manifest not
    enabled), this is a no-op — the ``load_manifest`` guard returns
    early.

    *unit_headers* holds, for each parsed unit, the header records that its
    store returned.  Their hashes, and the source hash of the entry, are the
    ones of the text that the parse read.  This function used to parse each
    unit a second time and to hash the disk: a file saved between the parse
    and this call gave the entry the hash of the new text beside the symbols
    of the old one, and the next index run found the unit current.
    """
    try:
        from ...indexer._parsed_text import file_of_parse, parsed_hash
        from ...indexer.config_hash import compute_flags_hash
        from ...indexer.manifest import (
            _intern_arguments,
            _is_generated_header,
            fold_headers,
            mark_entries_behind,
            merge_header_records,
            update_entry,
        )
        from ...indexer.manifest import (
            load as load_manifest,
        )
        from ...indexer.manifest import (
            save as save_manifest,
        )
        manifest_data = load_manifest(db_dir, config_hash)
        if manifest_data is not None:
            # The patterns the INDEX RUN used, read from the manifest it
            # wrote.  With None here _is_generated_header() answers False for
            # every header, so every record this function writes claimed
            # "generated": False.  After the end of vendor trust `generated`
            # is the only trust rule left, so that turned every build of a
            # re-indexed file into a full reparse.  Measured on the Zephyr project
            # variant nrf52840-dev: 27 generated headers dropped to 0.
            bdp = manifest_data.get("build_dir_patterns")
            if not bdp:
                bdp, _ = _reindex_build_patterns_for_generated(root)
            # Snapshot the shared header table BEFORE the loop.  A per-unit
            # difference would make the second unit read what the first one
            # just wrote as somebody else's change.
            table_before = {
                path: record.get("hash", "")
                for path, record in (manifest_data.get("headers") or {}).items()
            }
            refreshed: set[int] = set()
            listings: list[tuple[str, str]] = []
            for unit, _parsed in parsed_units:
                try:
                    rel = str(unit.file.resolve().relative_to(root))
                except ValueError:
                    rel = str(unit.file.resolve())
                flags = (
                    compute_flags_hash(unit.raw_entry, transient_defines=transient_defines)
                    if unit.raw_entry else ""
                )
                listings.append((rel, flags))
            slots = _entries_for_listings(manifest_data.get("entries", []), listings)
            for (unit, parsed), records, (tu_rel, flags_hash), slot in zip(
                parsed_units, unit_headers, listings, slots, strict=True,
            ):
                # A store without records parsed nothing: the entry keeps
                # what it says, and its old source hash makes the next run
                # parse the unit.
                if records is None:
                    continue
                # Records go into a per-unit table and are merged by
                # merge_header_records, so a header this re-parse is the first
                # to see still contributes its hash to the manifest's shared
                # map — without a downgrade of `generated` on the way.
                # `generated` comes from the patterns of the index run (bdp):
                # the store had none, see the comment on bdp above.
                header_records: dict[str, dict] = {}
                headers = fold_headers(
                    [{**record, "generated": _is_generated_header(record["path"], bdp)} for record in records],
                    header_records,
                )
                # "" when the TU is gone: an entry without a source hash
                # proves nothing, thus the next run parses the unit.
                source_hash = (
                    parsed_hash(parsed.tu, file_of_parse(parsed.tu, str(parsed.tu.spelling)))
                    if parsed.tu is not None else ""
                )
                if slot is not None:
                    update_entry(
                        manifest_data, slot, source_hash, headers, header_records,
                        flags_hash=flags_hash, arguments=unit.clang_args,
                    )
                    refreshed.add(slot)
                else:
                    # Same merge as update_entry uses.  This branch bypasses
                    # update_entry completely, so a plain .update() here is a
                    # second place where a False overwrites a True.
                    merge_header_records(manifest_data, header_records)
                    manifest_data.setdefault("entries", []).append({
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(root),
                        "arg_set": _intern_arguments(
                            unit.clang_args,
                            manifest_data.setdefault("arg_sets", []),
                        ),
                        "source_hash": source_hash,
                        "headers": headers,
                        "flags_hash": flags_hash,
                    })
                    refreshed.add(len(manifest_data["entries"]) - 1)
            # This function refreshed the SHARED header hashes, so the next
            # full index run sees no difference for the OTHER units that
            # include those headers — their rows still come from the old
            # text.  Flag them, or reindex_file hides the change from every
            # later run.
            changed = {
                path
                for path, record in (manifest_data.get("headers") or {}).items()
                if record.get("hash", "") != table_before.get(path, "")
            }
            behind = mark_entries_behind(manifest_data, changed, refreshed)
            if behind:
                log.info(
                    "reindex_file: %d entr(ies) flagged needs_reparse after "
                    "%d refreshed header(s)", behind, len(changed),
                )
            save_manifest(manifest_data, db_dir, config_hash)
    except OSError:
        log.debug("manifest.json update skipped during reindex_file", exc_info=True)


def _reindex_llm_analysis(
    conn: sqlite3.Connection,
    config_hash: str,
    cfg: Config,
    db_dir: Path,
    result: dict,
    *,
    project_root: Path,
) -> None:
    """Regenerate LLM symbol analysis for symbols affected by a reindex.

    Only runs when both ``cfg.llm.enabled`` and ``cfg.llm.analyze_symbols``
    are True.  Reads the stored ``analyze_vendor`` flag from the build
    config to determine whether vendor symbols should be re-analysed.

    Uses ``CacheClient`` to avoid redundant LLM calls for symbols whose
    source has not changed.  Failures are non-fatal — the result dict
    gets an ``analysis_warning`` key instead of a hard error.
    """
    if not (cfg.llm.enabled and cfg.llm.analyze_symbols):
        return
    try:
        from ...cache_client import CacheClient
        from ...indexer._llm_analysis import _build_llm_analysis
        try:
            stored_av = bool(
                conn.execute(
                    "SELECT analyze_vendor FROM build_configs WHERE config_hash = ?",
                    (config_hash,),
                ).fetchone()["analyze_vendor"]
            )
        except sqlite3.OperationalError:
            stored_av = False
        cc = CacheClient.from_config(cfg)
        try:
            _build_llm_analysis(
                conn, config_hash, cfg.llm, db_dir,
                write_lock_held=False, retry_unparseable=True,
                cache_client=cc, project_only=not stored_av,
                project_root=project_root,
            )
            conn.commit()
        finally:
            if cc:
                cc.close()
        analyzed_count = conn.execute(
            "SELECT COUNT(*) FROM llm_analysis a JOIN symbols s ON s.id = a.symbol_id WHERE s.config_hash = ?",
            (config_hash,),
        ).fetchone()[0]
        result["analysis_updated"] = analyzed_count
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        result["analysis_warning"] = f"LLM analysis skipped: {exc}"


def _reindex_overrides(
    conn: sqlite3.Connection,
    config_hash: str,
    cfg: Config,
    db_dir: Path,
    result: dict,
) -> None:
    """Rebuild virtual method override relationships after reindex.

    Only runs when ``cfg.index.index_refs`` is True (reference indexing
    must be enabled).  The override table maps virtual methods to their
    overrides in derived classes and is used by ``get_method_overrides``.

    Failures are non-fatal — an ``overrides_warning`` key is added to
    the result dict.
    """
    if not cfg.index.index_refs:
        return
    try:
        from ...indexer.runner import _build_overrides
        _build_overrides(conn, config_hash, db_dir, write_lock_held=False)
        conn.commit()
    except (sqlite3.Error, RuntimeError) as exc:
        result["overrides_warning"] = f"Override analysis skipped: {exc}"


def _reindex_pagerank(
    conn: sqlite3.Connection,
    config_hash: str,
    cfg: Config,
    result: dict,
) -> None:
    """Rebuild PageRank scores and the hotspot cache after reindex.

    PageRank runs on the call graph to rank functions by architectural
    importance.  The hotspot cache pre-computes the top-N most-called
    functions for ``find_hotspots``.  Both depend on the reference index
    (``cfg.index.index_refs`` must be True).

    Failures are non-fatal.
    """
    if not cfg.index.index_refs:
        return
    try:
        from ...indexer.runner import _build_hotspot_cache, _build_pagerank
        _build_pagerank(conn, config_hash, write_lock_held=False)
        _build_hotspot_cache(conn, config_hash)
        conn.commit()
    except (sqlite3.Error, RuntimeError) as exc:
        result["pagerank_warning"] = f"PageRank/hotspot recompute skipped: {exc}"


def _reindex_embeddings(
    conn: sqlite3.Connection,
    config_hash: str,
    cfg: Config,
    db_dir: Path,
    target: Path,
    root: Path,
    result: dict,
) -> None:
    """Regenerate vector embeddings for symbols in the reindexed file.

    Only runs when ``cfg.llm.enabled`` is True (the embedding model is
    configured through the LLM subsystem).  Queries for all symbol IDs
    belonging to the file path, then calls ``_build_embeddings`` with
    the filtered ID set to avoid recomputing embeddings for the entire
    index.

    When no symbols are found for the file path (possible for symlink
    or out-of-root files), the warning is logged but no error is raised.
    """
    if not cfg.llm.enabled:
        return
    from ...indexer.ops import _normalize_file_path
    try:
        from ...indexer.runner import _build_embeddings as _reembed
        file_symbol_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM symbols WHERE config_hash = ? AND file_path = ?",
                (config_hash, _normalize_file_path(str(target), root)),
            ).fetchall()
        ]
        if file_symbol_ids:
            _reembed(conn, config_hash, cfg.llm, db_dir, symbol_ids=file_symbol_ids)
        else:
            log.warning(
                "No symbols found for path %s in DB — skipping embedding regeneration "
                "(this may happen for files outside the project root or after a symlink change)",
                target,
            )
        conn.commit()
        reembedded = conn.execute(
            "SELECT COUNT(DISTINCT symbol_id) FROM embeddings e "
            "JOIN symbols s ON s.id = e.symbol_id WHERE s.config_hash = ?",
            (config_hash,),
        ).fetchone()[0]
        result["embeddings_updated"] = reembedded
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        result["embedding_warning"] = f"Embedding regeneration skipped: {exc}"


def _reindex_post_write_phases(
    conn: sqlite3.Connection,
    config_hash: str,
    cfg: Config,
    total_symbols: int,
    db_dir: Path,
    target: Path,
    result: dict,
    root: Path,
) -> dict:
    """Run post-write enrichment phases after reindex_file.

    Each phase is independent — failure in one does not prevent others
    from running.  The phases are:

    1. LLM symbol analysis (function descriptions)
    2. Virtual method override relationships
    3. PageRank scores and hotspot cache
    4. Vector embeddings for semantic search

    When ``total_symbols <= 0``, all phases are skipped (nothing was
    added or updated).

    The coverage warning for a header is NOT set here.  It used to be, and
    that put it behind two gates it has nothing to do with: this function
    returns early when ``total_symbols <= 0``, and the caller skips it
    entirely when ``with_analysis=False`` — the mode the background
    auto-reindex uses.  A caller that re-parsed a header through one unit
    has to hear about it whether or not the enrichment phases ran, thus
    _header_coverage_warning() is applied by the caller.
    """
    if total_symbols <= 0:
        return result

    _reindex_llm_analysis(conn, config_hash, cfg, db_dir, result, project_root=root)
    _reindex_overrides(conn, config_hash, cfg, db_dir, result)
    _reindex_pagerank(conn, config_hash, cfg, result)

    _reindex_embeddings(conn, config_hash, cfg, db_dir, target, root, result)
    return result


def _header_coverage_warning(
    target: Path, matching: list, via_header: bool
) -> str | None:
    """Say that a header was re-parsed through one compilation context.

    Another unit can include the same header under a different set of
    ``#define`` values and still hold symbols from before the edit.  Only a
    full index covers every context, and the caller has to know that the
    answer is partial.

    *via_header* covers the manifest fallback.  The suffix test covers a
    header that compile_commands.json does list, which some build systems
    emit.  The suffix test alone is not enough: it names ``.h`` and
    ``.hpp``, while an ``#include`` reaches ``.hh``, ``.hxx``, ``.inc`` and
    extension-less headers just as well, and the fallback resolves those
    too.
    """
    if not (via_header or (len(matching) == 1 and target.suffix in HEADER_EXTENSIONS)):
        return None
    return (
        "Header re-indexed via one TU. Other TUs including this header "
        "may still have stale symbols — run 'fw-context index' for full accuracy."
    )


# ── moved from server.py ──
def reindex_file_impl(
    file_path: Annotated[
        str,
        Field(
            description="Absolute or project-relative path to the file to re-parse. A source file must have an entry in compile_commands.json. A header is re-parsed through one translation unit that includes it, and the result then carries a warning that other units can still hold stale symbols.",
            min_length=1,
        ),
    ],
    project_root: Annotated[
        str | None, Field(description="Project root directory. Auto-detected from cwd if omitted.")
    ] = None,
    *,
    with_analysis: Annotated[
        bool,
        Field(
            description="When True (default), also regenerates LLM symbol analysis and method override relationships — slower but produces a fully up-to-date index. Set False for a fast symbol-only update (used by background auto-reindex)."
        ),
    ] = True,
) -> dict:
    """Re-parse a single source file with libclang and update its symbols in the index.

    Not read-only — uses the exact compiler flags from ``compile_commands.json``.
    Use after editing a file to keep the index current without a full rebuild.

    A source file must be listed in ``compile_commands.json``.  A header is
    not listed there — compile_commands.json names translation units — so it
    is re-parsed through one unit that includes it, taken from the manifest.
    That answer describes a single compilation context, thus the result
    carries a ``warning``: another unit can see the header under a different
    set of ``#define`` values and still hold stale symbols.  Only a full
    ``fw-context index`` covers every context.  One unit and not all of them
    is a cost decision — an application header reaches a median of 3 units
    but as many as 266 on a real project, at tens of seconds each.

    Also regenerates LLM analysis and method override relationships for
    affected symbols when ``with_analysis=True``.  The analysis is
    content-addressed, thus an unchanged symbol is never re-analysed.

    Args:
        file_path: Path to the file to re-parse.  A source file must be in
            compile_commands.json; a header goes through one including unit.
        project_root: Project root directory. Auto-detected if omitted.
        with_analysis: When True (default), also regenerates LLM symbol analysis,
            method override relationships, PageRank, and embeddings. Set False
            for a fast symbol-only update (used by background auto-reindex).

    A file that is gone from disk but still in the index has its records
    removed; the result is then {file, symbols_removed, action: "deleted"}.

    Returns:
        dict: {file, translation_units, symbols_updated, elapsed_s}, plus
        when they apply: skipped_tus and skipped_count (units that failed to
        parse), warning (header coverage), analysis_updated and
        embeddings_updated (counts for the whole build, not this file), and
        analysis_warning, overrides_warning, pagerank_warning, or
        embedding_warning when a phase failed.

        On failure the dict holds ``error`` with the reason, and can add
        ``skipped_tus`` or ``action``.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root, skip_ready_check=True)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}

    # Config read via a short-lived read-only connection.
    ro_conn = _quick_open_readonly(db_path)
    try:
        cfg_data = get_active_config(ro_conn, project_id)
        if not cfg_data:
            return {"error": "No build config indexed."}
        config_hash = cfg_data["config_hash"]
    finally:
        ro_conn.close()

    executor = get_executor(db_path)

    def _query(conn, config_hash):
        # WRITE path: the executor is not read-only — writes are
        # serialized with reads by the same lock.  A collision with a
        # concurrently running CLI indexer is absorbed by the executor's
        # 120 s busy_timeout (the CLI indexer keeps its own 10 s
        # fail-fast).  Timeout is enforced by _wrap_tool, not here.
        target, error = _reindex_resolve_target(file_path, root)
        if error:
            return error
        if not target.exists():
            return _reindex_cleanup_deleted_file(conn, cfg_data, target, root, db_path)

        # The manifest of THIS build carries the vendor set the index run
        # applied.  reindex_file must write is_project with that same
        # boundary, or one file ends up on the other side of it.
        #
        # Loaded before the match because it answers a second question:
        # compile_commands.json lists no headers, so a header target finds
        # its translation unit only through the manifest.  One load serves
        # both — it costs 14 ms on a 9.3 MB manifest.
        from ...indexer.manifest import load as load_manifest

        build_manifest = load_manifest(db_path.parent, config_hash)

        matching, via_header, error = _reindex_match_tus(
            target, cfg_data, manifest=build_manifest, project_root=root,
        )
        if error:
            return error

        vendor_patterns, project_patterns_list = _reindex_build_patterns(
            cfg, root, build_manifest
        )
        total_symbols, result = _reindex_parse_and_store(
            conn, matching, cfg_data, cfg.index.index_refs,
            root, db_path, target, vendor_patterns, project_patterns_list,
            cfg.index.transient_defines,
        )
        if "error" in result:
            return result

        # Set before the with_analysis gate: the warning describes how much
        # of the header the re-parse covered, which does not depend on
        # whether the enrichment phases run.
        coverage_warning = _header_coverage_warning(target, matching, via_header)
        if coverage_warning:
            result["warning"] = coverage_warning

        if not with_analysis:
            return result

        return _reindex_post_write_phases(
            conn, config_hash, cfg, total_symbols, db_path.parent,
            target, result, root,
        )

    try:
        return executor.execute_sync(_query, config_hash)
    finally:
        # Best-effort statistics refresh after the write.  Runs through
        # the executor so it serializes with other queries.
        def _optimize(conn, _ch):
            conn.execute("PRAGMA optimize")

        try:
            executor.execute_sync(_optimize, config_hash)
        except sqlite3.Error:
            pass


def _reindex_resolve_target(
    file_path: str, root: Path
) -> tuple[Path, dict | None]:
    """Resolve *file_path* to an absolute path relative to *root*.

    When the path is relative, it is joined with *root* and resolved.
    When absolute, it is resolved directly.  Resolution follows symlinks
    so the path matches the canonical form in compile_commands.json.

    Returns ``(target, None)`` on success or ``(target, error_dict)``
    when the path cannot be resolved.  The error_dict arm is reserved
    for future path-safety checks; currently this function never fails.
    """
    target = Path(file_path)
    if not target.is_absolute():
        target = (root / target).resolve()
    else:
        target = target.resolve()
    return target, None


def _tu_for_header(
    target: Path, manifest: dict | None, project_root: Path
) -> Path | None:
    """Return one translation unit that includes *target*, or None.

    A header is never its own entry in compile_commands.json, thus the
    direct match cannot find it.  The manifest records each TU with the
    header paths it pulled in, which answers the reverse question.

    ONE unit, not all of them.  The fan-out is large: measured on
    the Mbed project, an application header reaches a median of 3 translation
    units but ``sdk_config.h`` reaches 266, and one re-parse costs about
    42 s on that project.  Re-parsing every including unit would turn a
    single tool call into hours, thus the caller warns that the answer
    comes from one compilation context and points at a full index.

    The choice is the lowest path in sort order, not the first entry.
    Manifest order follows compile_commands.json, which a rebuild can
    reshuffle, and a tool that picked a different unit on every call
    would be hard to reason about.

    Paths are compared as strings.  The manifest already stores resolved
    forms — relative to the project root when inside it, absolute
    otherwise (see ops.py) — and *target* arrives resolved from
    _reindex_resolve_target, thus another resolve() per header would only
    add a syscall for each of the tens of thousands of header references
    a large manifest holds.
    """
    if not manifest:
        return None
    root = project_root.resolve()
    target_str = str(target)
    best: str | None = None
    for entry in manifest.get("entries") or ():
        tu_file = entry.get("file")
        if not tu_file:
            continue
        for header in entry.get("headers") or ():
            header_abs = header if Path(header).is_absolute() else str(root / header)
            if header_abs != target_str:
                continue
            tu_abs = tu_file if Path(tu_file).is_absolute() else str(root / tu_file)
            if best is None or tu_abs < best:
                best = tu_abs
            break
    return Path(best) if best is not None else None


def _reindex_match_tus(
    target: Path,
    cfg_data: sqlite3.Row,
    *,
    manifest: dict | None = None,
    project_root: Path | None = None,
) -> tuple[list, bool, dict | None]:
    """Find compilation units in compile_commands.json that build *target*.

    Parses the compile_commands.json file and matches by absolute,
    resolved file path.  A single source file may appear in more than one
    unit — the same .cpp built with two flag sets — and every match is
    returned so symbols from each context are updated.

    A header matches nothing this way, because compile_commands.json
    lists translation units and a header is not one.  For that case
    *manifest* supplies one unit that includes the header; see
    _tu_for_header for why one and which one.

    Returns ``(matching_tus, via_header, None)`` on success, where
    *via_header* says the units came from the manifest fallback and the
    result therefore describes a single compilation context.  On failure
    it returns an empty list with an ``error_dict`` — the
    compile_commands.json is missing, or nothing builds the target.
    """
    cc_path = Path(cfg_data["compile_commands_path"])
    if not cc_path.exists():
        return [], False, {"error": f"compile_commands.json not found: {cc_path}"}
    # list(), not the iterator parse_cc returns: the header fallback below
    # walks the units a second time, and a generator is empty by then.
    units = list(parse_cc(cc_path))
    matching = [u for u in units if Path(u.file).resolve() == target]
    if matching:
        return matching, False, None

    tu_path = _tu_for_header(target, manifest, project_root or target.parent)
    if tu_path is not None:
        # The manifest can name a unit that a later build dropped from
        # compile_commands.json.  An empty result here falls through to
        # the error below, which is the same answer as before.
        via = [u for u in units if Path(u.file).resolve() == tu_path]
        if via:
            return via[:1], True, None

    # The whole path, not target.name.  The basename made the refusal name
    # something the caller never wrote: "../../../../etc/passwd" came back
    # as "passwd not found in compile_commands.json", which reads as a fact
    # about a file of the project and says nothing about the path that was
    # refused.
    return [], False, {"error": f"{target} is not built by this project — it has no entry in compile_commands.json, and no indexed translation unit includes it."}


def _effective_vendor_patterns(
    db_dir: Path, config_hash: str, root: Path
) -> list[str]:
    """Return the vendor set stored with THIS build, or [] when unknown.

    Read-only and best-effort: this feeds a status report, so a manifest that
    cannot be read must not make the report fail.
    """
    from ...indexer.manifest import load as load_manifest

    try:
        manifest = load_manifest(db_dir, config_hash)
    except OSError:
        log.debug("manifest unreadable for the status report", exc_info=True)
        return []
    if manifest is None:
        return []
    return list(manifest.get("vendor_patterns") or [])


def _reindex_build_patterns_for_generated(root: Path) -> tuple[list[str], None]:
    """Return build-output patterns for a manifest that stores none.

    Only for an index written before the manifest carried the key.  The
    manifest is the better source: it holds the value the index run applied,
    while this one re-detects and can answer differently after the build
    system changes.
    """
    from ...indexer.build import detect_build_system
    from ...indexer.builders import registry

    system = detect_build_system(root)
    builder_cls = registry.get(system) if system else None
    if builder_cls is None:
        return [], None
    from ...utils import build_dir_patterns_with_fw_context

    return build_dir_patterns_with_fw_context(
        list(builder_cls().get_build_dir_patterns(root))
    ), None


def _reindex_build_patterns(
    cfg, root: Path, manifest: dict | None = None
) -> tuple[list[str], list[str]]:
    """Collect vendor-exclude and project-include file path patterns.

    Returns two lists of SQL LIKE patterns:

    *vendor_patterns* — paths that the SDK detector and the user config
    mark as vendor/SDK code.  These patterns exclude symbols from the
    ``is_project`` flag and from ``project_only`` queries.

    *project_patterns_list* — paths explicitly listed as project code
    in the user config.  These override the auto-detected vendor
    boundary when the detector misclassifies a path.

    Pass *manifest* whenever the caller has it.  The set stored there is the
    one the index run applied, and to derive a second one here would give
    this build two different boundaries.
    """
    from ...indexer.sdk_detect import _normalize_patterns, vendor_patterns_for_build

    vendor_patterns = vendor_patterns_for_build(
        manifest, root, build_system=cfg.build.system
    )
    if cfg.index.vendor_paths:
        vendor_patterns.extend(_normalize_patterns(cfg.index.vendor_paths))
    project_patterns_list = (
        _normalize_patterns(list(cfg.index.project_paths))
        if cfg.index.project_paths
        else []
    )
    return vendor_patterns, project_patterns_list


# ── moved from server.py ──
def reindex_file(
    file_path: Annotated[str, Field(description="Path to the file to re-parse. A source file must be in compile_commands.json. A header goes through one translation unit that includes it, and the result then carries a warning about the other units.", min_length=1)],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
) -> dict:
    """Re-parse a single source file with libclang and update its symbols in the index.

    Not read-only — uses the exact compiler flags from ``compile_commands.json``.
    Use after editing a file to keep the index current without a full rebuild.

    A source file must be listed in ``compile_commands.json``.  A header is
    not listed there, thus it is re-parsed through one unit that includes
    it.  That answer covers a single compilation context, thus the result
    carries a ``warning`` — only a full ``fw-context index`` covers every
    unit that includes the header.

    Also regenerates LLM analysis and method override relationships for
    affected symbols when those features are enabled in config.  An
    unchanged symbol keeps its stored analysis.

    Args:
        file_path: Path to the file to re-parse.  A source file must be in
            compile_commands.json; a header goes through one including unit.
        project_root: Project root directory. Auto-detected if omitted.

    A file that is gone from disk but still in the index has its records
    removed; the result is then {file, symbols_removed, action: "deleted"}.

    Returns:
        dict: {file, translation_units, symbols_updated, elapsed_s}, plus
        optional skipped_tus, skipped_count, warning, analysis_updated,
        embeddings_updated, and per-phase ``*_warning`` keys — the same
        shape as ``reindex_file_impl``.  On failure the dict holds
        ``error``.
    """
    return reindex_file_impl(file_path, project_root, with_analysis=True)


# ── moved from server.py ──
def check_ollama(
    project_root: Annotated[
        str | None, Field(description="Project root. Auto-detected if omitted. "
        "Used to locate LLM config. Falls back to auto-detection when omitted.")
    ] = None,
) -> dict:
    """Check whether the LLM backend is running and the configured embedding/chat model is installed.

    Read-only: yes. No side effects. Call before smart_search,
    semantic_search, or explain_symbol (when on-demand fallback is
    expected — pre-computed analysis returns instantly without the LLM
    backend).

    Args:
        project_root: Project root. Auto-detected if omitted. Used to
            locate the project's LLM configuration.

    Returns:
        dict: {ollama_enabled (bool), status (str — "ok"|"disabled"|
        "not_configured"|"model_missing"|"embedding_unavailable"|"error"),
        ollama_running (bool), ollama_url (str), configured_model (str),
        num_ctx (int), installed_models (list[str]),
        chat_api (dict — ``{configured (bool), model (str)}``: whether an
        external chat API replaces Ollama, and the model it names; a
        configured one adds endpoint, format, and compliance_warning for an
        external host),
        configured_embed_model (str), embedding_installed (bool),
        embedding_backend (str — "ollama", or "local" for a
        sentence-transformers or ``ft://`` model, which needs no Ollama:
        it counts as installed, and with chat on an external API a
        stopped Ollama still gives "ok"),
        message (str, when status is not "ok"), model_details (list[dict]),
        suggest_cloud (bool), vec_available (bool),
        vec_error (str, optional), debug_log (str, optional — only when
        debug logging is enabled)}

        ollama_url, num_ctx, installed_models, model_details,
        configured_embed_model, and embedding_installed are present only
        when Ollama runs; configured_model also needs chat on Ollama.
        ``"disabled"`` gives only status, ollama_enabled,
        ollama_running, configured_model, num_ctx, and message;
        embedding_backend is present on every other status.
    """
    try:
        _, cfg, _, _ = _resolve_context(project_root)
    except RuntimeError:
        # Project has no index — irrelevant for check_ollama (only needs
        # LLM config).  Fall back to loading config directly without
        # requiring an active index.
        from pathlib import Path

        from fw_context_mcp.config.settings import load

        cfg = load(Path(project_root) if project_root else None)
    if not cfg.llm.enabled:
        # LLM is explicitly disabled in config.  Return immediately
        # with a message that explains the implications for tools that
        # depend on the LLM backend.
        return {
            "status": "disabled",
            "ollama_enabled": False,
            "ollama_running": False,
            "configured_model": cfg.llm.model,
            "num_ctx": cfg.llm.num_ctx,
            "message": (
                "LLM backend is disabled in config ([llm] enabled = false). "
                "explain_symbol with pre-computed analysis (default) returns instantly. "
                "Without analysis, it returns source + explain_prompt for the agent to answer. "
                "smart_search will use raw text queries."
            ),
        }
    # Delegates to check_setup which probes the Ollama server / chat API,
    # lists installed models, and determines the status string.
    result = check_setup(cfg.llm)
    result["ollama_enabled"] = True

    # sqlite-vec is needed for semantic_search embeddings.
    # Report availability so tools can degrade gracefully.
    from ...deps import _vec_available
    vec_ok, vec_err = _vec_available()
    result["vec_available"] = vec_ok
    if vec_err is not None:
        result["vec_error"] = vec_err

    return result


def _dep_to_schema(r) -> dict:
    """Map one ``DepCheckResult`` to the ``{name, status, critical, message, action}`` schema.

    The optional ``action`` carries a human-readable ``message`` (what is
    wrong or what to do) and an exact shell ``command`` (``fix_cmd``).
    When ``instructions`` merely repeats ``fix_cmd`` (the common pip/apt
    one-liner case), the status ``message`` is used as the human part so
    ``action.message`` is not a bare duplicate of ``action.command``.
    ``status="skipped"`` passes through unchanged — it signals a missing
    prerequisite, not a failure.
    """
    d: dict = {"name": r.name, "status": r.status, "critical": r.critical, "message": r.message}
    if r.fix_cmd or r.instructions:
        human = r.instructions or r.message
        if r.fix_cmd and human.strip() == r.fix_cmd.strip():
            human = r.message
        d["action"] = {"message": human, "command": r.fix_cmd}
    return d


# The checks that need no project config: they run also before `fw-context
# init`.  libclang-so needs only libclang-python, thus the signal "missing
# libclang .so → apt install libclang-18-dev" works before the first init.
_PRE_INIT_CHECKS = frozenset({"pysqlite3", "sqlite-vec", "libclang-python", "libclang-so", "watchfiles", "tomli-w"})


def check_dependencies(
    project_root: Annotated[
        str | None,
        Field(description="Project root. Auto-detected if omitted. Pass explicitly when the project is not the server cwd."),
    ] = None,
) -> list[dict]:
    """Run the dependency audit of the installation. Read-only, and it creates no file.

    Call it when get_project_status or another tool reports a problem of
    the installation, or before the first ``fw-context init``.  It is not a
    call for each session: it runs every check of the installation.

    Each entry gives what to do in ``action``: a ``message`` for a human,
    and the shell ``command`` that repairs it.  ``status="skipped"`` means a
    prerequisite is missing (e.g. ``libclang-so`` skipped because
    ``libclang-python`` is absent) — not a failure.

    In a project that is not initialized, only the checks that need no
    project config run: the Python packages and libclang.  The Ollama,
    model, database and build checks need the config.

    Args:
        project_root: Project root directory. Auto-detected from CWD if omitted.

    Returns:
        list[dict]: one dict per check, each with ``name``, ``status``,
        ``critical`` (bool), ``message``, and an optional ``action``
        ({message, command}).
    """
    from ...deps import run_full_check

    root = resolve_project_root(project_root)
    if load_config(root).project.id:
        results = run_full_check(project_root=str(root))
    else:
        results = run_full_check(project_root=str(root), subset=set(_PRE_INIT_CHECKS))
    return [_dep_to_schema(r) for r in results]


def get_project_info(
    project_id: Annotated[str, Field(description="Project ID (UUID4 hex) to look up.")] = "",
) -> dict:
    """Return project metadata (name, type, root_path) for a project ID.

    Looks up the global project registry at ``~/.fw-context/projects.db``.
    Use this to identify a project from its UUID4 — find out what build
    system it uses, its name, and where it was last indexed.

    Read-only. No side effects.

    Args:
        project_id: Project ID (UUID4 hex) to look up.

    Returns:
        dict: {project_id, name, project_type, root_path, created_at, updated_at}
        or {"error": "..."} when the project_id is not registered.

        On failure the dict holds only ``error`` with the reason.
    """
    from ...config.global_db import get_project_by_id

    if not project_id:
        # Empty string is the default — the LLM must be told that a
        # project_id is necessary before this tool can return anything.
        #
        # The message names the two tools that give the value.  It used to
        # say "from fw-context init", which is a command the caller cannot
        # run to answer a question about a project that is already indexed.
        return {
            "error": "project_id is necessary. Call list_projects for the id of every "
            "indexed project, or get_project_status for the id of this one."
        }
    result = get_project_by_id(project_id)
    if result is None:
        # The UUID was not found in the global registry (~/.fw-context/projects.db).
        # This can happen when the project was deleted or the registry was reset.
        return {"error": f"Project '{project_id}' not found in the global registry."}
    return result


def configure_llm(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    chat_api_base: Annotated[
        str | None,
        Field(
            description=(
                "Chat API URL. None = use local Ollama for chat. "
                "Auto-detects format: :11434 or /api/generate -> Ollama, "
                "/v1 or bare host -> OpenAI-compatible. "
                "Examples: 'https://api.deepseek.com/v1' (DeepSeek), "
                "'http://localhost:4000' (LiteLLM), "
                "'http://localhost:8080/v1' (llama.cpp). "
                "WARNING: external URLs send source code to that host."
            )
        ),
    ] = None,
    chat_api_key: Annotated[
        str | None,
        Field(description="API key for cloud/proxy APIs. None for local/no-auth."),
    ] = None,
    chat_api_format: Annotated[
        str | None,
        Field(description="Format override: 'ollama' or 'openai'; 'auto' returns to detection from the URL. "
              "None = keep current."),
    ] = None,
    model: Annotated[
        str | None,
        Field(description="Chat model name. None = keep current."),
    ] = None,
    embed_model: Annotated[
        str | None,
        Field(description="Embedding model: an Ollama tag, a sentence-transformers name (e.g. 'BAAI/bge-small-en-v1.5'), or 'ft://latest'. None = keep current."),
    ] = None,
    auto_pull: Annotated[
        bool | None,
        Field(description="Auto-pull models on 404 (Ollama only). False for intranet. None = keep current."),
    ] = None,
    stream: Annotated[
        bool | None,
        Field(
            description=(
                "Stream chat responses via SSE (both OpenAI-compatible and Ollama-native). "
                "True = send stream:true, consume SSE chunks — avoids reverse-proxy idle "
                "timeouts (nginx 60s, Cloudflare 100s). None = keep current setting."
            )
        ),
    ] = None,
) -> dict:
    """Configure LLM settings for the current project.

    Writes to ``<project>/.fw-context/local.toml`` ONLY (gitignored,
    per-developer). Does NOT modify the global config or the shared
    project ``config.toml``. After writing, tests the configuration
    by making a simple API call (skipped when LLM is disabled).  When the
    test fails, the previous ``local.toml`` is restored and ``status`` is
    ``"error"``.  A call with no change returns ``"error"`` and writes
    nothing.

    IMPORTANT: When ``chat_api_base`` points to an external host, source
    code snippets in chat prompts will be sent to that endpoint. Ensure
    this complies with your organization's data security policies.
    Consider using local Ollama or an internal API proxy first.

    Args:
        project_root: Project root directory. Auto-detected if omitted.
        chat_api_base: Chat API URL (see description for format details).
        chat_api_key: Bearer token for cloud/proxy APIs.
        chat_api_format: Override auto-detection: "ollama" or "openai";
            "auto" returns to detection from the URL.  None keeps the
            current value.
        model: Chat model name.
        embed_model: Embedding model: an Ollama tag, a sentence-transformers
            name (``BAAI/...``, needs the ``st`` extra), or ``ft://``.
        auto_pull: Whether to auto-pull models on 404.  None keeps the
            current value.
        stream: Stream chat responses via SSE. True avoids reverse-proxy idle timeouts.

    Returns:
        dict: {status ("ok"|"error"), chat_api (dict — configured, endpoint,
        format, model), model (str), auto_pull (bool), stream (bool),
        test_latency_s (float, on success), test_response (str, on success),
        compliance_warning (str, when chat_api_base is external),
        message (str)}
    """
    from ...config.settings import ProjectNotInitializedError

    try:
        _, cfg, _, root = _resolve_context(project_root, skip_ready_check=True)
    except ProjectNotInitializedError as e:
        # configure_llm writes local.toml — the project must be initialized
        # so the config directory (.fw-context/) exists.
        return {
            "status": "error",
            "message": (
                f"Project is not initialized. Run 'fw-context init' in the "
                f"project root first. ({e.root})"
            ),
        }

    # Collect only the parameters the caller passed: None means "keep
    # current".  A value equal to the current one is not a change, so a
    # call that repeats the config writes nothing.
    if chat_api_format is not None and chat_api_format not in ("auto", "ollama", "openai"):
        return {
            "status": "error",
            "message": f"Invalid chat_api_format {chat_api_format!r} — use 'auto', 'ollama', or 'openai'.",
        }
    updates: dict[str, object] = {}
    if chat_api_base is not None:
        updates["chat_api_base"] = chat_api_base
    if chat_api_key is not None:
        updates["chat_api_key"] = chat_api_key
    if chat_api_format is not None and chat_api_format != cfg.llm.chat_api_format:
        updates["chat_api_format"] = chat_api_format
    if model is not None:
        updates["model"] = model
    if embed_model is not None:
        updates["embed_model"] = embed_model
    if auto_pull is not None and auto_pull != cfg.llm.auto_pull:
        updates["auto_pull"] = auto_pull
    if stream is not None:
        updates["stream"] = stream

    if not updates:
        return {
            "status": "error",
            "message": "No configuration changes to write — all parameters are None or equal to the current values.",
        }

    # Delegate to the shared write+test implementation (llm/configure.py).
    # It writes only local.toml — never global or committed config —
    # reloads the config, and tests the endpoint.
    from ...llm.configure import configure_llm_core

    return configure_llm_core(Path(root), updates, chat_api_base=chat_api_base)
