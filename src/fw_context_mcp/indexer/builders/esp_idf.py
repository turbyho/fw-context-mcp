"""ESP-IDF build system — detection, build, and validation."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)


class ESPIDFBuildSystem:
    """ESP-IDF build system (``idf.py build``).

    Detected by the presence of ``sdkconfig`` alongside a CMakeLists.txt
    that references ``idf_build``.  Registered AFTER the Mbed/Zephyr/PlatformIO
    builders so those take precedence in ambiguous setups.
    """

    name: str = "ESP-IDF"
    config_key: str = "esp-idf"
    markers: list[str] = ["sdkconfig"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        # Primary marker: sdkconfig in root (created by idf.py set-target)
        if not (root / "sdkconfig").exists():
            return False
        # Secondary: CMakeLists.txt references idf_build (distinguishes from generic CMake)
        cmake_file = root / "CMakeLists.txt"
        if cmake_file.exists():
            try:
                content = cmake_file.read_text(encoding="utf-8")
                if "idf_build" in content or "IDF" in content:
                    return True
            except OSError:
                pass
        # Fallback: sdkconfig alone is suggestive enough
        return True

    # ── Build ──

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json via ``idf.py build``."""
        idf_py = shutil.which("idf.py")
        if not idf_py:
            raise RuntimeError(
                "idf.py is required for ESP-IDF builds.  Install the ESP-IDF framework:\n"
                "  git clone --recursive https://github.com/espressif/esp-idf.git\n"
                "  cd esp-idf && ./install.sh && source export.sh"
            )

        cmd: list[str] = [idf_py, "build"]

        if cfg.clean:
            # idf.py fullclean removes all build artifacts
            clean_cmd = [idf_py, "fullclean"]
            log.info("esp-idf clean: %s", " ".join(clean_cmd))
            subprocess.run(clean_cmd, cwd=project_root)

        env = None
        if cfg.idf_path:
            env = {"IDF_PATH": cfg.idf_path}

        log.info("esp-idf build: %s", " ".join(cmd))
        result = subprocess.run(cmd, cwd=project_root, env=env)

        if result.returncode != 0:
            raise RuntimeError(f"idf.py build failed with exit code {result.returncode}")

        # ESP-IDF puts compile_commands.json in the build directory
        cc_in_build = project_root / "build" / "compile_commands.json"
        if not cc_in_build.exists():
            raise RuntimeError(
                "compile_commands.json not found in build/ directory. "
                "Ensure the ESP-IDF project was configured correctly."
            )

        # Copy to project root for consistency
        target_cc = project_root / "compile_commands.json"
        shutil.copy2(cc_in_build, target_cc)
        log.info("Copied %s → %s", cc_in_build, target_cc)

        return target_cc

    # ── Dep tracking ──

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        # CMake + GCC always emits .d files with ESP-IDF.
        return []

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        issues: list[BuildIssue] = []
        build_dir = project_root / "build"
        if not build_dir.exists():
            issues.append(BuildIssue(
                severity="error",
                category="missing_build_dir",
                message="ESP-IDF build/ directory not found",
                auto_fixable=False,
                fix_hint="Run 'fw-context index --build' to compile the project.",
            ))
        return issues

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["idf.py"]


# Register
registry.register(ESPIDFBuildSystem)
