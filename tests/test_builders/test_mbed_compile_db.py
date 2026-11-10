"""The script that records the commands of ``mbed compile`` (data/mbed/compile_db.py).

The tests run the script as the Mbed OS backend does: a separate Python
process, with a fake mbed-cli on PYTHONPATH and a fake ``mbed-os/tools``
tree in the project.  The fakes keep the parts that the script depends
on: mbed-cli starts make.py through its ``popen``, make.py puts the root
of mbed-os on sys.path, and the toolchain gives each command through
``mbedToolchain.compile_command``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fw_context_mcp.indexer.builders.mbed_os import _COMPILE_DB_SCRIPT

_FAKE_MBED_CLI = '''
import os
import subprocess
import sys

python_cmd = sys.executable


def popen(command, **kwargs):
    status = subprocess.call(command, **kwargs)
    if status:
        sys.exit(status)


def main():
    args = sys.argv[1:]
    if args[0] == "compile":
        tools = os.path.join(os.getcwd(), "mbed-os", "tools")
        popen([python_cmd, "-u", os.path.join(tools, "make.py")] + args[1:])
    sys.exit(0)
'''

_FAKE_MAKE = """
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import site_marker  # a module of the PYTHONPATH entry that mbed-cli gives
if not getattr(sys.flags, "safe_path", False):
    import tools_helper  # a module beside make.py, as "python make.py" finds it
from tools.toolchains import mbedToolchain

toolchain = mbedToolchain()
given = [
    toolchain.compile_command("./src/a.c", "BUILD/src/a.o", ["inc"]),
    toolchain.compile_command(source="./src/b.cpp", object="BUILD/src/b.o", includes=["inc"]),
    toolchain.compile_command("./src/start.S", "BUILD/src/start.o", []),
    toolchain.compile_command("./src/old.c", "BUILD/src/old.o", []),
    toolchain.compile_command("./src/api.h", "BUILD/src/api.o", []),
]
# What the build itself got: the hook must not change it.
with open(os.environ["FAKE_MAKE_GIVEN"], "w") as stream:
    json.dump(given, stream)
if "--fail" in sys.argv:
    sys.exit(3)
"""

# The toolchain as mbed-os has it: need_update() decides, None for an
# object file that is current, False for a file that is not a source.
_FAKE_TOOLCHAIN = """
class mbedToolchain:
    def need_update(self, target, dependencies):
        return not target.endswith("old.o")

    def compile_command(self, source, object, includes):
        if source.endswith(".h"):
            return False
        if not self.need_update(object, [source]):
            return None
        if source.endswith(".S"):
            return [["arm-none-eabi-cpp", "-E", source], ["arm-none-eabi-as", source, "-o", object]]
        return [["arm-none-eabi-gcc", "-DA=1", "-c", source, "-o", object]]
"""


@pytest.fixture(params=["module", "package"])
def project(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A project with a fake mbed-os tree, and a fake mbed-cli beside it.

    ``module``: the class in toolchains/mbed_toolchain.py, as Mbed OS 6
    has it.  ``package``: the class in toolchains/__init__.py, as older
    releases have it.
    """
    site = tmp_path / "site"
    (site / "mbed").mkdir(parents=True)
    (site / "mbed" / "__init__.py").write_text("", encoding="utf-8")
    (site / "mbed" / "mbed.py").write_text(_FAKE_MBED_CLI, encoding="utf-8")
    (site / "site_marker.py").write_text("", encoding="utf-8")

    root = tmp_path / "project"
    tools = root / "mbed-os" / "tools"
    (tools / "toolchains").mkdir(parents=True)
    (tools / "__init__.py").write_text("", encoding="utf-8")
    if request.param == "module":
        (tools / "toolchains" / "mbed_toolchain.py").write_text(_FAKE_TOOLCHAIN, encoding="utf-8")
        (tools / "toolchains" / "__init__.py").write_text(
            "from . import mbed_toolchain\nmbedToolchain = mbed_toolchain.mbedToolchain\n", encoding="utf-8"
        )
    else:
        (tools / "toolchains" / "__init__.py").write_text(_FAKE_TOOLCHAIN, encoding="utf-8")
    (tools / "make.py").write_text(_FAKE_MAKE, encoding="utf-8")
    (tools / "tools_helper.py").write_text("", encoding="utf-8")
    return root


