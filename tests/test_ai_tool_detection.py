"""The detection of the AI tools by their global directories.

The Kilo CLI keeps its global config in ``~/.config/kilo`` on each host
(kilo.ai/docs/getting-started/settings).  The detection looked only for
``~/.kilocode``, the directory of the older releases, thus init did not
see a Kilo of today.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fw_context_mcp.config.tools import TOOLS


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    # expanduser reads HOME on POSIX and USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)
    return tmp_path


@pytest.mark.parametrize("directory", [".config/kilo", ".kilocode"])
def test_kilo_is_found_by_each_of_its_directories(home: Path, directory: str):
    (home / directory).mkdir(parents=True)

    assert TOOLS["kilocode"].is_detected()


def test_kilo_is_not_found_without_its_directories(home: Path):
    assert not TOOLS["kilocode"].is_detected()
