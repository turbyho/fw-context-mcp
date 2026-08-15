"""Tests for multi-build orchestration (variant/image retention, zephyr helpers)."""

from pathlib import Path

from fw_context_mcp.indexer.build import BuildImage, BuildVariant
from fw_context_mcp.indexer.builders.zephyr import ZephyrBuildSystem
from fw_context_mcp.mcp.shared.variants import resolve_scopes


class TestZephyrHelpers:
    def test_normalize_board(self):
        assert ZephyrBuildSystem._normalize_board("nrf52840dk/nrf52840") == "nrf52840dk/nrf52840"
        assert ZephyrBuildSystem._normalize_board("nrf52840dk_nrf52840") == "nrf52840dk/nrf52840"
        assert (
            ZephyrBuildSystem._normalize_board("nrf54lm20dk/nrf54lm20a/cpuapp")
            == "nrf54lm20dk/nrf54lm20a/cpuapp"
        )

    def test_discover_images(self, tmpdir):
        build_dir = Path(tmpdir)
        (build_dir / "app").mkdir()
        (build_dir / "app" / "compile_commands.json").write_text("[]")
        (build_dir / "stage0").mkdir()
        (build_dir / "stage0" / "compile_commands.json").write_text("[]")
        # Non-image directories must be excluded by the M4 predicate.
        (build_dir / "_sysbuild").mkdir()
        (build_dir / "CMakeFiles").mkdir()
        (build_dir / "zephyr").mkdir()
        (build_dir / "CMakeCache.txt").write_text("")
        images = ZephyrBuildSystem._discover_images(build_dir)
        assert images == ["app", "stage0"]


class TestCleanupRetention:
    def test_keeps_newest_per_pair(self, temp_db):
        from fw_context_mcp.indexer._postprocess import cleanup_old_builds_multi
        from fw_context_mcp.indexer.db import transaction, upsert_build_config, upsert_project

        with transaction(temp_db):
            upsert_project(temp_db, "proj-001", "test", "/tmp/test")
            # variant nrf52840 has two builds (old + new) → old must be deleted.
            upsert_build_config(temp_db, "hash-n52-old", "proj-001", "/tmp/a.json", variant="nrf52840", image="app")
            upsert_build_config(temp_db, "hash-n52-new", "proj-001", "/tmp/b.json", variant="nrf52840", image="app")
            # variant nrf54 has one build → must survive (untouched pair).
            upsert_build_config(temp_db, "hash-n54", "proj-001", "/tmp/c.json", variant="nrf54lm20a", image="app")

        deleted = cleanup_old_builds_multi(temp_db, "proj-001", Path("/tmp/dbdir"), [("nrf52840", "app")])

        remaining = {
            r["config_hash"]
            for r in temp_db.execute("SELECT config_hash FROM build_configs WHERE project_id='proj-001'").fetchall()
        }
        assert deleted == 1
        assert "hash-n52-old" not in remaining
        assert "hash-n52-new" in remaining
        assert "hash-n54" in remaining  # untouched variant preserved

    def test_narrowed_run_preserves_other_variants(self, temp_db):
        from fw_context_mcp.indexer._postprocess import cleanup_old_builds_multi
        from fw_context_mcp.indexer.db import transaction, upsert_build_config, upsert_project

        with transaction(temp_db):
            upsert_project(temp_db, "proj-001", "test", "/tmp/test")
            upsert_build_config(temp_db, "hash-a", "proj-001", "/tmp/a.json", variant="a", image="")
            upsert_build_config(temp_db, "hash-b", "proj-001", "/tmp/b.json", variant="b", image="")

        # A narrowed run touching only variant 'a' must not delete 'b'.
        cleanup_old_builds_multi(temp_db, "proj-001", Path("/tmp/dbdir"), [("a", "")])

        remaining = {
            r["config_hash"]
            for r in temp_db.execute("SELECT config_hash FROM build_configs WHERE project_id='proj-001'").fetchall()
        }
        assert remaining == {"hash-a", "hash-b"}


