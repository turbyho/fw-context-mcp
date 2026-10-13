"""Variant/image scoping for MCP query tools.

Resolves the ``(variant, image)`` selection carried by query-tool parameters
into ONE concrete build scope (``config_hash`` + identity).

── One question about code, one build ──

A project can hold several builds, and they are not variations of one answer.
Measured on one Zephyr project, nine builds sit on two axes: two boards
(``nrf52840-dev``, ``nrf54lm20a-dev``) and, inside each, several images —
``app``, ``mcuboot``, ``stage0``, ``app_alt``.  An image is a
separate firmware binary: a bootloader is not the application.

Nobody reasons about code across two programs or two boards at once, thus a
merged answer serves no question and carries two hazards.  It blends symbols
of different binaries, and where two builds ARE near-identical it duplicates
almost every row: ``app`` and ``app_alt`` of that project share
10585 of about 11000 names.

── The caller selects the build, and the server does not guess ──

A project of several builds has no "active" build.  The agent knows the
task, thus the agent selects the build, and when the task does not identify
one, the agent asks the user.  The server keeps no selection between calls,
and it fills in no part of one:

- one build in the index → the query needs no selector.  A selector that
  does not name that build is an error.
- several builds → ``(variant, image)`` must be the pair of one build,
  exactly.  A build without an image name is selected without ``image``.
  A build without both names, from an older index run, is not selected.
- no completed build → a call that names a build gets a refusal, and not
  an empty answer.

WHY no default, from the config or from the build system.  This module
chose one until 2026-10: ``[build] default_variant``, ``[build]
default_image``, the image that the build system names as its application,
the only image of a variant.  Each one was a guess on behalf of the caller,
and an answer from a guessed build looks correct.  A refusal costs one more
call, and that call names the build that the caller means.

WHY no completion of a part, such as ``variant="a"`` for the build
``a/app`` when ``app`` is the only image of ``a``: the next index run can
add an image, and the same call then means another thing or nothing.

Each refusal lists the builds, with the arguments to repeat the call.  A
caller that wants to know whether a symbol is in the bootloader as well
asks twice, once per build.  Two plain answers beat one blended answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from ...config.settings import Config

# The tool that gives the state of each build.  Every refusal names it,
# thus one constant keeps the refusals and the tool name in step.
STATUS_TOOL = "get_project_status"

# The descriptions of the two selector parameters, one text for every tool.
# WHY one constant: 28 tools take the selector, and 28 copies of a rule
# change at different times.
VARIANT_DESCRIPTION = (
    "Variant of the build (a board, or a PlatformIO environment). On a project of more "
    "than one build, give the arguments of one build as the refusal lists them: the "
    "variant, the image, or the two. fw-context does not select a build for you. One "
    "query answers for ONE build."
)
IMAGE_DESCRIPTION = (
    "Image of the build: a separate program, such as the application or the "
    "bootloader. On a project of more than one build, give it when that build has "
    "an image name, as the refusal lists it."
)


def build_arguments(variant: str, image: str) -> str:
    """Return the selector arguments of the build ``(variant, image)``, as a caller writes them.

    A build without an image name gives only ``variant``, because the caller
    selects it without ``image``.  A build without both names is the build
    of a call without a selector, in a project of one build.
    """
    parts = [f'{key}="{value}"' for key, value in (("variant", variant), ("image", image)) if value]
    return ", ".join(parts) if parts else "no variant and no image"


# The line of a build without a name in the list of a project of several
# builds.  No call can name it, see :func:`resolve_build`.
_UNNAMED_BUILD = (
    "a build without a variant and an image name, from an older index run. "
    "No call can select it. Run 'fw-context index' to replace it"
)


def _pair(row) -> tuple[str, str]:
    return (row["variant"] or "", row["image"] or "")


def _refusal(builds: list, asked: tuple[str, str]) -> str:
    """Return the refusal of a selector that names no build of *builds*.

    One shape for every refusal: what the call asked, and the arguments of
    each build to repeat it with.  WHY the list is in the refusal and not
    only in a status tool: the caller can repeat the call at once, without
    one more call to find the names.
    """
    pairs = sorted(_pair(row) for row in builds)
    status = f"To see the state of each build, call {STATUS_TOOL}()."
    if len(pairs) == 1:
        listing = f"  {build_arguments(*pairs[0])}"
        return (
            f"This project holds one build, and no build has the arguments "
            f"{build_arguments(*asked)}. Remove the build arguments, or repeat the "
            f"call with the arguments of that build:\n{listing}\n{status}"
        )
    listing = "\n".join(
        f"  {build_arguments(*pair) if pair != ('', '') else _UNNAMED_BUILD}" for pair in pairs
    )
    if asked == ("", ""):
        head = (
            f"This project holds {len(pairs)} builds, and a query about code "
            f"answers for one build. This call names no build."
        )
    else:
        head = (
            f"No build of this project has exactly the arguments "
            f"{build_arguments(*asked)}. A query about code answers for one build."
        )
    return (
        f"{head} Repeat the call with the arguments of one build:\n{listing}\n"
        f"Select the build from the task. If the task does not identify one "
        f"build, ask the user. Do not select a build from its position in this "
        f"list. {status}"
    )


def resolve_build(
    conn: sqlite3.Connection,
    project_id: str,
    cfg: Config,
    variant: str = "",
    image: str = "",
) -> tuple[str | None, str | None]:
    """Resolve ``(variant, image)`` → ``(config_hash, error)``.

    The answer is ONE build, thus the answer is one ``config_hash``.  Three
    outcomes, and the two fields tell them apart:

    * ``(hash, None)`` — this build answers the query.
    * ``(None, error)`` — a refusal, in words for the caller to pass on.
      See the module docstring for the rule.
    * ``(None, None)`` — the project has no completed build, and the call
      names no build.  That is not a refusal: the caller decides between
      an empty answer and an error, and the two callers differ.

    The builds are the newest completed build of each pair
    (``latest_builds``).  An older build of a pair, which an interrupted
    run left behind, is not one more build, and a build that is still
    indexing answers no query.

    *cfg* gives the declared variants.  A declared variant that the index
    does not hold gets the command that indexes it, and not the list: the
    caller named the right build, and the index does not hold it yet.

    WHY a named build in an index without a completed build is a refusal,
    and not ``(None, None)``: during the first index run every row is
    still indexing, and the search tools give an empty list for
    ``(None, None)``.  An empty list reads as "no such code".

    WHY a call without a selector never selects a build without names in
    a project of several builds: the caller named no build, thus the call
    must get the list.  Such a build is a row of an older index run, from
    before variants or images, and the next full run removes it
    (``cleanup_retired_builds``).  Until then no call can select it, and
    the list says so.
    """
    from .stale import latest_builds

    builds = latest_builds(conn, project_id)
    asked = (variant or "", image or "")
    if len(builds) == 1 and asked == ("", ""):
        return builds[0]["config_hash"], None
    if asked != ("", ""):
        for row in builds:
            if _pair(row) == asked:
                return row["config_hash"], None

    indexed_variants = {row["variant"] or "" for row in builds}
    declared = {v.name for v in cfg.build.variants}
    if asked[0] in declared and asked[0] not in indexed_variants:
        return None, (
            f"Variant '{asked[0]}' is declared in config but not indexed. "
            f"Run 'fw-context index --build --variant {asked[0]}'."
        )
    if not builds:
        if asked == ("", ""):
            return None, None
        return None, (
            f"The index of this project holds no completed build, thus no build has "
            f"the arguments {build_arguments(*asked)}. When an index run is in "
            f"progress, wait until it ends. Else run 'fw-context index'. Then repeat "
            f"the call."
        )
    return None, _refusal(builds, asked)


@dataclass(frozen=True)
class _DefaultBuild:
    """The build that :func:`active_build` reports, see :func:`_default_build`."""

    row: sqlite3.Row | dict | None
    application_missing: bool


def _default_build(cfg: Config, project_root: Path | None, variant: str, rows: list) -> _DefaultBuild:
    """Return the build of *variant* that the build system names as its application.

    *rows* are the indexed builds of *variant*, newest first.

    1. The build whose database is the database of the application, as the
       build system names it (``builders.application_database``).  Only the
       part of the path in the output directory is compared: the index
       stores absolute paths, and a project that moved has another root.
    2. A build without an image name, when the build system names an
       application.  The index holds such a build from before images: then
       a build of several programs indexed only the database that the build
       returned, and that was the database of the application.
    3. Nothing, with ``application_missing`` when the build system names an
       application that no build read.

    WHY the index and not the build output for ESP-IDF: the path is enough to
    tell the application from the bootloader, thus no file is read and a
    check does not fail while a clean build has removed the output.
    """
    application = _application_in_out(cfg, project_root, variant)
    if application is None:
        return _DefaultBuild(None, False)
    for row in rows:
        if _path_in_out(row["compile_commands_path"], variant) == application:
            return _DefaultBuild(row, False)
    for row in rows:
        if not row["image"]:
            return _DefaultBuild(row, False)
    return _DefaultBuild(None, True)


def _path_in_out(path: str | None, variant: str) -> PurePath | None:
    """Return the part of *path* in ``.fw-context/build/<variant>/out``, or None.

    None for a path outside that directory, for example a database of the
    user or a database of the layout before ``.fw-context/build/<variant>``.
    """
    from ...indexer.build_layout import BUILD_ROOT_REL, DEFAULT_VARIANT, OUT_DIR_NAME

    if not path:
        return None
    parts = PurePath(path).parts
    marker = (*BUILD_ROOT_REL.parts, variant or DEFAULT_VARIANT, OUT_DIR_NAME)
    for start in range(len(parts) - len(marker), -1, -1):
        if parts[start:start + len(marker)] == marker:
            return PurePath(*parts[start + len(marker):])
    return None


def _application_in_out(cfg: Config, project_root: Path | None, variant: str) -> PurePath | None:
    """Return the database of the application of *variant*, relative to its output directory.

    None when there is no project root, no backend, or the backend does not
    name an application (a build of one program).
    """
    if project_root is None:
        return None

    from ...indexer.build import detect_build_system
    from ...indexer.build_layout import BuildLayout, InvalidVariantName
    from ...indexer.builders import application_database
    from ...indexer.builders import registry as builder_registry

    # Callers pass a str or a Path.
    root = Path(project_root)
    system = cfg.build.system or detect_build_system(root)
    builder_cls = builder_registry.get(system) if system else None
    if builder_cls is None:
        return None
    try:
        out_dir = BuildLayout(root).out_dir(variant)
    except InvalidVariantName:
        # A name from an older index that cannot be a directory has no
        # output directory, thus no build system names its application.
        return None
    database = application_database(builder_cls(), out_dir)
    return PurePath(database.relative_to(out_dir)) if database is not None else None


def active_build(
    conn: sqlite3.Connection,
    project_id: str,
    cfg: Config,
    project_root: Path | None,
) -> tuple[sqlite3.Row | dict | None, str | None]:
    """Return ``(build, refusal)``: the build that the checks of ``fw-context index`` read.

    No query reads it: a query names its build (:func:`resolve_build`).
    The index run checks one build:

    * one build → that build;
    * builds of several variants → the newest build;
    * images of a build without variants → the application that the build
      system names (``_default_build``), else a refusal.

    WHY the application and not the newest build for images: a build that
    makes several programs indexes them one after the other, and the newest
    one is the program that the run did last.  After a failed application,
    that is the bootloader, and its database does not tell whether the
    application has new sources.
    """
    from ...indexer.db import get_active_config
    from .stale import latest_builds

    newest = get_active_config(conn, project_id)
    if newest is None:
        return None, None
    builds = latest_builds(conn, project_id)
    if len(builds) <= 1 or cfg.build.variants or any(row["variant"] for row in builds):
        return newest, None
    default = _default_build(cfg, project_root, "", builds)
    if default.row is not None:
        return default.row, None
    images = sorted(row["image"] or "" for row in builds)
    if default.application_missing:
        return None, (
            f"The application of the build is not indexed. The index holds "
            f"{', '.join(images)}. Run 'fw-context index' to index the application."
        )
    return None, (
        f"The build holds {len(images)} images, and each is a separate program: "
        f"{', '.join(images)}. Nothing names one of them as the application."
    )


def checked_build(
    conn: sqlite3.Connection, project_id: str, cfg: Config, project_root: Path | None,
) -> sqlite3.Row | dict | None:
    """Return the build that the checks of the project read, or None without a build.

    :func:`active_build`, and the newest build when no build system names
    the application of several images, as the index run reads it
    (``cli._index._checked_build``).  WHY one function: the status tools
    and the check at the start of the daemon must look for new source files
    in the same build, else one of them reports a file that the other does
    not.
    """
    from ...indexer.db import get_active_config

    checked, _refusal = active_build(conn, project_id, cfg, project_root)
    return checked if checked is not None else get_active_config(conn, project_id)


def run_scoped_query(
    root,
    db_path,
    query_fn,
    variant: str = "",
    image: str = "",
) -> list[dict]:
    """Resolve ``(variant, image)`` and run ``query_fn(conn, config_hash)``.

    ``resolve_build`` answers with one build, thus the result is the output
    of that one build with no annotation.  Fail-closed: a resolution error is
    returned as a single ``{"error": …}`` dict, which names what the caller
    must choose.  A project with no indexed build gives an empty list.
    """
    from ...config import derive_project_id
    from ...config import load as load_config
    from .context import _quick_open_readonly
    from .stale import _with_stale_recovery

    project_id = derive_project_id(root)
    cfg = load_config(root)
    conn = _quick_open_readonly(db_path)
    try:
        config_hash, err = resolve_build(conn, project_id, cfg, variant or "", image or "")
    finally:
        conn.close()
    if err:
        return [{"error": err}]
    if config_hash is None:
        return []
    return _with_stale_recovery(root, db_path, query_fn, config_hash=config_hash)
