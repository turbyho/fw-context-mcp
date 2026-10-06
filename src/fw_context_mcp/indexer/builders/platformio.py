"""PlatformIO build system — detection, build, validation, and auto-fix."""

from __future__ import annotations

import configparser
import logging
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import run_build_command

from ..build_layout import COMPILE_COMMANDS_NAME, BuildLayout
from . import _link_command, _platformio_link, registry
from ._linker import LinkRecord
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)

_PIO_MARKERS = ["platformio.ini"]

# The SCons script that each build of fw-context adds through
# PLATFORMIO_EXTRA_SCRIPTS, see PlatformIOBuildSystem.build.
# COMPILATIONDB_PATH is the construction variable of the compiledb tool, and
# $BUILD_DIR is the build directory of the environment.
_COMPILEDB_SCRIPT_NAME = "fw_context_compiledb.py"
_BUILD_DIR_VARIABLE = "PLATFORMIO_BUILD_DIR"
_EXTRA_SCRIPTS_VARIABLE = "PLATFORMIO_EXTRA_SCRIPTS"
_COMPILEDB_SCRIPT = (
    "# Written by fw-context. PlatformIO runs it before each build of fw-context.\n"
    "# It puts compile_commands.json into the build directory of the environment,\n"
    "# thus the file in the project root stays as the build of the user wrote it.\n"
    'Import("env")\n'
    'env.Replace(COMPILATIONDB_PATH="$BUILD_DIR/compile_commands.json")\n'
)


def _libdeps_dir(project_root: Path) -> str:
    """Return the ``libdeps`` directory of *project_root*, relative to it.

    Defaults to ``.pio/libdeps``.  A project can move it with
    ``libdeps_dir`` in the ``[platformio]`` section of ``platformio.ini``,
    and a fixed pattern would then match nothing.

    Reads the file with configparser, which is what PlatformIO's own
    format is.  On any read or parse error the default is returned: a
    pattern that is merely wrong for one project is better than an index
    run that stops.  An absolute or out-of-tree value is dropped for the
    same reason it is dropped everywhere else — a path outside
    project_root is vendor by position and needs no pattern.
    """
    default = ".pio/libdeps"
    ini = project_root / "platformio.ini"
    if not ini.is_file():
        return default
    parser = configparser.ConfigParser()
    try:
        parser.read(ini, encoding="utf-8")
        raw = parser.get("platformio", "libdeps_dir", fallback="").strip()
    except (configparser.Error, OSError, UnicodeDecodeError):
        log.debug("Cannot read libdeps_dir from %s", ini, exc_info=True)
        return default
    if not raw:
        return default
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        rel = candidate.resolve().relative_to(project_root.resolve())
    except (ValueError, OSError):
        return default
    return str(rel) if str(rel) != "." else default


def _first_line(exc: RuntimeError) -> str:
    """Return the first line of the message of a failed ``pio`` run.

    The lines after it quote the output, and the output of ``envdump``
    holds the whole ``ENV`` of the process, which can hold a token.
    """
    return str(exc).split("\n", 1)[0]


