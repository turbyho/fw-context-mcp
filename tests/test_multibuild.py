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

    def test_an_indexed_variant_makes_a_project_multi_build(self, temp_db):
        """The INDEX is the fact; the config only declares an intention.

        An index run with ``--variant`` writes the variant name into
        build_configs.  A config that no longer declares that variant — it
        was edited, or the index came from elsewhere — used to send the
        query down the single-project path, where the NEWEST build wins in
        silence.  Measured on a seeded index holding an application and a
        bootloader, a query that named no build got the bootloader.
        """
        self._seed(temp_db)
        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), "", "")

        assert config_hash is None, (
            f"a query that named no build got {config_hash!r} of three builds"
        )
        assert err is not None and "variant" in err, f"got: {err}"
        assert "a" in err and "b" in err, f"the error names no choice: {err}"

    def test_the_indexed_names_reach_the_error(self, temp_db):
        """A config that declares nothing must still name the real choices."""
        self._seed(temp_db)
        _hash, err = resolve_build(temp_db, "proj", self._cfg(), "zzz", "")
        assert err is not None and "Unknown variant" in err
        assert "a" in err and "b" in err, f"got: {err}"

    def test_an_indexed_variant_still_answers_when_named(self, temp_db):
        """Fail-closed must not mean unreachable."""
        self._seed(temp_db)
        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), "a", "stage0")
        assert err is None, f"got: {err}"
        assert config_hash == "h-a-stage"

    def test_single_project_needs_no_selector(self, temp_db):
        from fw_context_mcp.indexer.db import transaction, upsert_build_config, upsert_project

        with transaction(temp_db):
            upsert_project(temp_db, "proj", "t", "/tmp/t")
            upsert_build_config(temp_db, "h-single", "proj", "/tmp/cc.json")

        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), "", "")
        assert err is None
        assert config_hash == "h-single"

    def _single(self, temp_db):
        """A project with ONE build and no declared variant."""
        from fw_context_mcp.indexer.db import (
            transaction,
            upsert_build_config,
            upsert_project,
        )

        with transaction(temp_db):
            upsert_project(temp_db, "proj", "t", "/tmp/t")
            upsert_build_config(temp_db, "h-single", "proj", "/tmp/cc.json")

    @pytest.mark.parametrize(
        ("variant", "image"),
        [("zzz", ""), ("", "zzz"), ("zzz", "yyy"), ("*", "")],
        ids=["variant", "image", "both", "star"],
    )
    def test_a_single_build_project_refuses_a_named_build(
        self, temp_db, variant: str, image: str
    ):
        """Naming a build that cannot exist must fail, not answer.

        This branch returned the active build without reading the two
        arguments at all, thus every fail-closed check below it — the
        refusal of ``variant="*"`` included — was out of reach for a
        project that declares no variant.  Measured over 13 indexed
        builds: the six single-build ones answered ``variant="zzz"`` with
        rows while the seven Zephyr images refused it by name.
        """
        self._single(temp_db)

        config_hash, err = resolve_build(temp_db, "proj", self._cfg(), variant, image)

        assert config_hash is None, "a named build must not answer from another"
        assert err is not None
        assert "ONE build" in err, err
        if variant:
            assert repr(variant) in err, err
        if image:
            assert repr(image) in err, err

    def test_multi_fail_closed_without_default(self, temp_db):
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a"), BuildVariant(name="b")])
        config_hash, err = resolve_build(temp_db, "proj", cfg, "", "")
        assert err is not None and "default_variant" in err
        assert config_hash is None

    def test_default_variant_still_needs_an_image(self, temp_db):
        """A variant with several images has not said which program to read.

        Variant 'a' holds 'app' and 'stage0', and those are separate
        binaries.  Answering for both would blend a loader with the
        application, thus the selection fails closed and names the choice.
        """
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a"), BuildVariant(name="b")], default_variant="a")
        config_hash, err = resolve_build(temp_db, "proj", cfg, "", "")
        assert config_hash is None
        assert err is not None and "image" in err
        assert "app" in err and "stage0" in err, f"the error names no choice: {err}"

    def test_a_variant_with_one_image_needs_no_image(self, temp_db):
        """With nothing to confuse, the query answers."""
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a"), BuildVariant(name="b")], default_variant="b")
        config_hash, err = resolve_build(temp_db, "proj", cfg, "", "")
        assert err is None
        assert config_hash == "h-b-app"

    def test_variant_star_is_refused(self, temp_db):
        """One query answers for ONE build — see shared/variants.py.

        The star used to answer for every build at once.  A bootloader and
        an application are separate programs, thus that answer served no
        question, and two builds of one application repeated nearly every
        row.
        """
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a"), BuildVariant(name="b")])
        config_hash, err = resolve_build(temp_db, "proj", cfg, "*", "")
        assert config_hash is None
        assert err is not None and "ONE build" in err
        assert "ask twice" in err, f"the error gives no way forward: {err}"

    def test_unknown_variant_errors(self, temp_db):
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a")])
        config_hash, err = resolve_build(temp_db, "proj", cfg, "zzz", "")
        assert err is not None and "Unknown variant" in err

    def test_a_declared_variant_does_not_hide_an_indexed_one(self, temp_db):
        """The declared names and the indexed ones are UNITED.

        ``declared or indexed`` read the index only when the config
        declared nothing at all.  One declared variant therefore hid every
        other variant the index holds: 'b' came back as unknown and its
        build was out of reach, with no command to reach it — config.toml
        would have to be edited to query a build that already exists.
        """
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a")])

        config_hash, err = resolve_build(temp_db, "proj", cfg, "b", "")

        assert err is None, f"an indexed variant was refused: {err}"
        assert config_hash == "h-b-app"

    def test_the_refusal_names_the_indexed_variant_too(self, temp_db):
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a")])

        _hash, err = resolve_build(temp_db, "proj", cfg, "", "")

        assert err is not None, "three builds, and no selector: this must refuse"
        # The whole list, and not the letter alone — "b" also sits inside
        # the word "build", which every one of these messages carries.
        assert "one of: a, b" in err, (
            f"the refusal hides a build that exists: {err}"
        )

    def test_a_declared_variant_that_is_not_indexed_still_says_so(self, temp_db):
        """The union must not turn 'declared but never built' into 'unknown'."""
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a"), BuildVariant(name="ghost")])

        config_hash, err = resolve_build(temp_db, "proj", cfg, "ghost", "")

        assert config_hash is None
        assert err is not None and "not indexed" in err, f"got: {err}"

    def test_unknown_image_names_the_known_ones(self, temp_db):
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a")])
        config_hash, err = resolve_build(temp_db, "proj", cfg, "a", "zzz")
        assert config_hash is None
        assert err is not None and "Unknown image" in err
        assert "app" in err and "stage0" in err, f"got: {err}"

    def test_specific_image_narrows(self, temp_db):
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a")])
        config_hash, err = resolve_build(temp_db, "proj", cfg, "a", "stage0")
        assert err is None and config_hash == "h-a-stage"

    def test_every_selection_names_its_own_build(self, temp_db):
        """The contract the rest of the code now rests on.

        One selection, one ``config_hash``, and no two selections share it.
        """
        self._seed(temp_db)
        cfg = self._cfg([BuildVariant(name="a"), BuildVariant(name="b")])
        seen: dict[str, tuple[str, str]] = {}
        for variant, image in (("a", "app"), ("a", "stage0"), ("b", "app")):
            config_hash, err = resolve_build(temp_db, "proj", cfg, variant, image)
            assert err is None, f"{variant}/{image}: {err}"
            assert config_hash is not None, f"{variant}/{image} named no build"
            assert config_hash not in seen, (
                f"{variant}/{image} and {seen.get(config_hash)} share one build"
            )
            seen[config_hash] = (variant, image)
