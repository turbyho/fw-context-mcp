"""Tests for the ``init-variants`` CLI (add/remove/list of [[build.variants]])."""

import tomllib
from pathlib import Path

from fw_context_mcp.cli._init_variants import _add_variant, _list_variants, _remove_variant


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        return None


class TestInitVariants:
    def _cfg(self, tmp_path: Path) -> Path:
        cfg = tmp_path / ".fw-context" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("[project]\nid = \"x\"\n\n[build]\nsystem = \"zephyr\"\n")
        return cfg

    def test_add_and_parse(self, tmp_path, capsys):
        cfg = self._cfg(tmp_path)
        rc = _add_variant(
            cfg,
            _Args(name="nrf52840-dev", board="nrf52840dk/nrf52840", env=["ZBOX_ENV=DEV"]),
        )
        assert rc == 0
        data = tomllib.loads(cfg.read_text())
        variants = data["build"]["variants"]
        assert len(variants) == 1
        assert variants[0]["name"] == "nrf52840-dev"
        assert variants[0]["board"] == "nrf52840dk/nrf52840"
        assert variants[0]["env"] == {"ZBOX_ENV": "DEV"}

    def test_add_duplicate_fails(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _add_variant(cfg, _Args(name="a", board="b"))
        rc = _add_variant(cfg, _Args(name="a", board="b"))
        assert rc == 1

    def test_remove(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _add_variant(cfg, _Args(name="a", board="b"))
        _add_variant(cfg, _Args(name="c", board="d"))
        rc = _remove_variant(cfg, "a")
        assert rc == 0
        data = tomllib.loads(cfg.read_text())
        assert [v["name"] for v in data["build"]["variants"]] == ["c"]

    def test_remove_missing(self, tmp_path):
        cfg = self._cfg(tmp_path)
        assert _remove_variant(cfg, "nope") == 1

    def test_list_empty(self, tmp_path, capsys):
        cfg = self._cfg(tmp_path)
        assert _list_variants(cfg, tmp_path) == 0
        assert "No [[build.variants]]" in capsys.readouterr().out
