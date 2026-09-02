"""A variant may ADD to the [index] path layers, never replace them.

These fixtures are SYNTHETIC.  The measured set of five projects has no
per-variant in-tree vendor tree at all: the Zephyr project is the only
multi-build project and all 6889 of its is_project=0 files sit under ~/ncs,
outside the project.  Do not read these tests as evidence about real
behaviour — they pin the semantics, not a measurement.

What IS measured, and must not be confused with the above: build_dir is
per-variant (9 builds, 2 distinct values on the Zephyr project).
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.cli._index import _layered_paths
from fw_context_mcp.config import load as load_config


def _write_config(root: Path, body: str) -> None:
    cfg_dir = root / ".fw-context"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "config.toml").write_text(body)


class TestLayeredPaths:
    def test_a_variant_adds_to_the_index_vendor_paths(self):
        """Adds, does NOT replace.

        An earlier revision had this the other way round.  Under replace a
        user with a value in both layers loses the shared entries with no
        message — the same class of silent wrongness as a lost staleness
        signal.  The precedent is _DICT_FIELDS in build.py, which merges env
        per key and says why.
        """
        assert _layered_paths(["third_party"], ["legacy_hal"]) == [
            "third_party", "legacy_hal",
        ]

    def test_a_variant_without_the_key_inherits_from_index(self):
        assert _layered_paths(["third_party"], []) == ["third_party"]

    def test_an_index_layer_without_the_key_gives_the_variant_alone(self):
        assert _layered_paths([], ["legacy_hal"]) == ["legacy_hal"]

    def test_a_duplicate_path_appears_once_and_the_order_is_stable(self):
        """dict.fromkeys, not set: the order shows in the log and the manifest."""
        assert _layered_paths(["a", "b"], ["b", "c"]) == ["a", "b", "c"]
        assert _layered_paths(["b", "a"], ["a"]) == ["b", "a"]


class TestVariantIndexOverrides:
    def test_an_index_key_in_a_variant_is_not_a_build_override(self, tmp_path: Path):
        """It must reach index_overrides, not overrides.

        In overrides it was dropped in silence by build_variant_config, and
        since the unknown-key warning it would be reported as a typo.
        """
        _write_config(tmp_path, """
[build]
system = "zephyr"

[[build.variants]]
name = "dev"
board = "nrf52840dk/nrf52840"
vendor_paths = ["legacy_hal"]
""")
        cfg = load_config(project_root=tmp_path)
        variant = cfg.build.variants[0]

        assert variant.index_overrides == {"vendor_paths": ["legacy_hal"]}
        assert "vendor_paths" not in variant.overrides

    def test_a_build_key_in_a_variant_still_reaches_overrides(self, tmp_path: Path):
        """The negative control: the split must not swallow a [build] key."""
        _write_config(tmp_path, """
[build]
system = "zephyr"

[[build.variants]]
name = "rel"
board = "nrf52840dk/nrf52840"
profile = "release"
""")
        cfg = load_config(project_root=tmp_path)
        variant = cfg.build.variants[0]

        assert variant.overrides.get("profile") == "release"
        assert variant.index_overrides == {}

    def test_a_valid_variant_index_key_does_not_warn(self, tmp_path: Path, caplog):
        """The unknown-key warning must fire on a typo only."""
        import logging

        from fw_context_mcp.indexer.build import build_variant_config

        _write_config(tmp_path, """
[build]
system = "zephyr"

[[build.variants]]
name = "dev"
board = "nrf52840dk/nrf52840"
vendor_paths = ["legacy_hal"]
""")
        cfg = load_config(project_root=tmp_path)

        with caplog.at_level(logging.WARNING, logger="fw_context_mcp.indexer.build"):
            build_variant_config(cfg.build, cfg.build.variants[0])

        assert caplog.text == "", caplog.text

    def test_a_typo_still_warns(self, tmp_path: Path, caplog):
        import logging

        from fw_context_mcp.indexer.build import build_variant_config

        _write_config(tmp_path, """
[build]
system = "zephyr"

[[build.variants]]
name = "dev"
board = "nrf52840dk/nrf52840"
vendor_pathz = ["legacy_hal"]
""")
        cfg = load_config(project_root=tmp_path)

        with caplog.at_level(logging.WARNING, logger="fw_context_mcp.indexer.build"):
            build_variant_config(cfg.build, cfg.build.variants[0])

        assert "vendor_pathz" in caplog.text

    def test_two_variants_get_different_path_layers(self, tmp_path: Path):
        """The end-to-end shape _run_multi builds per (variant, image)."""
        _write_config(tmp_path, """
[build]
system = "zephyr"

[index]
vendor_paths = ["third_party"]

[[build.variants]]
name = "dev"
board = "nrf52840dk/nrf52840"
vendor_paths = ["legacy_hal"]

[[build.variants]]
name = "rel"
board = "nrf52840dk/nrf52840"
""")
        cfg = load_config(project_root=tmp_path)
        base = list(cfg.index.vendor_paths)
        by_name = {v.name: v for v in cfg.build.variants}

        dev = _layered_paths(base, by_name["dev"].index_overrides.get("vendor_paths", []))
        rel = _layered_paths(base, by_name["rel"].index_overrides.get("vendor_paths", []))

        assert dev == ["third_party", "legacy_hal"]
        assert rel == ["third_party"]

    def test_an_existing_config_without_the_key_is_unchanged(self, tmp_path: Path):
        """No-op on the Zephyr project, which is the only real multi-build project.

        Its [index] vendor_paths and project_paths are both empty and both
        variants have no overrides, so the effective set stays [] — identical
        to what it was.
        """
        _write_config(tmp_path, """
[build]
system = "zephyr"

[[build.variants]]
name = "nrf52840-dev"
board = "nrf52840dk/nrf52840"

[[build.variants]]
name = "nrf54lm20a-dev"
board = "nrf54lm20dk/nrf54lm20a/cpuapp"
""")
        cfg = load_config(project_root=tmp_path)

        assert cfg.index.vendor_paths == []
        assert cfg.index.project_paths == []
        for variant in cfg.build.variants:
            assert variant.index_overrides == {}
            assert _layered_paths([], variant.index_overrides.get("vendor_paths", [])) == []
