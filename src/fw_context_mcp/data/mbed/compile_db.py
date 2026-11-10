"""Write compile_commands.json for an Mbed CLI 1 build (``mbed compile``).

Mbed CLI 1 writes no compilation database.  ``mbed compile`` starts
``mbed-os/tools/make.py``, and make.py gets the command of each source
from ``mbedToolchain.compile_command()``.  This script records these
commands where make.py makes them.  Thus the database holds the argv of
each compiler call as the build runs it, on each host.

WHY not bear: bear on macOS and Windows has only its wrapper mode.  A
wrapper sees a compiler only when the build finds it through PATH or
CC/CXX, and mbed compile calls the compiler of ``GCC_ARM_PATH`` by its
absolute path.  bear on Windows also exists only as a build from source
in MSYS2.  On Linux, bear and this script give the same commands
(measured: 881 commands of one project, the same arguments).

fw-context runs this file with the Python of the project, the one that
has mbed-cli (``[build] python``).  That Python does not have
fw-context, thus this file imports only the standard library, mbed-cli
and the tools of mbed-os.  Its directory holds no other module: Python
puts the directory of a script first on ``sys.path``, and a module there
could hide a module that mbed-cli or make.py imports.  The script needs
Python 3.7 or later (``from __future__ import annotations``).

The script has two modes:

``record OUT -- ARGS``
    Run mbed-cli with ARGS in this process.  When mbed-cli starts
    make.py, the command gets this file in mode ``hook`` before make.py.
    mbed-cli itself gives make.py its arguments and its environment
    (``.mbed``, ``mbed_settings.py``, ``MACROS.txt``, the profiles).

``hook OUT MAKE ARGS``
    Run make.py with ARGS in this process, as Python runs a script.
    The command of each source goes into OUT: the command that
    ``compile_command`` gives, and for an object file that is current the
    command that it would give.  Thus a build without ``--clean`` also
    gives a full database.  OUT gets its content only when make.py ends
    without an error, each of its builds runs to its end, and the build
    gives at least one command.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import runpy
import sys
from typing import Any

_MAKE_SCRIPT = "make.py"


def _exit_code(stop: SystemExit) -> int:
    """Return the exit status of *stop* as Python gives it to the shell."""
    if stop.code is None:
        return 0
    if isinstance(stop.code, int):
        return stop.code
    # sys.exit("text") writes the text to stderr and ends with status 1.
    print(stop.code, file=sys.stderr)
    return 1


def _record(out: str, mbed_args: list[str]) -> int:
    """Run ``mbed`` with *mbed_args*, with make.py in mode ``hook``."""
    cli: Any = importlib.import_module("mbed.mbed")
    original_popen = cli.popen
    starts: list[list[str]] = []
    this_file = os.path.abspath(__file__)

    def popen(command: list[str], **kwargs: Any) -> Any:
        """Put this file in mode ``hook`` before the make.py of *command*."""
        command = list(command)
        for index, word in enumerate(command):
            if os.path.basename(word) == _MAKE_SCRIPT:
                starts.append(command)
                command = command[:index] + [this_file, "hook", out] + command[index:]
                break
        return original_popen(command, **kwargs)

    cli.popen = popen
    sys.argv = ["mbed", *mbed_args]
    try:
        cli.main()
        status = 0
    except SystemExit as stop:
        status = _exit_code(stop)
    if status != 0:
        return status
    if len(starts) != 1:
        # A database from no make.py run, or from two, is not the build
        # of these arguments.  mbed-cli then did something other than a
        # compile of the application.
        print(
            f"compile_db.py: mbed {' '.join(mbed_args)} started make.py {len(starts)} times, not one time",
            file=sys.stderr,
        )
        return 1
    return 0


def _hook(out: str, make: str, make_args: list[str]) -> int:
    """Run *make* with *make_args* and write each compile command to *out*."""
    make = os.path.abspath(make)
    # As "python make.py": the directory of the script first on sys.path
    # (Python put the directory of this file there), and the arguments
    # after its name.  With -P or PYTHONSAFEPATH Python puts no directory
    # there, and sys.path[0] is a real entry that must stay.
    if not getattr(sys.flags, "safe_path", False):
        sys.path[0] = os.path.dirname(make)
    # make.py puts the root of mbed-os first on sys.path before it imports
    # the tools.  The import below needs the same root, thus it gets the
    # same module objects as make.py.
    sys.path.insert(0, os.path.dirname(os.path.dirname(make)))
    sys.argv = [make, *make_args]
    # tools.toolchains exports the class in each layout of mbed-os: the
    # class is in toolchains/mbed_toolchain.py of Mbed OS 6 and in
    # toolchains/__init__.py of older releases.
    toolchain_class: Any = importlib.import_module("tools.toolchains").mbedToolchain

    records: list[dict[str, Any]] = []
    original = toolchain_class.compile_command
    signature = inspect.signature(original)

    def command_of_current_object(self: Any, *args: Any, **kwargs: Any) -> Any:
        """Return the command of a source whose object file is current.

        compile_command() gives None when ``need_update()`` finds the object
        file current, and a build without --clean then names only the
        changed sources.  The database must name each source, thus this
        asks the original again with ``need_update()`` True for this one
        call.  The build itself still gets None and compiles nothing.
        """
        own = vars(self)
        had_own = "need_update" in own
        saved = own.get("need_update")
        self.need_update = lambda target, dependencies: True
        try:
            return original(self, *args, **kwargs)
        finally:
            if had_own:
                self.need_update = saved
            else:
                del self.need_update

    def compile_command(self: Any, *args: Any, **kwargs: Any) -> Any:
        """Record each command of the source, and give the build what the original gives.

        The arguments go to the original as they come.  The source and the
        object file come from the parameters ``source`` and ``object`` of
        the original, thus a call with positional arguments and a call
        with names give the same record.  False means a file that is not a
        source (no command), None an object file that is current.
        """
        commands = original(self, *args, **kwargs)
        recorded = command_of_current_object(self, *args, **kwargs) if commands is None else commands
        bound = signature.bind(self, *args, **kwargs).arguments
        for command in recorded or ():
            records.append(
                {
                    "directory": os.getcwd(),
                    "arguments": [str(word) for word in command],
                    "file": bound["source"],
                    "output": bound["object"],
                }
            )
        return commands

    toolchain_class.compile_command = compile_command

    # make.py catches KeyboardInterrupt in its wrapper of build_project,
    # prints "[CTRL+c] exit" and ends with status 0.  Its status thus does
    # not tell if the build ran to its end, and a database of a part of the
    # sources is not the database of the build.  make.py imports
    # build_project by name when it runs, thus it gets this wrapper.
    build_api: Any = importlib.import_module("tools.build_api")
    original_build = build_api.build_project
    builds = {"started": 0, "finished": 0}

    def build_project(*args: Any, **kwargs: Any) -> Any:
        """Count each build of make.py that starts and each that returns."""
        builds["started"] += 1
        result = original_build(*args, **kwargs)
        builds["finished"] += 1
        return result

    build_api.build_project = build_project
    try:
        runpy.run_path(make, run_name="__main__")
        status = 0
    except SystemExit as stop:
        status = _exit_code(stop)
    if status != 0:
        return status
    if builds["started"] == 0 or builds["finished"] != builds["started"]:
        print(
            f"compile_db.py: make.py ended with status 0, but {builds['finished']} of "
            f"{builds['started']} builds ran to their end (an interrupt stops a build of make.py "
            "with status 0)",
            file=sys.stderr,
        )
        return 1
    if not records:
        # Each build of an application compiles sources.  No record means
        # that the build did not call the compile_command of mbedToolchain:
        # this mbed-os has a toolchain that replaces it.
        print("compile_db.py: the build gave no compile command: this mbed-os is not supported", file=sys.stderr)
        return 1
    with open(out, "w", encoding="utf-8") as stream:
        json.dump(records, stream, indent=1)
    return 0


def main(argv: list[str]) -> int:
    """Run the mode that *argv* names; see the docstring of the module."""
    if len(argv) >= 3 and argv[0] == "record" and argv[2] == "--":
        return _record(argv[1], argv[3:])
    if len(argv) >= 3 and argv[0] == "hook":
        return _hook(argv[1], argv[2], argv[3:])
    print("usage: compile_db.py record OUT -- ARGS | compile_db.py hook OUT MAKE ARGS", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
