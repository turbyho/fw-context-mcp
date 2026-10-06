"""Each environment of platformio.ini is a variant, when the config declares none.

An environment is a build of its own (its board, its flags), thus one query
must name one of them, as for any other variant.  A project with one
environment stays a project without variants, so that its queries need no
``variant``.  The set comes from PlatformIO itself (``pio project config``),
see ``PlatformIOBuildSystem.environments``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def global_config(tmp_path: Path, monkeypatch) -> None:
    """Keep load() away from the global config of the operator (fixed at import)."""
    import fw_context_mcp.config.settings as settings

    global_cfg = tmp_path / "global.toml"
    global_cfg.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)


def _run(tmp_path: Path, monkeypatch, environments: list[str] | None, build_table: str = "") -> tuple[int, dict]:
    """Run ``cmd_index`` on a PlatformIO project whose pio names *environments*.

    None for *environments* keeps the ``environments`` that the test set.

    Returns the exit code and what the build step saw: the variant names on
    the multi path, or the environment on the single path.
    """
    from fw_context_mcp.cli import _index as index_mod
    from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / "platformio.ini").write_text("", encoding="utf-8")
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{PROJECT_ID}"\n[build]\nsystem = "platformio"\n{build_table}', encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text(
        f'[index]\ndb_dir = "{tmp_path / "index"}"\n', encoding="utf-8")

    seen: dict = {}
    if environments is not None:
        monkeypatch.setattr(PlatformIOBuildSystem, "environments", lambda self, root, cfg: list(environments))

    def _multi(args, cfg, *a, **kw) -> int:
        seen["variants"] = [v.name for v in cfg.build.variants]
        return 0

    def _single(args, cfg, *a, **kw) -> int:
        seen["environment"] = cfg.build.environment
        return 0

    monkeypatch.setattr(index_mod, "_run_multi", _multi)
    monkeypatch.setattr(index_mod, "_run_single", _single)
    monkeypatch.setattr(index_mod, "_build_run_kwargs", lambda *a, **kw: {})
    monkeypatch.setattr(index_mod, "_ensure_watcher_after_index", lambda root: None)
    args = SimpleNamespace(
        verbose=False, project=str(root), background=False, build=True,
        compile_commands=None, no_clean=False, force=False, takeover=False,
        vendor_paths=None, project_paths=None,
    )
    return index_mod.cmd_index(args), seen


@pytest.mark.usefixtures("global_config")
class TestTheEnvironmentsOfTheProject:
    def test_two_environments_are_two_variants(self, tmp_path: Path, monkeypatch, capsys) -> None:
        code, seen = _run(tmp_path, monkeypatch, ["nucleo", "native"])

        assert code == 0
        assert seen == {"variants": ["nucleo", "native"]}
        assert "default_variant" in capsys.readouterr().out, "the run says how a query needs no variant"

    def test_one_environment_is_no_variant(self, tmp_path: Path, monkeypatch) -> None:
        code, seen = _run(tmp_path, monkeypatch, ["esp32dev"])

        assert code == 0
        assert seen == {"environment": "esp32dev"}

    def test_declared_variants_win(self, tmp_path: Path, monkeypatch) -> None:
        code, seen = _run(
            tmp_path, monkeypatch, ["nucleo", "native"],
            build_table='[[build.variants]]\nname = "release"\nenvironment = "nucleo"\n',
        )

        assert code == 0
        assert seen == {"variants": ["release"]}

    def test_a_named_environment_is_no_variant(self, tmp_path: Path, monkeypatch) -> None:
        code, seen = _run(tmp_path, monkeypatch, ["nucleo", "native"], build_table='environment = "native"\n')

        assert code == 0
        assert seen == {"environment": "native"}

    def test_a_pio_that_cannot_answer_stops_the_run(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        def _fail(self, root, cfg):
            raise RuntimeError("pio project config failed")

        monkeypatch.setattr(PlatformIOBuildSystem, "environments", _fail)
        code, _seen = _run(tmp_path, monkeypatch, None)

        assert code == 1
        assert "cannot list the builds of the project" in capsys.readouterr().err

    def test_an_explicit_database_of_the_config_makes_no_variants(self, tmp_path: Path, monkeypatch) -> None:
        """[index] compile_commands names its build itself, as a file on the command line does."""
        calls: list[str] = []

        from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

        def _environments(self, root, cfg):
            calls.append("pio project config")
            return ["nucleo", "native"]

        monkeypatch.setattr(PlatformIOBuildSystem, "environments", _environments)
        code, seen = _run(tmp_path, monkeypatch, None, build_table='[index]\ncompile_commands = "ci/cc.json"\n')

        assert code == 0
        assert calls == []
        assert "variants" not in seen


@pytest.mark.usefixtures("global_config")
def test_init_does_not_build_one_of_several_environments(tmp_path: Path, monkeypatch) -> None:
    """`fw-context index` makes a variant of each, thus one build here would be the build of none."""
    from fw_context_mcp.cli._init import _implicit_variant_names
    from fw_context_mcp.config import load
    from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem

    (tmp_path / ".fw-context").mkdir()
    (tmp_path / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{PROJECT_ID}"\n[build]\nsystem = "platformio"\n', encoding="utf-8")
    monkeypatch.setattr(PlatformIOBuildSystem, "environments", lambda self, root, cfg: ["nucleo", "native"])

    assert _implicit_variant_names(tmp_path, "platformio", load(project_root=tmp_path)) == ["nucleo", "native"]
