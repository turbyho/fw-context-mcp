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

Both selectors are therefore fail-closed:

- single-project → one scope ``(variant='', image='')``, no error.
- multi-build, variant omitted → error unless ``[build] default_variant``.
- image omitted while the build holds several → the default image, else an
  error that lists the images.  The default image is ``[build]
  default_image`` when the build holds it, else the image that the build
  system names as its application (ESP-IDF: the application, and not its
  bootloader; Zephyr sysbuild: the default image of ``domains.yaml``), else
  the only image.  A build without variants can hold several images too.
- image omitted, and the build system names an application that the index
  does not hold → error: the one image that is there is another program.
- unknown variant or image → error listing the known names.
- declared-but-unindexed variant → error pointing at ``fw-context index``.

A caller that wants to know whether a symbol lives in the bootloader as well
asks twice, once per image.  Two plain answers beat one blended answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from ...config.settings import Config


def _variant_names(conn: sqlite3.Connection, project_id: str, cfg: Config) -> list[str]:
    """Return the variants of the project: the declared ones first, then the indexed ones.

    WHY the index decides and not the config alone: the config DECLARES an
    intention and ``build_configs`` records the FACT.  An index run with
    ``--variant`` writes the name into that table, and a config that no
    longer declares it — edited since, or an index made elsewhere — used to
    send the query down the single-project path.  There the newest build
    wins in silence, and the variant and image of the caller are not even
    read: measured on a seeded index holding an application and a
    bootloader, a query for the application got the bootloader.

    The two are UNITED, and it is not one or the other.  ``declared or
    indexed`` read the index only when the config declared NOTHING, thus one
    declared variant hid every indexed one beside it: that name came back as
    unknown and its build was out of reach, with no command to reach it.
    The declared order leads, because it is the order the operator wrote
    and the order the refusal of ``resolve_build`` reads out.
    """
    from ...indexer.db import get_builds_for_scope

    declared = [v.name for v in cfg.build.variants]
    indexed = sorted({r["variant"] for r in get_builds_for_scope(conn, project_id) if r["variant"]})
    return declared + [v for v in indexed if v not in declared]


