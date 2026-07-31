"""PlatformIO build system — detection, build, validation, and auto-fix."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from fw_context_mcp.utils import run_build_command
from typing import TYPE_CHECKING

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)

_PIO_MARKERS = ["platformio.ini"]


class PlatformIOBuildSystem:
    """PlatformIO build system (``pio run --target compiledb``)."""

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
        if not shutil.which("pio") and not shutil.which("platformio"):
            raise RuntimeError("PlatformIO CLI is required.  Install it:  pip install platformio")

        pio_bin = "pio" if shutil.which("pio") else "platformio"

        cmd: list[str] = [pio_bin, "run", "--project-dir", str(project_root), "--target", "compiledb"]

        if cfg.clean:
            clean_cmd = [pio_bin, "run", "--project-dir", str(project_root), "--target", "clean"]
            log.info("platformio clean: %s", " ".join(clean_cmd))
            subprocess.run(clean_cmd, cwd=project_root, capture_output=True, text=True)

        log.info("platformio build: %s", " ".join(cmd))
        run_build_command(cmd, cwd=project_root, description="pio run --target compiledb")

        cc_path = project_root / "compile_commands.json"
        if not cc_path.exists():
            raise RuntimeError("compile_commands.json was not generated — pio run may have failed silently")

        # ── Full build for .d file generation ──
        # compiledb only writes compile_commands.json — GCC may not have
        # run.  A full pio run compiles and emits .d files that the indexer
        # uses for header-change staleness detection.  When the build is
        # already up-to-date, pio run exits quickly (no-op).
        build_cmd = [pio_bin, "run", "--project-dir", str(project_root)]
        log.info("platformio compile: %s", " ".join(build_cmd))
        build_result = subprocess.run(build_cmd, cwd=project_root, capture_output=True, text=True)
        if build_result.returncode != 0:
            log.warning(
                "Full build failed (exit code %d) — .d files may be missing. "
                "Header change detection will be limited. "
                "Fix compilation errors and re-run 'fw-context index --build'.",
                build_result.returncode,
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


# Register
registry.register(PlatformIOBuildSystem)