class PlatformIOBuildSystem:
    """PlatformIO build system (``pio run --target compiledb``).

    WHY two build steps (compiledb + full build): SCons (PlatformIO's
    build engine) tracks targets independently.  The ``compiledb`` target
    generates compile_commands.json by inspecting SCons internals but may
    not invoke GCC.  A subsequent ``pio run`` ensures actual compilation
    happens, producing .d dependency files needed for header-change
    detection.

    WHY clean is best-effort: the build directory may not exist on a fresh
    checkout, so ``pio run --target clean`` may fail.  We catch the error
    and continue — if the build dir exists, it's cleaned; if not, we skip.
    """

    name: str = "PlatformIO"
    config_key: str = "platformio"
    markers: list[str] = ["platformio.ini"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return any((root / m).exists() for m in _PIO_MARKERS)

    # ── Build ──

    @staticmethod
    def _pio_prefix(cfg: BuildConfig) -> list[str]:
        """Return the command that runs the PlatformIO CLI, or raise."""
        if cfg.python:
            return [cfg.python, "-m", "platformio"]
        if shutil.which("pio"):
            return ["pio"]
        if shutil.which("platformio"):
            return ["platformio"]
        raise RuntimeError("PlatformIO CLI is required.  Install it:  pip install platformio")

    def environments(self, project_root: Path, cfg: BuildConfig) -> list[str]:
        """Return the environments that ``pio run`` builds, or raise RuntimeError.

        The answer comes from PlatformIO itself (``pio project config``), not
        from a read of ``platformio.ini``: ``extends``, ``extra_configs`` and
        ``default_envs`` decide the set, and only PlatformIO applies them as
        its build does.  See ``_platformio_link.run_environments``.
        """
        cmd = self._pio_prefix(cfg) + ["project", "config", "--project-dir", str(project_root), "--json-output"]
        result = run_build_command(cmd, cwd=project_root, description="pio project config", build_cfg=cfg)
        names = _platformio_link.run_environments(result.stdout or "")
        if not names:
            raise RuntimeError("pio project config names no environment that pio run builds")
        return names

    def implicit_variants(self, project_root: Path, cfg: BuildConfig) -> list:
        """Return one variant for each environment, or [] for a project with one.

        Each environment of ``platformio.ini`` is a build of its own (its
        board, its flags), thus each one is a variant, named as the
        environment.  A project with one environment stays a project
        without variants, and this method sets that environment on *cfg*:
        its queries then need no ``variant``.  ``[build] environment``
        selects one environment, thus it asks for no variant either.
        """
        from ..build import BuildVariant

        if cfg.environment:
            return []
        names = self.environments(project_root, cfg)
        if len(names) == 1:
            cfg.environment = names[0]
            return []
        return [BuildVariant(name=name, overrides={"environment": name}) for name in names]

    def _environment(self, project_root: Path, cfg: BuildConfig) -> str:
        """Return the one environment that the build of *cfg* builds.

        Each environment is a variant: ``cfg.environment``, or the name of
        the variant.  A project without variants builds its only
        environment.  ``cli._index`` makes a variant of each environment of
        a project that has more than one, thus that case does not reach this
        point through an index run.
        """
        if cfg.environment:
            return cfg.environment
        if cfg.variant_name:
            return cfg.variant_name
        names = self.environments(project_root, cfg)
        if len(names) != 1:
            raise RuntimeError(
                f"platformio.ini builds {len(names)} environments ({', '.join(names)}), and each "
                "is a variant of its own. Name one with [build] environment, or declare "
                "[[build.variants]]."
            )
        return names[0]

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Build one environment with ``pio run -e <env>`` and return its database.

        Three steps, each with ``-e <env>``:

        1. ``--target compiledb`` writes the compilation database.
        2. ``--target envdump`` records the link (see ``_record_link``).
        3. A full ``pio run`` compiles, thus the ``.d`` files exist for the
           header-change detection.

        Every call gets two environment variables.  ``PLATFORMIO_BUILD_DIR``
        sends the build to ``out/`` of the variant (PlatformIO adds the
        directory ``<env>``).  ``PLATFORMIO_EXTRA_SCRIPTS`` adds a script that
        moves ``compile_commands.json`` into that build directory: PlatformIO
        appends the value to the ``extra_scripts`` of ``platformio.ini``
        (``ProjectConfigBase.getraw``), thus the scripts of the user still
        run, and the file in the project root, which the clangd of the user
        reads, stays as the build of the user wrote it.  Measured on two
        projects: the root file kept its mtime, and the database was in
        ``out/<env>/``.
        """
        pio_prefix = self._pio_prefix(cfg)
        env_name = self._environment(project_root, cfg)
        layout = BuildLayout(project_root)
        out_dir = layout.out_dir(cfg.variant_name)
        cfg, build_env = self._build_environment(
            cfg, out_dir, self._compiledb_script(layout.variant_dir(cfg.variant_name)),
        )
        self._remove_other_environments(out_dir, env_name)
        run = pio_prefix + ["run", "--project-dir", str(project_root), "--environment", env_name]

        if cfg.clean:
            clean_cmd = run + ["--target", "clean"]
            log.info("platformio clean: %s", " ".join(clean_cmd))
            try:
                run_build_command(clean_cmd, cwd=project_root, description="pio run --target clean", build_cfg=cfg, env=build_env)
            except RuntimeError:
                pass  # clean is best-effort — build dir may not exist yet

        # The database of an earlier build goes first: a compiledb that
        # writes elsewhere (a script of the user that sets
        # COMPILATIONDB_PATH after ours) must give an error, and not leave
        # the old file to read as the new one.
        cc_path = out_dir / env_name / COMPILE_COMMANDS_NAME
        cc_path.unlink(missing_ok=True)
        cmd = run + ["--target", "compiledb"]
        log.info("platformio build: %s", " ".join(cmd))
        run_build_command(cmd, cwd=project_root, description="pio run --target compiledb", build_cfg=cfg, env=build_env)

        if not cc_path.exists():
            raise RuntimeError(
                f"pio run --target compiledb wrote no {cc_path}; an extra_script that sets "
                "COMPILATIONDB_PATH sends the database elsewhere"
            )

        self._record_link(project_root, cfg, pio_prefix, env_name, build_env, cc_path)

        # ── Full build for .d file generation ──
        # compiledb only writes compile_commands.json — GCC may not have
        # run.  A full pio run compiles and emits .d files that the indexer
        # uses for header-change staleness detection.  When the build is
        # already up-to-date, pio run exits quickly (no-op).
        build_cmd = run
        log.info("platformio compile: %s", " ".join(build_cmd))
        try:
            run_build_command(build_cmd, cwd=project_root, description="pio run (full build for .d files)", build_cfg=cfg, env=build_env)
        except RuntimeError:
            log.warning(
                "Full build failed — .d files may be missing. "
                "Header change detection will be limited. "
                "Fix compilation errors and re-run 'fw-context index --build'."
            )

        return cc_path

    @staticmethod
    def _build_environment(cfg: BuildConfig, out_dir: Path, script: str) -> tuple[BuildConfig, dict[str, str]]:
        """Return the config and the environment variables of each pio call.

        ``run_build_command`` puts ``[build] env`` and ``extra_env`` over
        the variables that a backend gives, thus a ``PLATFORMIO_EXTRA_SCRIPTS``
        of the user there would remove the script of fw-context, and the
        one of fw-context would remove a script that the user adds through
        the process environment (a CI job).  The two are joined instead,
        with a newline as PlatformIO joins the option of the ini and the
        variable.  The config that comes back holds the variable no more.

        ``PLATFORMIO_BUILD_DIR`` in the config is an error: the build of
        fw-context goes to its output directory, and a value of the user
        would send it to another one without a word.
        """
        user_sources = [cfg.env, cfg.extra_env]
        if any(_BUILD_DIR_VARIABLE in source for source in user_sources):
            raise RuntimeError(
                f"{_BUILD_DIR_VARIABLE} in [build] env or extra_env: fw-context builds into "
                f"{out_dir}. Remove the variable from .fw-context/config.toml or local.toml."
            )
        user_scripts = (
            cfg.extra_env.get(_EXTRA_SCRIPTS_VARIABLE)
            or cfg.env.get(_EXTRA_SCRIPTS_VARIABLE)
            or os.environ.get(_EXTRA_SCRIPTS_VARIABLE, "")
        )
        joined = f"{user_scripts.rstrip()}\n{script}" if user_scripts.strip() else script
        run_cfg = replace(
            cfg,
            env={k: v for k, v in cfg.env.items() if k != _EXTRA_SCRIPTS_VARIABLE},
            extra_env={k: v for k, v in cfg.extra_env.items() if k != _EXTRA_SCRIPTS_VARIABLE},
        )
        return run_cfg, {_BUILD_DIR_VARIABLE: str(out_dir), _EXTRA_SCRIPTS_VARIABLE: joined}

    @staticmethod
    def _compiledb_script(variant_dir: Path) -> str:
        """Write the script that moves the database, and return the value of ``PLATFORMIO_EXTRA_SCRIPTS``.

        The script sits beside ``out/``: PlatformIO removes its build
        directory when ``project.checksum`` changes.  The value ends with a
        newline because ``ProjectConfigBase.parse_multi_values`` splits a
        value without a newline at ``", "``, thus a project path with a
        comma and a space would break into two items.  A ``;`` starts an
        inline comment there, and PlatformIO and SCons expand a ``$`` as a
        variable, thus no form of the value can hold one of the two.
        """
        script = variant_dir / _COMPILEDB_SCRIPT_NAME
        bad = sorted({char for char in str(script) if char in ";$"})
        if bad:
            raise RuntimeError(
                f"PlatformIO cannot run a script whose path holds {' or '.join(repr(c) for c in bad)}: "
                f"{script}. Move the project to a path without it."
            )
        variant_dir.mkdir(parents=True, exist_ok=True)
        if not script.is_file() or script.read_text(encoding="utf-8") != _COMPILEDB_SCRIPT:
            script.write_text(_COMPILEDB_SCRIPT, encoding="utf-8")
        return f"pre:{script}\n"

    @staticmethod
    def _remove_other_environments(out_dir: Path, env_name: str) -> None:
        """Remove the build directories of other environments from *out_dir*.

        A variant builds one environment, and ``output_compile_commands``
        reads the database of the environment that is in ``out/``.  An
        environment that the variant built before (platformio.ini renamed
        it, or ``[build] environment`` changed) would leave a second
        database there, and the answer would be ambiguous.  ``out/`` belongs
        to the build tree of fw-context, thus nothing of the user goes.  A
        symbolic link is not followed: it is no build that PlatformIO made
        here.
        """
        if not out_dir.is_dir():
            return
        for child in out_dir.iterdir():
            if child.is_dir() and not child.is_symlink() and child.name != env_name \
                    and (child / COMPILE_COMMANDS_NAME).is_file():
                log.info("Removing the build of environment %s from %s", child.name, out_dir)
                shutil.rmtree(child)

    def output_compile_commands(self, out_dir: Path, cfg: BuildConfig) -> dict[str, Path]:
        """Return the database of the environment that the build in *out_dir* built.

        PlatformIO puts the build of an environment into ``<out>/<env>/``,
        and the script of ``build()`` puts the database there.  A variant
        names its environment (see ``_environment``).  The build without
        variants does not name it in the config, but it builds one, and
        ``build()`` removes the directories of other ones, thus one database
        is the answer.  Two databases mean an ``out/`` that a build of
        fw-context did not leave, and no answer is given: the caller then
        runs the build.
        """
        named = cfg.environment or cfg.variant_name
        if named:
            single = out_dir / named / COMPILE_COMMANDS_NAME
            return {"": single} if single.is_file() else {}
        if not out_dir.is_dir():
            return {}
        found = sorted(
            child / COMPILE_COMMANDS_NAME
            for child in out_dir.iterdir()
            if child.is_dir() and (child / COMPILE_COMMANDS_NAME).is_file()
        )
        return {"": found[0]} if len(found) == 1 else {}

    def _record_link(
        self,
        project_root: Path,
        cfg: BuildConfig,
        pio_prefix: list[str],
        env_name: str,
        build_env: dict[str, str],
        cc_path: Path,
    ) -> None:
        """Record the link inputs of the environment *env_name* for *cc_path*.

        The sidecar is in the directory of the variant, beside ``out/``:
        PlatformIO removes its build directory when ``project.checksum``
        changes, and a file inside it would go too.  Its entry holds the
        build variant and the hash of *cc_path*, which can be the staging
        file of this build.  The rename of the staging file keeps the
        content, thus the hash stays correct for the published database.

        ``pio run -t envdump`` prints the SCons environment and compiles
        nothing.  It gets the same Python, project and ``PLATFORMIO_BUILD_DIR``
        as ``compiledb``, thus ``BUILD_DIR`` in the dump is the directory of
        the object files that *cc_path* names.  ``PLATFORMIO_NO_ANSI`` keeps
        colour sequences out of the dump: ``PLATFORMIO_FORCE_ANSI`` in the
        environment of the user puts one at the start of each line.

        A failure removes the entry of this build and does not stop the
        build.  A change of the link alone keeps the hash of the database,
        thus an old entry would give the old link to the new build.
        """
        target = _platformio_link.sidecar_path(BuildLayout(project_root).variant_dir(cfg.variant_name))
        envs = self._dump_link(project_root, cfg, pio_prefix, env_name, {**build_env, "PLATFORMIO_NO_ANSI": "true"})
        for name, link in sorted((envs or {}).items()):
            if link.unknown:
                log.info("PlatformIO environment %s: the link cannot be read: %s", name, link.unknown)
        try:
            _platformio_link.record_link(target, cfg.variant_name, cc_path, envs)
        except OSError as exc:
            # The database of this build cannot be read for its hash.  The
            # build stops later on the same file, and this step must not
            # stop it first.
            log.warning("Cannot record the link inputs for %s: %s", cc_path, exc)
            _platformio_link.forget(target)
            return
        if envs is not None:
            log.info("Recorded the link inputs of PlatformIO environment %s in %s", env_name, target)

    def _dump_link(
        self,
        project_root: Path,
        cfg: BuildConfig,
        pio_prefix: list[str],
        env_name: str,
        dump_env: dict[str, str],
    ) -> dict[str, _platformio_link.EnvLink] | None:
        """Return the link inputs of *env_name* from ``envdump``, or None.

        One ``pio run -e <env> --target envdump`` dumps the one environment
        that the variant builds.  None when the dump fails or holds no dict
        of that environment: the index then gets no memory map, and the
        build goes on.
        """
        cmd = pio_prefix + [
            "run", "--project-dir", str(project_root), "--environment", env_name, "--target", "envdump",
        ]
        log.info("platformio link inputs: %s", " ".join(cmd))
        try:
            result = run_build_command(
                cmd, cwd=project_root, description=f"pio run -e {env_name} --target envdump",
                build_cfg=cfg, env=dump_env,
            )
        except RuntimeError as exc:
            # BuildTimeoutError is a RuntimeError too.
            log.warning("pio run --target envdump failed, thus the index gets no memory map: %s", _first_line(exc))
            return None
        link = _platformio_link.parse_envdump(result.stdout or "").get(env_name)
        if link is None:
            log.warning("pio run --target envdump gave no dict of %s, thus the index gets no memory map", env_name)
            return None
        return {env_name: link}

    def get_link_record(
        self,
        project_root: Path,
        *,
        compile_commands: Path | None = None,
        variant: str = "",
        units: list | None = None,
    ) -> LinkRecord | None:
        """Return the link of the environment that the index holds, or None.

        None means "not known": no build of this database and variant
        recorded its link, the units do not name exactly one environment,
        the link of that environment cannot be read (``EnvLink.unknown``),
        or a script that the link names is not on disk.  The index then
        keeps the memory map it has, see ``builders.link_record``.

        The sidecar is in the directory of the variant, beside ``out/``,
        where ``build()`` writes it.  The entry is keyed by the hash of the
        database, thus a database that the user gives explicitly matches an
        entry only when it has the content of the build that recorded it.
        """
        envs = _platformio_link.read_link(
            _platformio_link.sidecar_path(BuildLayout(project_root).variant_dir(variant)),
            variant,
            compile_commands,
        )
        if not envs:
            return None
        name = _platformio_link.select_env(envs, units, project_root)
        if name is None:
            return None
        link = envs[name]
        scripts = _platformio_link.resolve_scripts(link, project_root)
        if scripts is None:
            return None
        return LinkRecord(scripts=scripts, defsyms=_link_command.defsyms_from_flags(link.linkflags))

    def background_build_safe(self, cfg: BuildConfig) -> bool:
        """Safe — every artifact goes to the output directory of fw-context.

        ``PLATFORMIO_BUILD_DIR`` wins over ``build_dir`` in platformio.ini,
        and every pio call of this backend gets it, thus the object files
        stay out of ``.pio/build/``.  The script that
        ``PLATFORMIO_EXTRA_SCRIPTS`` adds moves ``compile_commands.json`` into
        that directory too, thus the file in the project root, which the
        clangd of the user reads, is not written either.

        ``.pio/libdeps`` is shared: ``pio run`` installs the ``lib_deps`` of
        the project there, as the build of the user does.  That is the
        source of the libraries, not build output.
        """
        return True

    # ── Build dir patterns ──

    def get_linker_scripts(
        self,
        project_root: Path,
        *,
        compile_commands: Path | None = None,
        variant: str = "",
        units: list | None = None,
    ) -> list[Path]:
        """Return the linker scripts that the link of the indexed environment reads.

        SCons writes no file that records the link command.  ``build()``
        records the ``LINKFLAGS`` and ``LIBPATH`` of each environment from
        ``pio run -t envdump`` instead, see ``_platformio_link``.  Measured
        on an STM32 and an ESP32 project: a forced relink printed a link
        line whose options are those tokens.  The STM32 project gives two
        scripts, an ``INSERT`` script and the script of the variant, and the
        ESP32 project gives nine.

        The answer is empty when ``get_link_record`` has no record: no build
        of this database and variant recorded its link, the units do not
        name exactly one environment, the link of that environment cannot
        be read, or a script of the link is not on disk.  A search of the
        framework directories would find a candidate,
        and a candidate is a guess.  The index pass asks ``get_link_record``,
        which also tells "not known" from "no script" and gives the
        ``--defsym`` values.
        """
        record = self.get_link_record(
            project_root, compile_commands=compile_commands, variant=variant, units=units,
        )
        return record.scripts if record is not None else []

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return the directory PlatformIO writes its build output into.

        ``.pio/build/``, not ``.pio/``.  These patterns are matched as a
        SUBSTRING by _is_generated_header(), so the wider form also caught
        ``.pio/libdeps/``, which holds the SOURCE of the libraries the tool
        downloads — not build output.

        That mattered once a build-generated header became the only thing the
        staleness check still trusts: every vendored library header under
        ``.pio/libdeps/`` was trusted, so an edit to one went unnoticed.
        Measured: 56 of the 56 headers the ESP32 project called generated were under
        libdeps and none were under build, and 1 of 1 on the STM32 project.  Every other
        build system was clean — Mbed 0 of 1, Zephyr 0 of 27.

        ``.pio/libdeps/`` keeps its own answer in get_vendor_patterns(): it is
        vendor code, which is a different question from build output.
        """
        return [".pio/build/"]

    def get_vendor_patterns(
        self,
        project_root: Path,
        *,
        units: list | None = None,
    ) -> list[str]:
        """Return the directories that hold the libraries PlatformIO downloads.

        ``.pio/libdeps/`` holds the ``lib_deps`` packages, and
        ``.platformio/`` is the global package and framework store.  The
        team writes neither, and PlatformIO overwrites both.

        ``.pio/build/`` is NOT in this list, so the pattern is
        ``.pio/libdeps/%`` and not ``.pio/%``.  ``.pio/build/`` is build
        output: it has get_build_dir_patterns(), and generated code counts
        as project code.

        The path of ``libdeps`` is configurable, so it is read from
        ``platformio.ini`` when the project sets ``libdeps_dir``.
        """
        libdeps = _libdeps_dir(project_root)
        return [f"{libdeps}/%", "%.platformio/%"]

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        """No extra validation beyond generic checks."""
        return []

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        """Auto-fix is not supported for PlatformIO."""
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["pio"]

    # ── Environment auto-detection ──

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        pio_python = Path.home() / ".platformio" / "penv" / "bin" / "python"
        if pio_python.exists():
            return {"python": str(pio_python), "activate": None}

        if shutil.which("pio") or shutil.which("platformio"):
            return {"python": None, "activate": None}

        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        return (
            "Install PlatformIO CLI:\n"
            "  pip install platformio\n"
            "Or set in .fw-context/local.toml:\n"
            '  [build]\n  python = "/path/to/pio/venv/bin/python"'
        )


# Register
registry.register(PlatformIOBuildSystem)