def resolve_build(
    conn: sqlite3.Connection,
    project_id: str,
    cfg: Config,
    variant: str = "",
    image: str = "",
    project_root: Path | None = None,
) -> tuple[str | None, str | None]:
    """Resolve ``(variant, image)`` → ``(config_hash, error)``.

    *project_root* lets the build system name the default image (see
    :func:`_default_build`).  Without it only ``[build] default_image``
    names one.

    The answer is ONE build, thus the answer is one ``config_hash``.  Three
    outcomes, and the two fields tell them apart:

    * ``(hash, None)`` — this build answers the query.
    * ``(None, error)`` — fail-closed refusal, in words for the caller to
      pass on.  See the module docstring for the refusals.
    * ``(None, None)`` — the project has no indexed build at all.  That is
      not a refusal: the caller decides between an empty answer and an
      error, and the two callers differ.

    This returned a list of scopes and a ``multi`` flag until the merging
    of builds went away.  Neither survived the removal: the list could hold
    one element, which invited a second pass and kept an unreachable merge
    branch alive, and ``multi`` was read nowhere — ``list_variants`` and
    ``get_active_build`` each compute that flag from the config themselves.
    """
    from ...indexer.db import get_active_config, get_builds_for_scope

    build_cfg = cfg.build
    names = _variant_names(conn, project_id, cfg)

    if not names:
        # WHY the arguments are read even here.  This branch used to
        # return the active build without looking at `variant` or
        # `image`, thus a project that declares no variant swallowed both
        # silently — `variant="no_such_variant"` answered with rows, and
        # so did `variant="*"`, which the refusal below calls out by
        # name.  A caller that names a build is asking a question this
        # project cannot answer, and an answer from the one build it has
        # looks correct while the caller believes it asked about another.
        rows = get_builds_for_scope(conn, project_id)
        images = sorted({r["image"] for r in rows if r["image"]})
        if variant and images:
            return None, (
                f"This project declares no variants, thus variant={variant!r} names "
                f"nothing. Its build makes {len(images)} images: {', '.join(images)}. "
                f"Drop `variant`, and name the program with `image`. "
                f"Call get_active_build() for what is indexed."
            )
        if variant or (image and not images):
            asked = f"variant={variant!r}" if variant else ""
            if image:
                asked = f"{asked} and image={image!r}" if asked else f"image={image!r}"
            return None, (
                f"This project has ONE build and declares no variants, thus "
                f"{asked} names nothing. Drop the argument to ask about the "
                f"build it has. Call get_active_build() for what is indexed."
            )
        if not images:
            row = get_active_config(conn, project_id)
            return (row["config_hash"] if row is not None else None), None
        # One build without variants that makes several programs (ESP-IDF:
        # the application and its bootloader).  The image is chosen as in a
        # variant below.
        return _choose_image(rows, images, "", image, cfg, project_root)

    # ── Multi-build: fail-closed on variant ──
    if not variant:
        if build_cfg.default_variant:
            variant = build_cfg.default_variant
        else:
            return None, (
                f"Project has {len(names)} build variant(s). Specify ``variant`` "
                f"(one of: {', '.join(names)}) or set [build] default_variant in "
                f"config.toml. Call get_active_build() for the variants/images table."
            )

    if variant == "*":
        return None, (
            "A query about code answers for ONE build. A project can hold a "
            "bootloader, a first-stage loader and the application, and they "
            "are separate programs. Name one variant, and one image of it. "
            "To learn whether a symbol is in two of them, ask twice. Call "
            "get_active_build() for the variants/images table."
        )

    if variant not in set(names):
        return None, f"Unknown variant '{variant}'. Available: {', '.join(sorted(names))}."

    rows = get_builds_for_scope(conn, project_id, variant, image or "")
    if not rows:
        if image:
            # A name of its own: ``names`` above holds the VARIANTS, and a
            # second meaning for one name inside one function reads as the
            # first one until a reader checks.
            known = get_builds_for_scope(conn, project_id, variant)
            known_images = sorted({r["image"] or "" for r in known if r["image"]})
            if known_images:
                return None, (
                    f"Unknown image '{image}' of variant '{variant}'. "
                    f"Available: {', '.join(known_images)}."
                )
        return None, (
            f"Variant '{variant}' is declared in config but not indexed. "
            f"Run 'fw-context index --build --variant {variant}'."
        )

    # ── Fail-closed on image ──
    # Several images of one variant are several PROGRAMS, thus a query that
    # names none of them has not said what it asks about.  Answering for all
    # of them would blend a bootloader with an application, and two builds of
    # one application would duplicate nearly every row.
    if image:
        # One build answers.  The rows arrive newest first, thus an older
        # build left behind by an interrupted index run cannot win.
        return rows[0]["config_hash"], None
    images = sorted({r["image"] or "" for r in rows if r["image"]})
    return _choose_image(rows, images, variant, "", cfg, project_root)


def _choose_image(
    rows: list,
    images: list[str],
    variant: str,
    image: str,
    cfg: Config,
    project_root: Path | None,
) -> tuple[str | None, str | None]:
    """Return the newest build of *image*, or of the default image when *image* is "".

    *rows* are the builds of *variant* (or of the build without variants),
    newest first, and *images* their image names.  A name that is not one of
    *images* is an error that lists them.  WHY a default and not a refusal:
    most questions are about the application, and a build that also makes a
    bootloader made each of them name the image.  The default is a name,
    thus a query without ``image`` still answers for ONE program, and
    ``get_active_build`` reports it as ``active_image``.
    """
    owner = f"variant '{variant}'" if variant else "the build"
    if image:
        if image not in images:
            return None, f"Unknown image '{image}' of {owner}. Available: {', '.join(images)}."
        return next(r["config_hash"] for r in rows if r["image"] == image), None
    if not images:
        # The build makes one program, and its image has no name.
        return rows[0]["config_hash"], None
    default = _default_build(cfg, project_root, variant, rows)
    if default.row is not None:
        return default.row["config_hash"], None
    if default.application_missing:
        # The build system names its application, and the index holds only
        # the other programs: the first index run is not complete, or a run
        # that --image narrowed indexed only them.  The one image that is
        # there is not the program that the query is about.
        return None, (
            f"The application of {owner} is not indexed. The index holds "
            f"{', '.join(images)}. Name it with ``image`` to ask about it, or "
            f"run 'fw-context index' to index the application."
        )
    if len(images) == 1:
        return next(r["config_hash"] for r in rows if r["image"] == images[0]), None
    holder = f"Variant '{variant}'" if variant else "The build"
    return None, (
        f"{holder} holds {len(images)} images, and each is a separate program: "
        f"{', '.join(images)}. Specify ``image``, or set [build] default_image in "
        f"config.toml. Call get_active_build() for the variants/images table."
    )


