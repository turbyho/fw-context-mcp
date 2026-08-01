"""Zephyr build system — detection, build, and validation."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import run_build_command

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)

_ZEPHYR_MARKERS = ["west.yml", "zephyr"]


class ZephyrBuildSystem:
    """Zephyr RTOS build system (``west build``)."""

    name: str = "Zephyr"
    config_key: str = "zephyr"
    markers: list[str] = ["west.yml", "zephyr"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return any((root / m).exists() for m in _ZEPHYR_MARKERS)

    # ── Build ──

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json via ``west build``."""
        if not shutil.which("west"):
            raise RuntimeError("west is required for Zephyr builds.  Install the Zephyr SDK and west tool.")

        if not cfg.board:
            raise RuntimeError(
                'Zephyr requires a board name.  Set it in .fw-context/config.toml:\n  [build]\n  board = "your_board"'
            )

        build_dir = project_root / "build"

        # Ninja deletes .d depfiles after reading them by default.
        # Create a wrapper that adds -d keepdepfile so .d files persist
        # on disk for incremental re-indexing.
        # We place the wrapper at $PATH priority (not CMAKE_MAKE_PROGRAM)
        # because sysbuild's ExternalProject_Add does not forward
        # CMAKE_MAKE_PROGRAM to the inner Zephyr CMake project.
        ninja = shutil.which("ninja")
        if ninja is None:
            raise RuntimeError("ninja is required for Zephyr builds")
        wrapper_dir = project_root / ".fw-context"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        ninja_wrapper = wrapper_dir / "ninja"
        expected = f'#!/bin/sh\nexec "{ninja}" -d keepdepfile "$@"\n'
        # Atomic write via temp file + rename — prevents TOCTOU between
        # check and write where another process could replace the wrapper.
        if not ninja_wrapper.exists() or ninja_wrapper.read_text(encoding="utf-8") != expected:
            tmp_wrapper = wrapper_dir / ".ninja.tmp"
            tmp_wrapper.write_text(expected, encoding="utf-8")
            tmp_wrapper.chmod(0o755)
            tmp_wrapper.rename(ninja_wrapper)

        cmd: list[str] = [
            "west",
            "build",
            "-b",
            cfg.board,
            "-d",
            str(build_dir),
        ]

        if cfg.clean:
            cmd.append("--pristine")

        cmd.append("--")
        cmd.append("-DCMAKE_EXPORT_COMPILE_COMMANDS=ON")
        # Inject .d dependency tracking via Zephyr's built-in EXTRA_CPPFLAGS
        # mechanism (cmake/extra_flags.cmake).  This propagates -MMD through
        # zephyr_interface to all Zephyr targets and survives sysbuild.
        cmd.append("-DEXTRA_CPPFLAGS=-MMD")

        log.info("zephyr build: %s", " ".join(cmd))
        # ccache with depend_mode=false (default) skips .d file regeneration
        # on cache hits.  CCACHE_DEPEND=1 forces ccache to cache dependency
        # info so .d files are present after every build.
        # Prepend the .fw-context wrapper directory to PATH so our ninja
        # wrapper (which adds -d keepdepfile) shadows the real ninja binary
        # in both sysbuild (outer) and Zephyr (inner) CMake projects.
        env = {
            **os.environ,
            "CCACHE_DEPEND": "1",
            "PATH": f"{wrapper_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        # NOTE: no timeout= — build commands can run for minutes; adding a fixed
        # timeout would break long builds.  Network-filesystem stalls remain a risk.
        run_build_command(cmd, cwd=project_root, description="west build", env=env)

        cc_in_build = build_dir / "compile_commands.json"
        if not cc_in_build.exists():
            # Sysbuild (NCS ≥2.0) puts cc.json one level deeper:
            #   build/zephyr/compile_commands.json
            cc_sysbuild = build_dir / "zephyr" / "compile_commands.json"
            if cc_sysbuild.exists():
                cc_in_build = cc_sysbuild
            else:
                raise RuntimeError(
                    "compile_commands.json not found in build directory. "
                    "Ensure CMAKE_EXPORT_COMPILE_COMMANDS is enabled."
                )

        # Copy to project root for consistency
        target_cc = project_root / "compile_commands.json"
        shutil.copy2(cc_in_build, target_cc)
        log.info("Copied %s → %s", cc_in_build, target_cc)

        return target_cc

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["build/"]

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        issues: list[BuildIssue] = []
        # Zephyr via CMake: CMAKE_EXPORT_COMPILE_COMMANDS is enabled so
        # compile_commands.json is complete.  No extra builder-specific checks.
        return issues

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["west"]


# Register
registry.register(ZephyrBuildSystem)
