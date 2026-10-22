"""Shared fixtures for fw-context-mcp tests.

Provides:
- Standard fixtures: tmpdir, temp_db, populated_db, isolation, etc.
- Auto-skip for missing dependencies (Ollama, gcc/libclang)
- Test result logging to tests/results/
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# ── Result logging ─────────────────────────────────────────────────────────

_RESULTS_DIR = Path(__file__).resolve().parent / "results"

log = logging.getLogger(__name__)


def _ensure_results_dir() -> Path:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return _RESULTS_DIR


def _check_ollama() -> bool:
    """Check if Ollama is running and reachable."""
    try:
        import httpx
        resp = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


def _check_gcc() -> bool:
    """Check if gcc is available on PATH."""
    return shutil.which("gcc") is not None


# Cache checks so we don't spawn subprocesses / HTTP calls for every test
_OLLAMA_AVAILABLE: bool | None = None
_GCC_AVAILABLE: bool | None = None


def ollama_available() -> bool:
    global _OLLAMA_AVAILABLE
    if _OLLAMA_AVAILABLE is None:
        _OLLAMA_AVAILABLE = _check_ollama()
    return _OLLAMA_AVAILABLE


def gcc_available() -> bool:
    global _GCC_AVAILABLE
    if _GCC_AVAILABLE is None:
        _GCC_AVAILABLE = _check_gcc()
    return _GCC_AVAILABLE


# ── Auto-skip hooks ────────────────────────────────────────────────────────

_SESSION_START: float | None = None


def pytest_configure(config):
    """Ensure results directory exists, and isolate the session (``_isolate_session``)."""
    _ensure_results_dir()
    _isolate_session()


def pytest_unconfigure(config):
    """End the isolation of the session (``_end_isolation``)."""
    _end_isolation()


def pytest_terminal_summary(terminalreporter):
    """Name each process that the session left running (``_stop_leftover_processes``): a test that leaks one."""
    lines = _isolation.get("leftovers") or []
    if lines:
        terminalreporter.section("processes that the test session left running (stopped)")
        for line in lines:
            terminalreporter.write_line(line)


def pytest_sessionstart(session):
    """Record session start time for duration tracking."""
    global _SESSION_START
    _SESSION_START = time.monotonic()


def pytest_collection_modifyitems(config, items):
    """Auto-skip tests that require unavailable dependencies.

    - Tests marked ``ollama`` are skipped when Ollama is not running.
    - Tests marked ``libclang`` are skipped when gcc is not on PATH.
    """
    skip_ollama = pytest.mark.skip(reason="Ollama not available — install and start it for these tests")
    skip_libclang = pytest.mark.skip(reason="gcc not available — install gcc for indexing tests")

    for item in items:
        if item.get_closest_marker("ollama"):
            if not ollama_available():
                item.add_marker(skip_ollama)

        if item.get_closest_marker("libclang"):
            if not gcc_available():
                item.add_marker(skip_libclang)


def pytest_sessionfinish(session, exitstatus):
    """Stop the processes that the session left running, and write test run summary to tests/results/<timestamp>.json."""
    global _SESSION_START

    operator_home = os.environ.get(_OPERATOR_HOME_ENV)
    if _isolation and operator_home:
        _isolation["leftovers"] = _stop_leftover_processes(operator_home)

    results_dir = _ensure_results_dir()
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    result_file = results_dir / f"test_run_{timestamp}.json"

    failed = getattr(session, "testsfailed", 0)
    collected = session.testscollected
    duration = round(time.monotonic() - _SESSION_START, 3) if _SESSION_START else None

    summary = {
        "timestamp": datetime.now(UTC).isoformat(),
        "exit_code": exitstatus,
        "tests_passed": collected - failed,
        "tests_failed": failed,
        "tests_collected": collected,
        "duration_s": duration,
        "ollama_available": ollama_available(),
        "gcc_available": gcc_available(),
    }

    result_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Also write latest.json for convenience
    latest = results_dir / "latest.json"
    latest.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ── Standard fixtures ──────────────────────────────────────────────────────


#: The temp home with the copy of the global config for the tests of the mark
#: ``ollama``.  Also the marker of the processes of this session: no test
#: changes it (``_stop_leftover_processes``).
_OPERATOR_HOME_ENV = "FW_CONTEXT_TEST_OPERATOR_HOME"
#: The profile of the embeddings in the isolated config (``_write_isolated_config``).
_TEST_EMBED_PROFILE = "test-embeddings"
#: The variables that the isolation sets; each gets its value of before back.
_ISOLATION_ENV = (
    "FW_CONTEXT_INDEX_DIR", "FW_CONTEXT_PROJECTS_DB", "FW_CONTEXT_CLANG_RESOURCE_DIR", "FW_CONTEXT_HOME",
    "FW_CONTEXT_NO_UPDATE_CHECK", "PYTHON_KEYRING_BACKEND", "FW_CONTEXT_NO_OLLAMA_RELEASE", _OPERATOR_HOME_ENV,
)
#: The state of the isolation between pytest_configure and pytest_unconfigure:
#: ``temp`` (the TemporaryDirectory), ``env`` (the values of before),
#: ``global_path`` (the global config of the operator), ``unreachable_url``,
#: ``leftovers`` (the lines of ``_stop_leftover_processes``).
_isolation: dict[str, Any] = {}


def _unreachable_url() -> str:
    """Return the URL of a port of this machine that no server listens on: a request gets "connection refused".

    The kernel gives a free port to a bind on port 0, and the socket closes
    at once.  WHY not a fixed port: a discard service on port 9 accepts the
    connection and never answers, and each request of a test then waits
    for its timeout.
    """
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _isolate_session() -> None:
    """Redirect every fw-context write of this run to a session temp dir, before the first test.

    WHY ``pytest_configure`` and not a session fixture: a session fixture
    starts in the setup of the first test, thus inside its protocol, and
    the wrapper of the tests of ``ollama`` (``pytest_runtest_protocol``)
    then saw no temp home for that test.  ``pytest -m ollama`` failed.

    Two stores, and both need the redirect:

    * ``FW_CONTEXT_INDEX_DIR`` — the index databases.
    * ``FW_CONTEXT_PROJECTS_DB`` — the global project registry.

    Only the first one had it.  The registry therefore took a row for
    every throwaway project that this suite registers, in the registry of
    the operator: measured on one machine, 28989 rows of which 6 named a
    project that exists.  A row carries a NAME, and two rows of one name
    make the ``project="<name>"`` selector fail — which is how it was
    found.

    A third store came later: ``FW_CONTEXT_CLANG_RESOURCE_DIR``, where the
    bundled clang headers are unpacked.  Without it a test that runs
    ``doctor --fix`` or a parse wrote ``~/.fw-context/clang-resource`` of
    the operator.

    A fourth: ``FW_CONTEXT_HOME``, the global directory (the global config,
    the local LLM cache ``llm_cache.db``, and the files that
    ``housekeeping.clean`` removes there).  A test that
    ran ``fw-context index`` as a process removed a file from
    ``~/.fw-context`` of the operator.  The session gets a copy of the
    global config of the operator without its servers
    (``_write_isolated_config``), and a write goes to the copy.  A test of
    the mark ``ollama`` gets a copy with the servers of the LLM, so that it
    uses the models of the operator.

    A fifth is not a store: ``FW_CONTEXT_NO_UPDATE_CHECK`` stops the
    request to PyPI (``version_check``).  An MCP server that a test starts
    as a process must not send a request from the test machine.  The tests
    of the check remove the variable with ``monkeypatch``.

    A sixth: the keyring of the operator.  ``PYTHON_KEYRING_BACKEND``
    selects the backend that fails each call, for each subprocess, and
    ``keyring.set_keyring`` does the same in this process.  A test that
    stores an API key thus writes ``secrets.toml`` of the temp home, never
    an entry of the keyring of the operator.  A test of the keyring itself
    sets its own backend in memory.  The isolation does not put the backend
    of the operator back at the end: to get it, ``keyring`` connects to it,
    and the process ends with the session.

    A seventh: ``FW_CONTEXT_NO_OLLAMA_RELEASE``.  No test unloads a model
    of the Ollama of the operator (``llm.ollama.release_other_model``), and
    no test loads one to select the context of an analysis
    (``indexer._llm_analysis.ContextChoice``).

    All variables are inherited by any subprocess a test spawns, thus a
    CLI invocation stays isolated too.  The temp dir is removed at the end
    of the session (``_end_isolation``).
    """
    import keyring
    from keyring.backends import fail

    import fw_context_mcp.config.settings as settings

    # ignore_cleanup_errors: a process of a test that a SIGKILL stopped can
    # still hold a file in it at the end of the session.
    temp = tempfile.TemporaryDirectory(prefix="fw-context-index-", ignore_cleanup_errors=True)
    d = temp.name
    home, operator_home = Path(d) / "home", Path(d) / "operator-home"
    home.mkdir()
    operator_home.mkdir()
    source = settings._GLOBAL_CONFIG_PATH
    url = _unreachable_url()
    _write_isolated_config(source, home / "config.toml", keep_llm_servers=False, url=url)
    _write_isolated_config(source, operator_home / "config.toml", keep_llm_servers=True, url=url)
    _isolation.update(temp=temp, env={name: os.environ.get(name) for name in _ISOLATION_ENV},
                      global_path=source, unreachable_url=url)
    os.environ[_OPERATOR_HOME_ENV] = str(operator_home)
    os.environ["FW_CONTEXT_INDEX_DIR"] = d
    os.environ["FW_CONTEXT_PROJECTS_DB"] = str(Path(d) / "projects.db")
    os.environ["FW_CONTEXT_CLANG_RESOURCE_DIR"] = str(Path(d) / "clang-resource")
    os.environ["FW_CONTEXT_HOME"] = str(home)
    os.environ["FW_CONTEXT_NO_UPDATE_CHECK"] = "1"
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    os.environ["FW_CONTEXT_NO_OLLAMA_RELEASE"] = "1"
    keyring.set_keyring(fail.Keyring())
    # The module computed the path at import, before this hook.
    settings._GLOBAL_CONFIG_PATH = home / "config.toml"
    settings._config_cache.clear()


@contextmanager
def operator_environ(*names: str, global_config: bool = False) -> Iterator[None]:
    """Give *names* the values of before the isolation, for the body: the live tests READ the index of the operator.

    *global_config* also gives the global config of the operator back:
    its ``[index] db_dir`` names the index directory, and the isolated copy
    has none.  Only when it is there: ``load`` would else make it.

    WHY here: the isolation starts in ``pytest_configure``, before the
    collection, thus a test module cannot read the values of before at its
    import any more.

    Raises:
        KeyError: A name that the isolation does not set (a typo would else
            remove a variable without a word).
    """
    import fw_context_mcp.config.settings as settings

    unknown = [name for name in names if name not in _ISOLATION_ENV]
    if unknown:
        raise KeyError(f"not a variable of the isolation: {', '.join(unknown)}")
    saved = {name: os.environ.get(name) for name in names}
    saved_path = settings._GLOBAL_CONFIG_PATH
    before = _isolation.get("env", {})
    for name in names:
        if before.get(name) is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = before[name]
    if global_config and _isolation.get("global_path") is not None and _isolation["global_path"].is_file():
        settings._GLOBAL_CONFIG_PATH = _isolation["global_path"]
    settings._config_cache.clear()
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        settings._GLOBAL_CONFIG_PATH = saved_path
        settings._config_cache.clear()


def _end_isolation() -> None:
    """Give each variable and the global config path their values of before, and remove the temp dir."""
    import fw_context_mcp.config.settings as settings

    if not _isolation:
        return
    settings._GLOBAL_CONFIG_PATH = _isolation["global_path"]
    settings._config_cache.clear()
    for name, value in _isolation["env"].items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    _isolation.pop("temp").cleanup()
    _isolation.clear()


def _write_isolated_config(source: Path, target: Path, *, keep_llm_servers: bool, url: str) -> None:
    """Write the global config of the test session: the copy of *source* without a server of the operator.

    WHY: the copy keeps the models of the operator, thus a test that
    indexes gets a valid config.  Measured 2026-10-21: with the unchanged
    copy, ``analyze_symbols = true`` sent the LLM analysis of each
    ``fw-context index`` of the system tests to the Ollama of the operator
    (a load of the model, 43 s), and the embeddings and the cache server
    went the same way.

    Each copy loses ``[cache_server]`` (a server of the operator, with a
    token) and ``[index] db_dir`` (a value that is not the default wins
    over ``FW_CONTEXT_INDEX_DIR``).  *keep_llm_servers* False (each test
    but the tests of ``ollama``) also changes the LLM:

    - Each profile but ``chatgpt`` gets *url*, which refuses the
      connection, and ``api_key_env = ""``: the shell of the operator can
      hold the key of an API.  A ``chatgpt`` profile has no URL, and the
      temp home has no sign-in.  A selected ``chatgpt`` profile gives the
      selection to the Ollama profile at *url*: else each ``load`` names an
      external host, and the tests depend on the selection of the operator.
    - ``embed_profile`` names an Ollama profile at that address: without
      it, a selected API profile sends the embeddings to the default
      ``http://localhost:11434``.
    - The keys of the old format (``ollama_url``, ``chat_api_base``...) go:
      a test that runs ``init`` or ``doctor --fix`` would make a profile of
      them with the real server.
    - ``analyze_symbols = false``: an analysis cannot reach a server, and
      each symbol would cost three attempts.  A test that needs one sets it.

    Without a global config, or with one that is not valid TOML, the copy
    has one Ollama profile at that address: ``load`` would else make the
    default config, whose Ollama is the one of this machine.
    """
    import tomllib

    import tomli_w

    from fw_context_mcp.config.settings import _MOVED_LLM_KEYS

    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        log.warning("the global config cannot be read (%s): the test session uses a config of its own", e)
        data = {}
    data.pop("cache_server", None)
    index = data.get("index")
    if isinstance(index, dict):
        index.pop("db_dir", None)
    if not keep_llm_servers:
        llm = data.get("llm")
        llm = llm if isinstance(llm, dict) else {}
        data["llm"] = llm
        for key in _MOVED_LLM_KEYS:
            llm.pop(key, None)
        profiles = llm.get("profiles")
        profiles = profiles if isinstance(profiles, dict) else {}
        llm["profiles"] = profiles
        if not profiles:
            profiles["local"] = {"provider": "ollama", "model": "qwen2.5-coder:7b"}
        if "profile" not in llm and len(profiles) == 1:
            # The extra profile of the embeddings below must not make the
            # selection of the only profile ambiguous.
            llm["profile"] = next(iter(profiles))
        for profile in profiles.values():
            if isinstance(profile, dict) and profile.get("provider") != "chatgpt":
                profile["url"] = url
                if profile.get("provider") != "ollama":
                    profile["api_key_env"] = ""
        profiles[_TEST_EMBED_PROFILE] = {"provider": "ollama", "model": "qwen2.5-coder:7b", "url": url}
        llm["embed_profile"] = _TEST_EMBED_PROFILE
        selected = profiles.get(llm.get("profile"))
        if isinstance(selected, dict) and selected.get("provider") == "chatgpt":
            llm["profile"] = _TEST_EMBED_PROFILE
        llm["analyze_symbols"] = False
    elif not data:
        return  # no config of the operator: the tests of ollama get the default config
    target.write_text(tomli_w.dumps(data), encoding="utf-8")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Give a test of the mark ``ollama`` the copy of the global config with the servers of the LLM.

    WHY the whole protocol and not a fixture: a fixture of a larger scope
    (a class of these tests indexes once) starts in the setup of the first
    test, before an autouse fixture of the test.
    """
    import fw_context_mcp.config.settings as settings

    operator_home = os.environ.get(_OPERATOR_HOME_ENV)
    if item.get_closest_marker("ollama") is None or not operator_home:
        return (yield)
    prev_home, prev_path = os.environ.get("FW_CONTEXT_HOME"), settings._GLOBAL_CONFIG_PATH
    os.environ["FW_CONTEXT_HOME"] = operator_home
    settings._GLOBAL_CONFIG_PATH = Path(operator_home) / "config.toml"
    settings._config_cache.clear()
    try:
        return (yield)
    finally:
        if prev_home is None:
            os.environ.pop("FW_CONTEXT_HOME", None)
        else:
            os.environ["FW_CONTEXT_HOME"] = prev_home
        settings._GLOBAL_CONFIG_PATH = prev_path
        settings._config_cache.clear()


