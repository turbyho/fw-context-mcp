"""Mbed OS build system — detection, build, and validation."""

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

_MBED_MARKERS = [".mbed", "mbed-os", "mbed_app.json"]


def _parse_mbed_dotfile(project_root: Path) -> dict[str, str]:
    """Parse ``.mbed`` into a dict of KEY=VALUE pairs."""
    dotfile = project_root / ".mbed"
    result: dict[str, str] = {}
    if not dotfile.exists():
        return result
    for line in dotfile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _mbed_target_from_custom_targets(project_root: Path) -> str | None:
    """Extract the first board name from custom_targets.json."""
    import json

    ct = project_root / "custom_targets.json"
    if not ct.exists():
        return None
    try:
        data = json.loads(ct.read_text(encoding="utf-8"))
        for key in data:
            if isinstance(data[key], dict) and "inherits" in data[key]:
                return key
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _resolve_mbed_extra_profiles(project_root: Path, extra: list[str]) -> list[str]:
    """Resolve extra profile paths."""
    resolved: list[str] = []
    for p in extra:
        if "/" in p or p.startswith("."):
            resolved.append(p)
        else:
            candidate = f"mbed-os/tools/profiles/extensions/{p}"
            if (project_root / candidate).exists():
                resolved.append(candidate)
            else:
                resolved.append(p)
    return resolved


class MbedOSBuildSystem:
    """Mbed OS 5/6 build system (``mbed compile`` via ``bear``).

    WHY bear: Mbed OS uses its own build tool (``mbed compile``) which
    internally calls the ARM GCC toolchain but does not produce
    compile_commands.json.  ``bear`` intercepts the compiler invocations
    via LD_PRELOAD and captures them into compile_commands.json.

    WHY target resolution from multiple sources: the target board can be
    specified in .mbed (mbed config), custom_targets.json, or
    .fw-context/config.toml.  Each source has a different precedence
    because users configure their board in different ways depending on
    their workflow.
    """

    name: str = "Mbed OS"
    config_key: str = "mbed-os"
    markers: list[str] = [".mbed", "mbed-os", "mbed_app.json"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return any((root / m).exists() for m in _MBED_MARKERS)

    # ── Build ──

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json via ``bear -- mbed compile --clean``."""
        if not shutil.which("bear"):
            raise RuntimeError(
                "bear is required to generate compile_commands.json. "
                "Install it:  sudo pacman -S bear   (or your distro's equivalent)"
            )

        dot = _parse_mbed_dotfile(project_root)
        target = cfg.target or dot.get("TARGET") or _mbed_target_from_custom_targets(project_root)
        toolchain = cfg.toolchain or dot.get("TOOLCHAIN") or "GCC_ARM"

        if not target:
            raise RuntimeError(
                "Cannot determine target board.  Set it in .mbed (mbed config target <BOARD>), "
                'in custom_targets.json, or in .fw-context/config.toml [build] target = "..."'
            )

        if cfg.python:
            mbed_prefix = [cfg.python, "-m", "mbed"]
        else:
            mbed_prefix = ["mbed"]

        cmd: list[str] = [
            "bear",
            "--output",
            "compile_commands.json",
            "--",
        ] + mbed_prefix + [
            "compile",
            "-t",
            toolchain,
            "-m",
            target,
            "--profile",
            cfg.profile,
            "--app-config",
            cfg.app_config,
        ]

        for ep in _resolve_mbed_extra_profiles(project_root, cfg.extra_profiles):
            cmd += ["--profile", ep]

        for d in cfg.defines:
            cmd += ["-D", d]

        if cfg.clean:
            cmd.append("--clean")

        log.info("mbed-os build: %s", " ".join(cmd))
        run_build_command(cmd, cwd=project_root, description="mbed compile", build_cfg=cfg)

        cc_path = project_root / "compile_commands.json"
        if not cc_path.exists():
            raise RuntimeError("compile_commands.json was not generated — bear may have failed silently")

        return cc_path

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["BUILD/"]

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        issues: list[BuildIssue] = []
        # Mbed OS via bear: .d files are generated alongside .o files in the
        # build directory.  If bear ran successfully, .d files should exist.
        # No extra validation beyond the generic checks in validate_and_fix().
        return issues

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["bear", "mbed"]

    # ── Environment auto-detection ──

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        pyenv_versions = Path.home() / ".pyenv" / "versions"
        if pyenv_versions.is_dir():
            for vdir in sorted(pyenv_versions.iterdir(), reverse=True):
                if (vdir / "bin" / "mbed").exists():
                    return {"python": str(vdir / "bin" / "python"), "activate": None}

        for venv_dir in [
            Path.home() / "mbed_venv",
            Path.home() / ".virtualenvs" / "mbed",
        ]:
            python_bin = venv_dir / "bin" / "python"
            if python_bin.exists():
                return {"python": str(python_bin), "activate": None}

        if (Path.home() / ".local" / "bin" / "mbed").exists():
            return {"python": None, "activate": None}

        if shutil.which("mbed"):
            return {"python": None, "activate": None}

        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        return (
            "mbed-cli requires Python 3.11 or older.\n"
            "Install into a venv or pyenv:\n"
            "  python3.11 -m venv ~/mbed_venv\n"
            "  ~/mbed_venv/bin/pip install mbed-cli\n"
            "Or set in .fw-context/local.toml:\n"
            '  [build]\n  python = "/path/to/python3.11/bin/python"'
        )


# Register
registry.register(MbedOSBuildSystem)
