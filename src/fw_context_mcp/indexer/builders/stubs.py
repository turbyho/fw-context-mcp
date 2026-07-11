"""Tier 3/4 stub builders — detection without automated build.

These builders recognise proprietary/legacy build systems and provide
helpful instructions for generating ``compile_commands.json`` manually.
They cannot build automatically — the user must provide a
``compile_commands.json`` or a custom ``[build] command`` in config.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)


class KeilMDKStub:
    """Keil MDK (``.uvprojx``).

    Cannot build automatically.  Use ``keil2clangd`` or ``uvconvertor``
    to convert the Keil project to ``compile_commands.json``.
    """

    name: str = "Keil MDK"
    config_key: str = "keil-mdk"
    markers: list[str] = [".uvprojx"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return bool(list(root.glob("*.uvprojx")))

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        raise RuntimeError(
            "Keil MDK detected, but automatic build is not supported.\n"
            "Generate compile_commands.json with keil2clangd or uvconvertor:\n"
            "  https://github.com/dcristoloveanu/keil2clangd\n"
            "Then run 'fw-context index' with the generated file:\n"
            "  fw-context index compile_commands.json"
        )

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        return []

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return []


class IAREWARMStub:
    """IAR EWARM (``.ewp``, ``.eww``).

    Cannot build automatically.  Community workarounds exist — IAR's
    ``iarbuild.exe`` can be scripted with ``bear`` or ``compiledb``.
    """

    name: str = "IAR EWARM"
    config_key: str = "iar-ewarm"
    markers: list[str] = [".ewp", ".eww"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return (
            bool(list(root.glob("*.ewp")))
            or bool(list(root.glob("*.eww")))
        )

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        raise RuntimeError(
            "IAR EWARM detected, but automatic build is not supported.\n"
            "Use bear or compiledb with IAR's command-line builder:\n"
            "  bear -- iarbuild.exe your.ewp -build Debug\n"
            "Or provide a custom build command in .fw-context/config.toml:\n"
            "  [build]\n  command = \"bear -- iarbuild.exe your.ewp -build Debug\""
        )

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        return []

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return []


class STM32CubeIDEStub:
    """STM32CubeIDE (``.cproject``, ``.project``).

    Eclipse-based IDE with a built-in JSON Compilation Database generator.
    Go to Project Properties → C/C++ Build → Builder Settings →
    Generate JSON Compilation Database.
    """

    name: str = "STM32CubeIDE"
    config_key: str = "stm32cubeide"
    markers: list[str] = [".cproject", ".project"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return (root / ".cproject").exists() or (root / ".project").exists()

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        raise RuntimeError(
            "STM32CubeIDE detected, but automatic build is not supported.\n"
            "Enable JSON Compilation Database in CubeIDE:\n"
            "  Project Properties → C/C++ Build → Builder Settings →\n"
            "  ✓ Generate JSON Compilation Database\n"
            "Then run 'fw-context index' with the generated file."
        )

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        return []

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return []


class TICCSStub:
    """TI Code Composer Studio (``.projectspec``).

    Eclipse-based IDE. Similar to STM32CubeIDE — enable the built-in
    JSON Compilation Database generator in Project Settings.
    """

    name: str = "TI Code Composer Studio"
    config_key: str = "ti-ccs"
    markers: list[str] = [".projectspec"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return (root / ".projectspec").exists()

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        raise RuntimeError(
            "TI Code Composer Studio detected, but automatic build is not supported.\n"
            "Enable JSON Compilation Database in CCS Project Settings, or\n"
            "use bear to wrap a headless build:\n"
            "  bear -- eclipse -nosplash -application com.ti.ccstudio.apps.projectBuild ..."
        )

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        return []

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return []


class MakefileStub:
    """Bare Makefile project.

    Cannot build implicitly — use ``bear`` to generate
    ``compile_commands.json`` from a Make-based build.
    """

    name: str = "Makefile (bare)"
    config_key: str = "makefile"
    markers: list[str] = ["Makefile"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return (root / "Makefile").exists()

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        raise RuntimeError(
            "Makefile detected, but automatic build is not supported.\n"
            "Use bear or compiledb to generate compile_commands.json:\n"
            "  bear -- make\n"
            "Or provide a custom build command:\n"
            "  [build]\n  command = \"bear -- make\""
        )

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        return []

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return ["make"]


class BareGCCStub:
    """Bare GCC / no build system.

    No project markers detected.  The user must provide
    ``compile_commands.json`` manually.  This is always the last
    builder checked and matches ANY directory — it acts as a
    fallback that tells the user what to do.
    """

    name: str = "No build system"
    config_key: str = "bare"
    markers: list[str] = []  # No markers — this is a conscious fallback

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        # Always returns False — we never auto-detect "bare".
        # The user must explicitly set [build] system = "bare" or provide
        # a compile_commands.json manually.
        return False

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        raise RuntimeError(
            "No compile_commands.json found and no build system detected.\n"
            "Generate compile_commands.json manually (e.g. with bear), then run:\n"
            "  fw-context index compile_commands.json\n"
            "Or configure a custom build command:\n"
            "  [build]\n  command = \"bear -- make\""
        )

    def ensure_dep_tracking(self, project_root: Path, *, fix: bool = False) -> list[str]:
        return []

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return []


# Register stub builders
registry.register(KeilMDKStub)
registry.register(IAREWARMStub)
registry.register(STM32CubeIDEStub)
registry.register(TICCSStub)
registry.register(MakefileStub)
