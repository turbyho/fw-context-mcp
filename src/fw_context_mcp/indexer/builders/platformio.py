"""PlatformIO build system — detection, build, validation, and auto-fix."""

from __future__ import annotations

import configparser
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import CC_OUTPUT_REL, BuildTimeoutError, cc_output_path, run_build_command

from . import _link_command, _platformio_link, registry
from ._linker import LinkRecord
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)

_PIO_MARKERS = ["platformio.ini"]


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

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json via ``pio run --target compiledb``
        and then run a full build to generate ``.d`` dependency files.

        The ``compiledb`` target captures compile commands from SCons
        internals but may not invoke GCC — ``.d`` files are only emitted
        during actual compilation.  A subsequent ``pio run`` ensures they
        exist for header-change detection.
        """
        if cfg.python:
            pio_prefix = [cfg.python, "-m", "platformio"]
        elif shutil.which("pio"):
            pio_prefix = ["pio"]
        elif shutil.which("platformio"):
            pio_prefix = ["platformio"]
        else:
            raise RuntimeError("PlatformIO CLI is required.  Install it:  pip install platformio")

        cmd: list[str] = pio_prefix + ["run", "--project-dir", str(project_root), "--target", "compiledb"]

        # PlatformIO has no CLI flag for the build directory; the documented
        # override is the environment variable, which beats build_dir in
        # platformio.ini.  run_build_command merges this into the child
        # environment, thus every pio call below writes to it.
        build_env: dict[str, str] | None = None
        if cfg.isolated_build_dir:
            build_env = {"PLATFORMIO_BUILD_DIR": cfg.isolated_build_dir}

        if cfg.clean:
            clean_cmd = pio_prefix + ["run", "--project-dir", str(project_root), "--target", "clean"]
            log.info("platformio clean: %s", " ".join(clean_cmd))
            try:
                run_build_command(clean_cmd, cwd=project_root, description="pio run --target clean", build_cfg=cfg, env=build_env)
            except RuntimeError:
                pass  # clean is best-effort — build dir may not exist yet

        log.info("platformio build: %s", " ".join(cmd))
        run_build_command(cmd, cwd=project_root, description="pio run --target compiledb", build_cfg=cfg, env=build_env)

        # PlatformIO writes compile_commands.json natively to the project
        # root; copy it to the gitignored fw-context build dir for a stable
        # location that survives a clean of the project root.
        native_cc = project_root / "compile_commands.json"
        if not native_cc.exists():
            raise RuntimeError("compile_commands.json was not generated — pio run may have failed silently")

        cc_path = cc_output_path(project_root, cfg)
        shutil.copy2(native_cc, cc_path)
        log.info("Copied %s → %s", native_cc, cc_path)

        self._record_link(project_root, cfg, pio_prefix, build_env, cc_path)

        # ── Full build for .d file generation ──
        # compiledb only writes compile_commands.json — GCC may not have
        # run.  A full pio run compiles and emits .d files that the indexer
        # uses for header-change staleness detection.  When the build is
        # already up-to-date, pio run exits quickly (no-op).
        build_cmd = pio_prefix + ["run", "--project-dir", str(project_root)]
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

    def _record_link(
        self,
        project_root: Path,
        cfg: BuildConfig,
        pio_prefix: list[str],
        build_env: dict[str, str] | None,
        cc_path: Path,
    ) -> None:
        """Record the link inputs of each environment for *cc_path*.

        The sidecar is in the fw-context build directory.  Its entry holds
        the build variant and the hash of *cc_path*, which can be the staging
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
        target = _platformio_link.sidecar_path((project_root / CC_OUTPUT_REL).parent)
        envs = self._dump_links(project_root, cfg, pio_prefix, {**(build_env or {}), "PLATFORMIO_NO_ANSI": "true"})
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
            log.info("Recorded the link inputs of %d PlatformIO environment(s) in %s", len(envs), target)

    def _dump_links(
        self,
        project_root: Path,
        cfg: BuildConfig,
        pio_prefix: list[str],
        dump_env: dict[str, str],
    ) -> dict[str, _platformio_link.EnvLink] | None:
        """Return the link inputs of each environment from ``envdump``, or None.

        One ``pio run --target envdump`` dumps every environment.  When it
        fails, the dump of each environment is asked for again with ``-e``,
        but only for a project with more than one environment, and not after
        a timeout, which could repeat once per environment.  WHY: the
        exit code of the first run is the exit code of the worst
        environment, and its output can then hold a dict that an
        ``extra_script`` printed before the failure.  A separate run gives
        each environment its own exit code.  An environment that fails
        stays in the result as unknown, see ``EnvLink.unknown``.
        """
        cmd = pio_prefix + ["run", "--project-dir", str(project_root), "--target", "envdump"]
        log.info("platformio link inputs: %s", " ".join(cmd))
        try:
            result = run_build_command(
                cmd, cwd=project_root, description="pio run --target envdump", build_cfg=cfg, env=dump_env,
            )
        except BuildTimeoutError as exc:
            # A dump that hung would hang again for each environment.
            log.warning("pio run --target envdump failed, thus the index gets no memory map: %s", _first_line(exc))
            return None
        except RuntimeError as exc:
            log.warning("pio run --target envdump failed: %s", _first_line(exc))
            return self._dump_each_link(project_root, cfg, pio_prefix, dump_env)
        envs = _platformio_link.parse_envdump(result.stdout or "")
        if not envs:
            log.warning("pio run --target envdump gave no environment, thus the index gets no memory map")
            return None
        return envs

    def _dump_each_link(
        self,
        project_root: Path,
        cfg: BuildConfig,
        pio_prefix: list[str],
        dump_env: dict[str, str],
    ) -> dict[str, _platformio_link.EnvLink] | None:
        """Return the link inputs from one ``envdump`` run per environment, or None.

        None when the project has fewer than two environments, or when
        ``pio project config`` cannot name them.  With one environment, the
        run that failed was the run of that environment.
        """
        config_cmd = pio_prefix + ["project", "config", "--project-dir", str(project_root), "--json-output"]
        try:
            config = run_build_command(
                config_cmd, cwd=project_root, description="pio project config", build_cfg=cfg, env=dump_env,
            )
        except RuntimeError as exc:
            log.warning("pio project config failed, thus the index gets no memory map: %s", _first_line(exc))
            return None
        names = _platformio_link.run_environments(config.stdout or "")
        if not names or len(names) < 2:
            log.warning("pio run --target envdump failed, thus the index gets no memory map")
            return None
        envs: dict[str, _platformio_link.EnvLink] = {}
        for position, name in enumerate(names):
            cmd = pio_prefix + [
                "run", "--project-dir", str(project_root), "--environment", name, "--target", "envdump",
            ]
            try:
                result = run_build_command(
                    cmd, cwd=project_root, description=f"pio run -e {name} --target envdump",
                    build_cfg=cfg, env=dump_env,
                )
            except BuildTimeoutError as exc:
                # The next environments could hang the same way, each for
                # the whole timeout.  They stay in the result as unknown.
                log.warning("pio run -e %s --target envdump failed, thus no more are run: %s", name, _first_line(exc))
                for rest in names[position:]:
                    envs[rest] = _platformio_link.EnvLink(unknown="pio run --target envdump timed out")
                break
            except RuntimeError as exc:
                log.warning("pio run -e %s --target envdump failed: %s", name, _first_line(exc))
                envs[name] = _platformio_link.EnvLink(unknown="pio run --target envdump failed for this environment")
                continue
            envs[name] = _platformio_link.parse_envdump(result.stdout or "").get(
                name, _platformio_link.EnvLink(unknown="the dump holds no dict of this environment"),
            )
        return envs

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

        The sidecar is in the fw-context build directory, where ``build()``
        writes it, and not next to *compile_commands*.  A database that the
        user gives explicitly, such as the ``compile_commands.json`` that
        PlatformIO writes in the project root, has the same content and thus
        the same hash, but another directory.
        """
        envs = _platformio_link.read_link(
            _platformio_link.sidecar_path((project_root / CC_OUTPUT_REL).parent),
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
        """Safe — ``PLATFORMIO_BUILD_DIR`` keeps the object files apart.

        The variable wins over ``build_dir`` in platformio.ini, and every
        pio call of this backend gets it, thus the artifacts stay out of
        ``.pio/build/``.  That is what the contract asks for.

        ``pio run -t compiledb`` ALSO rewrites
        ``<project>/compile_commands.json``, and that cannot be redirected
        from outside.  Three ways were checked and none exists:
        ``COMPILATIONDB_PATH`` is a SCons construction variable that
        ``clivars`` does not declare (builder/main.py:39-47,78), PlatformIO
        has no project option for it, and the SCons tool takes the path from
        the argument main.py passes.  Only an ``extra_scripts`` pre-script
        could move it, which means editing the platformio.ini of the user.

        That write is deliberately NOT repaired, and this paragraph is here
        so nobody repairs it.  Measured over 209 entries, the file differs
        from the one the build of the user produces in exactly one token per
        entry — the ``.o`` output path — with no difference in -I, -D, -std,
        -isystem or directory.  clangd does not read -o; config_hash and
        flags_hash normalise it away (config_hash.py:_normalize_entry), so
        alternating between an automatic and an explicit build neither
        splits the index nor reparses one translation unit; and
        ``fw-context init`` gitignores the file.

        Both repairs that suggest themselves are worse than the write.  A
        save/restore can leave the file missing or truncated, and on a real
        project it is the ONLY compile_commands.json the user has — removing
        it takes away what their clangd reads.  A generated second
        platformio.ini works, but it has to reproduce the whole resolved
        configuration, and getting that wrong feeds fw-context the wrong
        flags in silence.  See plans/review_8d98343_fixes.md, finding 9.
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
