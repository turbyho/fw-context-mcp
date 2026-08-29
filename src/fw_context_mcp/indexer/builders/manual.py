"""Manual / bare build system — generate compile_commands.json from configured flags.

No build system required.  The user specifies include directories, defines,
source directories, and compiler flags in ``.fw-context/config.toml`` under
``[build]``.  ``fw-context`` runs a syntax-only compilation of every
``.c``/``.cpp`` file found in ``source_dirs`` with ``-MD -MF`` so that
``.d`` dependency files are available for header-change detection.

Useful for simple projects with a handful of files, or when the real build
system cannot generate ``compile_commands.json`` at all.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import (
    DEPS_REL,
    TU_EXTENSIONS,
    cc_output_path,
    resolve_build_dir,
    run_build_command,
)

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)




def _scan_source_files(dirs: list[Path]) -> list[Path]:
    """Recursively collect all C/C++ source files from *dirs*."""
    seen: set[str] = set()
    result: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            log.warning("source directory does not exist: %s", d)
            continue
        for f in sorted(d.rglob("*")):
            if f.suffix in TU_EXTENSIONS and str(f) not in seen:
                # Deduplicate by full path — two source_dirs might contain same-named files
                seen.add(str(f))
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

    WHY manual mode: some projects have no standard build system (e.g., a
    handful of .c files with a handwritten Makefile that doesn't support
    ``compiledb``).  Manual mode lets these projects still benefit from
    fw-context indexing by specifying source directories, include paths,
    and defines directly in config.toml.

    Generates compile_commands.json by syntax-checking each source file
    with ``-MD -MF -fsyntax-only`` — produces .d dependency files for
    header-change tracking without compiling to object files.
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
            '  system = "bare"\n'
            '  source_dirs = ["src", "lib"]\n'
            '  include_dirs = ["include"]\n'
            "Or provide a generic compile_commands.json and run:\n"
            "  fw-context index compile_commands.json\n"
            "Or configure a custom build command:\n"
            '  [build]\n  command = "bear -- make"'
        )

    def generate(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json and .d dependency files.

        Scans ``source_dirs`` for ``.c``/``.cpp`` files, runs a syntax-only
        compilation of each source with ``-MD -MF`` to produce ``.d`` dependency
        files, then generates ``compile_commands.json`` with one entry per file.
        """
        root = project_root.resolve()
        source_dirs = [root / d for d in cfg.source_dirs]
        sources = _scan_source_files(source_dirs)

        if not sources:
            raise RuntimeError(f"No source files (.c/.cpp) found in source_dirs: {cfg.source_dirs}")

        flags = _build_flags(cfg)
        compiler_name = cfg.compiler or "gcc"
        compiler_path = shutil.which(compiler_name)
        if compiler_path is None:
            raise RuntimeError(
                f"Compiler '{compiler_name}' not found in PATH. "
                "Install gcc or set [build] compiler in .fw-context/config.toml"
            )
        compiler = str(compiler_path)

        # Compile each source with -MD -MF -fsyntax-only to generate .d files.
        # -fsyntax-only: check syntax but produce no object file (fast).
        # -MD -MF <path>.d: emit dependency file for header change detection.
        # -MP: add phony targets for each header (prevents errors if headers move).
        #
        # The .d files go to a directory that fw-context owns, never beside
        # the source.  Beside the source they sit where the build of the user
        # reads them, and the compiler does not write them atomically, thus a
        # concurrent `make` could read a truncated file.  resolve_build_dir
        # gives the isolated directory when fw-context started the build, and
        # .fw-context/build/deps otherwise.
        dep_root = resolve_build_dir(root, cfg, str(DEPS_REL))
        entries: list[dict] = []
        for src in sources:
            # Mirror the tree under dep_root: `a/foo.c` and `b/foo.c` would
            # otherwise share one `foo.d`.  A source outside the project root
            # keeps its bare name — it has no relative path to mirror.
            try:
                rel = src.resolve().relative_to(root.resolve())
            except ValueError:
                rel = Path(src.name)
            d_file = dep_root / rel.with_suffix(".d")
            d_file.parent.mkdir(parents=True, exist_ok=True)
            dep_args = ["-MD", "-MF", str(d_file), "-MP"]
            cmd = [compiler] + dep_args + flags + ["-fsyntax-only", str(src)]
            log.debug("Compile: %s", " ".join(cmd))
            try:
                run_build_command(cmd, cwd=root, description=f"Syntax check: {src.name}", build_cfg=cfg)
            except RuntimeError as exc:
                raise RuntimeError(f"Syntax check failed for {src.name}: {exc}") from exc

            entries.append(
                {
                    "directory": str(root),
                    "arguments": [compiler] + dep_args + flags + ["-c", str(src)],
                    "file": str(src),
                }
            )

        cc_path = cc_output_path(root)
        cc_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info(
            "Generated compile_commands.json + .d files (%d entries, compiler=%s)",
            len(entries),
            compiler_name,
        )
        return cc_path

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        """No extra validation beyond generic checks."""
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        """Auto-fix is not supported for manual mode."""
        return False

    def required_tools(self) -> list[str]:
        """Manual mode does not require any external tools."""
        return []

    def background_build_safe(self, cfg: BuildConfig) -> bool:
        """Safe — every artifact goes to a directory that fw-context owns.

        ``-fsyntax-only`` writes no object file, but ``-MD -MF`` still writes
        one ``.d`` file per source.  Those used to land beside the source.
        The earlier note here argued that the content depends on the source
        alone, thus a concurrent build would write the same bytes — true of
        the content, and beside the point: the compiler does not write the
        file atomically, so a `make` that reads `src/foo.d` while this build
        rewrites it can see a truncated one.

        They now go under ``cfg.isolated_build_dir`` when fw-context started
        the build, and under ``.fw-context/build/deps`` otherwise.  Either
        way nothing reaches the tree of the user, which is the first branch
        of the contract in protocol.py.
        """
        return True

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return []

    def get_vendor_patterns(
        self,
        project_root: Path,
        *,
        units: list | None = None,
    ) -> list[str]:
        """Return no pattern — manual mode already gets the paths from the config.

        The team lists its source directories and its flags in ``[build]``,
        and it adds ``[index] vendor_paths`` for the trees it does not own.
        A second, detected answer would only compete with that one.
        """
        return []

    # ── Environment auto-detection ──

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        return ""


# Register
registry.register(ManualBuildSystem)
