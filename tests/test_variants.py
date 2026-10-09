"""Tests for multi-variant build configuration ([[build.variants]])."""

from fw_context_mcp.indexer.build import (
    BuildConfig,
    BuildVariant,
    build_variant_config,
)


class TestBuildVariantConfig:
    def test_scalar_override(self):
        base = BuildConfig(system="zephyr", board="foo", toolchain="GCC_ARM")
        v = BuildVariant(name="nrf52840", board="nrf52840dk/nrf52840")
        c = build_variant_config(base, v)
        assert c.board == "nrf52840dk/nrf52840"
        assert c.toolchain == "GCC_ARM"  # inherited

    def test_list_replace(self):
        base = BuildConfig(defines=["A=1"], extra_profiles=["lto.json"])
        v = BuildVariant(name="v", overrides={"defines": ["HW_REV=2"]})
        c = build_variant_config(base, v)
        assert c.defines == ["HW_REV=2"]  # authoritative, not appended
        assert c.extra_profiles == ["lto.json"]  # inherited

    def test_dict_merge(self):
        base = BuildConfig(env={"BOARD_ENV": "DEV", "SHARED": "x"})
        v = BuildVariant(name="v", env={"BOARD_ENV": "TST"})
        c = build_variant_config(base, v)
        assert c.env == {"BOARD_ENV": "TST", "SHARED": "x"}

    def test_base_not_mutated(self):
        base = BuildConfig(defines=["A=1"], env={"BOARD_ENV": "DEV"})
        v = BuildVariant(name="v", overrides={"defines": ["B=2"]}, env={"BOARD_ENV": "TST"})
        build_variant_config(base, v)
        assert base.defines == ["A=1"]
        assert base.env == {"BOARD_ENV": "DEV"}

    def test_the_retired_build_dir_does_not_reach_the_build(self):
        """Each build goes to .fw-context/build/<variant>/out; build_dir has no effect."""
        base = BuildConfig()
        v = BuildVariant(name="v", build_dir="build/nrf52840_sysbuild")
        c = build_variant_config(base, v)
        assert c.build_dir is None

    def test_the_retired_build_dir_is_found_everywhere(self):
        from fw_context_mcp.indexer.build import retired_build_dir_keys

        cfg = BuildConfig(
            build_dir="build/",
            variants=[BuildVariant(name="a", build_dir="build/a"), BuildVariant(name="b")],
        )
        assert retired_build_dir_keys(cfg) == ["[build] build_dir", "[[build.variants]] 'a' build_dir"]
        assert retired_build_dir_keys(BuildConfig(variants=[BuildVariant(name="b")])) == []


class TestVariantConfigParsing:
    def test_variants_parsed(self):
        from fw_context_mcp.config.settings import _from_dict

        data = {
            "build": {
                "system": "zephyr",
                "sysbuild": True,
                "source_dir": "proj/app",
                "variants": [
                    {
                        "name": "nrf52840-dev",
                        "board": "nrf52840dk/nrf52840",
                        "build_dir": "build/nrf52840_sysbuild",
                        "env": {"BOARD_ENV": "DEV"},
                        "images": [
                            {"name": "app", "dir": "proj/app", "type": "project"},
                            {"name": "mcuboot", "dir": "${NCS}/bootloader/mcuboot", "type": "sdk"},
                        ],
                        "defines": ["HW_REV=2"],
                    },
                ],
            },
        }
        cfg = _from_dict(data)
        b = cfg.build
        assert b.sysbuild is True
        assert b.source_dir == "proj/app"
        assert len(b.variants) == 1
        v = b.variants[0]
        assert v.name == "nrf52840-dev"
        assert v.board == "nrf52840dk/nrf52840"
        assert v.env == {"BOARD_ENV": "DEV"}
        assert v.overrides == {"defines": ["HW_REV=2"]}
        assert [i.name for i in v.images] == ["app", "mcuboot"]
        assert v.images[1].type == "sdk"


class TestRetiredSelectionKeys:
    """``[build] default_variant`` and ``default_image`` selected the build of a query until 2026-10.

    A query names its build now, thus the keys have no effect.  An older
    config.toml must keep working, and its author must learn that the key
    does nothing.
    """

    def test_the_keys_go_and_the_warning_names_them(self, tmp_path, caplog):
        import logging

        from fw_context_mcp.config.settings import _drop_selection_keys

        layer = {"build": {"system": "zephyr", "default_variant": "a", "default_image": "app"}}
        path = tmp_path / "config.toml"

        with caplog.at_level(logging.WARNING, logger="fw_context_mcp.config.settings"):
            _drop_selection_keys(layer, path)

        assert layer == {"build": {"system": "zephyr"}}
        assert "default_image, default_variant have no effect" in caplog.text, caplog.text

    def test_a_variant_table_loses_the_keys_too(self, tmp_path):
        """A key in a variant table went to its overrides, and the warning called it unknown."""
        from fw_context_mcp.config.settings import _drop_selection_keys

        layer = {"build": {"variants": [{"name": "a", "default_image": "app"}]}}
        _drop_selection_keys(layer, tmp_path / "config.toml")

        assert layer == {"build": {"variants": [{"name": "a"}]}}

    def test_one_warning_for_each_file(self, tmp_path, caplog):
        import logging

        from fw_context_mcp.config.settings import _drop_selection_keys

        path = tmp_path / "config.toml"
        with caplog.at_level(logging.WARNING, logger="fw_context_mcp.config.settings"):
            for _ in range(3):
                _drop_selection_keys({"build": {"default_variant": "a"}}, path)

        assert caplog.text.count("has no effect") == 1, "load() runs many times in one MCP session"

    def test_a_layer_without_the_keys_is_unchanged(self, tmp_path, caplog):
        from fw_context_mcp.config.settings import _drop_selection_keys

        layer = {"build": {"system": "zephyr"}, "index": {"db_dir": "x"}}
        _drop_selection_keys(layer, tmp_path / "config.toml")

        assert layer == {"build": {"system": "zephyr"}, "index": {"db_dir": "x"}}
        assert caplog.text == ""
