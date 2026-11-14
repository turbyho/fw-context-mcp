"""ESP-IDF build system — detection, build, and validation."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import build_tool_available, missing_tool_message, run_build_command

from ..build_layout import COMPILE_COMMANDS_NAME, BuildLayout
from . import _linker, registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)


def _description_field(build_dir: Path, key: str) -> str | None:
    """Return one string field of ``project_description.json`` in *build_dir*, or None.

    ESP-IDF writes the file in each build directory, the directory of the
    application and the directory of the bootloader alike.  None when the
    file is missing or the field is not a non-empty string.
    """
    try:
        description = json.loads((build_dir / "project_description.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = description.get(key) if isinstance(description, dict) else None
    return value if isinstance(value, str) and value else None


def _app_elf(build_dir: Path) -> str | None:
    """Return the ELF of the program that ``project_description.json`` names, or None."""
    return _description_field(build_dir, "app_elf")


# The bootloader of ESP-IDF is a CMake project of its own, and idf.py builds
# it in this subdirectory of the build directory of the application.
_BOOTLOADER_DIR = "bootloader"


#: The export script of ESP-IDF for the shell of the host: ``export.bat`` for
#: Command Prompt on Windows, where a ``.sh`` script cannot run.  Detection and
#: the question of ``fw-context init`` use this one name.
if sys.platform == "win32":
    EXPORT_SCRIPT = "export.bat"
else:
    EXPORT_SCRIPT = "export.sh"


class ESPIDFBuildSystem:
    """ESP-IDF build system (``idf.py build``).

    Detected by ``sdkconfig``, or by the include of ``project.cmake`` of
    ESP-IDF in ``CMakeLists.txt`` (:meth:`detect`).  Registered AFTER the
    Mbed/Zephyr/PlatformIO builders so those take precedence in ambiguous
    setups.

    WHY ``idf.py build`` (not raw CMake): ESP-IDF uses CMake internally
    but the project structure is non-standard — CMakeLists.txt in an
    IDF-managed directory hierarchy.  ``idf.py build`` invokes CMake with
    ESP-IDF-specific toolchain paths and component discovery that a raw
    ``cmake`` call would miss.  It also handles the two-phase build
    (bootloader + app) and the ``sdkconfig`` configuration system.
    """

    name: str = "ESP-IDF"
    config_key: str = "esp-idf"
    markers: list[str] = ["sdkconfig"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        """Say if *project_root* is an ESP-IDF project: ``sdkconfig``, or the ``project.cmake`` of ESP-IDF.

        ``idf.py set-target`` writes ``sdkconfig`` in the root.  A project
        that keeps only ``sdkconfig.defaults`` in git has no ``sdkconfig``
        after a clone.  The root ``CMakeLists.txt`` of each ESP-IDF project
        includes ``$ENV{IDF_PATH}/tools/cmake/project.cmake`` (the build
        system guide of ESP-IDF), thus that line also marks a project.
        A PlatformIO project with the framework ``espidf`` has the same
        line: the registry asks the PlatformIO builder first.
        """
        root = project_root.resolve()
        if (root / "sdkconfig").exists():
            return True
        try:
            content = (root / "CMakeLists.txt").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "/tools/cmake/project.cmake" in content

    # ── Build ──

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json via ``idf.py build``."""
        # The name, not a path from the PATH of this process: with activate,
        # the build finds idf.py after `source <activate>`, in the
        # environment of the project (build_tool_available).
        idf_py = "idf.py"
        if not build_tool_available(idf_py, project_root, cfg):
            raise RuntimeError(missing_tool_message(
                idf_py, cfg,
                "Install the ESP-IDF framework:\n"
                "  git clone --recursive https://github.com/espressif/esp-idf.git\n"
                "  cd esp-idf && ./install.sh && source export.sh",
            ))

        # ONE value for every use below: the directory idf.py writes into,
        # the gate that asks whether it is configured, and the read of the
        # compilation database.  Three spellings of the same thing is how
        # they came apart — `-B` moved and neither of the other two
        # followed, so a build either failed outright or read the stale
        # compile_commands.json of the build of the user.
        build_dir = BuildLayout(project_root).out_dir(cfg.variant_name)
        # -B is a global flag of idf.py, thus it comes before the command.
        build_dir_flag: list[str] = ["-B", str(build_dir)]

        cmd: list[str] = [idf_py, *build_dir_flag, "build"]

        if cfg.clean:
            # idf.py fullclean removes all build artifacts
            clean_cmd = [idf_py, *build_dir_flag, "fullclean"]
            log.info("esp-idf clean: %s", " ".join(clean_cmd))
            try:
                run_build_command(clean_cmd, cwd=project_root, description="idf.py fullclean", build_cfg=cfg)
            except RuntimeError:
                pass  # clean is best-effort — build dir may not exist yet

        # The build asks for no .d dependency files: the manifest of the
        # index records the headers of each unit, and no part of fw-context
        # reads a .d file.  An older version put a ninja wrapper with
        # `-d keepdepfile` first on PATH and added -MMD and CCACHE_DEPEND=1.
        #
        # Only the overrides go here: build_env() gives the inherited
        # environment without the shell startup hooks (BASH_ENV), and it
        # applies these values after that removal.  A full copy of
        # os.environ put BASH_ENV back into each idf.py build.
        env: dict[str, str] = {}
        if cfg.idf_path:
            env["IDF_PATH"] = cfg.idf_path

        # CMake 3.30+ misparses file(TO_CMAKE_PATH $ENV{ESP_ROM_ELF_DIR} ...)
        # when ESP_ROM_ELF_DIR is unset or empty — it treats the first argument
        # as a path instead of a subcommand keyword.  Set a non-empty default
        # so ESP-IDF's gdbinit.cmake does not crash.
        if not os.environ.get("ESP_ROM_ELF_DIR"):
            env["ESP_ROM_ELF_DIR"] = "/dev/null"

        # The ESP-IDF build appends EXTRA_CFLAGS / EXTRA_CXXFLAGS to the
        # flags of each unit.  A value from the shell of the operator does
        # not go into the build of the index, as before.
        env["EXTRA_CFLAGS"] = ""
        env["EXTRA_CXXFLAGS"] = ""

        # No `idf.py set-target`: ESP-IDF selects the target itself
        # (__target_init in tools/cmake/targets.cmake): IDF_TARGET of the
        # environment, the sdkconfig, CONFIG_IDF_TARGET of
        # sdkconfig.defaults, else esp32 (measured with ESP-IDF 6.1: a
        # project with only sdkconfig.defaults for esp32s3 got esp32s3).
        # An older version ran `set-target esp32` without an sdkconfig, and
        # that took esp32 also for a project of an other chip.
        # ensure_build_directory() of idf.py runs cmake in a new build
        # directory on its own.
        log.info("esp-idf build: %s", " ".join(cmd))
        run_build_command(cmd, cwd=project_root, description="idf.py build", env=env, build_cfg=cfg)

        # ESP-IDF puts compile_commands.json in the build directory — the
        # one `-B` named, which is why this reads build_dir and not a second
        # spelling of it.  The message names the directory it looked in;
        # saying "build/" while looking elsewhere is what hid this.  The
        # database stays there, beside build.ninja and
        # project_description.json, which the linker pass reads.
        cc_in_build = build_dir / "compile_commands.json"
        if not cc_in_build.exists():
            raise RuntimeError(
                f"compile_commands.json not found in {build_dir}. "
                "Ensure the ESP-IDF project was configured correctly."
            )
        return cc_in_build

    def background_build_safe(self, cfg: BuildConfig) -> bool:
        """Safe — ``idf.py -B <dir>`` puts every artifact in the output directory of fw-context."""
        return True

    def output_compile_commands(self, out_dir: Path, cfg: BuildConfig) -> dict[str, Path]:
        """Return the database of each image that the build in *out_dir* made.

        ``idf.py build`` makes two programs, the application and the
        second-stage bootloader.  Each has a build directory of its own, with
        its compile_commands.json, build.ninja and project_description.json:
        the application in *out_dir*, the bootloader in ``out_dir/bootloader``.

        The image name is the ``project_name`` of the description beside each
        database: the project name for the application, ``bootloader`` for
        the bootloader.  WHY that name and not a fixed one: it is the name
        that the build system gives the program, and the name of its ELF.
        A database without a description with a name gets no entry, because
        the build that wrote it is not complete.  The two names cannot be
        equal: ESP-IDF has a build target ``bootloader``, and a project of
        that name does not build.
        """
        found: dict[str, Path] = {}
        for build_dir in (out_dir, out_dir / _BOOTLOADER_DIR):
            cc = build_dir / COMPILE_COMMANDS_NAME
            name = _description_field(build_dir, "project_name")
            if name is not None and cc.is_file():
                found[name] = cc
        return found

    def application_database(self, out_dir: Path) -> Path:
        """Return the database of the application, the program that a query without ``image`` is about.

        ``idf.py build`` writes it in *out_dir*, and the database of the
        bootloader in ``out_dir/bootloader``.
        """
        return out_dir / COMPILE_COMMANDS_NAME

    # ── Build dir patterns ──

    def get_linker_scripts(
        self,
        project_root: Path,
        *,
        compile_commands: Path | None = None,
        variant: str = "",
        units: list | None = None,
    ) -> list[Path]:
        """Return the scripts of the link of the application, see ``get_link_record``.

        ESP-IDF does not use one script.  It passes about ten — the ROM
        symbol files of the chip, `memory.ld`, and `sections.ld` — so the
        caller reads them all and each one adds what it holds.
        """
        record = self.get_link_record(
            project_root, compile_commands=compile_commands, variant=variant, units=units,
        )
        return record.scripts if record is not None else []

    def get_link_record(
        self,
        project_root: Path,
        *,
        compile_commands: Path | None = None,
        variant: str = "",
        units: list | None = None,
    ) -> _linker.LinkRecord | None:
        """Return the link of the application from the build directory, or None.

        The build directory holds ``compile_commands.json``, ``build.ninja``
        and ``project_description.json``.  The last one names the ELF of the
        application (``app_elf``), and ``_linker.ninja_link`` reads the link
        edge of that ELF.  Measured on the ESP-IDF fixture (v5.2.5): the
        edge passes nine ``-T`` names with no directory, which ld finds in
        the ``-L`` directories of ``LINK_PATH``.  ``from_ninja`` does not
        know ``-L``, and found none of them.

        Without ``project_description.json`` the only executable edge of
        the file is the link.  None ("not known") when the directory of
        *compile_commands* holds no ``build.ninja``, or when the link cannot
        be read.  ``build()`` leaves the database in the build directory,
        thus that directory is the build directory.
        """
        if compile_commands is None:
            return None
        build_dir = compile_commands.parent
        if not (build_dir / "build.ninja").is_file():
            log.info("linker script: no build.ninja beside %s", compile_commands)
            return None
        return _linker.ninja_link(build_dir, _app_elf(build_dir))

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["build/"]

    def get_vendor_patterns(
        self,
        project_root: Path,
        *,
        units: list | None = None,
    ) -> list[str]:
        """Return the directory the IDF Component Manager writes into.

        ``managed_components/`` holds the components that ``idf.py`` pulls
        from the ESP Component Registry.  The team does not write them and
        the manager overwrites them.

        ``components/`` is NOT in this list.  It is the standard place for
        the application's OWN components, so a pattern for it would hide the
        team's code from a project_only query.
        """
        return ["managed_components/%"]

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        """Warn about a database whose directory is no ESP-IDF build directory.

        ``build()`` leaves the database in the build directory that `-B`
        names, beside ``build.ninja``.  A database without ``build.ninja``
        beside it is a file that the user gives (a copy for clangd, a file
        of a CI machine).  The index of its units is correct, but fw-context
        does not know where its build is, thus the link of the application
        and the memory map are not known.  That is a warning and not an
        error: a search for the build from the paths of the units would be a
        guess.
        """
        issues: list[BuildIssue] = []
        built = (compile_commands.parent / "build.ninja").is_file()
        if not built:
            issues.append(
                BuildIssue(
                    severity="warning",
                    category="missing_build_dir",
                    message=(
                        f"no ESP-IDF build directory beside {compile_commands}: the linker "
                        "scripts and the memory map stay unknown"
                    ),
                    auto_fixable=False,
                    fix_hint="Run 'fw-context index --build' to index the build of fw-context.",
                )
            )
        return issues

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["idf.py"]

    # ── Environment auto-detection ──

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        """Return the script that activates the ESP-IDF of this user.

        The sequence:

        1. The ESP-IDF of the shell (``IDF_PATH``): the EIM script of that
           ESP-IDF, else its ``export.sh``.  WHY first: the user selected
           it, and a build in the background must use the same ESP-IDF
           (measured: with IDF_PATH on v6.1, init took an EIM script of
           v5.2.5).
        2. The newest EIM script, by its version as numbers (v5.10 is
           newer than v5.2).
        3. ``export.sh`` in ``~/esp/esp-idf`` or ``~/esp-idf``.

        WHY the EIM script before ``export.sh`` of the same ESP-IDF: an EIM
        (Espressif Installation Manager) script sets IDF_PATH, IDF_TOOLS_PATH
        and the PATH of the toolchain without the full install check of
        ``idf_tools.py``.  That check fails on an install for some targets
        only (``eim install -t esp32`` without the RISC-V toolchain), and
        ``export.sh`` runs it.

        Windows: export.bat of ESP-IDF, for Command Prompt.  A .sh script
        cannot run there, and the form of the EIM scripts on Windows is not
        known here, thus they are left out.
        """
        windows = sys.platform == "win32"
        eim_scripts = [] if windows else _eim_scripts()

        idf_path = os.environ.get("IDF_PATH")
        if idf_path:
            for script, script_idf_path in eim_scripts:
                if script_idf_path is not None and _same_path(script_idf_path, idf_path):
                    return {"python": None, "activate": str(script)}
            export_script = Path(idf_path) / EXPORT_SCRIPT
            if export_script.exists():
                return {"python": None, "activate": str(export_script)}

        if eim_scripts:
            return {"python": None, "activate": str(eim_scripts[0][0])}

        for candidate in [
            Path.home() / "esp" / "esp-idf" / EXPORT_SCRIPT,
            Path.home() / "esp-idf" / EXPORT_SCRIPT,
        ]:
            if candidate.exists():
                return {"python": None, "activate": str(candidate)}

        if shutil.which("idf.py"):
            return {"python": None, "activate": None}

        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        return (
            "ESP-IDF builds require an activated environment.\n"
            "Source the export script:\n"
            "  source ~/esp/esp-idf/export.sh\n"
            "Or set in .fw-context/local.toml:\n"
            '  [build]\n  activate = "~/esp/esp-idf/export.sh"'
        )


