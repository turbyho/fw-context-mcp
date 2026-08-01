"""Makefile build system — generate compile_commands.json via compiledb.

Wraps ``compiledb make`` to capture compile commands from a Makefile-based
build without needing ``bear`` or ``LD_PRELOAD`` interception.  Supports
dry-run mode (``make -n``) for projects where a real build is undesirable
or slow.
"""

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


class MakefileBuildSystem:
    """Makefile project — compile_commands.json via ``compiledb make``.

    Auto-detected when a ``Makefile`` exists in the project root.
    Uses the ``compiledb`` Python package, which is a pure-Python
    tool that parses make output — no need for ``bear``.
    """

    name: str = "Makefile"
    config_key: str = "makefile"
    markers: list[str] = ["Makefile"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return (root / "Makefile").exists()

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Build is delegated to generate() — compiledb IS the build step."""
        return self.generate(project_root, cfg)

    def generate(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json via ``compiledb make``.

        Runs ``compiledb make [target] [vars...]`` in *project_root*.
        When ``make_dry_run`` is True (the default), ``-n`` is passed
        to make so no actual compilation occurs — only the command
        database is generated.
        """
        root = project_root.resolve()

        if not shutil.which("compiledb"):
            raise RuntimeError(
                "compiledb is required for Makefile projects.\n"
                "Install it:  pip install compiledb\n"
                "Or use bear instead with a custom command:\n"
                '  [build]\n  command = "bear -- make"'
            )

        target = cfg.make_target or "all"

        cmd: list[str] = ["compiledb"]

        if cfg.make_dry_run:
            cmd.append("-n")

        cmd += [
            "-o",
            str(root / "compile_commands.json"),
            "-f",  # overwrite
            "make",
            "-C",
            str(root),
        ]

        if cfg.makefile:
            cmd += ["-f", cfg.makefile]

        # Pass extra vars like V=1 CROSS_COMPILE=arm-none-eabi-
        for k, v in cfg.make_vars.items():
            cmd.append(f"{k}={v}")

        cmd.append(target)

        log.info("makefile build: %s", " ".join(cmd))
        run_build_command(cmd, cwd=root, description="compiledb make")

        cc_path = root / "compile_commands.json"
        if not cc_path.exists():
            raise RuntimeError("compile_commands.json was not generated — compiledb may have failed silently")

        return cc_path

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return ["compiledb", "make"]

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["build/"]


# Register
registry.register(MakefileBuildSystem)
