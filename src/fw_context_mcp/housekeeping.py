"""Remove what an older fw-context wrote and no version uses: files, directories, config keys.

One registry and one function, :func:`clean`.  A new retired path or key
is one entry here, and every caller removes it:

* ``fw-context index``, at the start of each run, before it loads the config;
* ``fw-context cleanup`` (``--dry-run`` shows the list and changes nothing);
* ``fw-context doctor`` reports the list as the check ``obsolete-files``, and
  ``doctor --fix`` (and ``fw-context init``, through the same fixes) removes it.

WHY remove, and not only warn: a warning came at each start, and the user
had to find and delete each file and key by hand.  The registry holds only
what fw-context itself wrote, with the commit that retired it.  A file
whose origin the history does not show stays: a guess must not delete a
file of the user.

The MCP server and the daemon remove nothing: a process that only answers
questions does not change the files of the user.  The daemon starts its
background runs as ``fw-context index``, thus the cleanup runs there too.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
import tomlkit.exceptions

from .utils import SAFE_EXCEPT, write_text_atomic

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetiredKey:
    """A config key that no version of fw-context reads any more.

    *in_variants* says that the key can also stand in a ``[[build.variants]]``
    table, where a variant copies the keys of ``[build]`` and ``[index]``.
    """

    section: str
    key: str
    retired_by: str
    in_variants: bool = False


#: The keys that fw-context removes from each config file.
RETIRED_KEYS: tuple[RetiredKey, ...] = (
    # The allowlist of the compilers that fw-context ran for their system
    # headers and macros.  fw-context runs the compiler of the build in all
    # cases now (29dd3db).
    RetiredKey("index", "query_driver", "29dd3db", in_variants=True),
    RetiredKey("index", "query_driver_extra", "29dd3db", in_variants=True),
    RetiredKey("index", "query_driver_auto", "29dd3db", in_variants=True),
    # A switch that nothing read (7b813bf).
    RetiredKey("llm", "allow_external_llm", "7b813bf"),
    # Each build goes to .fw-context/build/<variant>/out; the key had no
    # effect, and an index run refused a config that held it.
    RetiredKey("build", "build_dir", "build layout .fw-context/build/<variant>/out", in_variants=True),
)

#: Files in the global directory that an older fw-context wrote.
#: ``platformio-shared.ini``: the PlatformIO dependency tracking of 2026-07,
#: retired by fb227b0 (the manifest replaced the .d files).
_RETIRED_GLOBAL_FILES: tuple[str, ...] = ("platformio-shared.ini",)


@dataclass
class CleanupReport:
    """What :func:`clean` removed, or would remove with ``dry_run``.

    *failures* holds the paths that could not be read or changed, with the
    reason; the cleanup goes on after each one.
    """

    removed_paths: list[Path] = field(default_factory=list)
    removed_keys: list[tuple[Path, str]] = field(default_factory=list)
    failures: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.removed_paths and not self.removed_keys

    def lines(self) -> list[str]:
        """Return one line for each item, for the CLI and for doctor."""
        out = [str(path) for path in self.removed_paths]
        out += [f"{label} in {path}" for path, label in self.removed_keys]
        return out


def clean(
    project_root: Path | None,
    *,
    keep: Iterable[Path] = (),
    dry_run: bool = False,
    global_dir: Path | None = None,
    quiet: bool = False,
) -> CleanupReport:
    """Remove the retired files, directories and config keys, and return the report.

    *project_root* None cleans the global directory and the global config
    only.  *keep* holds databases that must stay in addition to the ones
    that the builds of the index read (the file on the command line of
    ``fw-context index``).  *global_dir* is the global directory of
    fw-context (``~/.fw-context`` when None); the global config is the one
    that ``config.settings`` reads.  *quiet* writes no log line: the CLI
    and doctor give the report themselves, and a log line on stderr then
    showed each failure two times.

    The config keys go first: the decision about ``[index] compile_commands``
    reads the build system of the config, and the files after them read
    the index that the config names.  The files go in the order of the
    layers (global, project, local), because the decision about
    ``compile_commands`` in a layer reads the layers below it.
    """
    from .config import settings
    from .indexer.build import _BUILD_DATABASE_VALUES

    report = CleanupReport()
    # The value of [index] compile_commands that the layers below the
    # current file give; at the start, the built-in default.
    lower_database: str | None = _BUILD_DATABASE_VALUES[-1].as_posix()
    runnable: bool | None = None
    global_config = settings._GLOBAL_CONFIG_PATH
    # (path, of the project, gets a .bak): config.toml of a project is in
    # git, which holds the old text; local.toml and the global config are not.
    config_files: list[tuple[Path, bool, bool]] = [(global_config, False, True)]
    if project_root is not None:
        config_dir = project_root / settings._PROJECT_CONFIG_DIR
        # In the home directory (a dotfiles repository makes it a git
        # root), the config of the "project" is the global config: it must
        # not come a second time, as a file of a project.
        config_files += [
            (path, True, backup)
            for path, backup in (
                (config_dir / settings._PROJECT_CONFIG_NAME, False),
                (config_dir / settings._PROJECT_LOCAL_CONFIG_NAME, True),
            )
            if not _is_the_same_file(path, global_config)
        ]
    for path, of_project, backup in config_files:
        document, unknown = _parse_config_file(path, report)
        if document is None:
            if unknown:
                # A layer that cannot be read: its value is not known.
                lower_database = None
            continue
        # tomlkit gives the parsed text back unchanged: the copy for .bak.
        original = document.as_string()
        removed = _remove_retired_keys(document)
        if of_project and project_root is not None and _has_no_effect(document, lower_database):
            if runnable is None:
                runnable = _runs_its_build(project_root)
            if runnable:
                removed.append(f"[index] {_key_line(document['index'], 'compile_commands')}")
                del document["index"]["compile_commands"]
        is_set, value = _database_value(document)
        if is_set:
            lower_database = value
        if removed:
            _write_config_file(path, document, original, removed, report, backup=backup, dry_run=dry_run)

    if project_root is not None:
        _clean_project_files(project_root, keep, report, dry_run=dry_run)
    home_dir = global_dir if global_dir is not None else settings._GLOBAL_CONFIG_PATH.parent
    for name in _RETIRED_GLOBAL_FILES:
        _remove_path(home_dir / name, report, dry_run=dry_run)

    if not quiet:
        if not dry_run:
            for line in report.lines():
                log.info("removed obsolete %s", line)
        for path, reason in report.failures:
            log.warning("Cannot clean %s: %s", path, reason)
    return report


def _is_the_same_file(path: Path, other: Path) -> bool:
    """Say if *path* and *other* are one file.

    ``os.path.samefile`` also sees a link, and two spellings on a file
    system that ignores the case.  A path that is not there, or that
    cannot be examined, gives False: the read of that path then fails
    too, and reports it, thus the global config is not changed twice.
    """
    try:
        return os.path.samefile(path, other)
    except OSError:
        return False


def _parse_config_file(path: Path, report: CleanupReport) -> tuple[tomlkit.TOMLDocument | None, bool]:
    """Return the TOML document of *path*, and if a file there could not be read.

    (None, False) means that there is no file.  (None, True) means a file
    that cannot be read, or a directory: the value of that layer is not
    known.  Each check of the file system is in the ``try``: on Python
    3.11 and 3.12 ``Path.exists`` and ``Path.is_file`` raise
    ``PermissionError`` for a directory without search permission, and the
    cleanup must not stop ``fw-context index``.

    tomlkit keeps the comments, the order and the format of the file: a
    ``config.toml`` is in the git of the user, and a rewrite through
    ``tomli_w`` gave a diff of each line.  The bytes are read as they are,
    thus a CRLF file stays CRLF.
    """
    try:
        if not path.is_file():
            if not path.exists():
                return None, False
            # A directory in the place of the config: the user must see it.
            report.failures.append((path, "this is not a file"))
            return None, True
        return tomlkit.parse(path.read_bytes().decode("utf-8")), False
    except (OSError, UnicodeDecodeError, tomlkit.exceptions.TOMLKitError) as exc:
        report.failures.append((path, str(exc)))
        return None, True


def _remove_retired_keys(document: tomlkit.TOMLDocument) -> list[str]:
    """Remove the keys of :data:`RETIRED_KEYS` from *document*; return a label for each.

    The label holds the line that went, with its value, thus the user can
    write it back from the report.  The retired keys hold paths and
    switches, no secret.
    """
    removed: list[str] = []
    for retired in RETIRED_KEYS:
        table = document.get(retired.section)
        if isinstance(table, dict) and retired.key in table:
            removed.append(f"[{retired.section}] {_key_line(table, retired.key)}")
            del table[retired.key]
        if retired.in_variants:
            for variant in _variant_tables(document):
                if retired.key in variant:
                    removed.append(f"[[build.variants]] {variant.get('name', '?')!r} {_key_line(variant, retired.key)}")
                    del variant[retired.key]
    return removed


def _key_line(table, key: str) -> str:
    """Return ``key = value`` for *key* in *table*, on one line of valid TOML.

    The value goes through new tomlkit items, thus a multi-line array of
    the file becomes one line, and a comment of the file stays out.
    """
    value = table[key]
    plain = value.unwrap() if hasattr(value, "unwrap") else value
    return f"{key} = {tomlkit.item(_inline(plain)).as_string()}"


def _inline(value: object) -> object:
    """Return *value* with each table as an inline table, also in an array.

    ``tomlkit.item`` makes a plain dict a ``[table]`` on more lines, and an
    array of tables loses the bounds between the tables.
    """
    if isinstance(value, dict):
        table = tomlkit.inline_table()
        table.update({key: _inline(item) for key, item in value.items()})
        return table
    if isinstance(value, list):
        array = tomlkit.array()
        array.extend(_inline(item) for item in value)
        return array
    return value


def _write_config_file(
    path: Path,
    document: tomlkit.TOMLDocument,
    original: str,
    removed: list[str],
    report: CleanupReport,
    *,
    backup: bool,
    dry_run: bool,
) -> None:
    """Write *document* back to *path*, keep a copy of *original* when *backup*, and record *removed*.

    The scope is the three config files of fw-context: ``config.toml`` of a
    project (in git), ``local.toml`` and the global config.  The rewrite
    makes a new file and renames it over the old one, as the other config
    writers of fw-context do (``config._toml_editor``): a stop of the
    process leaves the old file or the new one, never a cut one.  Of the
    properties of the old file, it keeps the ones that these files have
    in practice:

    * The mode: a config can hold ``chat_api_key`` or a ``[cache_server]
      token`` in a file of mode 0600.
    * A symbolic link: a global config from a dotfiles repository stays a
      link, and the target gets the new text.
    * A file without write permission for the owner stays as it is: the
      user made it read-only (``os.access`` says yes to root).

    A file that is not in git gets a copy of the old text first, at
    ``<name>.bak`` beside *path* (the place that the user knows, also for a
    link).  The copy replaces the copy of an earlier cleanup.  It is the
    only way back for a removed key there; ``config.toml`` of a project has
    git for that.  When the copy cannot be written, the config stays.
    """
    try:
        target = path.resolve()
        mode = stat.S_IMODE(target.stat().st_mode)
        if not mode & stat.S_IWUSR:
            report.failures.append((path, "the owner has no write permission, thus the file stays as it is"))
            return
        if not dry_run:
            if backup:
                write_text_atomic(path.with_name(path.name + ".bak"), original, mode=mode)
            write_text_atomic(target, tomlkit.dumps(document), mode=mode)
    except (OSError, RuntimeError) as exc:
        # RuntimeError: Path.resolve on a loop of links (Python 3.11, 3.12).
        report.failures.append((path, str(exc)))
        return
    report.removed_keys += [(path, label) for label in removed]


def _variant_tables(document) -> list:
    """Return the ``[[build.variants]]`` tables of *document*, or none."""
    build = document.get("build")
    if not isinstance(build, dict):
        return []
    variants = build.get("variants")
    if not isinstance(variants, list):
        return []
    return [table for table in variants if isinstance(table, dict)]


def _database_value(document: tomlkit.TOMLDocument) -> tuple[bool, str | None]:
    """Return if *document* sets ``[index] compile_commands``, and the value.

    A value that is not a string is None: the layers above cannot know
    what it gives, thus no key above it can go.
    """
    index = document.get("index")
    if not isinstance(index, dict) or "compile_commands" not in index:
        return False, None
    value = index["compile_commands"]
    return True, (str(value) if isinstance(value, str) else None)


def _has_no_effect(document: tomlkit.TOMLDocument, lower_database: str | None) -> bool:
    """Say if ``[index] compile_commands`` of *document* is an old default that has no effect.

    For a build that fw-context runs, the two values of
    ``indexer.build._BUILD_DATABASE_VALUES`` mean "the database of the
    build of fw-context" (``explicit_compile_commands``).  The key goes only
    when the layers below give such a value too: a removal must not bring
    up a value of a lower layer (``ci/compile_commands.json`` in
    ``config.toml`` under an old default in ``local.toml``).  The caller
    asks :func:`_runs_its_build` after this cheap test.
    """
    from .indexer.build import _BUILD_DATABASE_VALUES

    is_set, value = _database_value(document)
    if not is_set or value is None or lower_database is None:
        return False
    return Path(value) in _BUILD_DATABASE_VALUES and Path(lower_database) in _BUILD_DATABASE_VALUES


def _runs_its_build(project_root: Path) -> bool:
    """Say if fw-context runs the build of the project; False when it is not known.

    For a stub build (STM32CubeIDE) ``[index] compile_commands`` names the
    database of the user, and it stays.  The build system comes from the
    config of the project, else from the markers of the project.
    """
    from .config import load
    from .indexer.build import can_run_build, detect_build_system

    try:
        cfg = load(project_root)
        return can_run_build(cfg.build, cfg.build.system or detect_build_system(project_root))
    except SAFE_EXCEPT:
        log.debug("Cannot find the build system of %s", project_root, exc_info=True)
        return False


def _clean_project_files(project_root: Path, keep: Iterable[Path], report: CleanupReport, *, dry_run: bool) -> None:
    """Remove the files and directories of an older layout in the project.

    The list and its rules are ``indexer.build_layout._legacy_candidates``:
    the build output before ``.fw-context/build/<variant>/out`` and the
    retired ``.fw-context/toolchains.toml``.  A database that a build of the
    index still reads stays (see :func:`databases_in_use`): a run that
    ``--variant`` narrowed leaves the other variants on their old copies.
    When the index cannot be read, the build output stays, because an old
    copy can be the only database of a build.
    """
    from .indexer.build_layout import _legacy_candidates, _protects

    try:
        candidates = _legacy_candidates(project_root)
    except OSError as exc:
        report.failures.append((project_root, str(exc)))
        return
    if not candidates:
        return
    in_use = databases_in_use(project_root)
    if in_use is None:
        report.failures.append((project_root, "the index cannot be read, thus the old build output stays"))
        candidates = [path for path in candidates if path.name == "toolchains.toml"]
        in_use = []
    kept = [*keep, *in_use]
    for path in candidates:
        if not _protects(path, kept):
            _remove_path(path, report, dry_run=dry_run)


def databases_in_use(project_root: Path) -> list[Path] | None:
    """Return the databases that the project reads: the builds of its index, and its config.

    The config counts too: for a build that fw-context cannot run (a stub),
    ``[index] compile_commands`` names the database of the user, and the
    old default ``.fw-context/build/compile_commands.json`` is a copy in the
    old layout by its name.  A cleanup before the first index of such a
    project removed it.

    None when the index cannot be read: then no database may go.  The index
    stores absolute paths; a relative one is relative to the project root.

    The index opens read-only, not through ``open_db``: that runs the
    schema migrations, and ``doctor``, ``cleanup --dry-run`` and the MCP
    tool ``get_environment_status`` call this function and must change
    nothing.  An index of an older schema that the query cannot read gives
    None, thus its old copies stay until an index run migrates it.
    """
    from .config import load
    from .indexer.build import explicit_compile_commands
    from .indexer.db import get_builds_for_scope

    try:
        cfg = load(project_root)
        configured = explicit_compile_commands(project_root, cfg)
        in_use = [configured] if configured is not None else []
        project_id = cfg.project.id
        # No ID: `fw-context init` did not run, thus no index can exist.
        if not project_id:
            return in_use
        db_path = cfg.index.db_dir / project_id / "index.db"
        if not db_path.exists():
            return in_use
        conn = _open_index_readonly(db_path)
        try:
            stored = [
                Path(row["compile_commands_path"])
                for row in get_builds_for_scope(conn, project_id)
                if row["compile_commands_path"]
            ]
        finally:
            conn.close()
    except SAFE_EXCEPT:
        log.debug("Cannot read the index of %s", project_root, exc_info=True)
        return None
    return in_use + [path if path.is_absolute() else project_root / path for path in stored]


def _open_index_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the index read-only: no migration, no write lock.

    The same open as ``mcp.shared.connection._quick_open_readonly``, which
    this layer must not import.  ``as_uri()`` encodes a space, '?' and '#'
    in the path, which break a plain ``file:`` URI.
    """
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _remove_path(path: Path, report: CleanupReport, *, dry_run: bool) -> None:
    """Remove the file or directory *path* when it is there, and record it.

    A symlink goes as a link: the cleanup never follows it into a
    directory that fw-context did not make.
    """
    try:
        if not (path.exists() or path.is_symlink()):
            return
        if dry_run:
            report.removed_paths.append(path)
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        report.failures.append((path, str(exc)))
        return
    report.removed_paths.append(path)
