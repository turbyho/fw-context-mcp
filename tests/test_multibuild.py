"""Tests for multi-build orchestration (variant/image retention, zephyr helpers)."""

from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import BuildImage, BuildVariant
from fw_context_mcp.indexer.builders.zephyr import ZephyrBuildSystem
from fw_context_mcp.mcp.shared.variants import resolve_build


class TestZephyrHelpers:
    def test_normalize_board(self):
        assert ZephyrBuildSystem._normalize_board("nrf52840dk/nrf52840") == "nrf52840dk/nrf52840"
        assert ZephyrBuildSystem._normalize_board("nrf52840dk_nrf52840") == "nrf52840dk/nrf52840"
        assert (
            ZephyrBuildSystem._normalize_board("nrf54lm20dk/nrf54lm20a/cpuapp")
            == "nrf54lm20dk/nrf54lm20a/cpuapp"
        )


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


class TestRetiredBuilds:
    """A run over the whole set removes the builds that the project no longer makes.

    ``resolve_build`` reads the variants of the index, thus a retired
    variant stayed queryable with old data, and a project that went from two
    PlatformIO environments to one could not reach its new build.
    """

    @staticmethod
    def _seed(temp_db, rows: list[tuple[str, str, str]]) -> None:
        from fw_context_mcp.indexer.db import transaction, upsert_build_config, upsert_project

        with transaction(temp_db):
            upsert_project(temp_db, "proj-001", "test", "/tmp/test")
            for config_hash, variant, image in rows:
                upsert_build_config(temp_db, config_hash, "proj-001", f"/tmp/{config_hash}.json",
                                    variant=variant, image=image)

    @staticmethod
    def _remaining(temp_db) -> set[str]:
        return {
            r["config_hash"]
            for r in temp_db.execute("SELECT config_hash FROM build_configs WHERE project_id='proj-001'").fetchall()
        }

    def test_a_variant_out_of_the_set_goes(self, temp_db, tmp_path):
        from fw_context_mcp.indexer._postprocess import cleanup_retired_builds

        self._seed(temp_db, [("h-a", "a", ""), ("h-b", "b", "")])

        retired = cleanup_retired_builds(temp_db, "proj-001", tmp_path, {"a"}, {("a", "")})

        assert retired == [("b", "")]
        assert self._remaining(temp_db) == {"h-a"}

    def test_two_environments_become_one_build(self, temp_db, tmp_path):
        """The variants go, and the build without variants is the one left."""
        from fw_context_mcp.indexer._postprocess import cleanup_retired_builds

        self._seed(temp_db, [("h-a", "a", ""), ("h-b", "b", ""), ("h-single", "", "")])

        cleanup_retired_builds(temp_db, "proj-001", tmp_path, {""}, {("", "")})

        assert self._remaining(temp_db) == {"h-single"}

    def test_an_image_that_the_build_no_longer_makes_goes(self, temp_db, tmp_path):
        from fw_context_mcp.indexer._postprocess import cleanup_retired_builds

        self._seed(temp_db, [("h-app", "dev", "app"), ("h-alt", "dev", "app_alt")])

        cleanup_retired_builds(temp_db, "proj-001", tmp_path, {"dev"}, {("dev", "app")})

        assert self._remaining(temp_db) == {"h-app"}

    def test_a_variant_whose_build_failed_keeps_its_builds(self, temp_db, tmp_path):
        """The failure says nothing about the images that the variant makes."""
        from fw_context_mcp.indexer._postprocess import cleanup_retired_builds

        self._seed(temp_db, [("h-a", "a", "app"), ("h-b-app", "b", "app"), ("h-b-boot", "b", "boot")])

        cleanup_retired_builds(temp_db, "proj-001", tmp_path, {"a", "b"}, {("a", "app")})

        assert self._remaining(temp_db) == {"h-a", "h-b-app", "h-b-boot"}

    def test_the_artifacts_of_a_retired_build_go(self, temp_db, tmp_path):
        from fw_context_mcp.indexer._postprocess import cleanup_retired_builds

        self._seed(temp_db, [("h-a", "a", ""), ("h-b", "b", "")])
        artifact = tmp_path / "compile_commands.h-b.json"
        artifact.write_text("[]", encoding="utf-8")

        cleanup_retired_builds(temp_db, "proj-001", tmp_path, {"a"}, {("a", "")})

        assert not artifact.exists()


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

    def test_an_indexed_variant_is_discovered_without_config(self):
        """get_active_build and list_variants must agree on ``multi``.

        The index is the fact.  A config that declares no variant over an
        index that holds two builds of one board reported ``multi: false``
        and an empty ``variants`` list, while ``variant_images`` of the
        SAME payload named the builds — a payload that contradicted itself,
        and a tool that contradicted ``list_variants``.
        """
        from fw_context_mcp.mcp.handlers.maintenance import _build_variant_discovery

        cfg = self._make_cfg()
        cfg.build.variants = []          # the config declares nothing
        cfg.build.default_variant = None
        builds = [
            {"variant": "nrf52840", "image": "app", "board": "b1", "config_hash": "h1"},
            {"variant": "nrf52840", "image": "mcuboot", "board": "b1", "config_hash": "h2"},
        ]

        discovery = _build_variant_discovery(cfg, builds, Path("/tmp"))

        assert discovery["multi"] is True, "an indexed variant makes it multi-build"
        assert [v["name"] for v in discovery["variants"]] == ["nrf52840"], (
            f"a caller told to choose a variant got no list: {discovery['variants']}"
        )
        assert discovery["variants"][0]["board"] == "b1"
        assert discovery["variant_images"]["nrf52840"] == ["app", "mcuboot"]

    def test_a_declared_variant_does_not_hide_an_indexed_one(self):
        """This table is where the refusal of ``resolve_build`` sends a reader.

        ``resolve_build`` unites the declared names with the indexed ones,
        thus its refusal reads "one of: a, b. Call get_active_build() for
        the variants/images table".  Listing the declared names alone left
        that table showing 'a' only, and a reader who followed the advice
        could not find 'b'.
        """
        from fw_context_mcp.mcp.handlers.maintenance import _build_variant_discovery

        cfg = self._make_cfg()
        cfg.build.variants = [BuildVariant(name="a", board="board-a")]
        cfg.build.default_variant = None
        builds = [
            {"variant": "a", "image": "app", "board": "board-a", "config_hash": "h1"},
            {"variant": "b", "image": "app", "board": "board-b", "config_hash": "h2"},
        ]

        discovery = _build_variant_discovery(cfg, builds, Path("/tmp"))

        assert [v["name"] for v in discovery["variants"]] == ["a", "b"], (
            f"the table hides a build that exists: {discovery['variants']}"
        )
        assert discovery["variants"][1]["board"] == "board-b"

    def test_a_single_project_stays_single(self):
        """No variant anywhere means no choice to make."""
        from fw_context_mcp.mcp.handlers.maintenance import _build_variant_discovery

        cfg = self._make_cfg()
        cfg.build.variants = []
        builds = [{"variant": "", "image": "", "board": "b", "config_hash": "h"}]

        discovery = _build_variant_discovery(cfg, builds, Path("/tmp"))

        assert discovery["multi"] is False
        assert discovery["variants"] == []

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