def _run(project: Path, out: Path, *mbed_args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # Only a test that sets it runs with PYTHONSAFEPATH.
    env.pop("PYTHONSAFEPATH", None)
    env.update(
        PYTHONPATH=str(project.parent / "site"), FAKE_MAKE_GIVEN=str(project.parent / "given.json"), **extra_env
    )
    return subprocess.run(
        [sys.executable, str(_COMPILE_DB_SCRIPT), "record", str(out), "--", *mbed_args],
        cwd=project, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )


def _expected(project: Path) -> list[dict]:
    # The working directory of the build, as os.getcwd() gives it (a real path).
    directory = os.path.realpath(project)

    def entry(arguments: list[str], source: str, output: str) -> dict:
        return {"directory": directory, "arguments": arguments, "file": source, "output": output}

    return [
        entry(["arm-none-eabi-gcc", "-DA=1", "-c", "./src/a.c", "-o", "BUILD/src/a.o"], "./src/a.c", "BUILD/src/a.o"),
        # A call with names gives the same record as a positional call.
        entry(["arm-none-eabi-gcc", "-DA=1", "-c", "./src/b.cpp", "-o", "BUILD/src/b.o"], "./src/b.cpp", "BUILD/src/b.o"),
        # Each command of one source: the preprocessor and the assembler.
        entry(["arm-none-eabi-cpp", "-E", "./src/start.S"], "./src/start.S", "BUILD/src/start.o"),
        entry(["arm-none-eabi-as", "./src/start.S", "-o", "BUILD/src/start.o"], "./src/start.S", "BUILD/src/start.o"),
        # A current object file: its command too, else a build without
        # --clean gives a database of the changed sources only.
        entry(["arm-none-eabi-gcc", "-DA=1", "-c", "./src/old.c", "-o", "BUILD/src/old.o"], "./src/old.c", "BUILD/src/old.o"),
    ]


def test_records_the_command_of_each_source(project: Path, tmp_path: Path):
    out = tmp_path / "cc.json"

    result = _run(project, out, "compile", "-t", "GCC_ARM", "-m", "BOARD")

    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == _expected(project)


def test_the_build_gets_what_the_toolchain_gives(project: Path, tmp_path: Path):
    """The hook records the command of a current object file, and the build still compiles none."""
    result = _run(project, tmp_path / "cc.json", "compile", "-t", "GCC_ARM", "-m", "BOARD")

    assert result.returncode == 0, result.stderr
    given = json.loads((tmp_path / "given.json").read_text(encoding="utf-8"))
    assert [None if g is None else type(g).__name__ for g in given] == ["list", "list", "list", None, "bool"]
    assert given[4] is False


def test_safe_path_keeps_the_first_entry_of_sys_path(project: Path, tmp_path: Path):
    """With PYTHONSAFEPATH the first entry is the PYTHONPATH of mbed-cli, not a script directory."""
    out = tmp_path / "cc.json"

    result = _run(project, out, "compile", "-t", "GCC_ARM", "-m", "BOARD", PYTHONSAFEPATH="1")

    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == _expected(project)


def test_a_failed_build_writes_no_database(project: Path, tmp_path: Path):
    out = tmp_path / "cc.json"

    result = _run(project, out, "compile", "-t", "GCC_ARM", "-m", "BOARD", "--fail")

    assert result.returncode == 3
    assert not out.exists()


def test_a_command_that_does_not_start_make_py_is_an_error(project: Path, tmp_path: Path):
    """No make.py run gives no database of the build, thus the script fails."""
    out = tmp_path / "cc.json"

    result = _run(project, out, "config", "--list")

    assert result.returncode == 1
    assert "started make.py 0 times" in result.stderr
    assert not out.exists()


def test_the_directory_of_the_script_holds_no_other_module():
    """Python puts this directory first on sys.path of mbed-cli and make.py.

    A second module there could hide a module of the standard library, of
    mbed-cli or of mbed-os.
    """
    names = {path.name for path in _COMPILE_DB_SCRIPT.parent.iterdir() if path.name != "__pycache__"}

    assert names == {"compile_db.py"}


def test_wrong_arguments_give_the_usage(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(_COMPILE_DB_SCRIPT), "record", str(tmp_path / "cc.json")],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 2
    assert "usage: compile_db.py" in result.stderr
