"""Individual dependency check functions.

Each check returns a :class:`DepCheckResult` — never raises.
Heavy imports (httpx, clang, watchfiles) are local to the function body
so that importing this module stays fast for pre-flight.

WHY: Checks must never crash the CLI.  If a check raises an unhandled
exception, the entire audit fails and the user sees a raw traceback.
Every check catches all exceptions internally and returns an error
result instead.  Imports are deferred so ``check_pysqlite3`` and
``check_sqlite3_extensions`` (the pre-flight checks) can run without
paying the cost of importing httpx, clang, or the config subsystem.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import DepCheckResult
from ._instructions import (
    clang_resource_instructions,
    db_integrity_instructions,
    disk_space_instructions,
    get_platform_info,
    libclang_conflict_instructions,
    libclang_python_instructions,
    libclang_so_instructions,
    ollama_instructions,
    pysqlite3_instructions,
    sqlite_ext_instructions,
    vec0_load_error_instructions,
    watchfiles_instructions,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _import(name: str):
    """Import a module by name — indirection point for test monkeypatching.

    WHY: Direct ``import`` statements cannot be intercepted by tests.
    Wrapping in a helper function lets test suites inject mock modules
    without modifying production code.
    """
    return importlib.import_module(name)


def _version(pkg: str) -> str:
    """Return the installed version of *pkg*, or ``"unknown"``.

    Uses ``importlib.metadata`` which is the standard library replacement
    for the deprecated ``pkg_resources``.
    """
    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _require_tomli_w() -> bool:
    """Return True when ``tomli-w`` is importable.

    Shared by ``check_tomli_w`` (doctor) and ``--skip-doctor`` (init) so
    the knowledge of the tomli-w dependency lives in one place.  ``tomli-w``
    is a runtime dependency of the config writer — a missing package only
    crashes at ``set_key``/``merge_template`` time, so an import probe is
    the cheapest reliable check.
    """
    try:
        importlib.import_module("tomli_w")
        return True
    except ImportError:
        return False


# ── Checks ──────────────────────────────────────────────────────────────


def check_pysqlite3() -> DepCheckResult:
    """Verify pysqlite3 is installed and importable.

    WHY: pysqlite3 is the most common failure point on macOS and Windows.
    The Python.org installer and Homebrew Python do not ship with
    ``--enable-loadable-sqlite-extensions``, which means sqlite-vec
    cannot load at all.  pysqlite3 bundles a compatible SQLite.  The
    ``"degraded"`` status catches the case where pysqlite3 is installed
    but did not replace the stdlib sqlite3 module (e.g. wrong import
    order due to PYTHONPATH).
    """
    platform_ctx = get_platform_info()
    try:
        _import("pysqlite3")
        ver = _version("pysqlite3")
        # Verify it actually replaced stdlib sqlite3
        import sqlite3
        if "pysqlite3" in str(sqlite3.__file__):
            return DepCheckResult(
                name="pysqlite3",
                status="ok",
                message=f"pysqlite3 {ver}",
                critical=True,
            )
        return DepCheckResult(
            name="pysqlite3",
            status="degraded",
            message=f"pysqlite3 {ver} loaded but sqlite3 redirect may not be active",
            critical=True,
        )
    except ImportError:
        return DepCheckResult(
            name="pysqlite3",
            status="missing",
            message="pysqlite3 not installed — required for sqlite-vec extension support",
            fix_cmd=f"{platform_ctx['pip_cmd']} pysqlite3",
            instructions=pysqlite3_instructions(platform_ctx),
            critical=True,
        )


def check_sqlite3_extensions() -> DepCheckResult:
    """Verify sqlite3 supports loadable extensions.

    WHY: ``sqlite3.enable_load_extension(True)`` raises AttributeError
    on macOS system Python and some pyenv builds.  This is distinct from
    the pysqlite3 check — a Python with extension support but without
    pysqlite3 will pass this check but fail the sqlite-vec load test.
    """
    platform_ctx = get_platform_info()
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
            return DepCheckResult(
                name="sqlite-ext",
                status="ok",
                message="enable_load_extension() supported",
                critical=True,
            )
        except AttributeError:
            return DepCheckResult(
                name="sqlite-ext",
                status="missing",
                message="sqlite3 missing enable_load_extension() — stdlib sqlite3 on macOS/pyenv",
                fix_cmd=f"{platform_ctx['pip_cmd']} pysqlite3",
                instructions=sqlite_ext_instructions(platform_ctx),
                critical=True,
            )
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        return DepCheckResult(
            name="sqlite-ext",
            status="error",
            message=f"sqlite3 in-memory connection failed: {e}",
            instructions=sqlite_ext_instructions(platform_ctx),
            critical=True,
        )


def check_sqlite_vec() -> DepCheckResult:
    """Verify sqlite-vec can load into SQLite.

    WHY: sqlite-vec can be installed (pip succeeds) but fail to load at
    runtime.  Common causes: missing vec0.so shared library dependency
    (libgcc_s.so.1 on Alpine, libc++.dylib on old macOS), SQLite version
    too old, or architecture mismatch (arm64 pip on x86_64 Python).
    The ``"degraded"`` status captures load-time failures; the
    instructions include platform-specific diagnostic commands.
    """
    platform_ctx = get_platform_info()
    try:
        mod = _import("sqlite_vec")
        ver = _version("sqlite-vec")
        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
            mod.load(conn)
            return DepCheckResult(
                name="sqlite-vec",
                status="ok",
                message=f"sqlite-vec {ver}",
                critical=True,
            )
        except sqlite3.OperationalError as e:
            msg = str(e)
            return DepCheckResult(
                name="sqlite-vec",
                status="degraded",
                message=f"sqlite-vec {ver} installed but load failed: {e}",
                instructions=vec0_load_error_instructions(msg, platform_ctx),
                critical=True,
            )
        finally:
            conn.close()
    except ImportError:
        return DepCheckResult(
            name="sqlite-vec",
            status="missing",
            message="sqlite-vec not installed",
            fix_cmd=f"{platform_ctx['pip_cmd']} sqlite-vec",
            instructions=f"{platform_ctx['pip_cmd']} sqlite-vec",
            critical=True,
        )


def check_libclang_python() -> DepCheckResult:
    """Verify that the libclang-ng wheel is installed, in the version that fw-context declares.

    WHY the check of the old ``libclang`` package: an upgrade of fw-context
    installs libclang-ng, and pip does not remove ``libclang``.  The two
    install the same ``clang`` package, thus each file comes from the
    package that pip wrote last, and a later removal of ``libclang`` removes
    files of libclang-ng.
    """
    from packaging.requirements import Requirement

    from ..indexer._clang_resource import LIBCLANG_DISTRIBUTION, libclang_requirement

    platform_ctx = get_platform_info()
    try:
        _import("clang.cindex")
        ver = _version(LIBCLANG_DISTRIBUTION)
        old = _version("libclang")
        if old != "unknown":
            return DepCheckResult(
                name="libclang-python",
                status="degraded",
                message=(
                    f"the old libclang {old} is installed next to {LIBCLANG_DISTRIBUTION} {ver}; "
                    "the two install the same clang package"
                ),
                fix_cmd=libclang_conflict_instructions(platform_ctx),
                instructions=libclang_conflict_instructions(platform_ctx),
                critical=True,
            )
        requirement = Requirement(libclang_requirement())
        if ver == "unknown" or not requirement.specifier.contains(ver, prereleases=True):
            return DepCheckResult(
                name="libclang-python",
                status="degraded",
                message=f"{LIBCLANG_DISTRIBUTION} {ver} is installed, and fw-context needs {requirement}",
                fix_cmd=libclang_python_instructions(platform_ctx),
                instructions=libclang_python_instructions(platform_ctx),
                critical=True,
            )
        return DepCheckResult(
            name="libclang-python",
            status="ok",
            message=f"{LIBCLANG_DISTRIBUTION} {ver}",
            critical=True,
        )
    except ImportError:
        return DepCheckResult(
            name="libclang-python",
            status="missing",
            message="clang Python bindings not installed",
            fix_cmd=libclang_python_instructions(platform_ctx),
            instructions=libclang_python_instructions(platform_ctx),
            critical=True,
        )


def check_libclang_so() -> DepCheckResult:
    """Verify the native libclang shared library is discoverable."""
    platform_ctx = get_platform_info()
    # First, make sure the Python bindings are available
    try:
        from ..indexer._libclang import cindex
    except ImportError:
        return DepCheckResult(
            name="libclang-so",
            status="skipped",
            message="depends on libclang-python (missing)",
            critical=True,
        )

    # Check if the library file exists.
    # Config().lib returns a CDLL object — extract the real path via ._name.
    try:
        lib_cdll = cindex.Config().lib
        lib_path = getattr(lib_cdll, "_name", str(lib_cdll))
    except Exception as exc:
        return DepCheckResult(
            name="libclang-so",
            status="missing",
            message=f"cindex.Config().lib failed: {exc}",
            fix_cmd=libclang_so_instructions(platform_ctx),
            instructions=libclang_so_instructions(platform_ctx),
            critical=True,
        )

    if lib_path and "/" in lib_path and Path(lib_path).exists():
        # Smoke test: try to actually load the library
        try:
            cindex.Index.create()
            return DepCheckResult(
                name="libclang-so",
                status="ok",
                message=lib_path,
                critical=True,
            )
        except Exception as exc:
            return DepCheckResult(
                name="libclang-so",
                status="degraded",
                message=f"library found at {lib_path} but failed to load: {exc}",
                fix_cmd=libclang_so_instructions(platform_ctx),
                instructions=libclang_so_instructions(platform_ctx),
                critical=True,
            )

    return DepCheckResult(
        name="libclang-so",
        status="missing",
        message=f"libclang not found at {lib_path}",
        fix_cmd=libclang_so_instructions(platform_ctx),
        instructions=libclang_so_instructions(platform_ctx),
        critical=True,
    )


def check_clang_resource() -> DepCheckResult:
    """Verify that the clang compiler headers of the libclang major version are there.

    WHY a check of its own: the libclang-ng wheel lacks the generated ARM
    and RISC-V intrinsic headers.  Without clang's headers a GCC build
    parses with the headers of GCC, and some of
    those do not parse in libclang (2574 errors from ``arm_acle.h`` in one
    STM32 build).  Not critical: the index works, with those errors.
    """
    from ..indexer._clang_resource import (
        bundled_source,
        find_resource_include,
        libclang_major,
        managed_include,
    )

    major = libclang_major()
    if major is None:
        return DepCheckResult(
            name="clang-resource", status="skipped",
            message="the libclang version is not known", critical=False,
        )
    # unpack=False: a check reports and does not change the machine.
    found = find_resource_include(major, unpack=False)
    shipped = managed_include(major)
    if found is not None and found.source.startswith("managed") and shipped is not None and found.include != shipped:
        return DepCheckResult(
            name="clang-resource",
            status="degraded",
            message=(
                f"the headers that ship with this fw-context are not unpacked yet; "
                f"the parse uses {found.include}, from another fw-context version"
            ),
            fix_cmd="fw-context doctor --fix --only clang-resource",
            instructions="Run `fw-context doctor --fix` to unpack the headers that ship with this version.",
            critical=False,
        )
    if found is not None:
        return DepCheckResult(
            name="clang-resource", status="ok",
            message=f"clang {major} headers: {found.include} ({found.source})", critical=False,
        )
    return DepCheckResult(
        name="clang-resource",
        status="degraded",
        message=(
            f"no clang {major} compiler headers; a GCC build parses with the headers of GCC, "
            f"and some of them do not parse in libclang"
        ),
        fix_cmd="fw-context doctor --fix --only clang-resource" if bundled_source(major) else None,
        instructions=clang_resource_instructions(get_platform_info(), major, bundled=bundled_source(major) is not None),
        critical=False,
    )


def check_watchfiles() -> DepCheckResult:
    """Verify watchfiles is installed."""
    platform_ctx = get_platform_info()
    try:
        _import("watchfiles")
        ver = _version("watchfiles")
        return DepCheckResult(
            name="watchfiles",
            status="ok",
            message=f"watchfiles {ver}",
            critical=False,
        )
    except ImportError:
        return DepCheckResult(
            name="watchfiles",
            status="missing",
            message="watchfiles not installed — background file watcher unavailable",
            fix_cmd=f"{platform_ctx['pip_cmd']} watchfiles",
            instructions=watchfiles_instructions(platform_ctx),
            critical=False,
        )


def check_config(problem: Exception | None, project_root: str | Path | None = None) -> DepCheckResult:
    """Report if the config files of fw-context can be used.

    *problem* is the ``ConfigError`` of ``load`` for the project, or
    ``None`` when the files load.  ``run_full_check`` loads the config one
    time, and the checks that read the config are ``skipped`` after this
    check fails.  WHY its own check: those checks reported "check crashed"
    with the same message, each with ``critical=True``, and ``db-integrity``
    among them read as a damaged database.

    For a value of the wrong type, the message lists each such value of the
    three files (``housekeeping.remove_unusable_values`` with ``dry_run``),
    not only the first one of ``load``.  The list also names a value that
    ``doctor --fix --only config`` cannot remove (``[project] id``, a file
    without write permission).  The row has a fix command when the fix can
    remove a value.  A file that is not valid TOML needs the user: the row
    then gives the error of ``load`` and no fix command.

    When the files load, a value of a key of ``_TUNING_KEYS`` with the wrong
    type can still be there: ``load`` did not use it (a warning).  The row
    is then ``degraded`` and not critical, with the same list and fix.
    """
    if problem is not None and not getattr(problem, "values", False):
        return DepCheckResult(name="config", status="error", message=str(problem), critical=True)
    from ..housekeeping import remove_unusable_values
    from ..utils import resolve_project_root

    try:
        root: Path | None = resolve_project_root(str(project_root) if project_root else None)
    except OSError:
        root = None
    from .._llm_profile_migration import migrate_llm_profiles

    migration = migrate_llm_profiles(root, dry_run=True)
    report = remove_unusable_values(root, dry_run=True)
    parts = [f"{path}: {label}" for path, label in migration.removed_keys + report.removed_keys]
    parts += [f"{path}: {reason}" for path, reason in migration.failures + report.failures]
    if problem is None and not parts:
        return DepCheckResult(name="config", status="ok", message="the config files load", critical=False)
    if problem is None:
        lead = "config values that fw-context does not use (a lower file or the default applies): "
    else:
        lead = "config values that fw-context cannot use: "
    return DepCheckResult(
        name="config",
        status="error" if problem is not None else "degraded",
        message=lead + "; ".join(parts),
        # --only config: a full `doctor --fix` also pulls the missing models,
        # after the fix of the config lets their checks run.
        fix_cmd="fw-context doctor --fix --only config" if report.removed_keys or migration.removed_keys else None,
        critical=problem is not None,
    )


@dataclass
class ProfileFindings:
    """What the check ``llm-profile`` found (``profile_findings``), for the check and for its fix.

    ``fix_profile`` is the value that the fix writes to ``[llm] profile``
    of ``local.toml``: the Ollama profile of this machine, or "" to remove
    the key (the global default is that profile).  None: nothing to fix.
    """

    root: Path | None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fix_profile: str | None = None


def _project_layers(root: Path) -> list[tuple[str, dict]]:
    """Return the ``[llm]`` table of ``config.toml`` and of ``local.toml`` of *root*, read from the files."""
    from ..config import settings

    layers = []
    for file_name in ("config.toml", "local.toml"):
        path = root / ".fw-context" / file_name
        try:
            llm = settings.read_config_file(path).get("llm") if path.is_file() else None
        except settings.ConfigError:
            continue  # The check "config" reports the file.
        layers.append((file_name, llm if isinstance(llm, dict) else {}))
    return layers


def profile_findings(project_root: str | Path | None) -> ProfileFindings:
    """Find what the check ``llm-profile`` reports (see ``check_llm_profile``)."""
    from ..config import settings
    from ..llm.profile_state import cloud_lists, is_ollama, known, ollama_profile_name, unknown_servers
    from ..utils import resolve_project_root

    try:
        found_root: Path | None = resolve_project_root(str(project_root) if project_root else None)
    except OSError:
        found_root = None
    if found_root is not None and not (found_root / ".fw-context").is_dir():
        found_root = None
    findings = ProfileFindings(found_root)
    try:
        llm = settings.read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm") or {}
    except settings.ConfigError:
        return findings  # The check "config" reports the file.
    raw = llm.get("profiles")
    profiles = {name: p for name, p in raw.items() if isinstance(p, dict)} if isinstance(raw, dict) else {}
    layers = dict(_project_layers(found_root)) if found_root is not None else {}
    _embedding_findings(findings, llm, layers)
    lists = cloud_lists(profiles)
    unknown = unknown_servers(profiles, lists)
    if unknown:
        # Without the list, a cloud model looks like a model of this
        # machine: a fix could send the code of the project out.
        findings.notes.append(f"Ollama at {', '.join(unknown)} gives no model list: the check of the Ollama "
                              "profiles waits for it")
        return findings
    cloud = known(lists)
    machine = ollama_profile_name(llm, cloud)
    ollama = [name for name, profile in profiles.items() if is_ollama(profile, cloud)]
    if machine is None and len(ollama) > 1:
        findings.problems.append(
            f"the global config has the Ollama profiles {', '.join(ollama)} and selects none as the Ollama profile "
            "of this machine: run 'fw-context llm default <profile> --ollama'")
    default = llm.get("profile")
    if machine is not None and isinstance(default, str) and default in ollama and default != machine:
        findings.problems.append(
            f"the global default profile {default!r} is an Ollama profile, and the Ollama profile of this machine "
            f"is {machine!r}: run 'fw-context llm default {machine} --global', or 'fw-context llm default "
            f"{default} --ollama'")
    selected = [(file_name, layer["profile"]) for file_name, layer in layers.items()
                if isinstance(layer.get("profile"), str) and layer["profile"]]
    if not selected:
        return findings
    file_name, name = selected[-1]
    profile = profiles.get(name)
    if profile is None or not is_ollama(profile, cloud) or name == machine:
        return findings
    findings.problems.append(
        f".fw-context/{file_name} selects the Ollama profile {name!r}, and the Ollama profile of this machine is "
        f"{machine or '(none)'!r}: one model in the memory for each project (use --llm {name} for one run)")
    if file_name != "local.toml" or machine is None:
        return findings
    if not settings._is_loopback_url(_profile_url(profile)) or settings._is_loopback_url(
            _profile_url(profiles[machine])):
        findings.fix_profile = _local_value(llm, layers, machine)
    else:
        findings.problems.append(
            f"the fix does not send the code of this project to {_profile_url(profiles[machine])}: select the "
            f"profile yourself with 'fw-context llm default {machine}'")
    return findings


def _profile_url(profile: dict) -> str:
    from ..config import settings

    return str(profile.get("url") or settings._DEFAULT_OLLAMA_URL).rstrip("/")


def _local_value(llm: dict, layers: dict[str, dict], machine: str) -> str:
    """Return what ``local.toml`` must hold as ``profile`` so that *machine* applies: "" to remove the key.

    The key goes only when the file below it selects *machine* too:
    ``config.toml`` of the project, else the global default.  WHY: before,
    the fix removed the key when the global default was *machine*, and a
    ``config.toml`` that selects an external profile then sent the code
    out.
    """
    below = (layers.get("config.toml") or {}).get("profile") or llm.get("profile")
    return "" if below == machine else machine


def _embedding_findings(findings: ProfileFindings, llm: dict, layers: dict[str, dict]) -> None:
    """Report an embedding model of the project that is not the global one: never fixed (a new one embeds again)."""
    global_embed = llm.get("embed_model")
    for file_name in ("local.toml", "config.toml"):
        embed = (layers.get(file_name) or {}).get("embed_model")
        if isinstance(embed, str) and embed:
            if isinstance(global_embed, str) and global_embed and embed != global_embed:
                findings.problems.append(
                    f"the embedding model of this project is {embed} (.fw-context/{file_name}), of the global "
                    f"config {global_embed}: the index of the project holds {embed}. To use {global_embed}, "
                    f"remove embed_model from .fw-context/{file_name}; the next index run embeds each symbol again")
            return


def check_llm_profile(project_root: str | Path | None = None) -> DepCheckResult:
    """Say if the project uses the Ollama profile of this machine, and the global embedding model.

    One Ollama profile for each machine (decision of the user,
    2026-10-21; ``llm.profile_state``).  A project that selects another
    Ollama profile in ``local.toml`` gets a fix: ``local.toml`` then
    selects the Ollama profile of this machine, or nothing when the file
    below it selects that profile too (``_local_value``).  The fix never
    lets an external profile apply, never moves the code of a project from
    this machine to another computer, and decides nothing while a local
    Ollama gives no model list (a cloud model then looks like a local
    one).  A global default that is another Ollama profile is reported.  ``config.toml`` (in git), several Ollama profiles without
    ``[llm] ollama_profile``, and an embedding model of the project that
    is not the global one only get a message: the user decides, and a new
    embedding model embeds each symbol again.  Not critical.
    """
    findings = profile_findings(project_root)
    if not findings.problems:
        # A note says that a part was not checked: the sentence then claims nothing.
        message = "; ".join(findings.notes) if findings.notes else (
            "the project uses the Ollama profile of this machine and the global embedding model")
        return DepCheckResult(name="llm-profile", status="ok", message=message, critical=False)
    return DepCheckResult(
        name="llm-profile", status="degraded", message="; ".join(findings.problems + findings.notes),
        fix_cmd="fw-context doctor --fix --only llm-profile" if findings.fix_profile is not None else None,
        critical=False)


def check_llm_models() -> DepCheckResult:
    """Say if the list of the models of Ollama in the global config is the list of this release.

    See ``config._model_list``: the check reads only, ``doctor --fix``
    adds, updates and removes the entries of fw-context.  Not critical: the
    list gives a recommendation of a model, and no answer changes.  An
    entry that the user changed stays; the message names it when this
    release has a new value for it.
    """
    import tomlkit

    from .. import __version__
    from ..config import settings
    from ..config._model_list import sync_global_config

    path = settings._GLOBAL_CONFIG_PATH
    fix = "fw-context doctor --fix --only llm-models"
    if not path.is_file():
        return DepCheckResult(name="llm-models", status="degraded", fix_cmd=fix, critical=False,
                              message=f"no global config ({path}): the list of the models is not there")
    try:
        report = sync_global_config(path, version=__version__, dry_run=True)
    except (OSError, UnicodeDecodeError, tomlkit.exceptions.ParseError) as e:
        return DepCheckResult(name="llm-models", status="degraded", critical=False,
                              message=f"cannot read the global config ({path}): {e}")
    if not report.changes and not report.skipped:
        message = f"the list of the models is the list of fw-context {__version__}"
        if report.kept:
            message += "; " + "; ".join(report.lines())
        return DepCheckResult(name="llm-models", status="ok", message=message, critical=False)
    # A skipped table needs the user: the fix can only add what it can write.
    return DepCheckResult(name="llm-models", status="degraded", fix_cmd=fix if report.changes else None,
                          critical=False,
                          message=f"the list of the models is not the list of fw-context {__version__}: "
                                  + "; ".join(report.lines()))


def check_llm_recommend(cfg, progress: Callable[[str], None] | None = None) -> DepCheckResult:
    """Say if a local model of Ollama is better than the chat model or the embedding model of the config.

    The rule of two bands (``llm.recommend``).  *progress* None: only the
    stored speeds count, and no model loads (``check_dependencies`` of
    the MCP server).  Else each installed model of the list without a
    stored speed is measured, and *progress* reports each measurement
    (``fw-context doctor``: the user waits, 5 to 70 s for each model).
    Not critical, and no fix: the user selects the model
    (``fw-context llm setup``), and a run never changes it.
    """
    if not cfg.enabled:
        return DepCheckResult(name="llm-recommend", status="skipped", message="LLM disabled in config",
                              critical=False)
    from ..config.settings import _is_loopback_url
    from ..llm.chat_router import chat_ollama_server
    from ..llm.embedder_factory import uses_ollama
    from ..llm.hardware import machine_memory
    from ..llm.recommend import CHAT, EMBEDDING, cloud_alternative, gather, verdict

    roles = []
    try:
        chat_server = chat_ollama_server(cfg)
    except ValueError:
        chat_server = None  # The check "chat-model" reports the URL.
    # Without the LLM analysis, no chat model runs: its measurement only
    # costs time.
    if chat_server is not None and cfg.model and cfg.analyze_symbols:
        roles.append((CHAT, chat_server, cfg.model))
    if uses_ollama(cfg) and cfg.embed_model:
        roles.append((EMBEDDING, cfg.ollama_url, cfg.embed_model))
    if not roles:
        return DepCheckResult(name="llm-recommend", status="skipped", critical=False,
                              message="no model of the config runs on Ollama")
    memory = machine_memory()
    better, parts = False, []
    for role, server, model in roles:
        recommendation = gather(role, server, memory if _is_loopback_url(server) else None,
                                measure=progress is not None, keep_alive=cfg.keep_alive, timeout=cfg.timeout,
                                progress=progress)
        found, text = verdict(recommendation, model)
        better = better or found
        parts.append(f"{role.name}: {text}")
    # A cloud service is always a choice too (decision of the user,
    # 2026-10-22): the message names the best one; the status is of the
    # local models only.
    cloud = [f"{role.name} {text}" for role, _server, _model in roles if (text := cloud_alternative(role))]
    if cloud:
        parts.append("a cloud service is a choice too: " + ", ".join(cloud))
    if better:
        parts.append("select a model with: fw-context llm setup")
    return DepCheckResult(name="llm-recommend", status="degraded" if better else "ok", message="; ".join(parts),
                          critical=False)


def check_tomli_w() -> DepCheckResult:
    """Verify tomli-w is installed — required for config writes during init.

    WHY: ``tomli-w`` is a runtime dependency of the config writer
    (``config/_toml_editor.py`` lazy-imports it in ``set_key`` and
    ``merge_template``).  ``pip install fw-context`` declares it, but a
    partially-broken environment can miss it.  ``fw-context init`` and
    ``doctor --fix`` write config, so a missing ``tomli-w`` crashes with
    ``No module named 'tomli_w'`` only at write time — this import probe
    surfaces the problem early.
    """
    platform_ctx = get_platform_info()
    if _require_tomli_w():
        return DepCheckResult(
            name="tomli-w",
            status="ok",
            message=f"tomli-w {_version('tomli-w')}",
            critical=False,
        )
    return DepCheckResult(
        name="tomli-w",
        status="missing",
        message="tomli-w not installed — blocks fw-context init (config writes)",
        fix_cmd=f"{platform_ctx['pip_cmd']} tomli-w",
        instructions=f"{platform_ctx['pip_cmd']} tomli-w",
        critical=False,
    )


#: The PCI vendor id of NVIDIA, and the PCI class of a display controller
#: (``0x03`` and a subclass), as ``/sys/bus/pci/devices/*/`` gives them.
_NVIDIA_VENDOR = "0x10de"
_DISPLAY_CLASS_PREFIX = "0x03"
_PCI_DEVICES = Path("/sys/bus/pci/devices")


def check_gpu_tools(cfg) -> DepCheckResult:
    """Check that ``nvidia-smi`` gives the GPU memory on a Linux machine with an NVIDIA GPU.

    WHY: the recommendation of a model reads the GPU memory with it
    (``llm.hardware``); without it, the machine counts as a machine without
    a GPU, and the setup recommends a small model.  WHY no fix: the tool is
    a part of the NVIDIA driver, not a Python package; its install needs
    the package manager of the system and root, and fw-context runs no
    command as root.  Thus the check names the package, or, for a tool
    that is installed and fails, the usual cause (the proprietary driver of
    Arch and Manjaro always brings it, thus a failure there is a driver
    update without a restart, or the kernel driver nouveau).  WHY only when
    a model of *cfg* (``LLMConfig``) runs on the Ollama of this machine:
    else the recommendation does not read the GPU.  WHY Linux only: on
    Windows the driver installs ``nvidia-smi``, and macOS has no NVIDIA
    driver for Ollama.
    """
    if sys.platform != "linux":
        return DepCheckResult(name="gpu-tools", status="skipped", critical=False,
                              message="only on Linux: the NVIDIA driver of Windows has nvidia-smi, and macOS has "
                                      "no NVIDIA driver")
    if not _local_ollama_used(cfg):
        return DepCheckResult(name="gpu-tools", status="skipped", critical=False,
                              message="no model of the config runs on the Ollama of this machine")
    if not _nvidia_gpu_present():
        return DepCheckResult(name="gpu-tools", status="skipped", critical=False, message="no NVIDIA GPU")
    from ..llm.hardware import nvidia_gpu, nvidia_smi_problem

    problem = nvidia_smi_problem()
    if problem is None:
        gpu = nvidia_gpu()
        return DepCheckResult(name="gpu-tools", status="ok", critical=False,
                              message=f"nvidia-smi: {gpu.total_mib} MiB" if gpu else "nvidia-smi")
    if problem.missing:
        instructions = (
            "nvidia-smi is a part of the NVIDIA driver. Install the driver utilities of your system:\n"
            "  Arch, Manjaro: sudo pacman -S nvidia-utils\n"
            "  Debian: sudo apt install nvidia-smi\n"
            "  another system: find the package that has nvidia-smi (apt-file search nvidia-smi, "
            "dnf provides '*/nvidia-smi')"
        )
        message = ("this machine has an NVIDIA GPU, but nvidia-smi is not installed: the recommendation of a "
                   "model counts no GPU")
    else:
        instructions = (
            "nvidia-smi is installed and fails. Usual causes:\n"
            "  a driver update without a restart (\"Driver/library version mismatch\"): restart the machine\n"
            "  the kernel driver nouveau and not the NVIDIA driver: install the NVIDIA driver of your system"
        )
        message = (f"this machine has an NVIDIA GPU, but nvidia-smi fails ({problem}): the recommendation of a "
                   "model counts no GPU")
    return DepCheckResult(name="gpu-tools", status="degraded", critical=False, instructions=instructions,
                          message=message)


def _local_ollama_used(cfg) -> bool:
    """Return True when the chat or the embeddings of *cfg* (``LLMConfig``) run on an Ollama of this machine."""
    from ..config.settings import _is_loopback_url
    from ..llm.chat_router import chat_ollama_server
    from ..llm.embedder_factory import uses_ollama

    if not cfg.enabled:
        return False
    try:
        chat_server = chat_ollama_server(cfg)
    except ValueError:
        chat_server = None
    chat = chat_server is not None and _is_loopback_url(chat_server)
    return chat or (uses_ollama(cfg) and _is_loopback_url(cfg.ollama_url))


def _nvidia_gpu_present() -> bool:
    """Return True when a PCI display controller of NVIDIA is in ``/sys/bus/pci/devices``."""
    try:
        devices = list(_PCI_DEVICES.iterdir())
    except OSError:
        return False
    for device in devices:
        try:
            vendor = (device / "vendor").read_text(encoding="ascii").strip()
            device_class = (device / "class").read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if vendor == _NVIDIA_VENDOR and device_class.startswith(_DISPLAY_CLASS_PREFIX):
            return True
    return False


def check_ollama_running(cfg) -> DepCheckResult:
    """Verify Ollama is reachable."""
    if not cfg.enabled:
        return DepCheckResult(name="ollama", status="skipped", message="LLM disabled in config", critical=False)

    platform_ctx = get_platform_info()
    from ..llm._diag import check_setup

    result = check_setup(cfg)
    if result.get("status") == "error":
        return DepCheckResult(
            name="ollama",
            status="degraded",
            message=result.get("message", "Ollama not reachable"),
            instructions=ollama_instructions(platform_ctx),
            critical=False,
        )
    from ..llm.ollama import ollama_version_problem

    # Also in `fw-context init`, which runs these checks.
    problem = ollama_version_problem(cfg.ollama_url)
    if problem:
        return DepCheckResult(
            name="ollama",
            status="degraded",
            message=problem,
            instructions=ollama_instructions(platform_ctx),
            critical=False,
        )
    return DepCheckResult(
        name="ollama",
        status="ok",
        message=f"{cfg.ollama_url} (running)",
        critical=False,
    )


def check_ollama_chat_model(cfg) -> DepCheckResult:
    """Verify the configured chat model is installed on the Ollama server of the chat calls.

    That server is ``ollama_url``, or the Ollama server of ``chat_api_base``
    (`chat_router.chat_ollama_server`).  WHY ``skipped`` for an
    OpenAI-compatible ``chat_api_base``: that API runs the model, and
    ``doctor --fix`` must not try to pull its name from Ollama.

    WHY this check asks the chat server itself, and not
    ``_diag.check_setup``: ``semantic_search`` calls ``check_setup`` on each
    query, and the embeddings do not use the chat server.  If
    ``check_setup`` asked the chat server, a chat server that does not
    answer would make each query wait.  Also, the check of a
    remote chat server does not depend on the server of ``ollama_url``.
    """
    if not cfg.enabled:
        return DepCheckResult(name="chat-model", status="skipped", message="LLM disabled in config", critical=False)

    from ..llm._diag import installed_models
    from ..llm.auto_model import _model_name_matches
    from ..llm.chat_router import chat_ollama_server
    from ..llm.ollama import ollama_pull_command

    try:
        server = chat_ollama_server(cfg)
    except ValueError as exc:
        return DepCheckResult(name="chat-model", status="error", message=str(exc), critical=False)
    if server is None:
        return _check_api_chat_model(cfg)
    models = installed_models(server)
    if models is None:
        if server.rstrip("/") == cfg.ollama_url.rstrip("/"):
            # The check "ollama" reports this server.
            return DepCheckResult(name="chat-model", status="skipped", message="Ollama not reachable", critical=False)
        return DepCheckResult(
            name="chat-model",
            status="degraded",
            message=f"the Ollama server {server} of the chat profile gives no model list (/api/tags)",
            critical=False,
        )
    if server.rstrip("/") != cfg.ollama_url.rstrip("/"):
        # The check "ollama" reports the version of ollama_url only.
        from ..llm.ollama import ollama_version_problem

        problem = ollama_version_problem(server)
        if problem:
            return DepCheckResult(name="chat-model", status="degraded", message=problem, critical=False)
    if not any(_model_name_matches(m, cfg.model) for m in models):
        command = ollama_pull_command(cfg.model, server, get_platform_info()["system"])
        return DepCheckResult(
            name="chat-model",
            status="missing",
            message=f"model '{cfg.model}' not installed",
            fix_cmd=command,
            instructions=command,
            critical=False,
        )
    return DepCheckResult(
        name="chat-model",
        status="ok",
        message=cfg.model,
        critical=False,
    )


def _check_api_chat_model(cfg) -> DepCheckResult:
    """Check that the model list of the API holds the chat model, and that its context is known.

    WHY (decision of the user, 2026-10-22): the context must be known, and
    the names come from the list of the provider.  A name that the list no
    longer holds still answered a request, and the analysis then stopped
    without the context.  WHY no fix: the user selects the model
    (``fw-context llm setup``); doctor does not change a profile.  A static
    ``context`` of the profile next to a context of the list is only
    reported: the list applies.
    """
    from ..llm.model_list import LIST_PROVIDERS, ModelContextError, model_context

    if cfg.provider not in LIST_PROVIDERS:
        return DepCheckResult(
            name="chat-model", status="skipped", critical=False,
            message=f"chat model '{cfg.model}' runs on an OpenAI-compatible API (profile {cfg.profile!r}), not on Ollama",
        )
    try:
        context = model_context(cfg)
    except ModelContextError as exc:
        return DepCheckResult(name="chat-model", status="error", message=str(exc), critical=False,
                              instructions="fw-context llm setup")
    if 0 < cfg.context != context:
        return DepCheckResult(
            name="chat-model", status="degraded", critical=False,
            message=f"profile {cfg.profile!r} sets context = {cfg.context}, and the model list of the API gives "
                    f"{context} for '{cfg.model}': the list applies; remove context from [llm.profiles.{cfg.profile}]",
        )
    return DepCheckResult(name="chat-model", status="ok", critical=False,
                          message=f"{cfg.model} (context {context} tokens)")


def check_ollama_embed_model(cfg) -> DepCheckResult:
    """Verify the configured embedding model is installed.

    An API profile of the embeddings (``embed_provider``) has no model to
    install: ``skipped``.  A request to the API would send text out and
    cost money for each doctor run; the index run reports its errors.
    """
    if not cfg.enabled:
        return DepCheckResult(name="embed-model", status="skipped", message="LLM disabled in config", critical=False)
    if cfg.embed_provider != "ollama":
        return DepCheckResult(name="embed-model", status="skipped", critical=False,
                              message=f"embedding model '{cfg.embed_model}' runs on the API {cfg.embed_api_base} "
                                      f"(profile {cfg.embed_profile!r}), not on Ollama")

    from ..llm._diag import check_setup
    from ..llm.ollama import ollama_pull_command

    result = check_setup(cfg)
    if result.get("status") == "error":
        return DepCheckResult(name="embed-model", status="skipped", message="Ollama not reachable", critical=False)
    if not result.get("embedding_installed"):
        command = ollama_pull_command(cfg.embed_model, cfg.ollama_url, get_platform_info()["system"])
        return DepCheckResult(
            name="embed-model",
            status="missing",
            message=f"embedding model '{cfg.embed_model}' not installed",
            fix_cmd=command,
            instructions=command,
            critical=False,
        )
    return DepCheckResult(
        name="embed-model",
        status="ok",
        message=cfg.embed_model,
        critical=False,
    )


def check_obsolete_files(project_root: str | Path | None = None) -> DepCheckResult:
    """List what an older fw-context wrote and no version uses, see ``housekeeping``.

    The check removes nothing: ``doctor`` and the MCP tool
    ``check_dependencies`` only read.  ``doctor --fix`` removes the list.
    Not critical: such a file or key takes space and gives a warning, and
    no answer changes.
    """
    from ..housekeeping import clean
    from ..utils import resolve_project_root

    try:
        root: Path | None = resolve_project_root(str(project_root) if project_root else None)
    except OSError:
        root = None
    report = clean(root, dry_run=True, quiet=True)
    if report.empty and not report.failures:
        return DepCheckResult(
            name="obsolete-files", status="ok",
            message="no file or config key of an older fw-context", critical=False,
        )
    parts = []
    if not report.empty:
        parts.append(f"{len(report.lines())} item(s) of an older fw-context: " + "; ".join(report.lines()))
    parts += [f"cannot clean {path}: {reason}" for path, reason in report.failures]
    # Without an item to remove, the fix cannot do more than this check:
    # a config that does not parse, a read-only config, or an index that
    # cannot be read needs the user, thus no fix command.
    return DepCheckResult(
        name="obsolete-files",
        status="degraded",
        message="; ".join(parts),
        # --only: a full `doctor --fix` also removes config values and
        # pulls models.
        fix_cmd=None if report.empty else "fw-context doctor --fix --only obsolete-files",
        critical=False,
    )


def check_db_integrity(project_root: str | Path | None = None) -> DepCheckResult:
    """Verify the index database is not corrupt.

    A read that fails with a lock, a missing permission or a fault of the
    disk gives ``degraded``, not critical (``error_shows_damage``).  WHY:
    the check reported each such failure as a critical ``error``, and the
    exit code 2 of doctor then said "damaged" for a good index that an
    index run held.
    """
    platform_ctx = get_platform_info()
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..utils import resolve_project_root

    try:
        root = resolve_project_root(str(project_root) if project_root else None)
    except OSError:
        return DepCheckResult(
            name="db-integrity",
            status="skipped",
            message="no project root found — nothing to check",
            critical=True,
        )

    cfg = load_config(root)
    if not cfg.project.id:
        return DepCheckResult(
            name="db-integrity",
            status="skipped",
            message="project not initialized — nothing to check",
            critical=True,
        )

    project_id = derive_project_id(root)
    db_path = cfg.index.db_dir / project_id / "index.db"
    if not db_path.exists():
        return DepCheckResult(
            name="db-integrity",
            status="ok",
            message="no index yet — nothing to check",
            critical=True,
        )

    try:
        # as_uri(): a space, "#", "?" or "%" of the path is a character of a
        # name, and a Windows path needs the form file:///C:/...
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if row and row[0] == "ok":
                # Get file size for context
                size_mb = db_path.stat().st_size / (1024 * 1024)
                return DepCheckResult(
                    name="db-integrity",
                    status="ok",
                    message=f"quick_check: ok (index.db, {size_mb:.0f} MB)",
                    critical=True,
                )
            # quick_check returned an error — run full integrity_check.
            # quick_check found damage: an error of the second read (a lock,
            # an I/O error) must not make it "not damage".
            quick_msg = row[0] if row else "unknown error"
            try:
                detail = conn.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.Error:
                detail = None
            detail_msg = detail[0] if detail else quick_msg
            return DepCheckResult(
                name="db-integrity",
                status="error",
                message=f"integrity_check: {detail_msg}",
                instructions=db_integrity_instructions(platform_ctx),
                critical=True,
            )
        finally:
            conn.close()
    except sqlite3.Error as e:
        return _read_failure(e, platform_ctx)


def _read_failure(error: sqlite3.Error, platform_ctx: dict) -> DepCheckResult:
    """The result for a read of the index that failed: damage, or a cause that is not damage."""
    from ..indexer.db import error_shows_damage

    if error_shows_damage(error):
        return DepCheckResult(
            name="db-integrity",
            status="error",
            message=f"database error: {error}",
            instructions=db_integrity_instructions(platform_ctx),
            critical=True,
        )
    return DepCheckResult(
        name="db-integrity",
        status="degraded",
        message=(
            f"cannot read the index now: {error}. This does not show damage: the cause can be a lock, "
            "a missing permission, a fault of the host (disk, memory) or a fault of the program. "
            "Repair that cause, and do not delete the database."
        ),
        critical=False,
    )


def check_disk_space(project_root: str | Path | None = None) -> DepCheckResult:
    """Verify sufficient free disk space on the index volume."""
    platform_ctx = get_platform_info()
    import shutil

    # Resolve the actual index directory from config when available,
    # so the check reflects the disk where the index lives.
    target = Path.home() / ".fw-context"
    try:
        from ..config import load as load_config
        from ..utils import resolve_project_root

        root = resolve_project_root(str(project_root) if project_root else None)
        cfg = load_config(root)
        db_dir = cfg.index.db_dir
        # Use db_dir if it exists, otherwise try its parent
        if db_dir.exists():
            target = db_dir
        elif db_dir.parent.exists():
            target = db_dir.parent
    except (OSError, RuntimeError):
        pass
    # Only create the directory if it already exists (read-only audit)
    if not target.exists():
        target = Path.home()

    try:
        usage = shutil.disk_usage(target)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < 128:
            return DepCheckResult(
                name="disk-space",
                status="error",
                message=f"{free_mb:.0f} MB free on {target} — critically low",
                instructions=disk_space_instructions(platform_ctx),
                critical=False,
            )
        if free_mb < 512:
            return DepCheckResult(
                name="disk-space",
                status="degraded",
                message=f"{free_mb:.0f} MB free on {target} — low",
                instructions=disk_space_instructions(platform_ctx),
                critical=False,
            )
        free_gb = usage.free / (1024 * 1024 * 1024)
        return DepCheckResult(
            name="disk-space",
            status="ok",
            message=f"{free_gb:.1f} GB free on {target}",
            critical=False,
        )
    except OSError as e:
        return DepCheckResult(
            name="disk-space",
            status="error",
            message=f"cannot check disk usage: {e}",
            critical=False,
        )


# ── Ordered check list ──────────────────────────────────────────────────

CHECK_ORDER: list[tuple[str, Any]] = [
    ("pysqlite3", check_pysqlite3),
    ("sqlite-ext", check_sqlite3_extensions),
    ("sqlite-vec", check_sqlite_vec),
    ("libclang-python", check_libclang_python),
    ("libclang-so", check_libclang_so),
    ("clang-resource", check_clang_resource),
    ("watchfiles", check_watchfiles),
    ("tomli-w", check_tomli_w),
    ("config", check_config),
    ("llm-models", check_llm_models),
    ("llm-profile", check_llm_profile),
    ("gpu-tools", check_gpu_tools),
    ("ollama", check_ollama_running),
    ("chat-model", check_ollama_chat_model),
    ("embed-model", check_ollama_embed_model),
    ("llm-recommend", check_llm_recommend),
    ("db-integrity", check_db_integrity),
    ("obsolete-files", check_obsolete_files),
    ("disk-space", check_disk_space),
]
