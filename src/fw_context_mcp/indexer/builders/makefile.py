"""Makefile build system — generate compile_commands.json via compiledb.

Wraps ``compiledb make`` to capture compile commands from a Makefile-based
build without needing ``bear`` or ``LD_PRELOAD`` interception.  Supports
dry-run mode (``make -n``) for projects where a real build is undesirable
or slow.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import cc_output_path, run_build_command

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)


def _resolve_compiledb(configured_python: str | None) -> list[str]:
    """Return the command prefix that runs compiledb, or raise.

    Three routes are tried in order of how much the answer is trusted:

    1. **The interpreter the user configured.**  An explicit choice wins.
    2. **The interpreter running fw-context**, when ``compiledb`` can be
       imported by it.  This is the route that matters in practice:
       fw-context is installed into its own virtualenv and reached through
       a symlink in ``~/.local/bin``, so that virtualenv's ``bin/`` is NOT
       on PATH.  ``compiledb`` sits right next to the running interpreter
       and ``-m`` finds it there whatever PATH says.
    3. **A ``compiledb`` on PATH**, for a system-wide install outside any
       virtualenv fw-context knows about.

    Route 2 exists because route 3 alone told a lie.  Measured: with
    ``~/.fw-context/.venv/bin/compiledb`` present and ``python -m
    compiledb`` working, ``shutil.which("compiledb")`` still found nothing,
    and a Makefile project failed with "Install it: pip install compiledb"
    — advice that could not have helped, because it was already installed.

    Raises:
        RuntimeError: No route found.  The message names what was tried,
            so the reader is not sent to reinstall something present.
    """
    if configured_python:
        return [configured_python, "-m", "compiledb"]

    if sys.executable and _module_is_importable("compiledb"):
        return [sys.executable, "-m", "compiledb"]

    on_path = shutil.which("compiledb")
    if on_path:
        return [on_path]

    raise RuntimeError(
        "compiledb is required for Makefile projects, and none of these "
        "found it:\n"
        "  [build] python  — not set in the project config\n"
        f"  {sys.executable or 'the running interpreter'} -m compiledb "
        "— not importable\n"
        "  compiledb on PATH — not found\n"
        "Install it into the environment that runs fw-context:\n"
        f"  {sys.executable or 'python'} -m pip install compiledb\n"
        "Or use bear instead with a custom command:\n"
        '  [build]\n  command = "bear -- make"'
    )


def _module_is_importable(name: str) -> bool:
    """Report whether *name* can be imported, without importing it.

    ``find_spec`` is used rather than a real import because compiledb is a
    build tool that this process only ever runs as a subprocess; importing
    it here would run its module-level code for no reason.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        # ValueError: `name` present in sys.modules but with no spec.
        return False


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

        compiledb_prefix = _resolve_compiledb(cfg.python)

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

    def get_linker_scripts(
        self,
        project_root: Path,
        *,
        compile_commands: Path | None = None,
        variant: str = "",
        units: list | None = None,
    ) -> list[Path]:
        """Return nothing: a makefile can name the script anywhere.

        The `-T` flag can be in any variable of any included makefile, and
        reading it means running `make` and reading the link line.  The
        backend compiles with `compiledb` or a dry run, which never links,
        so no link command exists to read.
        """
        return []

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
