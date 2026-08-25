"""Zephyr build system — detection, build, and validation."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import cc_output_path, resolve_real_binary, run_build_command

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)

_ZEPHYR_MARKERS = ["west.yml", "zephyr"]

# The flag Zephyr CMake uses to name the two SDK roots.  See
# _prefix_map_roots() for what each name means.
_PREFIX_MAP_FLAG = "-fmacro-prefix-map="


def _zephyr_vendor_patterns(
    project_root: Path,
    zephyr_base: Path | None,
    west_topdir: Path | None,
) -> list[str]:
    """Return the in-tree SDK patterns for this Zephyr build.

    Derived from ZEPHYR_BASE and WEST_TOPDIR, never guessed from a marker
    file.  The application directory gives no signal: it is arbitrary, and
    for a sysbuild image such as mcuboot it sits INSIDE the SDK.  Measured on
    zbox-ecb-fw-v5: four different CMAKE_SOURCE_DIR values across 9 builds,
    one of them under the SDK root.

    A root outside project_root needs no pattern.  A path outside
    project_root already gets is_project=0, so a pattern would add nothing
    and would make the staleness check trust a tree two times.

    ``modules/`` goes in ONLY as the real WEST_TOPDIR sibling.  A T2 manifest
    repository has west.yml and its own ``modules/`` for the TEAM's Zephyr
    modules, and a fixed ``modules/%`` pattern hides those from a
    project_only query.
    """
    patterns: list[str] = []
    for root in (zephyr_base, west_topdir):
        if root is None:
            continue
        try:
            rel = root.relative_to(project_root)
        except ValueError:
            continue                      # outside the project — no pattern
        if rel == Path("."):
            continue                      # project root IS the workspace
        pattern = f"{rel}/%"
        if pattern not in patterns:
            patterns.append(pattern)
    return patterns


def _prefix_map_roots(units: list | None) -> tuple[Path | None, Path | None]:
    """Return ``(ZEPHYR_BASE, WEST_TOPDIR)`` read from ``-fmacro-prefix-map``.

    Zephyr's CMake emits one prefix map per name for every translation unit,
    and it names the two roots directly::

        -fmacro-prefix-map=/home/u/ncs/v3.4.0/zephyr=ZEPHYR_BASE
        -fmacro-prefix-map=/home/u/ncs/v3.4.0=WEST_TOPDIR
        -fmacro-prefix-map=<app dir>=CMAKE_SOURCE_DIR

    Verified in all 9 production artifacts of zbox-ecb-fw-v5 with no
    exception.  This is the strongest source there is: it describes the
    build that is indexed, not the shell that runs fw-context and not the
    state of the filesystem.

    ``=CMAKE_SOURCE_DIR`` is read and DROPPED.  The application directory is
    arbitrary, and for a sysbuild image such as mcuboot it sits INSIDE the
    SDK — measured as four different values across those 9 builds.

    Returns ``(None, None)`` when *units* is None or carries no prefix map.
    """
    zephyr_base: Path | None = None
    west_topdir: Path | None = None
    for unit in units or ():
        for arg in getattr(unit, "clang_args", ()) or ():
            if not arg.startswith(_PREFIX_MAP_FLAG):
                continue
            mapping = arg[len(_PREFIX_MAP_FLAG):]
            path, sep, name = mapping.rpartition("=")
            if not sep or not path:
                continue
            if name == "ZEPHYR_BASE" and zephyr_base is None:
                zephyr_base = Path(path)
            elif name == "WEST_TOPDIR" and west_topdir is None:
                west_topdir = Path(path)
        if zephyr_base is not None and west_topdir is not None:
            break
    return zephyr_base, west_topdir


@lru_cache(maxsize=32)
def _west_paths(project_root: str) -> tuple[str | None, str | None]:
    """Return ``(zephyr.base, topdir)`` that ``west`` reports for this project.

    Cached for the life of the process.  ``west`` starts a Python
    interpreter, and get_vendor_patterns() also runs on the MCP query path,
    where that cost is paid for every request.  The two values name the
    workspace the project sits in, and that does not change while
    fw-context runs.

    Returns ``(None, None)`` when ``west`` is not on PATH or when it fails.
    The caller then uses a different source.
    """
    if not shutil.which("west"):
        return None, None

    def ask(*args: str) -> str | None:
        try:
            r = subprocess.run(
                ["west", *args],
                capture_output=True, text=True, timeout=5, cwd=project_root,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        return r.stdout.strip() or None

    return ask("config", "zephyr.base"), ask("topdir")


# The two toolchain families a Zephyr project can build with.  They are not
# interchangeable and are discovered differently, so the kind travels with
# every option instead of being inferred later.
#
#   NCS         — Nordic's nRF Connect SDK.  Versions live under the
#                 installation directory nrfutil manages, and the environment
#                 comes from `nrfutil sdk-manager toolchain env`, which
#                 bundles its own Zephyr SDK inside the toolchain.
#   ZEPHYR_SDK  — the upstream Zephyr SDK.  A zephyr-sdk-<version> directory
#                 that ships its own environment-setup-* script.
SDK_KIND_NCS = "ncs"
SDK_KIND_ZEPHYR = "zephyr-sdk"


@dataclass(frozen=True)
class SdkChoice:
    """One toolchain a Zephyr project could build with.

    ``usable`` is False when the pieces needed to compile are not all there —
    an NCS version whose toolchain is missing, or a Zephyr SDK with no
    environment script.  Offering those would only defer the failure to the
    build.
    """

    kind: str
    version: str
    path: Path
    usable: bool
    # NCS only: the toolchain bundle nrfutil installed for this version.
    toolchain_path: Path | None = None
    # Zephyr SDK only: the environment-setup-* script to source.
    env_script: Path | None = None

    @property
    def label(self) -> str:
        return "nRF Connect SDK" if self.kind == SDK_KIND_NCS else "Zephyr SDK"

    def describe(self) -> str:
        """One line for the init picker."""
        state = "" if self.usable else "  (incomplete)"
        return f"{self.label} {self.version}  {self.path}{state}"


class ZephyrBuildSystem:
    """Zephyr RTOS build system (``west build``).

    WHY ninja wrapper for .d files: Ninja deletes .d dependency files after
    reading them by default (``-d keepdepfile`` is needed to persist them).
    Zephyr's sysbuild uses Ninja as both outer and inner build tool.  The
    wrapper shadows the real ninja binary in PATH with ``-d keepdepfile``
    appended, ensuring .d files survive the build for header-change tracking.

    WHY PATH injection (not CMAKE_MAKE_PROGRAM): sysbuild's
    ExternalProject_Add does not forward CMAKE_MAKE_PROGRAM to the inner
    Zephyr CMake project.  Injecting the wrapper via PATH works for both
    the outer and inner Ninja invocations.

    WHY CCACHE_DEPEND=1: ccache with ``depend_mode=false`` (default) skips
    .d file regeneration on cache hits, meaning reindex can't detect which
    headers a TU depends on.  Setting ``CCACHE_DEPEND=1`` forces ccache to
    write .d files on every build, cache hit or not.
    """

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

        build_dir = project_root / (cfg.build_dir or "build")

        _, env = self._prepare_ninja_wrapper(project_root)

        cmd: list[str] = [
            "west",
            "build",
            "-b",
            self._normalize_board(cfg.board),
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
        # NOTE: no timeout= — build commands can run for minutes; adding a fixed
        # timeout would break long builds.  Network-filesystem stalls remain a risk.
        run_build_command(cmd, cwd=project_root, description="west build", env=env, build_cfg=cfg)

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

        # Copy to the gitignored fw-context build dir for a stable location
        target_cc = cc_output_path(project_root)
        shutil.copy2(cc_in_build, target_cc)
        log.info("Copied %s → %s", cc_in_build, target_cc)

        return target_cc

    # ── Multi-variant (sysbuild) ──

    @staticmethod
    def _normalize_board(board: str) -> str:
        """Normalize a board qualifier to the canonical ``board/soc[/cpu]`` form.

        Zephyr/NCS uses ``board/soc[/cpu]`` (``/``-separated); a legacy
        ``board_soc`` underscore form may appear when a user hand-writes it in
        config.  Board identifiers never contain underscores, so ``_``→``/`` is
        a safe defensive normalization (canonical 2-segment ``nrf52840dk/nrf52840``,
        3-segment ``nrf54lm20dk/nrf54lm20a/cpuapp``).
        """
        return board.replace("_", "/")

    def _prepare_ninja_wrapper(self, project_root: Path) -> tuple[str, dict[str, str]]:
        """Create the ninja ``-d keepdepfile`` wrapper and return ``(dir, env)``.

        WHY a wrapper: Ninja deletes ``.d`` depfiles after reading them by
        default (``-d keepdepfile`` is needed to persist them).  The wrapper is
        placed at PATH priority (not CMAKE_MAKE_PROGRAM) because sysbuild's
        ExternalProject_Add does not forward CMAKE_MAKE_PROGRAM to the inner
        Zephyr CMake project.

        WHY resolve the REAL ninja: a pyenv/asdf shim re-execs ``ninja`` by
        name; with the wrapper dir prepended to PATH that re-resolution finds
        the wrapper again and recurses.

        The returned env sets ``CCACHE_DEPEND=1`` (ccache with depend_mode=false
        skips ``.d`` regeneration on cache hits) and prepends the wrapper dir to
        PATH for both sysbuild (outer) and Zephyr (inner) CMake projects.
        """
        ninja = resolve_real_binary("ninja")
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

        env = {
            **os.environ,
            "CCACHE_DEPEND": "1",
            "PATH": f"{wrapper_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        return str(wrapper_dir), env

    @staticmethod
    def _discover_images(build_dir: Path) -> list[str]:
        """Return sysbuild image names from the build directory structure.

        An image is a subdir of *build_dir* that contains a
        ``compile_commands.json`` directly (verified on NCS 3.2.3 — the
        per-image compile_commands lives at ``<build_dir>/<image>/compile_commands.json``,
        not one level deeper).  This predicate excludes non-image directories
        that sysbuild also creates (``_sysbuild/``, ``CMakeFiles/``,
        ``CMakeCache.txt``, the top-level ``zephyr/``).  Image names are a
        discovery trigger + fallback label, not a stable identity — the durable
        label is ``images[].name`` from config and the durable identity is the
        per-image ``config_hash``.
        """
        if not build_dir.is_dir():
            return []
        return [
            sub.name
            for sub in sorted(build_dir.iterdir())
            if sub.is_dir() and (sub / "compile_commands.json").exists()
        ]

    def build_multi(
        self, project_root: Path, cfg: BuildConfig
    ) -> list[tuple[str, str, Path]]:
        """Build all variants × images via sysbuild, return per-image cc paths.

        Returns a list of ``(variant, image, compile_commands_path)`` — one
        entry per (variant, image) pair, with the per-image compile_commands
        copied to ``.fw-context/build/compile_commands.<variant>.<image>.json``.

        WHY per-image copies: each image is a separate Zephyr CMake project
        with its own ``compile_commands.json``; the per-(variant, image) file
        lets ``config_hash`` and the manifest stay per-image (§5.3.3).

        Clean is deliberately suppressed — indexing only needs
        ``compile_commands.json``, so ``--pristine=auto`` (incremental) is used
        instead of ``--pristine`` (always), keeping re-builds fast (§5.8).
        """
        from ..build import build_variant_config

        if not shutil.which("west"):
            raise RuntimeError("west is required for Zephyr builds.  Install the Zephyr SDK and west tool.")
        if not cfg.variants:
            raise RuntimeError("build_multi() requires [[build.variants]] in config.")

        _, env = self._prepare_ninja_wrapper(project_root)
        results: list[tuple[str, str, Path]] = []

        for variant in cfg.variants:
            vcfg = build_variant_config(cfg, variant)
            if not vcfg.board:
                raise RuntimeError(
                    f"variant '{variant.name}' has no board.  Set it in "
                    f"[[build.variants]] or inherit from [build] board."
                )
            board = self._normalize_board(vcfg.board)
            source_dir = vcfg.source_dir or "."
            build_dir = project_root / (vcfg.build_dir or f"build/{variant.name}")

            cmd: list[str] = [
                "west", "build", "--sysbuild",
                "-b", board,
                "-d", str(build_dir),
                source_dir,
                "--pristine=auto",
                "--",
                "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
                "-DEXTRA_CPPFLAGS=-MMD",
            ]

            variant_env = {**env, **(vcfg.env or {})}
            log.info("zephyr build_multi[%s]: %s", variant.name, " ".join(cmd))
            run_build_command(
                cmd,
                cwd=project_root,
                description=f"west build --sysbuild ({variant.name})",
                env=variant_env,
                build_cfg=vcfg,
            )

            for image_name in self._discover_images(build_dir):
                cc_src = build_dir / image_name / "compile_commands.json"
                if not cc_src.exists():
                    continue
                target = cc_output_path(project_root).with_name(
                    f"compile_commands.{variant.name}.{image_name}.json"
                )
                shutil.copy2(cc_src, target)
                results.append((variant.name, image_name, target))
                log.info("Copied %s → %s", cc_src, target)

        return results

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["build/"]

    def get_vendor_patterns(
        self,
        project_root: Path,
        *,
        units: list | None = None,
    ) -> list[str]:
        """Return the in-tree SDK patterns derived from this build's SDK root.

        For a standard Zephyr or NCS project the answer is an empty list:
        the SDK is outside the project, and a path outside project_root
        already gets is_project=0.  A pattern appears only when the
        workspace really is inside the project, which is the T3 layout.

        The SDK root comes from the sources in _sdk_roots(), in falling
        order of how well each one describes THIS build.  The markers in
        the project root are not one of them: ``west.yml`` is in a T2
        manifest repository as well, where ``modules/`` holds the team's own
        Zephyr modules.
        """
        zephyr_base, west_topdir = self._sdk_roots(project_root, units)
        return _zephyr_vendor_patterns(project_root, zephyr_base, west_topdir)

    @classmethod
    def _sdk_roots(
        cls,
        project_root: Path,
        units: list | None,
    ) -> tuple[Path | None, Path | None]:
        """Return ``(ZEPHYR_BASE, WEST_TOPDIR)`` for this build, or None for each.

        Four sources, in falling order of how well each one describes the
        build that is indexed:

        1. ``-fmacro-prefix-map`` in *units*.  It comes from the build that
           is indexed, so it beats every other source.
        2. ``ZEPHYR_BASE`` in the environment.
        3. ``west config zephyr.base`` and ``west topdir``.
        4. A ``zephyr/`` directory in project_root, which is the T3
           workspace layout.  This is the last resort: it is the only
           source that reads the filesystem instead of the build.

        The first source that answers wins, and the sources are not mixed:
        a value from the environment and a value from ``west`` can name two
        different workspaces, and a mix of the two describes neither.

        """
        from_flags = _prefix_map_roots(units)
        if from_flags != (None, None):
            return from_flags

        env_base = os.environ.get("ZEPHYR_BASE")
        if env_base:
            return Path(env_base).resolve(), None

        west_base, west_top = _west_paths(str(project_root))
        if west_base or west_top:
            return (
                Path(west_base).resolve() if west_base else None,
                Path(west_top).resolve() if west_top else None,
            )

        in_tree = project_root / "zephyr"
        if in_tree.is_dir():
            return in_tree.resolve(), None

        return None, None

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

    # ── Environment auto-detection ──

    @staticmethod
    def _provides_sdk_manager(candidate: str) -> bool:
        """Return True when *candidate* really provides ``sdk-manager``.

        WHY probe instead of trusting the name: ``nrfutil`` is a name several
        unrelated tools answer to.  A pip-installed ``nrfutil`` (the
        click-based nRF5 utility) has no ``sdk-manager`` command at all, and a
        machine can easily carry both — measured here, four different
        ``nrfutil`` binaries were on PATH and only one was the SDK manager.

        Accepting the wrong one is silent and expensive: detection writes an
        activation script around it, and every later Zephyr build dies with a
        bare ``Usage: nrfutil [OPTIONS] COMMAND [ARGS]...`` that names neither
        the script nor the binary that produced it.
        """
        try:
            result = subprocess.run(
                [candidate, "sdk-manager", "--version"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    @classmethod
    def _find_nrfutil(cls) -> str | None:
        """Locate an nrfutil binary that provides the SDK manager.

        Searches every PATH entry — not just the first hit, since a machine
        can carry several unrelated ``nrfutil`` binaries — then the standard
        install locations (``~/.nrfutil/bin/``, ``~/ncs_tools/``).  Each
        candidate is probed; the first that answers to ``sdk-manager`` wins.

        Version-manager shims are tried last.  The path found here is baked
        into the generated activation script, and a shim re-resolves the name
        at run time: the same shim answers the probe now and can dispatch to a
        different binary during the build, when the environment sets another
        interpreter version.  A real binary cannot change under us.
        """
        direct: list[str] = []
        shims: list[str] = []
        seen: set[str] = set()

        def add(path: Path) -> None:
            resolved = str(path)
            if resolved in seen or not path.is_file():
                return
            seen.add(resolved)
            # Same marker resolve_real_binary() uses — pyenv and asdf share it.
            (shims if "/shims/" in resolved else direct).append(resolved)

        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            for name in ("nrfutil-sdk-manager", "nrfutil"):
                add(Path(entry) / name)
        for cand in (
            Path.home() / ".nrfutil" / "bin" / "nrfutil-sdk-manager",
            Path.home() / ".nrfutil" / "bin" / "nrfutil",
            Path.home() / "ncs_tools" / "nrfutil",
        ):
            add(cand)

        for candidate in (*direct, *shims):
            if cls._provides_sdk_manager(candidate):
                return candidate
        if seen:
            log.warning(
                "Found %d nrfutil binaries, none providing 'sdk-manager': %s",
                len(seen), ", ".join(sorted(seen)[:4]),
            )
        return None

    @classmethod
    def zephyr_sdk_from_environment(cls) -> SdkChoice | None:
        """Return the upstream Zephyr SDK the environment already names.

        WHY only the environment, and no search of the standard install
        locations: an upstream Zephyr workflow sets its own paths — you
        source ``zephyr-env.sh`` or activate the workspace venv, and
        ``ZEPHYR_BASE`` and ``ZEPHYR_SDK_INSTALL_DIR`` are exported.  Someone
        working that way has them set already, so there is nothing to pick
        between and nothing to ask.

        NCS is the opposite case, which is why it gets a picker: its
        environment is *created* by ``nrfutil sdk-manager toolchain env``,
        and that command takes the version as an argument.  Before it runs
        there is nothing in the environment to read.
        """
        env_dir = os.environ.get("ZEPHYR_SDK_INSTALL_DIR")
        if not env_dir:
            return None
        sdk_dir = Path(env_dir).expanduser()
        if not sdk_dir.is_dir():
            return None
        script = next(iter(sorted(sdk_dir.glob("environment-setup-*"))), None)
        return SdkChoice(
            kind=SDK_KIND_ZEPHYR,
            version=sdk_dir.name.removeprefix("zephyr-sdk-"),
            path=sdk_dir,
            usable=script is not None,
            env_script=script,
        )

    @classmethod
    def list_installed_ncs(cls, nrfutil: str | None = None) -> list[SdkChoice]:
        """Return the NCS versions installed on this machine, newest first.

        WHY ask nrfutil rather than scan ``~/ncs``: the directory names give
        the version but not whether its toolchain is present, and a version
        whose toolchain is missing cannot build anything.  ``sdk-manager
        list`` reports both, and it knows the configured installation
        directory, which need not be ``~/ncs``.

        Returns an empty list when nrfutil is absent or answers with
        something unparseable — the caller falls back to asking the user.
        """
        nrfutil = nrfutil or cls._find_nrfutil()
        if nrfutil is None:
            return []
        try:
            result = subprocess.run(
                [nrfutil, "--json", "sdk-manager", "list"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []

        # --json emits JSON Lines; the versions arrive in one "info" record.
        installs: list[SdkChoice] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            versions = (payload.get("data") or {}).get("versions")
            if not isinstance(versions, list):
                continue
            for entry in versions:
                install = cls._parse_ncs_entry(entry)
                if install is not None:
                    installs.append(install)
        return installs

    @staticmethod
    def _parse_ncs_entry(entry: object) -> SdkChoice | None:
        """Turn one ``sdk-manager list`` record into an :class:`SdkChoice`."""
        if not isinstance(entry, dict):
            return None
        version = entry.get("version")
        dir_names = entry.get("dirNames") or []
        if not isinstance(version, str) or not dir_names:
            return None
        toolchain = entry.get("toolchainPath")
        return SdkChoice(
            kind=SDK_KIND_NCS,
            version=version,
            path=Path(str(dir_names[0])),
            toolchain_path=Path(str(toolchain)) if toolchain else None,
            # Both halves have to be present: an SDK tree without its
            # toolchain compiles nothing, and offering it would only produce
            # a build failure later.
            usable=(
                entry.get("sdkStatus") == "installed"
                and entry.get("toolchainStatus") == "installed"
            ),
        )

    @classmethod
    def preferred_ncs_version(cls, project_root: Path) -> str | None:
        """Return the NCS version this project's environment points at.

        ``ZEPHYR_BASE`` and ``west config zephyr.base`` both resolve to
        ``<ncs_root>/<version>/zephyr``, so the version is right there in the
        path.  It used to be discarded — both signals were reduced to the NCS
        root and the version re-derived as "the newest directory under it",
        which on a machine with several installs answers with a version the
        project does not use.  The index would then be built against
        different headers and different macros than the developer compiles
        with, which is the one thing this tool must not get wrong.

        Returns None when neither signal is present; the caller asks instead
        of guessing.
        """
        for source in (cls._zephyr_base_version(), cls._west_config_version(project_root)):
            if source is not None:
                return source
        return None

    @staticmethod
    def _version_from_zephyr_path(zephyr_dir: str | None) -> str | None:
        """Extract ``vX.Y.Z`` from a ``.../vX.Y.Z/zephyr`` path."""
        if not zephyr_dir:
            return None
        p = Path(zephyr_dir).resolve()
        if p.name != "zephyr" or not p.parent.name.startswith("v"):
            return None
        return p.parent.name

    @classmethod
    def _zephyr_base_version(cls) -> str | None:
        return cls._version_from_zephyr_path(os.environ.get("ZEPHYR_BASE"))

    @classmethod
    def _west_config_version(cls, project_root: Path) -> str | None:
        if not shutil.which("west"):
            return None
        try:
            r = subprocess.run(
                ["west", "config", "zephyr.base"],
                capture_output=True, text=True, timeout=5, cwd=str(project_root),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        return cls._version_from_zephyr_path(r.stdout.strip())

    @staticmethod
    def _ncs_version(ncs_root: Path) -> str | None:
        """Return the NCS version (``vX.Y.Z``) for *ncs_root*, or None.

        Only standard signals are consulted: ``<ncs_root>/v*/zephyr`` directory
        names.  Project-custom pins (``ci/sdk.env``) are deliberately NOT read
        — they are a per-project convention, not a generic NCS signal (design
        rule: fw-context must not depend on project-specific files).
        """
        if ncs_root.is_dir():
            for d in sorted(ncs_root.iterdir(), reverse=True):
                if d.name.startswith("v") and (d / "zephyr").is_dir():
                    return d.name
        return None

    @classmethod
    def _detect_ncs(cls, project_root: Path) -> tuple[str, str, str] | None:
        """Detect an NCS install and return ``(nrfutil, version, ncs_root)``.

        NCS root candidates, most specific first: ``ZEPHYR_BASE`` env,
        ``west config zephyr.base``, and ``~/ncs``.  A project-local ``ncs``
        symlink is deliberately NOT used — it is a project-custom convention,
        not a generic signal.  Returns None when no usable NCS install is found.
        """
        candidates: list[Path] = []

        zb = os.environ.get("ZEPHYR_BASE")
        if zb:
            p = Path(zb).resolve()
            if p.name == "zephyr" and p.parent.name.startswith("v"):
                candidates.append(p.parent.parent)

        west = shutil.which("west")
        if west:
            try:
                r = subprocess.run(
                    ["west", "config", "zephyr.base"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    p = Path(r.stdout.strip()).resolve()
                    if p.name == "zephyr" and p.parent.name.startswith("v"):
                        candidates.append(p.parent.parent)
            except Exception:  # nosec B110 — best-effort env detection
                pass

        home_ncs = Path.home() / "ncs"
        if home_ncs.is_dir():
            candidates.append(home_ncs)

        for ncs_root in candidates:
            if not ncs_root.is_dir():
                continue
            version = cls._ncs_version(ncs_root)
            if version is None or not (ncs_root / version / "zephyr").is_dir():
                continue
            nrfutil = cls._find_nrfutil()
            if nrfutil is None:
                continue
            return nrfutil, version, str(ncs_root)
        return None

    @staticmethod
    def _write_ncs_env_script(
        project_root: Path, nrfutil: str, version: str, ncs_root: str
    ) -> str:
        """Write (idempotently) a canonical NCS activation script to ``.fw-context/``.

        Mirrors the standard NCS environment activation — ``nrfutil sdk-manager
        toolchain env`` for the toolchain plus ``ZEPHYR_BASE`` — so fw-context
        does not depend on a project-specific setup script.  The script path is
        returned so it can be persisted as ``[build] activate``.

        ``ZEPHYR_BASE`` is set here because ``toolchain env`` does not set it;
        that is a known gap in the Nordic tooling, not an oversight.

        WHY ``zephyr-env.sh`` is deliberately NOT sourced: beyond exporting
        the same ``ZEPHYR_BASE``, it does two things.  It prepends
        ``$ZEPHYR_BASE/scripts`` to PATH — tools people run by hand
        (``twister``, ``checkpatch``), which ``west build`` does not need;
        measured, the toolchain env omits that directory and the build
        succeeds anyway.  And it sources ``~/.config/zephyr/zephyrrc`` or
        ``~/.zephyrrc``, a user file of arbitrary content.  Pulling that in
        would make a private rc file an invisible input to every index build,
        so two machines could produce different indexes with nothing in the
        project to explain it.  A project that genuinely needs its own
        environment sets ``[build] activate`` to its own script instead.
        """
        wrapper_dir = project_root / ".fw-context"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        target = wrapper_dir / "ncs-env.sh"
        zephyr_base = f"{ncs_root}/{version}/zephyr"
        content = (
            "#!/bin/sh\n"
            f'eval "$("{nrfutil}" sdk-manager toolchain env --ncs-version "{version}" --as-script)"\n'
            f'export ZEPHYR_BASE="{zephyr_base}"\n'
        )
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            tmp = wrapper_dir / ".ncs-env.tmp"
            tmp.write_text(content, encoding="utf-8")
            tmp.chmod(0o755)
            tmp.rename(target)
        return str(target)

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        # 1. NCS (nRF Connect SDK) — generic, via nrfutil + ZEPHYR_BASE.
        ncs = cls._detect_ncs(project_root)
        if ncs is not None:
            nrfutil, version, ncs_root = ncs
            activate = cls._write_ncs_env_script(project_root, nrfutil, version, ncs_root)
            return {"python": None, "activate": activate}

        # 2. Zephyr source tree — zephyr-env.sh (sets ZEPHYR_BASE).
        west = shutil.which("west")
        if west:
            try:
                r = subprocess.run(
                    ["west", "config", "zephyr.base"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    zephyr_env = Path(r.stdout.strip()) / "zephyr-env.sh"
                    if zephyr_env.exists():
                        return {"python": None, "activate": str(zephyr_env)}
            except Exception:  # nosec B110 — best-effort env detection
                pass

        # 3. Plain Zephyr SDK — environment-setup-* (sets toolchain env).
        for sdk_dir in sorted(Path.home().glob("zephyr-sdk-*"), reverse=True):
            for env_file in sdk_dir.glob("environment-setup-*"):
                return {"python": None, "activate": str(env_file)}

        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        return (
            "Zephyr builds require an activated toolchain environment.\n"
            "For Nordic NCS this is detected automatically via\n"
            "  nrfutil sdk-manager toolchain env\n"
            "and ZEPHYR_BASE.  To use a custom setup script instead, set:\n"
            "  [build]\n"
            '  activate = "~/ncs_tools/nordic_minimal_setup.sh"'
        )


# Register
registry.register(ZephyrBuildSystem)