@dataclass(frozen=True)
class _DefaultBuild:
    """The build of a query without ``image``, see :func:`_default_build`."""

    row: sqlite3.Row | dict | None
    application_missing: bool


def default_image(cfg: Config, project_root: Path | None, variant: str, rows: list) -> str:
    """Return the image that a query about *variant* without ``image`` gets, or "".

    *rows* are the indexed builds of *variant*, newest first, with ``image``
    and ``compile_commands_path``.  "" when nothing names a default among
    them, or when the default is a build without an image name.  See
    :func:`_default_build`.
    """
    row = _default_build(cfg, project_root, variant, rows).row
    return (row["image"] or "") if row is not None else ""


def _default_build(cfg: Config, project_root: Path | None, variant: str, rows: list) -> _DefaultBuild:
    """Return the build of *variant* that a query without ``image`` gets.

    *rows* are the indexed builds of *variant*, newest first.

    1. ``[build] default_image``, when *variant* holds that image: the
       operator wrote it.
    2. The build whose database is the database of the application, as the
       build system names it (``builders.application_database``).  Only the
       part of the path in the output directory is compared: the index
       stores absolute paths, and a project that moved has another root.
    3. A build without an image name, when the build system names an
       application.  The index holds such a build from before images: then
       a build of several programs indexed only the database that the build
       returned, and that was the database of the application.
    4. Nothing, with ``application_missing`` when the build system names an
       application that no build read.

    WHY the index and not the build output for ESP-IDF: the path is enough to
    tell the application from the bootloader, thus no file is read and a
    query does not fail while a clean build has removed the output.
    """
    images = {r["image"] for r in rows if r["image"]}
    configured = cfg.build.default_image
    if configured and configured in images:
        return _DefaultBuild(next(r for r in rows if r["image"] == configured), False)
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
) -> tuple[sqlite3.Row | None, str | None]:
    """Return ``(build, refusal)`` for a query without ``variant`` and ``image``.

    ``get_active_build`` reports this build, and ``smart_search`` and
    ``semantic_search``, which take no selector, answer for it.  It is the
    build that :func:`resolve_build` chooses.  When that refuses because the
    project has several variants and no ``default_variant``, the newest
    build stands in, as these tools always did.  Any other refusal comes
    back with no build: the application is not indexed, or the build holds
    several images and nothing names a default.

    WHY not the newest build: a build that makes several programs indexes
    them one after the other, and the newest one is the program that the
    run did last.  During the first index run, which builds the bootloader
    in minutes and the application in hours, that is the bootloader, and
    the tools without a selector answered from it while the others refused.
    """
    from ...indexer.db import get_active_config

    newest = get_active_config(conn, project_id)
    if newest is None:
        return None, None
    chosen, refusal = resolve_build(conn, project_id, cfg, project_root=project_root)
    if chosen is None:
        if _variant_names(conn, project_id, cfg) and not cfg.build.default_variant:
            return newest, None
        return (None, refusal) if refusal else (newest, None)
    if chosen == newest["config_hash"]:
        return newest, None
    row = conn.execute(
        "SELECT * FROM build_configs WHERE project_id = ? AND config_hash = ?",
        (project_id, chosen),
    ).fetchone()
    return (row, None) if row is not None else (newest, None)


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
        config_hash, err = resolve_build(conn, project_id, cfg, variant or "", image or "", project_root=root)
    finally:
        conn.close()
    if err:
        return [{"error": err}]
    if config_hash is None:
        return []
    return _with_stale_recovery(root, db_path, query_fn, config_hash=config_hash)