class TestResolveBuild:
    """``resolve_build``: the caller names the build, and the server does not guess.

    A project of several builds has no active build.  Until 2026-10 a query
    without a selector got ``[build] default_variant``, ``[build]
    default_image``, or the application that the build system names.  An
    answer from a guessed build looks correct, thus the caller could not
    see that it asked about another program.
    """

    def _cfg(self, variants=None, default_variant=None, default_image=None):
        from fw_context_mcp.config.settings import Config

        cfg = Config()
        cfg.build.variants = variants or []
        cfg.build.default_variant = default_variant
        cfg.build.default_image = default_image
        return cfg

    @staticmethod
    def _rows(conn, rows: list[tuple[str, str, str]]) -> None:
        """Index ``(config_hash, variant, image)`` in this order: the last row is the newest."""
        from fw_context_mcp.indexer.db import transaction, upsert_build_config, upsert_project

        with transaction(conn):
            upsert_project(conn, "proj", "t", "/tmp/t")
            for config_hash, variant, image in rows:
                upsert_build_config(conn, config_hash, "proj", f"/tmp/{config_hash}.json",
                                    variant=variant, image=image)

    def _seed(self, conn):
        self._rows(conn, [("h-a-app", "a", "app"), ("h-a-stage", "a", "stage0"), ("h-b-app", "b", "app")])

    # ── One build ──

    def test_a_single_build_needs_no_selector(self, temp_db):
        self._rows(temp_db, [("h-single", "", "")])

        assert resolve_build(temp_db, "proj", self._cfg(), "", "") == ("h-single", None)

    def test_a_single_build_answers_its_own_pair(self, temp_db):
        """A caller that copies the pair from the build list must not get a refusal."""
        self._rows(temp_db, [("h-only", "a", "app")])

        assert resolve_build(temp_db, "proj", self._cfg(), "a", "app") == ("h-only", None)
        assert resolve_build(temp_db, "proj", self._cfg(), "", "") == ("h-only", None)

    @pytest.mark.parametrize(
        ("variant", "image"),
        [("zzz", ""), ("", "zzz"), ("zzz", "yyy"), ("*", "")],
        ids=["variant", "image", "both", "star"],
    )
    def test_a_single_build_refuses_another_name(self, temp_db, variant: str, image: str):
        """A named build that the project does not hold must fail, and not answer from the one it has.

        Measured over 13 indexed builds before this check: the six
        single-build ones answered ``variant="zzz"`` with rows.
        """
        self._rows(temp_db, [("h-single", "", "")])

        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), variant, image)

        assert config_hash is None, "a named build must not answer from another"
        assert err is not None and "holds one build" in err, err
        for name in (variant, image):
            if name:
                assert f'"{name}"' in err, err
        assert "no variant and no image" in err, f"the refusal does not name the build it has: {err}"

    def test_a_part_of_the_pair_of_a_single_build_is_refused(self, temp_db):
        self._rows(temp_db, [("h-only", "a", "app")])

        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), "a", "")

        assert config_hash is None
        assert err is not None and 'variant="a", image="app"' in err, err

    # ── Several builds ──

    def test_no_selector_on_several_builds_is_refused(self, temp_db):
        """The refusal lists every build, with the arguments to repeat the call."""
        self._seed(temp_db)

        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), "", "")

        assert config_hash is None
        assert err is not None and "names no build" in err, err
        for pair in ('variant="a", image="app"', 'variant="a", image="stage0"', 'variant="b", image="app"'):
            assert pair in err, f"the refusal hides {pair}: {err}"
        assert "Repeat the call" in err, err
        assert "ask the user" in err, err

    def test_the_config_defaults_select_nothing(self, temp_db):
        """``[build] default_variant`` and ``[build] default_image`` have no effect."""
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a"), BuildVariant(name="b")], default_variant="b", default_image="app")

        config_hash, err = resolve_build(temp_db, "proj", cfg, "", "")

        assert config_hash is None, f"a default selected {config_hash}"
        assert err is not None and "names no build" in err, err

    def test_a_variant_alone_does_not_complete_its_only_image(self, temp_db):
        """``b`` holds one image today, and the next index run can add one."""
        self._seed(temp_db)

        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), "b", "")

        assert config_hash is None, f"a part of the pair selected {config_hash}"
        assert err is not None and 'exactly the arguments variant="b"' in err, err
        assert 'variant="b", image="app"' in err, err

    def test_an_image_alone_does_not_complete_a_variant(self, temp_db):
        self._seed(temp_db)

        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), "", "stage0")

        assert config_hash is None, f"a part of the pair selected {config_hash}"
        assert err is not None and 'variant="a", image="stage0"' in err, err

    @pytest.mark.parametrize(("variant", "image"), [("zzz", ""), ("a", "zzz"), ("*", "")])
    def test_an_unknown_name_is_refused_with_the_list(self, temp_db, variant: str, image: str):
        self._seed(temp_db)

        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), variant, image)

        assert config_hash is None
        assert err is not None and "No build of this project" in err, err
        assert 'variant="b", image="app"' in err, err

    def test_an_indexed_variant_that_the_config_does_not_declare_answers(self, temp_db):
        """The index is the fact.  A config that declares only 'a' must not hide 'b'."""
        self._seed(temp_db)

        config_hash, err = resolve_build(temp_db, "proj", self._cfg([BuildVariant(name="a")]), "b", "app")

        assert (config_hash, err) == ("h-b-app", None)

    def test_a_declared_variant_that_is_not_indexed_gives_the_command(self, temp_db):
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a"), BuildVariant(name="ghost")])

        config_hash, err = resolve_build(temp_db, "proj", cfg, "ghost", "")

        assert config_hash is None
        assert err is not None and "not indexed" in err and "--variant ghost" in err, err

    def test_every_build_answers_for_its_exact_pair(self, temp_db):
        """One selection, one ``config_hash``, and no two selections share it."""
        self._seed(temp_db)
        seen: dict[str, tuple[str, str]] = {}
        for variant, image in (("a", "app"), ("a", "stage0"), ("b", "app")):
            config_hash, err = resolve_build(temp_db, "proj", self._cfg(), variant, image)
            assert err is None, f"{variant}/{image}: {err}"
            assert config_hash is not None, f"{variant}/{image} named no build"
            assert config_hash not in seen, f"{variant}/{image} and {seen.get(config_hash)} share one build"
            seen[config_hash] = (variant, image)

    def test_a_build_without_an_image_name_is_selected_without_image(self, temp_db):
        self._rows(temp_db, [("h-a", "a", ""), ("h-b", "b", "")])

        assert resolve_build(temp_db, "proj", self._cfg(), "a", "") == ("h-a", None)
        _hash, err = resolve_build(temp_db, "proj", self._cfg(), "", "")
        assert err is not None and 'variant="a"\n' in err, f"the list must give the variant alone: {err}"

    def test_an_older_build_of_a_pair_is_not_one_more_build(self, temp_db):
        """An interrupted run leaves an older build of a pair behind, and the newest one answers."""
        self._rows(temp_db, [("h-old", "a", "app"), ("h-new", "a", "app")])

        assert resolve_build(temp_db, "proj", self._cfg(), "", "") == ("h-new", None)

    def test_a_build_that_is_still_indexing_is_not_a_build(self, temp_db):
        self._seed(temp_db)
        temp_db.execute("UPDATE build_configs SET manifest_verification = 'indexing' WHERE config_hash = 'h-b-app'")

        _hash, err = resolve_build(temp_db, "proj", self._cfg(), "", "")

        assert err is not None and 'variant="b"' not in err, err

    def test_a_build_without_names_is_not_selected_among_others(self, temp_db):
        """A call without a selector on several builds gets the list, never the unnamed build.

        An index from before images holds a build without names next to the
        images of a later run that failed for the application.  The call
        selected that old build in silence, and the caller did not learn
        that the bootloader is there too.
        """
        self._rows(temp_db, [("h-old", "", ""), ("h-boot", "", "bootloader")])

        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), "", "")

        assert config_hash is None, f"the unnamed build answered: {config_hash}"
        assert err is not None and 'image="bootloader"' in err, err
        assert "No call can select it" in err and "fw-context index" in err, err

    def test_no_build_is_no_refusal(self, temp_db):
        """The caller decides between an empty answer and an error."""
        assert resolve_build(temp_db, "proj", self._cfg(), "", "") == (None, None)

    @pytest.mark.parametrize("state", ["empty", "indexing"])
    def test_a_named_build_without_a_completed_build_is_refused(self, temp_db, state: str):
        """During the first index run every row is still indexing.

        ``(None, None)`` gives an empty list in the search tools, and an
        empty list reads as "no such code".
        """
        if state == "indexing":
            self._seed(temp_db)
            temp_db.execute("UPDATE build_configs SET manifest_verification = 'indexing'")

        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), "a", "app")

        assert config_hash is None
        assert err is not None and "no completed build" in err and "fw-context index" in err, err

    def test_the_refusal_names_the_status_tool(self, temp_db):
        from fw_context_mcp.mcp.shared.variants import STATUS_TOOL

        self._seed(temp_db)

        _hash, err = resolve_build(temp_db, "proj", self._cfg(), "", "")

        assert err is not None and f"{STATUS_TOOL}()" in err, err
