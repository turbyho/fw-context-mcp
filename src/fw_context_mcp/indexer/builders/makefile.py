"""Makefile build system — compile_commands.json from the log of ``make -Bnkwp``.

The builder runs ``make -Bnkwp``, which prints each command of the build,
and the data base of each make.  It runs no command of a goal.  It runs
the recipe of each included makefile that has a rule, as each ``make -n``
does, and ``-B`` makes each such makefile out of date (measured with GNU
make 4.4.1: ``-include dep.mk`` and a rule for it wrote ``dep.mk``).  ``_make_log`` reads the
compile commands from that log, by the rules of the shell that make uses.
When ``make_dry_run`` is false, a real ``make`` runs between two such runs.
No ``bear`` and no ``LD_PRELOAD``: the dry run works also for a project
that does not build in the current environment.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import cc_output_path, require_build_tool, run_build_command, write_text_atomic

from ..build_layout import BuildLayout
from . import registry
from ._make_log import CompileCommand, compile_entries, outputs_outside, read_log
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)

#: The flags of the run that gives the log: make each target (``-B``),
#: print the commands and run none (``-n``), go on after an error (``-k``),
#: print the directories (``-w``) and the data base (``-p``).  The data
#: base gives the shell of each make.  ``-j1``: the reader follows the
#: directory of each make, and the output of two makes must not mix.  A
#: ``-j`` of MAKEFLAGS in the environment comes before the command line,
#: thus ``-j1`` wins over it.
_LOG_FLAGS = ("-Bnkwp", "-j1")

#: The locale of the run that gives the log: the messages of make in
#: English.  GNU make translates "Entering directory" (gettext), and a
#: recursive make then gave no directory to the reader.  [build] env and the
#: activate script come after it, thus a config can set its own.
_LOG_LOCALE = {"LC_ALL": "C", "LANGUAGE": "C"}


class MakefileBuildSystem:
    """Makefile project — compile_commands.json from the log of ``make -Bnkwp``.

    Auto-detected when a ``Makefile`` exists in the project root.

    WHY the log of make (not bear): bear intercepts compiler calls via
    LD_PRELOAD, which requires the compiler to actually run, and Windows
    has no LD_PRELOAD.  The log of a dry run gives the commands without
    compiling — faster, and it works even for projects that do not build
    in the current environment.  WHY a reader of fw-context and not
    compiledb: see ``_make_log``.
    """

    name: str = "Makefile"
    config_key: str = "makefile"
    markers: list[str] = ["Makefile"]

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        return (root / "Makefile").exists()

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Build is delegated to generate(): the log of make IS the build step."""
        return self.generate(project_root, cfg)

    def generate(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Write compile_commands.json from the log of ``make -Bnkwp``.

        Runs ``make -Bnkwp -j1 -C <root> [-f <makefile>] [vars...] <target>``
        and reads the compile commands of its log (``_make_log``).  When
        ``make_dry_run`` is false, a first such run gives the files that the
        build writes, and a real ``make`` runs before the run that gives the
        database: the real build makes the generated sources.

        A Makefile names its output directory in a variable of its own, and
        no name is common to all of them.  ``[build] out_dir_var`` names it.
        The build gets ``<VAR>=<out>``, the absolute output directory of
        the variant (see ``build_layout``).  The path is absolute, thus it
        stays correct in a recursive ``$(MAKE) -C <subdir>``.  Without the
        name, a real build writes where the Makefile says, into the build
        directory of the user.  Thus ``make_dry_run = false`` without
        ``out_dir_var`` is an error.  fw-context does not guess the name
        from the Makefile.  A compile or a link of the first log that writes
        outside the output directory stops the run before the real build:
        a typo in ``out_dir_var`` or ``override`` in the Makefile.

        A log with no compile command is an error: an empty database would
        read as a project without code.
        """
        root = project_root.resolve()
        out_var = cfg.out_dir_var
        out_dir = BuildLayout(root).out_dir(cfg.variant_name)
        self._check_output_variable(cfg, out_dir)

        make = require_build_tool(
            "make", root, cfg,
            "Install GNU make, and put it on PATH or its directory into [build] extra_path.",
        )
        arguments: list[str] = ["-C", str(root)]
        if cfg.makefile:
            arguments += ["-f", cfg.makefile]
        # Extra vars like V=1 CROSS_COMPILE=arm-none-eabi-
        arguments += [f"{key}={value}" for key, value in cfg.make_vars.items()]
        if out_var:
            arguments.append(f"{out_var}={out_dir}")
        arguments.append(cfg.make_target or "all")

        target = cfg.make_target or "all"
        if not cfg.make_dry_run:
            outside = outputs_outside(self._commands(make, arguments, root, cfg, target), out_dir)
            if outside:
                raise RuntimeError(
                    f"make would write {outside[0]} outside {out_dir}"
                    f"{f' (and {len(outside) - 1} more files)' if len(outside) > 1 else ''}: the Makefile "
                    f"does not use {out_var} for this output, or it sets it with 'override'. fw-context ran "
                    "no build. Name the right variable in [build] out_dir_var."
                )
            out_dir.mkdir(parents=True, exist_ok=True)
            log.info("makefile build: %s", " ".join([make, *arguments]))
            run_build_command([make, *arguments], cwd=root, description="make", build_cfg=cfg)

        entries = compile_entries(self._commands(make, arguments, root, cfg, target))
        if not entries:
            raise RuntimeError(
                f"make {' '.join(_LOG_FLAGS)} printed no compile command of an existing source: the target "
                f"{target} compiles nothing, or its recipes are silent in a way that make -n does not print."
            )

        cc_path = cc_output_path(root, cfg)
        cc_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(cc_path, json.dumps(entries, indent=2) + "\n")
        return cc_path

    @staticmethod
    def _commands(make: str, arguments: list[str], root: Path, cfg: BuildConfig, target: str) -> list[CompileCommand]:
        """Run ``make -Bnkwp -j1`` and return the compiles of its log.

        A non-zero exit of make is an error: make did not print each
        command, and a database with a part of the build would read as the
        whole build.
        """
        log_cmd = [make, *_LOG_FLAGS, *arguments]
        log.info("makefile commands: %s", " ".join(log_cmd))
        # surrogateescape: the data base holds each environment variable,
        # and a value that is not UTF-8 (the ANSI code page of an older
        # make on Windows, a Latin-2 value) is no error of the build.  The
        # reader refuses a compile with such a byte (read_log): a path must
        # not get a character that it does not have.
        # _LOG_LOCALE: make writes "Entering directory" in the language of
        # the locale (measured: "Vstupuje se do adresáře" with a Czech
        # locale), and the reader knows the English line.
        # quote_stdout: the data base holds each environment variable.
        result = run_build_command(
            log_cmd, cwd=root, description=f"make {' '.join(_LOG_FLAGS)}", env=dict(_LOG_LOCALE), build_cfg=cfg,
            errors="surrogateescape", quote_stdout=False,
            failure_hint=(
                f"make could not print each command of the target {target}, thus fw-context writes no "
                f"compilation database. The error of make is below; make -n {target} in {root} shows it too."
            ),
        )
        return read_log(result.stdout or "", windows=sys.platform == "win32")

    @staticmethod
    def _check_output_variable(cfg: BuildConfig, out_dir: Path) -> None:
        """Refuse a configuration in which a real make cannot reach the output directory.

        make splits a target name and a recipe at whitespace, and it expands
        ``$``, thus an output directory with one of them breaks the build
        and can create directories outside the project (measured with a
        space).  A variable name with whitespace, ``=``, ``:`` or ``#`` is no
        assignment that make reads as one.
        """
        out_var = cfg.out_dir_var
        if not cfg.make_dry_run and not out_var:
            raise RuntimeError(
                "make_dry_run = false runs a real make into the output directory of fw-context. "
                'Name the output variable of the Makefile in [build] out_dir_var, for example "BUILD_DIR".'
            )
        if not out_var:
            return
        if any(char.isspace() or char in "=:#" for char in out_var):
            raise RuntimeError(f"[build] out_dir_var {out_var!r} is not the name of a make variable")
        if out_var in cfg.make_vars:
            raise RuntimeError(
                f"[build] make_vars sets {out_var}, and out_dir_var names it. "
                "fw-context gives it the output directory. Remove it from make_vars."
            )
        if any(char.isspace() or char == "$" for char in str(out_dir)):
            raise RuntimeError(
                f"make cannot build into {out_dir}: the path holds a space or '$'. "
                "Move the project, or rename the variant."
            )

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    def required_tools(self) -> list[str]:
        return ["make"]

    # ── Environment auto-detection ──

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        return (
            "Install GNU make, and put it on PATH or its directory into [build] extra_path."
        )

    def background_build_safe(self, cfg: BuildConfig) -> bool:
        """Safe in dry-run mode (the default), and with ``out_dir_var``.

        ``make -Bnkwp`` prints what make would do and runs no command of
        a goal, thus no artifact exists to collide with.  Limit: make runs
        the recipe of an included makefile that has a rule (see the
        module docstring).  A real build is safe when
        ``out_dir_var`` sends it to the output directory of fw-context; a
        real build without it is refused in ``generate``.
        """
        return bool(cfg.make_dry_run or cfg.out_dir_var)

    # ── Build dir patterns ──

    def get_linker_scripts(
        self,
        project_root: Path,
        *,
        compile_commands: Path | None = None,
        variant: str = "",
        units: list | None = None,
    ) -> list[Path]:
        """Return nothing: a makefile can name the script anywhere.

        The `-T` flag can be in any variable of any included makefile, and
        reading it means running `make` and reading the link line.  The
        backend reads the log of a dry run, which never links, so no link
        command exists to read.
        """
        return []

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["build/"]

    def get_vendor_patterns(
        self,
        project_root: Path,
        *,
        units: list | None = None,
    ) -> list[str]:
        """Return no pattern — a Makefile project has no canonical vendor tree.

        Where the third-party code sits is written in the Makefile itself,
        with a different variable in every project.  A fixed pattern would
        be a guess, and a wrong guess hides code the team owns.
        """
        return []


# Register
registry.register(MakefileBuildSystem)
