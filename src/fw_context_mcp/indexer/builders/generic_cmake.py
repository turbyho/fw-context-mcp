"""Generic CMake build system — detection, build, and validation.

Used for any CMake-based project that isn't Zephyr or ESP-IDF.
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


class GenericCMakeBuildSystem:
    """Generic CMake build system (``cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON``).

    Registered AFTER Zephyr and ESP-IDF so those specific builders match first.

    WHY registered last among CMake-based builders: Zephyr and ESP-IDF both
    use CMake but have specific project detection markers and build commands.
    The generic CMake builder acts as a fallback for any project with a
    CMakeLists.txt that doesn't match a more specific builder.
    """

    name: str = "CMake (generic)"
    config_key: str = "cmake"
    markers: list[str] = ["CMakeLists.txt"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        cmake_file = project_root.resolve() / "CMakeLists.txt"
        if not cmake_file.exists():
            return False
        # Exclude ESP-IDF and Zephyr — those are detected by their own builders
        try:
            content = cmake_file.read_text(encoding="utf-8")
            if "idf_build" in content or "IDF" in content:
                # Check for sdkconfig — if present, it's ESP-IDF territory
                if (project_root / "sdkconfig").exists():
                    return False
            if "find_package(Zephyr" in content or "zephyr" in content.lower():
                return False
        except OSError:
            pass
        return True

    # ── Build ──

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json via CMake configure + build."""
        if not shutil.which("cmake"):
            raise RuntimeError("cmake is required.  Install it:  sudo pacman -S cmake")

        build_dir = project_root / "build"

        # Configure
        configure_cmd: list[str] = [
            "cmake",
            "-B",
            str(build_dir),
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ]
        if cfg.cmake_generator:
            configure_cmd += ["-G", cfg.cmake_generator]

        if cfg.clean and build_dir.exists():
            shutil.rmtree(build_dir)

        log.info("cmake configure: %s", " ".join(configure_cmd))
        run_build_command(configure_cmd, cwd=project_root, description="cmake configure", build_cfg=cfg)

        # Build
        build_cmd: list[str] = ["cmake", "--build", str(build_dir)]
        log.info("cmake build: %s", " ".join(build_cmd))
        run_build_command(build_cmd, cwd=project_root, description="cmake build", build_cfg=cfg)

        cc_in_build = build_dir / "compile_commands.json"
        if not cc_in_build.exists():
            raise RuntimeError(
                "compile_commands.json not found in build/ directory. Ensure CMAKE_EXPORT_COMPILE_COMMANDS is enabled."
            )

        # Copy to the gitignored fw-context build dir for a stable location
        target_cc = cc_output_path(project_root)
        shutil.copy2(cc_in_build, target_cc)
        log.info("Copied %s → %s", cc_in_build, target_cc)

        return target_cc

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["build/", "cmake-build-"]

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["cmake"]

    # ── Environment auto-detection ──

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        return ""


# Register
registry.register(GenericCMakeBuildSystem)