def _stop_leftover_processes(marker_value: str) -> list[str]:
    """Stop each process that this session started and that still runs; return a line for each.

    A test that runs ``fw-context index`` as a process starts the watcher
    daemon (``cli._index._ensure_watcher_after_index``), and no test stops
    it.  Measured 2026-10-21: the daemon of a test project lived on after
    the session, saw the file that the test deleted, and started
    ``fw-context index --background``.

    WHY the environment and not the PID files: a test can set its own
    ``db_dir``, and a PID file can hold a PID that a test wrote on purpose.
    ``FW_CONTEXT_TEST_OPERATOR_HOME`` holds a temp dir of this session
    only, each process that the session started inherits it, and no test
    changes it.  SIGTERM first; a process that still runs after 5 s, and
    still has the marker (its PID can be of a new process now), gets
    SIGKILL and 1 s more: the temp dir goes next.  A zombie (a child of
    this process that ended) counts as stopped.  Linux only
    (``/proc/<pid>/environ``); elsewhere nothing is stopped.
    """
    import signal

    marker = f"{_OPERATOR_HOME_ENV}={marker_value}".encode()
    leftovers: dict[int, str] = {}
    for proc in Path("/proc").glob("[0-9]*"):
        pid = int(proc.name)
        if pid == os.getpid():
            continue
        if _has_marker(pid, marker):
            try:
                leftovers[pid] = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue  # gone
    for pid in leftovers:
        _signal_process(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and any(_running(pid) for pid in leftovers):
        time.sleep(0.05)
    killed = [pid for pid in leftovers if _running(pid) and _has_marker(pid, marker)]
    for pid in killed:
        _signal_process(pid, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and any(_running(pid) for pid in killed):
        time.sleep(0.05)
    return [f"PID {pid} ({'SIGKILL' if pid in killed else 'SIGTERM'}): {cmdline.strip()[:200]}"
            for pid, cmdline in leftovers.items()]


def _has_marker(pid: int, marker: bytes) -> bool:
    """Return True when the environment of *pid* holds *marker* as one variable."""
    try:
        return marker in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False  # gone, or a process of another user


def _signal_process(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except OSError:
        pass  # ended, or no permission: nothing more to do in the teardown of the session


def _running(pid: int) -> bool:
    """Return True when *pid* runs: it exists and is not a zombie."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


@pytest.fixture(autouse=True)
def _no_model_list_requests(monkeypatch):
    """No test reads the model list of a real provider (``llm.model_list``).

    WHY: the setup, the selection of a profile, ``llm test``, doctor and
    the analysis read the list of DeepSeek, OpenRouter or the ChatGPT plan
    with a GET.  Without this guard a test sent that GET to the real host,
    and its result depended on the network and on the keys of the operator.
    A test that needs a list replaces ``provider_models`` or
    ``model_context``.  The caches go too: a list of one test must not
    answer for the next one.
    """
    from fw_context_mcp.llm import model_list

    def _blocked(llm, url):
        raise model_list.ModelListError(f"no request to {url} in the test session")

    monkeypatch.setattr(model_list, "_get", _blocked)
    monkeypatch.setattr(model_list, "_LISTS", {})
    monkeypatch.setattr(model_list, "_ENDPOINT_CONTEXTS", {})
    from fw_context_mcp.llm import chatgpt_plan

    def _blocked_plan(profile):
        raise model_list.ModelListError("no request to the model list of the ChatGPT plan in the test session")

    monkeypatch.setattr(chatgpt_plan, "model_contexts", _blocked_plan)


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def fake_arm_gcc_toolchain(tmpdir):
    """Create a minimal fake ARM GCC toolchain directory structure."""
    toolchain = tmpdir / "gcc-arm-none-eabi-9-2020-q2-update"
    bin_dir = toolchain / "bin"
    bin_dir.mkdir(parents=True)

    compiler = bin_dir / "arm-none-eabi-g++"
    compiler.touch()

    gcc_include = toolchain / "lib" / "gcc" / "arm-none-eabi" / "12.3.1" / "include"
    gcc_include.mkdir(parents=True)
    gcc_include_fixed = toolchain / "lib" / "gcc" / "arm-none-eabi" / "12.3.1" / "include-fixed"
    gcc_include_fixed.mkdir(parents=True)

    libc_include = toolchain / "arm-none-eabi" / "include"
    libc_include.mkdir(parents=True)

    cxx_include = toolchain / "arm-none-eabi" / "include" / "c++" / "12.3.1"
    cxx_include.mkdir(parents=True)

    return toolchain, compiler


@pytest.fixture
def compile_commands_json(tmpdir):
    """Create a minimal compile_commands.json for testing."""
    data = [
        {
            "directory": str(tmpdir),
            "file": "src/main.cpp",
            "arguments": [
                "arm-none-eabi-g++",
                "-std=c++14",
                "-mcpu=cortex-m4",
                "-Os",
                "-DFOO=1",
                "-Isrc",
                "-o", "build/main.o",
                "-MD",
                "-MF", "build/main.d",
                "src/main.cpp",
            ],
        },
        {
            "directory": str(tmpdir),
            "file": "lib/helper.c",
            "arguments": [
                "arm-none-eabi-gcc",
                "-std=c11",
                "-O2",
                "-Ilib",
                "-o", "build/helper.o",
                "-MP",
                "lib/helper.c",
            ],
        },
    ]
    path = tmpdir / "compile_commands.json"
    path.write_text(__import__("json").dumps(data))
    return path


@pytest.fixture
def temp_db(tmpdir):
    """Create an empty SQLite database for testing."""
    from fw_context_mcp.indexer.db import open_db

    db_path = tmpdir / "test.db"
    conn = open_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def populated_db(temp_db):
    """Database with a project and build config pre-inserted."""
    from fw_context_mcp.indexer.db import transaction, upsert_build_config, upsert_project

    with transaction(temp_db):
        upsert_project(temp_db, "proj-001", "test-project", "/tmp/test-project")
        upsert_build_config(temp_db, "hash-deadbeef", "proj-001", "/tmp/compile_commands.json")

    return temp_db


@pytest.fixture
def isolation():
    """Isolate config file writes by redirecting to a temp directory."""
    import fw_context_mcp.config.settings as settings

    old_global = settings._GLOBAL_CONFIG_PATH
    old_project_dir = settings._PROJECT_CONFIG_DIR

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        settings._GLOBAL_CONFIG_PATH = tmp / "global.toml"
        settings._PROJECT_CONFIG_DIR = ".fw-context-test"
        yield tmp
        settings._GLOBAL_CONFIG_PATH = old_global
        settings._PROJECT_CONFIG_DIR = old_project_dir
