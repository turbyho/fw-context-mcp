"""A build belongs INSIDE the index lock, and it writes through a staging file.

Observed on 2026-09-23, on a project with a daemon:

    08:50:07  daemon        starts `fw-context index --background`
    08:50:11  that run      bear --output .fw-context/build/compile_commands.json ...
    08:50:17  the operator  fw-context index --build   (a second bear, same output)
    08:50:29  that run      Cannot parse compile_commands.json:
                            Expecting value: line 107842 column 7
    08:53     the operator  bear: error: Event processing failed: ... (os error 2)

``cmd_index`` called ``_resolve_compile_commands`` — the function that builds —
BEFORE it took ``index_run_lock``.  The lock covered the indexing only, thus
two runs of one project could build at the same time.  Both builds write one
canonical ``compile_commands.json``, because ``isolated_build_dir`` separates
the object files and not that file.

Two repairs, and these tests hold each one:

1. ``cmd_index`` builds inside the lock.  A run that loses the lock builds
   nothing at all.
2. ``generate_compile_commands`` writes a staging file and renames it.  The
   canonical file gets every byte at once, or it keeps what it had.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.exit_codes import EXIT_ALREADY_RUNNING
from fw_context_mcp.indexer.build import BuildConfig, generate_compile_commands
from fw_context_mcp.utils import (
    CC_OUTPUT_REL,
    cc_output_path,
    cc_staging_path,
    owner_token,
    staging_owner,
)

# ── Configuration stubs ──────────────────────────────────────────────────────


@dataclass
class _FakeIndexCfg:
    db_dir: Path
    vendor_paths: list = field(default_factory=list)
    project_paths: list = field(default_factory=list)


@dataclass
class _FakeBuildCfg:
    system: str | None = "makefile"
    variants: list = field(default_factory=list)


@dataclass
class _FakeCfg:
    index: _FakeIndexCfg
    build: _FakeBuildCfg = field(default_factory=_FakeBuildCfg)
    cache_server: None = None


# ── 1. The build runs inside the lock ────────────────────────────────────────


class TestTheBuildRunsUnderTheLock:
    """A run that cannot get the lock must not start a build.

    The build is the expensive step — minutes to an hour — and it is the step
    that writes the file two runs fought over.  A refusal that arrives after
    the build is thus no protection at all.
    """

    @staticmethod
    def _patch_cmd_index(monkeypatch, tmp_path: Path, cfg: _FakeCfg) -> list[str]:
        """Stub every step of ``cmd_index`` and record the order of two of them.

        The returned list gets ``"acquire"`` when ``cmd_index`` starts to take
        the index (and to take over from another run, when it may), and
        ``"build"`` when it resolves (and thus builds) compile_commands.json.
        The real ``_acquire_index`` runs, thus a refusal is the real one.
        """
        import fw_context_mcp.config as config_mod
        import fw_context_mcp.indexer.build as build_mod
        import fw_context_mcp.indexer.runner as runner_mod
        import fw_context_mcp.utils as utils_mod
        from fw_context_mcp.cli import _index as index_mod

        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text("[]", encoding="utf-8")

        events: list[str] = []

        monkeypatch.setattr(utils_mod, "resolve_project_root", lambda arg: tmp_path)
        monkeypatch.setattr(config_mod, "load", lambda project_root=None: cfg)
        monkeypatch.setattr(config_mod, "derive_project_id", lambda root: "pid")
        monkeypatch.setattr(build_mod, "detect_build_system", lambda root: "makefile")
        real_acquire = index_mod._acquire_index

        def _acquire(db_dir, stack, args):
            events.append("acquire")
            real_acquire(db_dir, stack, args)

        monkeypatch.setattr(index_mod, "_acquire_index", _acquire)
        monkeypatch.setattr(index_mod, "_build_run_kwargs", lambda *a, **kw: {})

        def _resolve(*a, **kw):
            events.append("build")
            return cc_path, True

        monkeypatch.setattr(index_mod, "_resolve_compile_commands", _resolve)
        monkeypatch.setattr(
            index_mod, "_validate_and_fix_artifacts", lambda *a, **kw: (cc_path, [], True)
        )
        monkeypatch.setattr(runner_mod, "run", lambda **kw: "0" * 64)
        monkeypatch.setattr(index_mod, "_post_index_optimize", lambda *a, **kw: None)
        monkeypatch.setattr(index_mod, "_record_still_uncovered", lambda *a, **kw: None)
        monkeypatch.setattr(index_mod, "_ensure_watcher_after_index", lambda root: None)
        return events

    @staticmethod
    def _args() -> SimpleNamespace:
        """The argv of ``fw-context index --build``, which is what the operator ran."""
        return SimpleNamespace(
            verbose=False,
            project=None,
            background=False,
            build=True,
            no_clean=False,
            force=False,
            takeover=False,
            vendor_paths=None,
            project_paths=None,
        )

    @staticmethod
    def _hold_the_lock(db_dir: Path) -> subprocess.Popen:
        """Start a process that holds ``index_run_lock`` and says when it does.

        A real second process is necessary: ``flock`` belongs to an open file
        description, thus a lock this process takes proves nothing about the
        exclusion the incident needed.
        """
        script = textwrap.dedent(
            f"""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
            from fw_context_mcp.indexer.db._locking import index_run_lock
            with index_run_lock(Path({str(db_dir)!r})):
                print("HOLDING", flush=True)
                time.sleep(30)
            """
        )
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "HOLDING"
        return proc

    def test_a_refused_run_builds_nothing(self, monkeypatch, tmp_path: Path):
        """THE regression test.  Before the fix this run built first, then lost."""
        from fw_context_mcp.cli._index import cmd_index

        cfg = _FakeCfg(index=_FakeIndexCfg(db_dir=tmp_path / "index"))
        events = self._patch_cmd_index(monkeypatch, tmp_path, cfg)

        holder = self._hold_the_lock(tmp_path / "index" / "pid")
        try:
            assert cmd_index(self._args()) == EXIT_ALREADY_RUNNING
        finally:
            holder.kill()
            holder.wait(timeout=10)

        assert "build" not in events, (
            "the refused run built anyway — two builds of one project write "
            "one compile_commands.json and destroy the output of each other"
        )

    def test_a_free_index_still_builds_and_indexes(self, monkeypatch, tmp_path: Path):
        """The lock must not stop the run it is supposed to let through."""
        from fw_context_mcp.cli._index import cmd_index

        cfg = _FakeCfg(index=_FakeIndexCfg(db_dir=tmp_path / "index"))
        events = self._patch_cmd_index(monkeypatch, tmp_path, cfg)

        assert cmd_index(self._args()) == 0
        assert events.count("build") == 1

    def test_the_index_is_owned_before_the_build(self, monkeypatch, tmp_path: Path):
        """``_acquire_index`` takes the lock and does the takeover, thus it must run first.

        A takeover after the build would give the background run the whole
        build to compete with, which is the incident itself.
        """
        from fw_context_mcp.cli._index import cmd_index

        cfg = _FakeCfg(index=_FakeIndexCfg(db_dir=tmp_path / "index"))
        events = self._patch_cmd_index(monkeypatch, tmp_path, cfg)

        assert cmd_index(self._args()) == 0
        assert events.index("acquire") < events.index("build")


# ── 2. The canonical file changes in one step ────────────────────────────────


class _GoodBuilder:
    """A backend that writes a complete compilation database."""

    PAYLOAD = '[{"file": "main.c"}]'

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        target = cc_output_path(project_root, cfg)
        target.write_text(self.PAYLOAD, encoding="utf-8")
        return target


class _FailingBuilder:
    """A backend that writes a part of the database and then fails.

    This is what a build looks like when a signal stops it, and what left a
    file that ended in mid-line on 2026-09-23.
    """

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        target = cc_output_path(project_root, cfg)
        target.write_text('[{"file": "ma', encoding="utf-8")
        raise RuntimeError("the build died")


@pytest.fixture
def fake_backend(monkeypatch):
    """Put one fake backend in the registry under the name ``fake``."""

    def _install(builder_cls) -> None:
        import fw_context_mcp.indexer.build as build_mod

        monkeypatch.setattr(
            build_mod,
            "_builder_registry",
            SimpleNamespace(
                get=lambda name: builder_cls if name == "fake" else None,
                keys=lambda: ["fake"],
            ),
        )

    return _install


class TestTheCanonicalFileChangesInOneStep:
    def test_the_backend_writes_a_staging_file(self, tmp_path: Path, fake_backend):
        """The build must not touch the canonical file while it runs."""
        seen: list[Path] = []

        class _Recorder:
            def build(self, project_root: Path, cfg: BuildConfig) -> Path:
                target = cc_output_path(project_root, cfg)
                seen.append(target)
                target.write_text("[]", encoding="utf-8")
                return target

        fake_backend(_Recorder)
        generate_compile_commands(tmp_path, BuildConfig(system="fake"))

        assert seen == [cc_staging_path(tmp_path)]

    def test_a_finished_build_reaches_the_canonical_file(self, tmp_path: Path, fake_backend):
        fake_backend(_GoodBuilder)
        result = generate_compile_commands(tmp_path, BuildConfig(system="fake"))

        canonical = tmp_path / CC_OUTPUT_REL
        assert result == canonical
        assert canonical.read_text(encoding="utf-8") == _GoodBuilder.PAYLOAD

    def test_a_failed_build_keeps_the_file_that_worked(self, tmp_path: Path, fake_backend):
        """The whole point of the rename: a dead build costs no good database."""
        canonical = tmp_path / CC_OUTPUT_REL
        canonical.parent.mkdir(parents=True)
        canonical.write_text('[{"file": "old.c"}]', encoding="utf-8")

        fake_backend(_FailingBuilder)
        with pytest.raises(RuntimeError, match="the build died"):
            generate_compile_commands(tmp_path, BuildConfig(system="fake"))

        assert canonical.read_text(encoding="utf-8") == '[{"file": "old.c"}]'

    def test_a_backend_that_writes_nothing_gives_a_readable_error(
        self, tmp_path: Path, fake_backend
    ):
        """Every caller of ``generate_compile_commands`` catches RuntimeError only.

        A backend that returns its output path and writes no file would make
        the rename raise FileNotFoundError, and that reaches the operator as a
        traceback.
        """

        class _SilentBuilder:
            def build(self, project_root: Path, cfg: BuildConfig) -> Path:
                return cc_output_path(project_root, cfg)

        fake_backend(_SilentBuilder)
        with pytest.raises(RuntimeError, match="no compilation database"):
            generate_compile_commands(tmp_path, BuildConfig(system="fake"))

    def test_a_left_file_with_the_same_pid_is_not_published(
        self, tmp_path: Path, fake_backend
    ):
        """A run that SIGKILL stopped leaves its staging file, and a PID comes back.

        The new run then owns a partial file with its own name.  A backend
        that reports success and writes nothing must not publish that file.
        """

        class _SilentBuilder:
            def build(self, project_root: Path, cfg: BuildConfig) -> Path:
                return cc_output_path(project_root, cfg)

        canonical = tmp_path / CC_OUTPUT_REL
        canonical.parent.mkdir(parents=True)
        canonical.write_text('[{"file": "old.c"}]', encoding="utf-8")
        cc_staging_path(tmp_path).write_text('[{"file": "ma', encoding="utf-8")

        fake_backend(_SilentBuilder)
        with pytest.raises(RuntimeError, match="no compilation database"):
            generate_compile_commands(tmp_path, BuildConfig(system="fake"))

        assert canonical.read_text(encoding="utf-8") == '[{"file": "old.c"}]'

    def test_the_cleanup_runs_before_the_build(self, tmp_path: Path, fake_backend):
        """The cleanup is part of each build, not only a function that exists."""
        fake_backend(_GoodBuilder)
        orphan = _staging_of(tmp_path, _token_of(2147483646))
        orphan.write_text("[]", encoding="utf-8")

        generate_compile_commands(tmp_path, BuildConfig(system="fake"))

        assert not orphan.exists()

    def test_a_failed_build_leaves_no_staging_file(self, tmp_path: Path, fake_backend):
        """A staging file holds a whole database — megabytes on a real project."""
        fake_backend(_FailingBuilder)
        with pytest.raises(RuntimeError, match="the build died"):
            generate_compile_commands(tmp_path, BuildConfig(system="fake"))

        assert list((tmp_path / CC_OUTPUT_REL).parent.glob(".compile_commands.*.json")) == []


class TestStagingPaths:
    def test_the_staging_file_is_beside_the_canonical_file(self, tmp_path: Path):
        """``os.replace`` is atomic only inside one directory."""
        assert cc_staging_path(tmp_path).parent == (tmp_path / CC_OUTPUT_REL).parent

    def test_the_name_holds_the_owner_token(self, tmp_path: Path):
        """The PID alone is not unique when a container shares the project."""
        token = staging_owner(cc_staging_path(tmp_path))
        assert token == owner_token()
        assert token.startswith(f"{os.getpid()}@")

    def test_the_name_hides_from_the_variant_scan(self, tmp_path: Path):
        """``compile_commands.<variant>.json`` is a variant.  A staging file is not.

        ``cli/_index._discover_existing_cc`` reads that name, and
        ``indexer/_embedding._cleanup_orphaned_cc_artifacts`` reads the same
        prefix.  A staging file that matched either would be read as a build.
        """
        assert cc_staging_path(tmp_path).name.startswith(".")

    def test_no_cfg_gives_the_canonical_path(self, tmp_path: Path):
        assert cc_output_path(tmp_path) == tmp_path / CC_OUTPUT_REL

    def test_a_cfg_with_a_staging_file_gives_that_file(self, tmp_path: Path):
        staging = tmp_path / ".fw-context" / "build" / ".compile_commands.1.json"
        assert cc_output_path(tmp_path, BuildConfig(cc_output=staging)) == staging


def _staging_of(tmp_path: Path, token: str) -> Path:
    """The staging file that the owner *token* would write in *tmp_path*."""
    return cc_staging_path(tmp_path).with_name(f".compile_commands.{token}.json")


def _token_of(pid: int | str) -> str:
    """The owner token that process *pid* of this PID namespace writes."""
    return f"{pid}@{owner_token().partition('@')[2]}"


class TestDeadStagingFiles:
    """A build that a signal stops leaves its staging file behind."""

    @staticmethod
    def _clear(tmp_path: Path) -> None:
        from fw_context_mcp.indexer.build import _clear_dead_staging_files

        _clear_dead_staging_files(cc_staging_path(tmp_path))

    def test_the_file_of_a_dead_process_goes(self, tmp_path: Path):
        orphan = _staging_of(tmp_path, _token_of(2147483646))
        orphan.write_text("[]", encoding="utf-8")

        self._clear(tmp_path)

        assert not orphan.exists()

    def test_a_host_name_with_dots_still_parses(self, tmp_path: Path):
        """A host name holds dots, thus the name must not split at each dot."""
        token = "2147483646@build.example.com-4026531836"
        assert staging_owner(_staging_of(tmp_path, token)) == token

    def test_the_file_of_another_pid_namespace_stays(self, tmp_path: Path):
        """A container and its host share the project, and each has its own lock.

        PID 2147483646 runs in no namespace here, but this process cannot see
        the PIDs of another namespace.  The file can thus belong to a live
        build in a container, and a delete would destroy that build.
        """
        foreign = _staging_of(tmp_path, "2147483646@another-host-1")
        foreign.write_text("[]", encoding="utf-8")

        self._clear(tmp_path)

        assert foreign.exists()

    def test_a_file_that_cannot_be_deleted_does_not_stop_the_build(self, tmp_path: Path):
        """An unlink in an ``except`` clause is not inside that ``try``.

        Its PermissionError would thus go past the next clause of the same
        ``try`` and stop the build with a traceback.
        """
        orphan = _staging_of(tmp_path, _token_of(2147483646))
        orphan.write_text("[]", encoding="utf-8")
        directory = orphan.parent
        directory.chmod(0o500)
        try:
            if os.access(directory, os.W_OK):
                pytest.skip("this user can write to a read-only directory (root)")
            self._clear(tmp_path)
        finally:
            directory.chmod(0o700)

        assert orphan.exists()

    def test_a_pid_too_large_for_pid_t_does_not_stop_the_build(self, tmp_path: Path):
        """``os.kill`` raises OverflowError for it, which is not an OSError."""
        odd = _staging_of(tmp_path, _token_of("9" * 30))
        odd.write_text("[]", encoding="utf-8")

        self._clear(tmp_path)

        assert odd.exists()

    def test_a_digit_that_int_refuses_does_not_stop_the_build(self, tmp_path: Path):
        """``"²".isdigit()`` is True, and ``int("²")`` raises ValueError."""
        odd = _staging_of(tmp_path, _token_of("²"))
        odd.write_text("[]", encoding="utf-8")

        self._clear(tmp_path)

        assert odd.exists()

    def test_a_name_in_the_old_format_stays(self, tmp_path: Path):
        """A name without an owner tag has an unknown source."""
        old = _staging_of(tmp_path, "2147483646")
        old.write_text("[]", encoding="utf-8")

        self._clear(tmp_path)

        assert old.exists()

    def test_the_file_of_a_live_process_stays(self, tmp_path: Path):
        """That build still writes it, and this one must not delete its output."""
        other = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            live = _staging_of(tmp_path, _token_of(other.pid))
            live.write_text("[]", encoding="utf-8")

            self._clear(tmp_path)

            assert live.exists()
        finally:
            other.kill()
            other.wait(timeout=10)

    def test_the_canonical_file_stays(self, tmp_path: Path):
        canonical = cc_output_path(tmp_path)
        canonical.write_text("[]", encoding="utf-8")

        self._clear(tmp_path)

        assert canonical.exists()
