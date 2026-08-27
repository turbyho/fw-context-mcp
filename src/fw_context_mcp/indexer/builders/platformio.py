"""PlatformIO build system — detection, build, validation, and auto-fix."""

from __future__ import annotations

import configparser
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import cc_output_path, run_build_command

from . import registry
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

        cc_path = cc_output_path(project_root)
        shutil.copy2(native_cc, cc_path)
        log.info("Copied %s → %s", native_cc, cc_path)

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

    def background_build_safe(self, cfg: BuildConfig) -> bool:
        """Safe — ``PLATFORMIO_BUILD_DIR`` overrides ``build_dir``.

        The variable wins over platformio.ini, and every pio call of this
        backend gets it, thus the artifacts stay out of ``.pio/build/``.
        """
        return True

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return the directory PlatformIO writes its build output into.

        ``.pio/build/``, not ``.pio/``.  These patterns are matched as a
        SUBSTRING by _is_generated_header(), so the wider form also caught
        ``.pio/libdeps/``, which holds the SOURCE of the libraries the tool
        downloads — not build output.

        That mattered once a build-generated header became the only thing the
        staleness check still trusts: every vendored library header under
        ``.pio/libdeps/`` was trusted, so an edit to one went unnoticed.
        Measured: 56 of the 56 headers HA_Boiler called generated were under
        libdeps and none were under build, and 1 of 1 on FM.  Every other
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
