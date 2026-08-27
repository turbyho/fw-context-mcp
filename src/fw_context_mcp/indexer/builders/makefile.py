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

from fw_context_mcp.utils import cc_output_path, run_build_command

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)


class MakefileBuildSystem:
    """Makefile project — compile_commands.json via ``compiledb make``.

    Auto-detected when a ``Makefile`` exists in the project root.

    WHY compiledb (not bear): bear intercepts compiler calls via
    LD_PRELOAD, which requires the compiler to actually run.  compiledb
    parses the Makefile to understand the build graph and can produce
    compile_commands.json without compiling — faster and works even for
    projects that don't build in the current environment.
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

        if cfg.python:
            compiledb_prefix: list[str] = [cfg.python, "-m", "compiledb"]
        elif not shutil.which("compiledb"):
            raise RuntimeError(
                "compiledb is required for Makefile projects.\n"
                "Install it:  pip install compiledb\n"
                "Or use bear instead with a custom command:\n"
                '  [build]\n  command = "bear -- make"'
            )
        else:
            compiledb_prefix = ["compiledb"]

        target = cfg.make_target or "all"

        cc_path = cc_output_path(root)

        cmd: list[str] = compiledb_prefix

        if cfg.make_dry_run:
            cmd.append("-n")

        cmd += [
            "-o",
            str(cc_path),
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
        run_build_command(cmd, cwd=root, description="compiledb make", build_cfg=cfg)

        if not cc_path.exists():
            raise RuntimeError("compile_commands.json was not generated — compiledb may have failed silently")

        return cc_path

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return ["compiledb", "make"]

    # ── Environment auto-detection ──

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        return (
            "Install compiledb:\n"
            "  pip install compiledb"
        )

    def background_build_safe(self, cfg: BuildConfig) -> bool:
        """Safe only in dry-run mode, which is the default.

        ``compiledb -n make`` reads what make would do and compiles nothing,
        thus no artifact exists to collide with.  With ``make_dry_run`` off
        the backend runs a real build, and the Makefile owns the output
        directory: fw-context cannot move it, thus it must not start that
        build on its own.
        """
        return bool(cfg.make_dry_run)

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["build/"]

    def get_vendor_patterns(
        self,
        project_root: Path,
        *,
        units: list | None = None,
    ) -> list[str]:
        """Return no pattern — a Makefile project has no canonical vendor tree.

        Where the third-party code sits is written in the Makefile itself,
        with a different variable in every project.  A fixed pattern would
        be a guess, and a wrong guess hides code the team owns.
        """
        return []


# Register
registry.register(MakefileBuildSystem)
