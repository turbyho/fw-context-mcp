"""The pre-test of the test session: each component that the selected tests need must be there.

WHY: a test that did not find a tool skipped itself, and the run looked
green.  Measured 2026-11-12 on a Windows machine: without clang in PATH
190 tests skipped, without gcc 710 more, and a machine without the C
headers ran the degraded path of each macro test.  Nothing told the
operator that the run did not verify these parts.

The rule: a component that a selected test needs and that is missing
stops the whole run, with a message that names it and the repair.  A skip
is correct only for a test that does not apply to the machine: a POSIX
test on Windows, a symbolic link where the system refuses one, a file
system that keeps the case, a test of a permission that root ignores.

Three kinds of component:

* Always: the installation of fw-context (the checks of
  ``fw-context doctor`` that read no config, the clang program among
  them), the packages of the extra ``dev``, and git.
* By fixture, when a selected test takes a toolchain (``toolchain``,
  ``arm_toolchain``, ``sdk_toolchain``): the embedded SDKs of the tests
  (``tests/sdk/versions.toml``).  The install has the pinned versions, and
  each toolchain compiles a C and a C++ probe for the CPU of its board.
* By marker, when a selected test carries it: ``make``
  (a real make; the compiler comes from the test SDKs), ``ollama``, and
  ``litellm`` (a LiteLLM proxy with the models of the streaming tests).

WHY no check of the C and C++ headers of the host: no test parses with
them now.  A unit of a test has the driver of a toolchain of the SDKs,
which gives the system headers of its C library; a test that gives its
own target (``--target=``) parses with ``-ffreestanding`` and the headers
of clang that fw-context carries.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

#: The packages of the extra ``dev`` that a test imports, and that
#: fw-context itself does not need (pyproject.toml).
DEV_MODULES = ("fastapi", "uvicorn", "numpy", "sentence_transformers", "pip", "hatchling")

#: The markers that make a component necessary, when a selected test has one.
MARKER_COMPONENTS = ("make", "ollama", "litellm")

#: The LiteLLM proxy of the tests of the marker ``litellm``, and the models
#: that they ask it for.
LITELLM_BASE = "http://localhost:4000/v1"
LITELLM_MODELS = ("Deepseek-v4-flash", "glm-5.2-fp8", "glm-5.2-fp8-no-thinking")

_C_PROBE = "#include <stdint.h>\n#include <stdio.h>\n#include <string.h>\nint main(void) { return (int)strlen(\"\"); }\n"
_CXX_PROBE = "#include <cstdint>\n#include <functional>\n#include <vector>\nint main() { std::function<int()> f = [] { return 0; }; return f(); }\n"
_TIMEOUT_S = 60


@dataclass(frozen=True)
class Problem:
    """One component that the run needs and that is missing or does not work."""

    component: str
    message: str
    repair: str


#: The component of the tests that take a fixture of the test SDKs (``toolchain``, ``arm_toolchain``,
#: ``sdk_toolchain``, ``test_sdks``), and of the tests of the marker ``sdk_project``.
TEST_SDKS = "test_sdks"
_SDK_FIXTURES = frozenset({"toolchain", "arm_toolchain", "sdk_toolchain", "test_sdks"})
#: The marker of the tests that build a native project of the test SDKs (tests/test_sdk_projects.py).
_SDK_MARKER = "sdk_project"
#: The component and the marker of the extended set of the test SDKs (tests/test_extra_platforms.py).
EXTRA_SDKS = "extra_sdks"
_EXTRA_MARKER = "extra_platforms"


def selected_components(items: Iterable[pytest.Item]) -> set[str]:
    """Return the components that a selected test needs: those of ``MARKER_COMPONENTS``, and ``TEST_SDKS``."""
    needed: set[str] = set()
    for item in items:
        needed.update(name for name in MARKER_COMPONENTS if item.get_closest_marker(name) is not None)
        if _SDK_FIXTURES & set(getattr(item, "fixturenames", ())) or item.get_closest_marker(_SDK_MARKER):
            needed.add(TEST_SDKS)
        if item.get_closest_marker(_EXTRA_MARKER):
            needed.update((TEST_SDKS, EXTRA_SDKS))
    return needed


def check_environment(items: Iterable[pytest.Item], operator_home: str | None) -> list[Problem]:
    """Return a Problem for each component that the selected *items* need and that does not work.

    First the step of the first parse of a user: it unpacks the bundled
    clang headers (``_clang_resource``) into the directory of the session.
    Without it, the check ``clang-resource`` reports them as missing.  A
    probe of the test SDKs needs no check of the installation: the
    compilers of the SDKs are programs of their own.
    """
    from fw_context_mcp.deps._instructions import get_platform_info
    from fw_context_mcp.indexer._clang_resource import find_resource_include

    platform_ctx = get_platform_info()
    needed = selected_components(items)
    find_resource_include()
    problems = _installation() + _dev_packages() + _git(platform_ctx)
    with tempfile.TemporaryDirectory(prefix="fw-context-pretest-") as temp:
        work = Path(temp)
        if TEST_SDKS in needed:
            problems += _test_sdks(work)
        if EXTRA_SDKS in needed:
            problems += _extra_sdks()
        if "make" in needed:
            problems += _make(work, platform_ctx)
    if "ollama" in needed:
        problems += _ollama(operator_home)
    if "litellm" in needed:
        problems += _litellm()
    return problems


def report(problems: list[Problem]) -> str:
    """Return the text that stops the run: each problem with its repair."""
    lines = ["The test environment lacks components that the selected tests need:", ""]
    for problem in problems:
        lines.append(f"- {problem.component}: {problem.message}")
        lines.extend(f"    {line}" for line in problem.repair.splitlines())
    lines += [
        "",
        "Repair each one, then run the tests again.  A test of an optional component",
        "(markers make, ollama, litellm) can be left out with -m \"not <marker>\".",
    ]
    return "\n".join(lines)


def _installation() -> list[Problem]:
    """Each check of the installation of fw-context (``fw-context doctor``) that is not ok."""
    from fw_context_mcp.deps import installation_checks, run_full_check

    problems = []
    for result in run_full_check(subset=set(installation_checks())):
        if result.status != "ok":
            repair = result.instructions or (f"run: {result.fix_cmd}" if result.fix_cmd else "see fw-context doctor")
            problems.append(Problem(result.name, f"{result.status}: {result.message}", repair))
    return problems


def _dev_packages() -> list[Problem]:
    """Each package of the extra ``dev`` that is not installed.

    ``find_spec`` and not an import: ``sentence_transformers`` imports
    torch, which takes seconds.
    """
    missing = [name for name in DEV_MODULES if importlib.util.find_spec(name) is None]
    if not missing:
        return []
    return [Problem("dev packages", f"not installed: {', '.join(missing)}", "make install   (pip install -e \".[dev]\")")]


def _git(platform_ctx: dict) -> list[Problem]:
    """git runs: the tests of the project root, of .gitignore and of the build layout run it."""
    system = platform_ctx.get("system", sys.platform)
    pkg = platform_ctx.get("pkg_manager", "unknown")
    if system == "darwin":
        repair = "xcode-select --install   (the Command Line Tools hold git)"
    elif system == "win32":
        repair = "winget install Git.Git"
    else:
        repair = {"apt": "sudo apt install git", "pacman": "sudo pacman -S git", "dnf": "sudo dnf install git"}.get(
            pkg, "the git package of your distribution")
    git = shutil.which("git")
    if git is None:
        return [Problem("git", "git is not in PATH", repair)]
    reason = _run([git, "--version"], Path.cwd())
    return [] if reason is None else [Problem("git", reason, repair)]


def _write_probes(work: Path) -> tuple[Path, Path]:
    """Write the C and the C++ probe into *work*; return their paths."""
    c_probe, cxx_probe = work / "probe.c", work / "probe.cpp"
    c_probe.write_text(_C_PROBE, encoding="utf-8")
    cxx_probe.write_text(_CXX_PROBE, encoding="utf-8")
    return c_probe, cxx_probe


def _run(argv: list[str], cwd: Path) -> str | None:
    """Run *argv*; return None when it succeeded, else the reason."""
    try:
        result = subprocess.run(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"`{argv[0]}` does not run: {exc}"
    if result.returncode != 0:
        output = (result.stderr or result.stdout).decode("utf-8", "replace").strip().splitlines()
        return f"`{' '.join(argv)}` ended with exit {result.returncode}: " + " | ".join(output[:3])
    return None


def _test_sdks(work: Path) -> list[Problem]:
    """Check the install of the test SDKs: its stamp, the tools of its venv, and each toolchain.

    The stamp holds the fingerprint of the files that pin the versions
    (``tests.sdk.fingerprint``).  A different fingerprint means that the
    install has other versions than the tests expect, and the expected
    values of the tests are for those versions.  Each toolchain compiles
    the C and the C++ probe with the flags of its board: this needs its
    C library and its C++ library.
    """
    from tests import sdk
    from tests._toolchains import find_driver

    root = sdk.sdk_root()
    repair = f"{sdk.PROVISION_COMMAND}   (make test-sdk; the root is {root}, environment variable {sdk.SDK_ROOT_ENV})"
    too_long = sdk.windows_root_too_long(root)
    if too_long is not None:
        return [Problem("test SDKs", too_long, repair)]
    stamp = sdk.read_stamp(root)
    if stamp is None:
        return [Problem("test SDKs", f"no complete install of the test SDKs in {root}", repair)]
    if stamp.get("fingerprint") != sdk.fingerprint():
        return [Problem("test SDKs", f"the install in {root} has other versions than tests/sdk/versions.toml", repair)]
    problems = [
        Problem("test SDKs", f"{program} is not in {sdk.venv_bin(root)}", repair)
        for program in ("west", "pio", "cmake", "ninja")
        if shutil.which(program, path=str(sdk.venv_bin(root))) is None
    ]
    versions = sdk.load_versions()
    # The venv must run the pinned Python of the root: the build of Zephyr
    # refused an older one (tests.sdk.provision.ensure_python).  A missing
    # venv is the problem of its tools above.
    if sdk.venv_python(root).exists():
        venv_version = sdk.python_version_of(sdk.venv_python(root))
        if venv_version != versions.python.version or not sdk.venv_is_of_the_pinned_python(root):
            problems.append(Problem(
                "test SDKs",
                f"the venv {sdk.venv_bin(root).parent} runs Python {venv_version or '(none)'} of "
                f"{sdk.venv_home(root) or 'an unknown base'}, not the Python {versions.python.version} "
                f"of tests/sdk/versions.toml in {sdk.python_dir(root)}",
                repair,
            ))
    c_probe, cxx_probe = _write_probes(work)
    for spec in versions.toolchains:
        component = f"toolchain {spec.id}"
        try:
            bin_dir = sdk.toolchain_bin(root, versions, spec)
        except sdk.SdkError as exc:
            problems.append(Problem(component, str(exc), repair))
            continue
        for probe, driver in ((c_probe, "gcc"), (cxx_probe, "g++")):
            found = find_driver(bin_dir, spec.prefix, driver)
            if found is None:
                problems.append(Problem(component, f"no {spec.prefix}{driver} in {bin_dir}", repair))
                continue
            obj = work / f"probe-{spec.id}-{driver}.o"
            reason = _run([str(found), *spec.flags, "-c", str(probe), "-o", str(obj)], work)
            if reason is not None:
                problems.append(Problem(component, reason, repair))
        # The archiver of the static libraries of tests/test_builder_projects.py.
        if find_driver(bin_dir, spec.prefix, "ar") is None:
            problems.append(Problem(component, f"no {spec.prefix}ar in {bin_dir}", repair))
    return problems


def _extra_sdks() -> list[Problem]:
    """Check the install of the extended set: its mark in the stamp, the export script of ESP-IDF, and arduino-cli.

    The mark holds the fingerprint of the extended set
    (``tests.sdk.extra_fingerprint``): another value means other versions
    than the tests expect.
    """
    from tests import sdk

    root = sdk.sdk_root()
    repair = f"{sdk.PROVISION_COMMAND} --extra   (make test-sdk-platforms; the root is {root})"
    stamp = sdk.read_stamp(root) or {}
    if stamp.get("extra_fingerprint") != sdk.extra_fingerprint():
        return [Problem("extended test SDKs", f"no install of the extended set of tests/sdk/versions.toml in {root}", repair)]
    versions = sdk.load_versions()
    problems = []
    if not sdk.esp_idf_export_script(root, versions).is_file():
        problems.append(Problem("extended test SDKs", f"no {sdk.esp_idf_export_script(root, versions)}", repair))
    if shutil.which("arduino-cli", path=str(sdk.arduino_cli_dir(root))) is None:
        problems.append(Problem("extended test SDKs", f"no arduino-cli in {sdk.arduino_cli_dir(root)}", repair))
    return problems


def _make(work: Path, platform_ctx: dict) -> list[Problem]:
    """Run a recipe with make, as the tests of the marker ``make`` do.

    The recipe compiles nothing: those tests give make the compiler of a
    toolchain of the test SDKs in ``CC`` (the fixture ``toolchain``, and the
    check of the test SDKs), as the Makefile of a firmware sets it.
    """
    system = platform_ctx.get("system", sys.platform)
    pkg = platform_ctx.get("pkg_manager", "unknown")
    if system == "darwin":
        repair = "xcode-select --install   (the Command Line Tools hold make)"
    elif system == "win32":
        repair = "winget install ezwinports.make"
    else:
        repair = {"apt": "sudo apt install make", "pacman": "sudo pacman -S make",
                  "dnf": "sudo dnf install make"}.get(pkg, "the make package of your distribution")
    make = shutil.which("make")
    if make is None:
        return [Problem("make", "make is not in PATH", repair)]
    (work / "Makefile").write_text("probe-make:\n\techo ok > probe-make\n", encoding="utf-8")
    reason = _run([make, "-f", "Makefile", "probe-make"], work)
    return [] if reason is None else [Problem("make", reason, repair)]


def _ollama(operator_home: str | None) -> list[Problem]:
    """Check the Ollama of the global config of the operator: the tests of ``ollama`` use it.

    WHY the config of the operator: ``pytest_runtest_protocol`` gives these
    tests the copy of it with the servers of the LLM, thus the check asks
    the server that the tests ask.
    """
    import fw_context_mcp.config.settings as settings
    from fw_context_mcp.config import ConfigError, load
    from fw_context_mcp.deps._checks import check_ollama_running

    if not operator_home:
        return [Problem("ollama", "the session has no copy of the global config", "see tests/conftest.py")]
    prev_home, prev_path = os.environ.get("FW_CONTEXT_HOME"), settings._GLOBAL_CONFIG_PATH
    os.environ["FW_CONTEXT_HOME"] = operator_home
    settings._GLOBAL_CONFIG_PATH = Path(operator_home) / "config.toml"
    settings._config_cache.clear()
    try:
        result = check_ollama_running(load(None).llm)
    except ConfigError as exc:
        return [Problem("ollama", f"the global config cannot be used: {exc}", "fw-context doctor --only config")]
    finally:
        if prev_home is None:
            os.environ.pop("FW_CONTEXT_HOME", None)
        else:
            os.environ["FW_CONTEXT_HOME"] = prev_home
        settings._GLOBAL_CONFIG_PATH = prev_path
        settings._config_cache.clear()
    if result.status == "ok":
        return []
    return [Problem("ollama", f"{result.status}: {result.message}", result.instructions or "start Ollama")]


def _litellm() -> list[Problem]:
    """The LiteLLM proxy answers, and it serves each model of ``LITELLM_MODELS``."""
    import httpx

    from fw_context_mcp import net

    repair = f"start the proxy with these models: litellm --config config.yaml   ({', '.join(LITELLM_MODELS)})"
    try:
        response = net.get(f"{LITELLM_BASE}/models", timeout=5.0)
        response.raise_for_status()
        served = {model.get("id", "") for model in response.json().get("data", [])}
    except (httpx.HTTPError, ValueError) as exc:
        return [Problem("litellm", f"the proxy at {LITELLM_BASE} does not answer: {exc}", repair)]
    missing = [model for model in LITELLM_MODELS if model not in served]
    if missing:
        return [Problem("litellm", f"the proxy serves no {', '.join(missing)}", repair)]
    return []
