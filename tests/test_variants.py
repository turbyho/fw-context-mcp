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

    def test_build_dir_override(self):
        base = BuildConfig(build_dir="build/")
        v = BuildVariant(name="v", build_dir="build/nrf52840_sysbuild")
        c = build_variant_config(base, v)
        assert c.build_dir == "build/nrf52840_sysbuild"


class TestVariantConfigParsing:
    def test_variants_parsed(self):
        from fw_context_mcp.config.settings import _from_dict

        data = {
            "build": {
                "system": "zephyr",
                "sysbuild": True,
                "source_dir": "proj/app",
                "default_variant": "nrf52840-dev",
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
        assert b.default_variant == "nrf52840-dev"
        assert len(b.variants) == 1
        v = b.variants[0]
        assert v.name == "nrf52840-dev"
        assert v.board == "nrf52840dk/nrf52840"
        assert v.env == {"BOARD_ENV": "DEV"}
        assert v.overrides == {"defines": ["HW_REV=2"]}
        assert [i.name for i in v.images] == ["app", "mcuboot"]
        assert v.images[1].type == "sdk"