_EIM_VERSION = re.compile(r"^activate_idf_v(?P<version>\d+(?:\.\d+)*)")
_EIM_IDF_PATH = re.compile(r"^IDF_PATH:(?P<path>.+)$", re.MULTILINE)


def _eim_scripts() -> list[tuple[Path, str | None]]:
    """Return the EIM scripts of this user, the newest version first, each with the IDF_PATH that it sets.

    The EIM script lists the variables that it sets as ``NAME:value`` lines
    (``get_env_var_pairs``; measured on EIM scripts of v5.2.5).  A script
    without that line has no IDF_PATH here.
    """
    scripts: list[tuple[tuple[int, ...], Path, str | None]] = []
    for script in (Path.home() / ".espressif" / "tools").glob("activate_idf_*.sh"):
        found = _EIM_VERSION.match(script.name)
        version = tuple(int(part) for part in found.group("version").split(".")) if found else ()
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        pair = _EIM_IDF_PATH.search(text)
        scripts.append((version, script, pair.group("path").strip() if pair else None))
    scripts.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return [(script, idf_path) for _, script, idf_path in scripts]


def _same_path(first: str, second: str) -> bool:
    """Say if two paths name the same directory (links resolved, the case of the file system)."""
    try:
        return os.path.samefile(first, second)
    except OSError:
        return os.path.normcase(os.path.normpath(first)) == os.path.normcase(os.path.normpath(second))


# Register
registry.register(ESPIDFBuildSystem)
