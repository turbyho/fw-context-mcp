"""Mbed OS build system — detection, build, and validation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import cc_output_path, run_build_command

from ..build_layout import BuildLayout
from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)

_MBED_MARKERS = [".mbed", "mbed-os", "mbed_app.json"]

# The preprocessed linker script that mbed-tools writes into its build
# directory (see MbedOSBuildSystem.get_linker_scripts).
_MBED_LINK_SCRIPT = ".link_script.ld"

# The script that runs `mbed compile` and records the command of each
# source (see its docstring).  The Python of the project runs it as a file,
# thus it must be a real file on disk: fw-context is not installed in that
# Python.  parents[2] is the package directory (builders -> indexer ->
# fw_context_mcp).
_COMPILE_DB_SCRIPT = Path(__file__).resolve().parents[2] / "data" / "mbed" / "compile_db.py"


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
    """Mbed OS 5/6 build system (``mbed compile`` of Mbed CLI 1).

    WHY the script ``data/mbed/compile_db.py``: ``mbed compile`` writes no
    compile_commands.json.  The script runs mbed-cli in the Python of the
    project and records the command of each source where the tools of
    mbed-os make it.  bear did this through LD_PRELOAD on Linux only: on
    macOS and Windows bear has only wrappers, and these do not see the
    compiler that mbed calls by its absolute path.

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
        """Run ``mbed compile`` through ``compile_db.py`` and return the database.

        The Python of ``[build] python`` runs the script, thus mbed-cli runs
        in that Python as ``python -m mbed`` does.  Without the setting the
        build stops: the script must run in the Python that has mbed-cli,
        and the ``mbed`` command on PATH does not tell which one that is.
        """
        if not cfg.python:
            raise RuntimeError(
                "The Mbed OS build needs the Python that has mbed-cli in "
                "[build] python of .fw-context/local.toml.\n" + self.environment_help()
            )

        dot = _parse_mbed_dotfile(project_root)
        target = cfg.target or dot.get("TARGET") or _mbed_target_from_custom_targets(project_root)
        toolchain = cfg.toolchain or dot.get("TOOLCHAIN") or "GCC_ARM"

        if not target:
            raise RuntimeError(
                "Cannot determine target board.  Set it in .mbed (mbed config target <BOARD>), "
                'in custom_targets.json, or in .fw-context/config.toml [build] target = "..."'
            )

        cc_path = cc_output_path(project_root, cfg)

        cmd: list[str] = [
            cfg.python,
            str(_COMPILE_DB_SCRIPT),
            "record",
            str(cc_path),
            "--",
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

        # `mbed compile --build <dir>` writes every artifact there, thus the
        # build cannot touch the object files in BUILD/ of the build that the
        # user runs in an IDE.  The database goes to the same directory.
        cmd += ["--build", str(BuildLayout(project_root).out_dir(cfg.variant_name))]

        if cfg.clean:
            cmd.append("--clean")

        log.info("mbed-os build: %s", " ".join(cmd))
        run_build_command(cmd, cwd=project_root, description="mbed compile", build_cfg=cfg)
        # generate_compile_commands() checks that the file exists, as for
        # each backend.
        return cc_path

    def background_build_safe(self, cfg: BuildConfig) -> bool:
        """Safe — ``mbed compile --build <dir>`` gets the output directory of fw-context.

        Every artifact goes under that directory, thus the build of
        fw-context cannot reach the object files in ``BUILD/<TARGET>/``.
        """
        return True

    # ── Build dir patterns ──

    def get_linker_scripts(
        self,
        project_root: Path,
        *,
        compile_commands: Path | None = None,
        variant: str = "",
        units: list | None = None,
    ) -> list[Path]:
        """Return the script that mbed-tools wrote for this build.

        mbed-tools preprocesses the target's script and writes the result as
        `.link_script.ld` in the build output directory.  Measured on the
        Mbed project: `.link_script.ld` in the directory of `--build`, which
        holds the four regions of that target and `__StackTop`.

        `build()` gives `--build` and the database the same directory, the
        output directory of the variant, thus the directory of
        *compile_commands* is the directory of the script.

        Only that name counts.  A database that the user gives can sit in
        the project root, and the only `.ld` file there is not the script of
        this build: taking it would be a guess from a pattern.
        """
        if compile_commands is None:
            return []
        script = compile_commands.parent / _MBED_LINK_SCRIPT
        return [script.resolve()] if script.is_file() else []

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["BUILD/"]

    def get_vendor_patterns(
        self,
        project_root: Path,
        *,
        units: list | None = None,
    ) -> list[str]:
        """Return the Mbed OS tree, which the project vendors in-tree.

        ``mbed-os/`` is the name the tool itself mandates: ``mbed deploy``
        writes the OS there, and this builder already reads that name as a
        detection marker (see ``_MBED_MARKERS``).

        ``BUILD/`` is NOT in this list.  It is build output, it has
        get_build_dir_patterns(), and generated code counts as project code.
        """
        return ["mbed-os/%"]

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        """Return no issue: the generic checks of validate_and_fix() apply.

        The database comes from the commands of the build itself, thus no
        check of this builder can find more than those checks.
        """
        issues: list[BuildIssue] = []
        return issues

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        """Return no tool on PATH.

        The build runs the Python of ``[build] python`` (mbed-cli is a
        package of it), and mbed finds the compiler through ``GCC_ARM_PATH``
        of ``.mbed`` or ``mbed_settings.py``.
        """
        return []

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

        # An `mbed` command on PATH (or in ~/.local/bin) does not tell
        # which Python has mbed-cli, and the build needs that Python.
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
