"""``utils.atomic_copy`` gives the target all of the source, or leaves it as it was.

The Zephyr multi-image build copied each compilation database in place with
``shutil.copy2``.  The index stores the path of each file, and the MCP server
reads it from there while a later build writes it again, thus a reader could
get a truncated file, and a copy that failed left one.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from fw_context_mcp.utils import atomic_copy


def test_the_target_gets_the_content_and_the_mtime(tmp_path: Path):
    """The staleness check compares the mtime, thus it must come from the source."""
    source = tmp_path / "src.json"
    source.write_text("[1]", encoding="utf-8")
    os.utime(source, (1_000_000, 1_000_000))
    target = tmp_path / "out" / "compile_commands.a.b.json"
    target.parent.mkdir()

    atomic_copy(source, target)

    assert target.read_text(encoding="utf-8") == "[1]"
    assert target.stat().st_mtime == 1_000_000


def test_a_failed_copy_keeps_the_old_target_and_no_temporary_file(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "src.json"
    source.write_text("[new]", encoding="utf-8")
    target = tmp_path / "compile_commands.a.b.json"
    target.write_text("[old]", encoding="utf-8")

    def _copy_a_part_and_fail(src, dst, *a, **kw):
        Path(dst).write_text("[ne", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", _copy_a_part_and_fail)

    with pytest.raises(OSError, match="disk full"):
        atomic_copy(source, target)

    assert target.read_text(encoding="utf-8") == "[old]"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["compile_commands.a.b.json", "src.json"]


def test_the_temporary_file_hides_from_the_variant_scan(tmp_path: Path, monkeypatch):
    """A name that starts with ``compile_commands.`` would be read as a build."""
    names: list[str] = []
    real_copy2 = shutil.copy2

    def _record(src, dst, *a, **kw):
        names.append(Path(dst).name)
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", _record)
    source = tmp_path / "src.json"
    source.write_text("[]", encoding="utf-8")

    atomic_copy(source, tmp_path / "compile_commands.a.b.json")

    assert len(names) == 1
    assert names[0].startswith(".")
    assert not names[0].startswith("compile_commands.")


def test_the_zephyr_multi_image_build_copies_atomically(tmp_path: Path, monkeypatch):
    """The wiring: ``build_multi`` writes each image through ``atomic_copy``."""
    import fw_context_mcp.indexer.builders.zephyr as zephyr_mod
    from fw_context_mcp.indexer.build import BuildConfig, BuildVariant

    def _fake_west(cmd, cwd, description="", env=None, build_cfg=None, **kw):
        build_dir = Path(cmd[cmd.index("-d") + 1])
        (build_dir / "app").mkdir(parents=True)
        (build_dir / "app" / "compile_commands.json").write_text("[7]", encoding="utf-8")

    copies: list[tuple[Path, Path]] = []

    def _recording_copy(source: Path, target: Path) -> None:
        copies.append((source, target))
        atomic_copy(source, target)

    monkeypatch.setattr(zephyr_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(zephyr_mod, "run_build_command", _fake_west)
    monkeypatch.setattr(zephyr_mod, "atomic_copy", _recording_copy)
    monkeypatch.setattr(
        zephyr_mod.ZephyrBuildSystem, "_prepare_ninja_wrapper", lambda self, root: (None, {})
    )
    monkeypatch.setattr(
        zephyr_mod.ZephyrBuildSystem, "_discover_images", lambda self, build_dir: ["app"]
    )

    cfg = BuildConfig(system="zephyr", variants=[BuildVariant(name="dev", board="nrf52840dk/nrf52840")])
    results = zephyr_mod.ZephyrBuildSystem().build_multi(tmp_path, cfg)

    [(variant, image, path)] = results
    assert (variant, image, path.name) == ("dev", "app", "compile_commands.dev.app.json")
    assert [target for _, target in copies] == [path]
    assert path.read_text(encoding="utf-8") == "[7]"


def test_a_temporary_file_that_sigkill_left_is_cleaned_up(tmp_path: Path):
    """The next build removes it, as it removes a dead staging file.

    Its name matched no cleanup, thus a copy that SIGKILL stopped left
    megabytes in ``.fw-context/build`` for good.
    """
    from fw_context_mcp.indexer.build import clear_dead_staging_files
    from fw_context_mcp.utils import cc_staging_path, owner_token, staging_owner

    tag = owner_token().partition("@")[2]
    left = cc_staging_path(tmp_path).with_name(f".compile_commands.dev.1.2.2147483646@{tag}.json")
    left.write_text("[", encoding="utf-8")

    assert staging_owner(left) == f"2147483646@{tag}"
    clear_dead_staging_files(cc_staging_path(tmp_path))
    assert not left.exists()


def test_the_temporary_name_carries_the_owner(tmp_path: Path, monkeypatch):
    from fw_context_mcp.utils import owner_token, staging_owner

    names: list[Path] = []
    real_copy2 = shutil.copy2

    def _record(src, dst, *a, **kw):
        names.append(Path(dst))
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", _record)
    source = tmp_path / "src.json"
    source.write_text("[]", encoding="utf-8")

    atomic_copy(source, tmp_path / "compile_commands.dev.app.json")

    assert staging_owner(names[0]) == owner_token()
