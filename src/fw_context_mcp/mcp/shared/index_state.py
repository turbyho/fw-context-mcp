"""The state of the index of one build, and the advice that goes with it.

An LLM that reads an answer of an index that is not current must know what
to do: wait for the index run that updates it, or ask the user for a
command.  Each query answer and each build in the two status tools thus
gets one of these states:

- ``None``: the checks found no reason, see below.
- ``"reindexing"``: the index is not current, and an index run is active.
- ``"pending"``: the index is not current, and the daemon has work that
  it did not start or complete.
- ``"failed"``: the index is not current, and the last index run stopped
  without an index (``autobuild.read_problem``).
- ``"not_scheduled"``: the index is not current, and nothing updates it.

The status tools do the full check of a build.  A query does only the
checks that cost no scan (:func:`annotate_index_state`), thus a query
without a state can still read an index that the full check finds old:
a changed header, a changed file that the answer does not name, or a new
source file when the answer is not empty.

WHY a state for each build, and not for the project: one build can be
current while another is not.  The run and the marker of a failed run are
facts of the project.  A run gives its state to each build that is not
current, and leaves a current build at ``None``.  The marker names no
build, thus it gives the state ``"failed"`` to each build.

WHY a run wins over ``"failed"``: the marker of a failed run stays until
a run ends well.  During the run that repairs it, "failed" gives the
advice to start a second run of the same work.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: The states in which the caller must wait: an index run updates the
#: index.  Each other state needs no wait.
RUN_STATES = frozenset({"reindexing", "pending"})

#: The text that each answer of an active run ends with.  WHY it names the
#: user: the operator can decide to continue with the old index, and the
#: LLM must not decide that alone.
WAIT_ADVICE = (
    "Wait until the run ends, then repeat the call, or call get_project_status "
    "after about 30 s. Use the answer of an index that is not current only "
    "when the user tells you to continue with the old index."
)


def index_state(behind: bool, *, run_state: str | None, failed: bool) -> str | None:
    """Return the state of the index of one build.

    *behind* is True when the index of the build is not current.
    *run_state* is the state of the project from
    ``background.read_reindex_state``.  *failed* is True when the last
    index run stopped without an index.  See the module docstring for the
    order.
    """
    if not behind:
        return None
    if run_state in RUN_STATES:
        return run_state
    if failed:
        return "failed"
    return "not_scheduled"


#: The action of ``"not_scheduled"`` when no reason needs a build.
PLAIN_ACTION = "run `fw-context index`"


def index_state_advice(
    state: str, reasons: list[str], *, problem_text: str = "", action: str = PLAIN_ACTION,
) -> str:
    """Return the text that tells the caller what to do in *state*.

    *reasons* say why the index of the build is not current.
    *problem_text* is the text of the failed run, and it names the command
    that repairs it.  *action* is the action of ``"not_scheduled"``, an
    instruction without a subject: after a checkout, and for a source file
    that compile_commands.json does not cover, only a build writes the file
    list, and :func:`_build_action` names it.  An empty *action* means that
    the action is not known: the advice then sends the caller to
    ``get_project_status``, which names it.
    """
    if state == "failed":
        return f"{problem_text} The answer below comes from the last index run that ended well."
    head = f"The index of this build is not current: {'; '.join(reasons)}."
    if state == "reindexing":
        return f"{head} An index run is in progress. {WAIT_ADVICE}"
    if state == "pending":
        return f"{head} The watcher scheduled an index run. {WAIT_ADVICE}"
    tail = "The answer below comes from the last index run."
    if not action:
        return (
            f"{head} No index run is in progress or scheduled, and only a build "
            "repairs this. Call get_project_status, and give the user the "
            f"command that it names. {tail}"
        )
    return (
        f"{head} No index run is in progress or scheduled. Tell the user to "
        f"{action}. get_project_status names the command for each reason. {tail}"
    )


def _build_action(root: Path, *, checkout_moved: bool) -> str:
    """Return the action that writes the file list of the project at *root* again.

    The status tools give the same action (``autobuild.build_advice``): a
    query and ``get_project_status`` must not name two commands.  A new
    source file that fw-context builds on its own needs only the next
    index run, as the status tools say.  After a checkout, a run without a
    build reads compile_commands.json of the old checkout, thus only the
    build counts.  An error gives "": the advice then sends the caller to
    ``get_project_status``.
    """
    from ...config import load as load_config
    from ...indexer.autobuild import AutobuildState, build_advice, project_state
    from ...utils import SAFE_EXCEPT

    try:
        cfg = load_config(root)
        if not checkout_moved and project_state(root, cfg) is AutobuildState.WILL_BUILD:
            return PLAIN_ACTION
        return build_advice(root, cfg)
    except SAFE_EXCEPT:
        log.debug("The build of %s is not known", root, exc_info=True)
        return ""


def manifest_reason(verification: str) -> str:
    """Return why an index with the manifest verification *verification* is not current.

    "" for ``"full"`` and for ``"partial"``.  ``"none"`` means that the run
    wrote no manifest.json, thus fw-context cannot find a changed header.
    ``"partial"`` means that it wrote one, with the headers, and that the
    embeddings are not complete (``_postprocess``): the index is current,
    and the status tools ask the caller to tell the user
    (``maintenance.PARTIAL_EMBEDDINGS``).  WHY "fw-context" and not
    a tool: the text goes into the answers of the two status tools and of
    each query.
    """
    if verification in ("full", "partial"):
        return ""
    return (
        "the last index run wrote no manifest.json, and fw-context cannot find a "
        f"changed header in this index (manifest verification: {verification})"
    )


@dataclass(frozen=True)
class _AnswerFacts:
    """What the answer itself says about the index: see :func:`answer_facts`."""

    reasons: list[str]
    needs_build: bool


def answer_facts(result) -> _AnswerFacts:
    """Return the reasons that the warnings of the answer *result* give.

    ``stale.annotate_stale`` and the source handlers set ``stale`` on a
    dict, and on a record of a list.  The warning about source files that
    compile_commands.json does not cover also sets ``unindexed_sources``.
    WHY each record of a list: a list can hold more than one leading
    warning, and their order is not part of the contract.  WHY the keys and
    not the text: a change of a text must not change a state.
    """
    records = [result] if isinstance(result, dict) else result if isinstance(result, list) else []
    records = [row for row in records if isinstance(row, dict)]
    unindexed = next((row["unindexed_sources"] for row in records if row.get("unindexed_sources")), 0)
    if unindexed:
        return _AnswerFacts([f"{unindexed} source file(s) are not in compile_commands.json"], True)
    if any(row.get("stale") is True for row in records):
        return _AnswerFacts(["files changed after the last index run"], False)
    return _AnswerFacts([], False)


def annotate_index_state(result, kw: dict):
    """Add the state of the index of the build of the query to the answer *result*.

    *kw* holds the arguments of the call: ``project_root`` (the project
    selector gives it), ``variant`` and ``image``.  A dict gets
    ``index_state`` and ``index_warning``.  A list gets a leading record
    ``{"warning": ..., "index_state": ...}``.  An answer with no reason
    gets nothing.

    ``"failed"`` also keeps ``build_warning`` on a dict, the field of the
    answers before ``index_state``.  A call whose build is not known (an
    error of the selector) gets the text of a failed run alone, as before:
    a refusal does not hide that the index is old.  While a run is active
    or scheduled it gets nothing: the repeated call with a build gets the
    state.

    WHY these checks only: they cost one stat and a few column reads.  The
    hash of each file of the build (``_count_modified_files``, 18 ms on a
    project of 7 builds) runs only while an index run is active or
    scheduled: a build that the run does not change must stay current, and
    only the files tell.  The header check (about 750 ms) stays in
    ``get_project_status``.

    Side effects: ``background.read_reindex_state`` removes a
    ``reindex.pid`` or a ``reindex.pending`` of a process that no longer
    runs, and it can make the empty ``write.lock``.  The status tools do
    the same.

    A warning must never fail the answer.  A project that cannot be
    resolved gets nothing, because the handler already gave the error that
    explains it.  An error of the database or of a file gives the answer
    without a state, and a log record.
    """
    from ...utils import resolve_project_root
    from .context import _db_path

    try:
        root = resolve_project_root(kw.get("project_root"))
        db_path = _db_path(root)
    except (RuntimeError, ValueError, OSError):
        return result
    try:
        return _annotate(result, kw, root, db_path)
    except (sqlite3.Error, OSError, RuntimeError, ValueError):
        # Not the handler: its answer is complete, and the state is only a
        # note on it.
        log.warning("The state of the index of %s is not known", root, exc_info=True)
        return result


def _annotate(result, kw: dict, root: Path, db_path: Path):
    """Do the work of :func:`annotate_index_state` for a project with an index."""
    from ...indexer.autobuild import read_problem
    from ..background import read_reindex_state

    problem = read_problem(db_path.parent)
    run_state = read_reindex_state(root)
    facts = answer_facts(result)
    known = list(facts.reasons)
    if problem is not None:
        known.append("the last index run failed")
    found = _build_reasons(
        db_path, root, kw.get("variant") or "", kw.get("image") or "",
        # Only to tell whether a run concerns this build: a reason that is
        # known already answers that.
        count_files=run_state in RUN_STATES and not known,
    )
    if found is None:
        # A run wins over "failed" here too: the text names the command of
        # the failed run, and the run in progress does that work.
        if problem is None or run_state in RUN_STATES:
            return result
        return _with_failed_text(result, problem.text)
    reasons, checkout_moved = found
    reasons += known
    state = index_state(bool(reasons), run_state=run_state, failed=problem is not None)
    if state is None:
        return result
    action = PLAIN_ACTION
    if state == "not_scheduled" and (checkout_moved or facts.needs_build):
        # The function reads the config and the markers of the build
        # system, thus the code asks it only here.
        action = _build_action(root, checkout_moved=checkout_moved)
    advice = index_state_advice(
        state, reasons, problem_text=problem.text if problem is not None else "", action=action,
    )
    if isinstance(result, list):
        return [{"warning": advice, "index_state": state}, *result]
    if isinstance(result, dict):
        annotated = {**result, "index_state": state, "index_warning": advice}
        if state == "failed" and problem is not None:
            annotated["build_warning"] = problem.text
        return annotated
    return result


def _with_failed_text(result, text: str):
    """Add the text of a failed run to an answer whose build is not known.

    WHY without a state: the state belongs to a build, and the refusal of
    the selector names none.  The text alone keeps what the answers gave
    before the state, thus the caller still learns that the index is old.
    """
    if isinstance(result, list):
        return [{"warning": text}, *result]
    if isinstance(result, dict):
        return {**result, "build_warning": text}
    return result


def _build_reasons(
    db_path: Path, root: Path, variant: str, image: str, *, count_files: bool,
) -> tuple[list[str], bool] | None:
    """Return the reasons why the index of the build of the query is not current.

    The result is ``(reasons, checkout_moved)``, or None when the arguments
    name no build: the handler then gave the refusal.  *count_files* adds
    the count of the changed files of the build.
    """
    from ...config import load as load_config
    from ...indexer.git_context import read_head
    from .context import get_executor
    from .stale import _count_modified_files, checkout_reason, outdated_index_fact, quick_structural_reasons
    from .variants import resolve_build

    cfg = load_config(root)
    # `derive_project_id` reads the same config again: the caller found the
    # database, thus the project has an ID.
    project_id = cfg.project.id
    if not project_id:
        return None
    live_head = read_head(root)

    def _query(conn, _config_hash):
        # No build of an index of an older version: the refusal of
        # `resolve_build` names the old schema and the status tool, and a
        # state here would read as the state of one build.
        if outdated_index_fact(conn):
            return None
        config_hash, _refusal = resolve_build(conn, project_id, cfg, variant, image)
        if config_hash is None:
            return None
        row = conn.execute("SELECT * FROM build_configs WHERE config_hash = ?", (config_hash,)).fetchone()
        if row is None:
            # A reindex removed the build after the handler read it.
            return None
        reasons = quick_structural_reasons(conn, row)
        # Here and not in `quick_structural_reasons`: the daemon shares
        # that check, and a build that a run again leaves without full
        # header tracking would start a run at each start of the daemon.
        # The status tools count it too, thus the two agree.
        manifest = manifest_reason(row["manifest_verification"] or "none")
        if manifest:
            reasons.append(manifest)
        moved = checkout_reason(row, live_head, root)
        if moved:
            reasons.append(moved)
        if count_files and not reasons:
            modified = _count_modified_files(conn, config_hash, root, use_cache=False)
            if modified:
                reasons.append(f"{modified} file(s) changed after the last index run")
        return reasons, bool(moved)

    return get_executor(db_path).execute_sync(_query, "")
