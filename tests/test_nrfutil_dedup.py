"""One binary behind two names must be probed once."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fw_context_mcp.indexer.builders.zephyr import ZephyrBuildSystem


@pytest.fixture
def probe_counter(monkeypatch):
    """Count the probes and answer no, so every candidate is tried."""
    seen: list[str] = []

    def fake_probe(candidate: str) -> bool:
        seen.append(candidate)
        return False

    monkeypatch.setattr(
        ZephyrBuildSystem, "_provides_sdk_manager", staticmethod(fake_probe)
    )
    return seen


def test_a_symlinked_binary_is_probed_once(tmp_path: Path, monkeypatch, probe_counter):
    """/usr/bin/nrfutil and /bin/nrfutil are one file behind a symlink.

    Keyed by the literal string they passed as two candidates, and each one
    then paid its own subprocess.run(..., timeout=15) probe.
    """
    real_dir = tmp_path / "usr" / "bin"
    real_dir.mkdir(parents=True)
    real = real_dir / "nrfutil"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)

    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "nrfutil").symlink_to(real)

    monkeypatch.setenv("PATH", os.pathsep.join([str(link_dir), str(real_dir)]))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nohome"))

    assert ZephyrBuildSystem._find_nrfutil() is None
    assert len(probe_counter) == 1, probe_counter


def test_two_different_binaries_are_both_probed(tmp_path: Path, monkeypatch, probe_counter):
    """The negative control: dedup must not merge two real binaries."""
    for name in ("a", "b"):
        d = tmp_path / name
        d.mkdir()
        binary = d / "nrfutil"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)

    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nohome"))

    assert ZephyrBuildSystem._find_nrfutil() is None
    assert len(probe_counter) == 2, probe_counter


def test_two_shims_keep_their_literal_paths(tmp_path: Path, monkeypatch, probe_counter):
    """A shim is NOT deduplicated by its real path.

    Two shims can resolve to one dispatcher now and to different binaries
    during the build, when the environment selects another interpreter
    version.  That is the same reason _find_nrfutil does not resolve a shim
    at all.
    """
    dispatcher = tmp_path / "dispatch"
    dispatcher.write_text("#!/bin/sh\n")
    dispatcher.chmod(0o755)

    dirs = []
    for name in ("pyenv", "asdf"):
        d = tmp_path / name / "shims"
        d.mkdir(parents=True)
        (d / "nrfutil").symlink_to(dispatcher)
        dirs.append(str(d))

    monkeypatch.setenv("PATH", os.pathsep.join(dirs))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nohome"))

    assert ZephyrBuildSystem._find_nrfutil() is None
    assert len(probe_counter) == 2, probe_counter