class TestVariantDiscovery:
    def _make_cfg(self):
        from fw_context_mcp.config.settings import Config

        cfg = Config()
        cfg.build.board = "default_board"
        cfg.build.variants = [
            BuildVariant(
                name="nrf52840",
                description="nRF52840 DK",
                board="nrf52840dk/nrf52840",
                images=[
                    BuildImage(name="app", dir="proj/app", type="project"),
                    BuildImage(name="mcuboot", dir="${NCS}/bootloader/mcuboot", type="sdk"),
                ],
            ),
            BuildVariant(
                name="nrf54lm20a",
                board="nrf54lm20dk/nrf54lm20a/cpuapp",
                images=[
                    BuildImage(name="app", dir="proj/app", type="project"),
                    BuildImage(name="app_flpr", dir="proj/app_flpr", type="project", board="nrf54lm20dk/nrf54lm20a/cpuflpr"),
                ],
            ),
        ]
        cfg.build.default_variant = "nrf52840"
        return cfg

    def test_discovery_from_config(self):
        from fw_context_mcp.mcp.handlers.maintenance import _build_variant_discovery

        cfg = self._make_cfg()
        discovery = _build_variant_discovery(cfg, [], Path("/tmp"))
        assert discovery["multi"] is True
        assert [v["name"] for v in discovery["variants"]] == ["nrf52840", "nrf54lm20a"]
        assert discovery["variants"][0]["board"] == "nrf52840dk/nrf52840"
        assert {i["name"] for i in discovery["images"]} == {"app", "mcuboot", "app_flpr"}
        # per-image board override (FLPR) surfaces on the image entry
        flpr = next(i for i in discovery["images"] if i["name"] == "app_flpr")
        assert flpr["board"] == "nrf54lm20dk/nrf54lm20a/cpuflpr"
        assert discovery["variant_images"]["nrf52840"] == ["app", "mcuboot"]
        assert discovery["active_variant"] == "nrf52840"

    def test_discovery_auto_detect_from_builds(self):
        from fw_context_mcp.mcp.handlers.maintenance import _build_variant_discovery

        cfg = self._make_cfg()
        cfg.build.variants = [BuildVariant(name="nrf52840", board="x")]  # no images
        builds = [
            {"variant": "nrf52840", "image": "app", "board": "x", "config_hash": "h1"},
            {"variant": "nrf52840", "image": "stage0", "board": "x", "config_hash": "h2"},
        ]
        discovery = _build_variant_discovery(cfg, builds, Path("/tmp"))
        assert [i["name"] for i in discovery["images"]] == ["app", "stage0"]
        assert discovery["variant_images"]["nrf52840"] == ["app", "stage0"]


class TestResolveScopes:
    def _cfg(self, variants=None, default_variant=None):
        from fw_context_mcp.config.settings import Config

        cfg = Config()
        cfg.build.variants = variants or []
        cfg.build.default_variant = default_variant
        return cfg

    def _seed(self, conn):
        from fw_context_mcp.indexer.db import transaction, upsert_build_config, upsert_project

        with transaction(conn):
            upsert_project(conn, "proj", "t", "/tmp/t")
            upsert_build_config(conn, "h-a-app", "proj", "/tmp/a", variant="a", image="app")
            upsert_build_config(conn, "h-a-stage", "proj", "/tmp/b", variant="a", image="stage0")
            upsert_build_config(conn, "h-b-app", "proj", "/tmp/c", variant="b", image="app")

    def test_single_project_returns_one_scope(self, temp_db):
        from fw_context_mcp.indexer.db import transaction, upsert_build_config, upsert_project

        with transaction(temp_db):
            upsert_project(temp_db, "proj", "t", "/tmp/t")
            upsert_build_config(temp_db, "h-single", "proj", "/tmp/cc.json")

        scopes, multi, err = resolve_scopes(temp_db, "proj", self._cfg(), "", "")
        assert err is None
        assert multi is False
        assert len(scopes) == 1
        assert scopes[0]["config_hash"] == "h-single"

    def test_multi_fail_closed_without_default(self, temp_db):
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a"), BuildVariant(name="b")])
        scopes, multi, err = resolve_scopes(temp_db, "proj", cfg, "", "")
        assert err is not None and "default_variant" in err
        assert scopes == []

    def test_multi_uses_default_variant(self, temp_db):
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a"), BuildVariant(name="b")], default_variant="a")
        scopes, multi, err = resolve_scopes(temp_db, "proj", cfg, "", "")
        assert err is None and multi is True
        # image omitted → all images of variant 'a'
        assert {s["image"] for s in scopes} == {"app", "stage0"}

    def test_variant_star_returns_all(self, temp_db):
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a"), BuildVariant(name="b")])
        scopes, multi, err = resolve_scopes(temp_db, "proj", cfg, "*", "")
        assert err is None and len(scopes) == 3

    def test_unknown_variant_errors(self, temp_db):
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a")])
        scopes, multi, err = resolve_scopes(temp_db, "proj", cfg, "zzz", "")
        assert err is not None and "Unknown variant" in err

    def test_specific_image_narrows(self, temp_db):
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a")])
        scopes, multi, err = resolve_scopes(temp_db, "proj", cfg, "a", "stage0")
        assert err is None and [s["image"] for s in scopes] == ["stage0"]
