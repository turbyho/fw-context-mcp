"""PlatformIO build system — detection, build, validation, and auto-fix."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import run_build_command

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)

_PIO_MARKERS = ["platformio.ini"]


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

        if cfg.clean:
            clean_cmd = pio_prefix + ["run", "--project-dir", str(project_root), "--target", "clean"]
            log.info("platformio clean: %s", " ".join(clean_cmd))
            try:
                run_build_command(clean_cmd, cwd=project_root, description="pio run --target clean", build_cfg=cfg)
            except RuntimeError:
                pass  # clean is best-effort — build dir may not exist yet

        log.info("platformio build: %s", " ".join(cmd))
        run_build_command(cmd, cwd=project_root, description="pio run --target compiledb", build_cfg=cfg)

        cc_path = project_root / "compile_commands.json"
        if not cc_path.exists():
            raise RuntimeError("compile_commands.json was not generated — pio run may have failed silently")

        # ── Full build for .d file generation ──
        # compiledb only writes compile_commands.json — GCC may not have
        # run.  A full pio run compiles and emits .d files that the indexer
        # uses for header-change staleness detection.  When the build is
        # already up-to-date, pio run exits quickly (no-op).
        build_cmd = pio_prefix + ["run", "--project-dir", str(project_root)]
        log.info("platformio compile: %s", " ".join(build_cmd))
        try:
            run_build_command(build_cmd, cwd=project_root, description="pio run (full build for .d files)", build_cfg=cfg)
        except RuntimeError:
            log.warning(
                "Full build failed — .d files may be missing. "
                "Header change detection will be limited. "
                "Fix compilation errors and re-run 'fw-context index --build'."
            )

        return cc_path

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return [".pio/"]

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
