"""Edge case tests for shared context helpers — _is_stale, _detect_build_system."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fw_context_mcp.mcp.shared.context import _detect_build_system, _is_stale


class TestIsStale:
    def test_compile_commands_newer_than_index(self, tmp_path: Path):
        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]")
        # Touch the file to set a mtime
        _ = cc.stat().st_mtime

        cfg = {
            "created_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        }
        # compile_commands mtime is newer → stale
        assert _is_stale(cfg, str(cc)) is True

    def test_index_newer_than_compile_commands(self, tmp_path: Path):
        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]")
        # Set index time in the future
        cfg = {
            "created_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
        assert _is_stale(cfg, str(cc)) is False

    def test_missing_compile_commands_returns_false(self, tmp_path: Path):
        cfg = {
            "created_at": datetime.now(UTC).isoformat(),
        }
        assert _is_stale(cfg, str(tmp_path / "nonexistent.json")) is False

    def test_missing_created_at_key(self, tmp_path: Path):
        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]")
        cfg: dict = {}
        assert _is_stale(cfg, str(cc)) is False

    def test_invalid_timestamp_format(self, tmp_path: Path):
        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]")
        cfg = {"created_at": "not-a-valid-timestamp"}
        assert _is_stale(cfg, str(cc)) is False

    def test_empty_timestamp(self, tmp_path: Path):
        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]")
        cfg = {"created_at": ""}
        assert _is_stale(cfg, str(cc)) is False

    def test_tolerance_margin(self, tmp_path: Path):
        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]")
        # Index time is just barely newer (within tolerance)
        just_barely = datetime.fromtimestamp(cc.stat().st_mtime + 0.5, tz=UTC)
        cfg = {"created_at": just_barely.isoformat()}
        # cc mtime (without tolerance) is older than index → not stale
        # Actually: cc.st_mtime > (index_time + tolerance)?
        # If tolerance > difference, then NOT stale
        # cc.st_mtime = X, index = X + 0.5, tolerance = 1.0
        # X > (X + 0.5) + 1.0 → X > X + 1.5 → False → not stale
        assert _is_stale(cfg, str(cc)) is False

    def test_permission_error_handled(self, tmp_path: Path, monkeypatch):
        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]")

        def mock_getmtime(path):
            raise PermissionError("Access denied")

        monkeypatch.setattr(os.path, "getmtime", mock_getmtime)
        cfg = {"created_at": datetime.now(UTC).isoformat()}
        assert _is_stale(cfg, str(cc)) is False

    def test_future_mtime_handled(self, tmp_path: Path, monkeypatch):
        cc = tmp_path / "compile_commands.json"
        cc.write_text("[]")

        # mtime in the far future
        future = (datetime.now(UTC) + timedelta(days=365)).timestamp()

        def mock_getmtime(path):
            return future

        monkeypatch.setattr(os.path, "getmtime", mock_getmtime)
        cfg = {"created_at": datetime.now(UTC).isoformat()}
        assert _is_stale(cfg, str(cc)) is True


class TestDetectBuildSystem:
    def test_mbed_os_dir(self, tmp_path: Path):
        (tmp_path / "mbed-os").mkdir()
        assert _detect_build_system(tmp_path) == "mbed-os"

    def test_mbed_app_json(self, tmp_path: Path):
        (tmp_path / "mbed_app.json").write_text("{}")
        assert _detect_build_system(tmp_path) == "mbed-os"

    def test_zephyr_west_yml(self, tmp_path: Path):
        (tmp_path / "west.yml").write_text("manifest:")
        assert _detect_build_system(tmp_path) == "zephyr"

    def test_zephyr_prj_conf(self, tmp_path: Path):
        (tmp_path / "prj.conf").write_text("")
        assert _detect_build_system(tmp_path) == "zephyr"

    def test_platformio_ini(self, tmp_path: Path):
        (tmp_path / "platformio.ini").write_text("[env:board]")
        assert _detect_build_system(tmp_path) == "platformio"

    def test_unknown_empty_dir(self, tmp_path: Path):
        assert _detect_build_system(tmp_path) == "unknown"

    def test_mbed_wins_over_zephyr(self, tmp_path: Path):
        (tmp_path / "mbed-os").mkdir()
        (tmp_path / "west.yml").write_text("")
        assert _detect_build_system(tmp_path) == "mbed-os"

    def test_mbed_wins_over_platformio(self, tmp_path: Path):
        (tmp_path / "mbed_app.json").write_text("{}")
        (tmp_path / "platformio.ini").write_text("")
        assert _detect_build_system(tmp_path) == "mbed-os"

    def test_nonexistent_dir(self, tmp_path: Path):
        # _detect_build_system doesn't care if root doesn't exist
        # (Path methods like .is_dir() return False for nonexistent)
        assert _detect_build_system(tmp_path / "nonexistent") == "unknown"
