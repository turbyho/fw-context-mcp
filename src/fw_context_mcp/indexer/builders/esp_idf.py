"""ESP-IDF build system — detection, build, and validation."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import resolve_real_binary, run_build_command

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


class ESPIDFBuildSystem:
    """ESP-IDF build system (``idf.py build``).

    Detected by the presence of ``sdkconfig`` alongside a CMakeLists.txt
    that references ``idf_build``.  Registered AFTER the Mbed/Zephyr/PlatformIO
    builders so those take precedence in ambiguous setups.

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

        # Ninja deletes .d depfiles after reading them by default.
        # Create a wrapper that adds -d keepdepfile so .d files persist
        # for incremental re-indexing.
        #
        # Resolve the REAL ninja binary, not a pyenv/asdf shim: the shim
        # re-execs `ninja` by name, and since the wrapper dir is prepended
        # to PATH below, that re-resolution would find the wrapper again
        # and recurse (appending -d keepdepfile each cycle).
        ninja = resolve_real_binary("ninja")
        if ninja is None:
            raise RuntimeError("ninja is required for ESP-IDF builds")
        wrapper_dir = project_root / ".fw-context"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        ninja_wrapper = wrapper_dir / "ninja"
        expected = f'#!/bin/sh\nexec "{ninja}" -d keepdepfile "$@"\n'
        if not ninja_wrapper.exists() or ninja_wrapper.read_text(encoding="utf-8") != expected:
            tmp_wrapper = wrapper_dir / ".ninja.tmp"
            tmp_wrapper.write_text(expected, encoding="utf-8")
            tmp_wrapper.chmod(0o755)
            tmp_wrapper.rename(ninja_wrapper)

        env = dict(os.environ)
        if cfg.idf_path:
            env["IDF_PATH"] = cfg.idf_path

        # CMake 3.30+ misparses file(TO_CMAKE_PATH $ENV{ESP_ROM_ELF_DIR} ...)
        # when ESP_ROM_ELF_DIR is unset or empty — it treats the first argument
        # as a path instead of a subcommand keyword.  Set a non-empty default
        # so ESP-IDF's gdbinit.cmake does not crash.
        if not os.environ.get("ESP_ROM_ELF_DIR"):
            env["ESP_ROM_ELF_DIR"] = "/dev/null"

        # Prepend our ninja wrapper directory to PATH so Ninja keeps .d files.
        # Also enable ccache depend_mode so .d files are regenerated on cache hits.
        env["PATH"] = f"{wrapper_dir}{os.pathsep}{env['PATH']}"
        env["CCACHE_DEPEND"] = "1"

        # Inject .d dependency tracking via EXTRA_CFLAGS / EXTRA_CXXFLAGS.
        # These environment variables are respected by the ESP-IDF build system
        # and appended to compiler flags without overriding the toolchain.
        env.pop("EXTRA_CFLAGS", None)  # Don't inherit from parent env
        env["EXTRA_CFLAGS"] = ""
        env.pop("EXTRA_CXXFLAGS", None)  # Don't inherit from parent env
        env["EXTRA_CXXFLAGS"] = ""
        if "-MMD" not in env["EXTRA_CFLAGS"]:
            env["EXTRA_CFLAGS"] += " -MMD" if env["EXTRA_CFLAGS"] else "-MMD"
        if "-MMD" not in env["EXTRA_CXXFLAGS"]:
            env["EXTRA_CXXFLAGS"] += " -MMD" if env["EXTRA_CXXFLAGS"] else "-MMD"

        # Configure the build directory when it holds no cmake cache.
        #
        # The gate asks about the directory THIS build writes to, not about
        # project_root/"build": with `-B` those are different, and asking
        # about the wrong one left the isolated directory unconfigured while
        # the build of the user made it look ready.
        #
        # `set-target` runs only when there is no sdkconfig, and that is a
        # correctness rule, not a saving.  It renames <project>/sdkconfig to
        # sdkconfig.old — `-B` does not move that, because
        # tools/cmake/project.cmake takes ${CMAKE_SOURCE_DIR}/sdkconfig and
        # renames it whenever _IDF_PY_SET_TARGET_ACTION is set — and that
        # file is usually committed.  An automatic build would have deleted
        # it, and the target it passed comes from the guess below, read out
        # of that same file with esp32 as the fallback, so it could also
        # regenerate the configuration for a different chip.
        #
        # Nothing is lost by skipping it: ensure_build_directory() in
        # idf_py_actions/tools.py creates the directory and runs cmake on
        # its own, and project.cmake takes the target from the sdkconfig
        # that is already there.  set-target is needed only when that file
        # does not exist — and then it renames nothing.
        sdkconfig = project_root / "sdkconfig"
        if not build_dir.exists() and not sdkconfig.exists():
            target = "esp32"
            log.warning(
                "No sdkconfig in %s — defaulting the ESP-IDF target to esp32. "
                "Run 'idf.py set-target <chip>' yourself if this project uses "
                "a different variant.", project_root,
            )
            set_target_cmd = [idf_py, *build_dir_flag, "set-target", target]
            log.info("esp-idf set-target: %s", " ".join(set_target_cmd))
            try:
                run_build_command(set_target_cmd, cwd=project_root, description="idf.py set-target", build_cfg=cfg)
            except RuntimeError:
                log.warning("idf.py set-target failed — build may use wrong target")

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
        # EIM (Espressif Installation Manager) activation scripts take
        # priority.  They set IDF_PATH / IDF_TOOLS_PATH / the toolchain
        # PATH directly without running idf_tools.py's full install check,
        # which fails on target-only installs (e.g. ``eim install -t esp32``
        # without the RISC-V toolchain).  ``export.sh`` by contrast runs
        # that check and aborts the build when any declared tool is absent.
        tools_dir = Path.home() / ".espressif" / "tools"
        eim_scripts = sorted(tools_dir.glob("activate_idf_*.sh"), reverse=True)
        if eim_scripts:
            return {"python": None, "activate": str(eim_scripts[0])}

        idf_path = os.environ.get("IDF_PATH")
        if idf_path:
            export_sh = Path(idf_path) / "export.sh"
            if export_sh.exists():
                return {"python": None, "activate": str(export_sh)}

        for candidate in [
            Path.home() / "esp" / "esp-idf" / "export.sh",
            Path.home() / "esp-idf" / "export.sh",
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


# Register
registry.register(ESPIDFBuildSystem)
