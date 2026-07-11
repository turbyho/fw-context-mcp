"""Manual / bare build system — generate compile_commands.json from configured flags.

No build system required.  The user specifies include directories, defines,
source directories, and compiler flags in ``.fw-context/config.toml`` under
``[build]``.  ``fw-context`` generates a ``compile_commands.json`` entry for
every ``.c``/``.cpp`` file found in ``source_dirs``, each with the same flags.

Useful for simple projects with a handful of files, or when the real build
system cannot generate ``compile_commands.json`` at all.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)

_SOURCE_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx", ".C", ".c++"}


def _scan_source_files(dirs: list[Path]) -> list[Path]:
    """Recursively collect all C/C++ source files from *dirs*."""
    seen: set[str] = set()
    result: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            log.warning("source directory does not exist: %s", d)
            continue
        for f in sorted(d.rglob("*")):
            if f.suffix in _SOURCE_EXTENSIONS and f.name not in seen:
                # Deduplicate by filename — two source_dirs might overlap
                seen.add(f.name)
                result.append(f)
    return result


def _build_flags(cfg: BuildConfig) -> list[str]:
    """Assemble compiler flags from BuildConfig fields."""
    flags: list[str] = []

    for d in cfg.include_dirs:
        flags.append("-I")
        flags.append(d)

    for d in cfg.system_include_dirs:
        flags.append("-isystem")
        flags.append(d)

    for d in cfg.defines:
        flags.append("-D")
        flags.append(d)

    flags.extend(cfg.extra_flags)

    return flags


class ManualBuildSystem:
    """Manual build system — compile_commands.json from TOML-configured flags.

    Never auto-detected — the user must explicitly set
    ``[build] system = "bare"`` in their config.
    """

    name: str = "Manual (bare)"
    config_key: str = "bare"
    markers: list[str] = []  # Never auto-detected — explicit user opt-in

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        """Always returns False — bare mode is never auto-detected."""
        return False

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Build is not supported for manual mode.

        Raises RuntimeError with instructions — use ``generate()`` instead,
        which is selected automatically when ``source_dirs`` is configured.
        """
        if cfg.source_dirs:
            return self.generate(project_root, cfg)

        raise RuntimeError(
            "Manual (bare) mode requires source_dirs in .fw-context/config.toml.\n"
            "Configure at minimum:\n"
            "  [build]\n"
            "  system = \"bare\"\n"
            "  source_dirs = [\"src\", \"lib\"]\n"
            "  include_dirs = [\"include\"]\n"
            "Or provide a generic compile_commands.json and run:\n"
            "  fw-context index compile_commands.json\n"
            "Or configure a custom build command:\n"
            "  [build]\n  command = \"bear -- make\""
        )

    def generate(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json from configured flags.

        Scans ``source_dirs`` for ``.c``/``.cpp`` files and creates one
        entry per file with the flags from ``include_dirs``, ``defines``,
        ``extra_flags``, and ``compiler``.
        """
        root = project_root.resolve()
        source_dirs = [root / d for d in cfg.source_dirs]
        sources = _scan_source_files(source_dirs)

        if not sources:
            raise RuntimeError(
                f"No source files (.c/.cpp) found in source_dirs: {cfg.source_dirs}"
            )

        flags = _build_flags(cfg)
        compiler = cfg.compiler or "gcc"

        entries: list[dict] = []
        for src in sources:
            entries.append({
                "directory": str(root),
                "command": " ".join([compiler] + flags + ["-c", str(src)]),
                "file": str(src),
            })

        cc_path = root / "compile_commands.json"
        cc_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info(
            "Generated compile_commands.json (%d entries, compiler=%s)",
            len(entries), compiler,
        )
        return cc_path

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        """Manual mode does not support automatic dependency tracking."""
        return []

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        """No extra validation beyond generic checks."""
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        """Auto-fix is not supported for manual mode."""
        return False

    def required_tools(self) -> list[str]:
        """Manual mode does not require any external tools."""
        return []


# Register
registry.register(ManualBuildSystem)
