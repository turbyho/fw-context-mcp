"""The marker of an index run that stored rows and stopped (``_run_marker``)."""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer import _run_marker
from fw_context_mcp.utils import _owner_tag, owner_token, remove_stale_tmp


def test_no_marker_means_nothing_to_parse(tmp_path: Path):
    assert not _run_marker.is_pending(tmp_path, "cafe")
    assert _run_marker.units_to_parse(tmp_path, "cafe") == frozenset()


def test_the_marker_holds_the_units_to_parse(tmp_path: Path):
    _run_marker.mark_pending(tmp_path, "cafe", {"src/b.cpp", "src/a.cpp"})

    assert _run_marker.is_pending(tmp_path, "cafe")
    assert _run_marker.units_to_parse(tmp_path, "cafe") == frozenset({"src/a.cpp", "src/b.cpp"})
    assert not _run_marker.is_pending(tmp_path, "beef"), "each build has its own marker"


def test_clear_removes_the_marker(tmp_path: Path):
    _run_marker.mark_pending(tmp_path, "cafe")
    _run_marker.clear_pending(tmp_path, "cafe")
    _run_marker.clear_pending(tmp_path, "cafe")

    assert not _run_marker.is_pending(tmp_path, "cafe")


def test_the_name_gives_the_config_hash_back(tmp_path: Path):
    name = _run_marker.marker_path(tmp_path, "cafe").name

    assert _run_marker.config_hash_of(name) == "cafe"
    assert _run_marker.config_hash_of("manifest.cafe.json") is None


def test_a_temporary_file_of_a_stopped_writer_is_removed(tmp_path: Path):
    """A kill between the write and the rename leaves a temporary file of tens of MB."""
    # A PID above the PID limit of the system names no process.
    gone = tmp_path / f".manifest.cafe.json.999999999@{_owner_tag()}.tmp"
    gone.write_text("{", encoding="utf-8")
    alive = tmp_path / f".manifest.cafe.json.{owner_token()}.tmp"
    alive.write_text("{", encoding="utf-8")
    other = tmp_path / ".notes.tmp"
    other.write_text("", encoding="utf-8")

    removed = remove_stale_tmp(tmp_path)

    assert alive.exists(), "the file of a writer that runs stays"
    assert other.exists(), "a file without an owner token is not ours"
    assert not gone.exists()
    assert removed == 1


def test_a_checkpoint_that_cannot_write_does_not_stop_the_run(tmp_path: Path, monkeypatch, caplog):
    """On Windows the rename fails while the MCP server reads the manifest.

    The checkpoint only saves work for a run that stops; the run goes on,
    and the next checkpoint writes again.
    """
    import fw_context_mcp.indexer._manifest_updater as updater
    from fw_context_mcp.indexer.runner import _checkpoint_manifest

    def busy(**kwargs):
        raise PermissionError("the file is open in another process")

    monkeypatch.setattr(updater, "_update_manifest_after_index", busy)
    _checkpoint_manifest(
        manifest={"entries": []}, units=[], project_root=tmp_path, db_dir=tmp_path,
        compile_commands=tmp_path / "compile_commands.json", updated=1, tu_headers={},
        build_dir_patterns=None, vendor_patterns=[], config_hash="cafe", scope=None,
        reparsed_tus={}, transient_defines=(), reached=0, flags_hashes=[],
    )

    assert "manifest checkpoint not written" in caplog.text
