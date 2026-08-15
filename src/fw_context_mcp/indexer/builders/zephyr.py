"""Zephyr build system — detection, build, and validation."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import resolve_real_binary, run_build_command

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)

_ZEPHYR_MARKERS = ["west.yml", "zephyr"]


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

        build_dir = project_root / "build"

        # Ninja deletes .d depfiles after reading them by default.
        # Create a wrapper that adds -d keepdepfile so .d files persist
        # on disk for incremental re-indexing.
        # We place the wrapper at $PATH priority (not CMAKE_MAKE_PROGRAM)
        # because sysbuild's ExternalProject_Add does not forward
        # CMAKE_MAKE_PROGRAM to the inner Zephyr CMake project.
        #
        # Resolve the REAL ninja binary, not a pyenv/asdf shim: the shim
        # re-execs `ninja` by name, and since the wrapper dir is prepended
        # to PATH below, that re-resolution would find the wrapper again
        # and recurse (appending -d keepdepfile each cycle).
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

    # ── Environment auto-detection ──

    @staticmethod
    def _find_nrfutil() -> str | None:
        """Locate the nrfutil SDK-manager binary.

        Prefers ``nrfutil-sdk-manager`` / ``nrfutil`` from PATH, then the
        standard install locations (``~/.nrfutil/bin/``, ``~/ncs_tools/``).
        """
        for name in ("nrfutil-sdk-manager", "nrfutil"):
            found = shutil.which(name)
            if found:
                return found
        for cand in (
            Path.home() / ".nrfutil" / "bin" / "nrfutil-sdk-manager",
            Path.home() / ".nrfutil" / "bin" / "nrfutil",
            Path.home() / "ncs_tools" / "nrfutil",
        ):
            if cand.is_file():
                return str(cand)
        return None

    @staticmethod
    def _ncs_version(project_root: Path, ncs_root: Path) -> str | None:
        """Return the NCS version (``vX.Y.Z``) for *ncs_root*, or None.

        Prefers the project-pinned ``ci/sdk.env`` (``NCS_VERSION``), then
        falls back to ``<ncs_root>/v*/zephyr`` directory names.
        """
        for anc in [project_root.resolve(), *project_root.resolve().parents]:
            sdk_env = anc / "ci" / "sdk.env"
            if not sdk_env.is_file():
                continue
            try:
                for line in sdk_env.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("NCS_VERSION="):
                        ver = line.split("=", 1)[1].strip()
                        if ver:
                            return ver if ver.startswith("v") else f"v{ver}"
            except OSError:
                pass
            break
        if ncs_root.is_dir():
            for d in sorted(ncs_root.iterdir(), reverse=True):
                if d.name.startswith("v") and (d / "zephyr").is_dir():
                    return d.name
        return None

    @classmethod
    def _detect_ncs(cls, project_root: Path) -> tuple[str, str, str] | None:
        """Detect an NCS install and return ``(nrfutil, version, ncs_root)``.

        NCS root candidates, most specific first: ``ZEPHYR_BASE`` env,
        ``west config zephyr.base``, an ``ncs`` symlink/dir in the project
        tree, and ``~/ncs``.  Returns None when no usable NCS install is found.
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

        for anc in [project_root.resolve(), *project_root.resolve().parents]:
            ncs_link = anc / "ncs"
            if ncs_link.exists():
                candidates.append(ncs_link.resolve())
                break

        home_ncs = Path.home() / "ncs"
        if home_ncs.is_dir():
            candidates.append(home_ncs)

        for ncs_root in candidates:
            if not ncs_root.is_dir():
                continue
            version = cls._ncs_version(project_root, ncs_root)
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
