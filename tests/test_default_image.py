"""Tests for the default image: the program that a query without ``image`` gets.

An ESP-IDF build makes two programs, the application and its bootloader,
and both are indexed as images of one build.  A query without ``image``
gets ``[build] default_image``, else the image whose index read the database
of the application, else an error that lists the images.
"""

import argparse
import json
from pathlib import Path

import pytest

from fw_context_mcp.config.settings import Config
from fw_context_mcp.indexer.build import BuildConfig, BuildVariant
from fw_context_mcp.indexer.build_layout import BuildLayout
from fw_context_mcp.indexer.builders import application_database, output_compile_commands
from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem
from fw_context_mcp.mcp.shared.variants import active_build, default_image, resolve_build

APP = "fwctx_app"


def _esp_idf_build(out_dir: Path, *, with_bootloader: bool = True) -> None:
    """Write the files that ``idf.py build`` leaves in *out_dir*: databases and descriptions."""
    programs = [(out_dir, APP)]
    if with_bootloader:
        programs.append((out_dir / "bootloader", "bootloader"))
    for build_dir, name in programs:
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "compile_commands.json").write_text("[]", encoding="utf-8")
        (build_dir / "project_description.json").write_text(
            json.dumps({"project_name": name, "app_elf": f"{name}.elf"}), encoding="utf-8"
        )


def _cfg(system: str | None = None, default_image_name: str | None = None, variants=None, default_variant=None):
    cfg = Config()
    cfg.build.system = system
    cfg.build.default_image = default_image_name
    cfg.build.variants = variants or []
    cfg.build.default_variant = default_variant
    return cfg


def _seed(conn, builds: list[tuple[str, str, str, Path]]) -> None:
    """Index rows ``(config_hash, variant, image, database)`` in this order: the last one is the newest."""
    from fw_context_mcp.indexer.db import transaction, upsert_build_config, upsert_project

    with transaction(conn):
        upsert_project(conn, "proj", "t", "/tmp/t")
        for config_hash, variant, image, database in builds:
            upsert_build_config(conn, config_hash, "proj", str(database), variant=variant, image=image)


def _two_images(conn, root: Path, *, application_last: bool = True) -> None:
    """A build without variants that indexed the bootloader and the application."""
    out_dir = BuildLayout(root).out_dir("")
    app = ("h-app", "", APP, out_dir / "compile_commands.json")
    boot = ("h-boot", "", "bootloader", out_dir / "bootloader" / "compile_commands.json")
    _seed(conn, [boot, app] if application_last else [app, boot])


class TestEspIdfImages:
    def test_two_images_named_by_their_descriptions(self, tmp_path):
        _esp_idf_build(tmp_path)

        found = ESPIDFBuildSystem().output_compile_commands(tmp_path, BuildConfig())

        assert found == {
            APP: tmp_path / "compile_commands.json",
            "bootloader": tmp_path / "bootloader" / "compile_commands.json",
        }

    def test_a_database_without_a_description_is_no_build(self, tmp_path):
        """The image has no name without its description, and the build is not complete."""
        _esp_idf_build(tmp_path)
        (tmp_path / "bootloader" / "project_description.json").unlink()

        found = ESPIDFBuildSystem().output_compile_commands(tmp_path, BuildConfig())

        assert found == {APP: tmp_path / "compile_commands.json"}

    def test_no_build_gives_nothing(self, tmp_path):
        assert ESPIDFBuildSystem().output_compile_commands(tmp_path, BuildConfig()) == {}

    def test_the_application_database_is_a_path_and_needs_no_build(self, tmp_path):
        assert application_database(ESPIDFBuildSystem(), tmp_path) == tmp_path / "compile_commands.json"

    def test_a_backend_without_the_method_names_no_application(self, tmp_path):
        class OneProgram:
            pass

        assert application_database(OneProgram(), tmp_path) is None
        assert application_database(None, tmp_path) is None

    def test_the_dispatcher_reaches_the_backend(self, tmp_path):
        _esp_idf_build(tmp_path)
        assert set(output_compile_commands(ESPIDFBuildSystem(), tmp_path, BuildConfig())) == {APP, "bootloader"}


class TestResolveWithoutVariants:
    def test_the_build_system_names_the_application(self, temp_db, tmp_path):
        """No config key is necessary: ESP-IDF says which database is the application."""
        _two_images(temp_db, tmp_path)

        config_hash, err = resolve_build(temp_db, "proj", _cfg("esp-idf"), project_root=tmp_path)

        assert err is None, err
        assert config_hash == "h-app"

    def test_no_build_output_is_necessary(self, temp_db, tmp_path):
        """A clean build removes out/ for a while, and the index stays complete."""
        _two_images(temp_db, tmp_path)
        assert not BuildLayout(tmp_path).root.exists()

        config_hash, err = resolve_build(temp_db, "proj", _cfg("esp-idf"), project_root=tmp_path)

        assert err is None, err
        assert config_hash == "h-app"

    def test_the_config_comes_before_the_build_system(self, temp_db, tmp_path):
        _two_images(temp_db, tmp_path)

        config_hash, err = resolve_build(
            temp_db, "proj", _cfg("esp-idf", default_image_name="bootloader"), project_root=tmp_path
        )

        assert err is None, err
        assert config_hash == "h-boot"

    def test_the_newest_build_does_not_decide(self, temp_db, tmp_path):
        """The default is the application, and not the program that the index run did last."""
        _two_images(temp_db, tmp_path, application_last=False)

        config_hash, err = resolve_build(temp_db, "proj", _cfg("esp-idf"), project_root=tmp_path)

        assert err is None, err
        assert config_hash == "h-app"

    def test_an_application_that_is_not_indexed_is_an_error(self, temp_db, tmp_path):
        """The one image that is there is the bootloader, and the query is about the application."""
        out_dir = BuildLayout(tmp_path).out_dir("")
        _seed(temp_db, [("h-boot", "", "bootloader", out_dir / "bootloader" / "compile_commands.json")])

        config_hash, err = resolve_build(temp_db, "proj", _cfg("esp-idf"), project_root=tmp_path)

        assert config_hash is None
        assert err is not None and "application" in err and "not indexed" in err, err
        assert "bootloader" in err, err

    def test_a_project_at_another_path_finds_its_application(self, temp_db, tmp_path):
        """A moved project, a second clone and a git worktree share the index of the first path."""
        _two_images(temp_db, tmp_path / "old", application_last=False)

        config_hash, err = resolve_build(temp_db, "proj", _cfg("esp-idf"), project_root=tmp_path / "new")

        assert err is None, err
        assert config_hash == "h-app"

    def test_the_build_from_before_images_is_the_application(self, temp_db, tmp_path):
        """After an upgrade the index holds the old build without an image name and the new bootloader.

        The old build is the application: a build of several programs then
        indexed only the database that the build returned.  It answers
        until the first run indexes the application as an image, and
        that run removes it.
        """
        out_dir = BuildLayout(tmp_path).out_dir("")
        _seed(temp_db, [
            ("h-old", "", "", tmp_path / "build" / "compile_commands.json"),
            ("h-boot", "", "bootloader", out_dir / "bootloader" / "compile_commands.json"),
        ])

        config_hash, err = resolve_build(temp_db, "proj", _cfg("esp-idf"), project_root=tmp_path)

        assert err is None, err
        assert config_hash == "h-old"

    def test_one_image_answers_when_no_application_is_named(self, temp_db, tmp_path):
        _seed(temp_db, [("h-only", "", "only", tmp_path / "cc.json")])

        config_hash, err = resolve_build(temp_db, "proj", _cfg("cmake"), project_root=tmp_path)

        assert err is None, err
        assert config_hash == "h-only"

    def test_without_a_default_the_query_must_name_the_image(self, temp_db, tmp_path):
        """No project root and no config key: fail closed, and name the choices."""
        _two_images(temp_db, tmp_path)

        config_hash, err = resolve_build(temp_db, "proj", _cfg("esp-idf"))

        assert config_hash is None
        assert err is not None and "image" in err
        assert APP in err and "bootloader" in err, err
        assert "default_image" in err, err

    def test_a_named_image_answers(self, temp_db, tmp_path):
        _two_images(temp_db, tmp_path)

        config_hash, err = resolve_build(temp_db, "proj", _cfg("esp-idf"), "", "bootloader", project_root=tmp_path)

        assert err is None, err
        assert config_hash == "h-boot"

    def test_an_unknown_image_names_the_known_ones(self, temp_db, tmp_path):
        _two_images(temp_db, tmp_path)

        config_hash, err = resolve_build(temp_db, "proj", _cfg("esp-idf"), "", "zzz", project_root=tmp_path)

        assert config_hash is None
        assert err is not None and "Unknown image 'zzz'" in err
        assert APP in err and "bootloader" in err, err

    def test_a_variant_names_nothing_and_the_error_names_the_images(self, temp_db, tmp_path):
        _two_images(temp_db, tmp_path)

        config_hash, err = resolve_build(temp_db, "proj", _cfg("esp-idf"), "zzz", "", project_root=tmp_path)

        assert config_hash is None
        assert err is not None and "declares no variants" in err, err
        assert APP in err and "bootloader" in err, err


class TestResolveInAVariant:
    def _seed(self, conn) -> None:
        _seed(conn, [
            ("h-a-app", "a", "app", Path("/tmp/a")),
            ("h-a-boot", "a", "mcuboot", Path("/tmp/b")),
            ("h-b-app", "b", "app", Path("/tmp/c")),
            ("h-b-boot", "b", "mcuboot", Path("/tmp/d")),
        ])

    def test_default_image_applies_to_the_default_variant(self, temp_db):
        self._seed(temp_db)
        cfg = _cfg(variants=[BuildVariant(name="a"), BuildVariant(name="b")], default_variant="a",
                   default_image_name="app")

        config_hash, err = resolve_build(temp_db, "proj", cfg)

        assert err is None, err
        assert config_hash == "h-a-app"

    def test_default_image_applies_to_a_named_variant_that_holds_it(self, temp_db):
        self._seed(temp_db)
        cfg = _cfg(variants=[BuildVariant(name="a"), BuildVariant(name="b")], default_variant="a",
                   default_image_name="app")

        config_hash, err = resolve_build(temp_db, "proj", cfg, "b")

        assert err is None, err
        assert config_hash == "h-b-app"

    def test_a_default_image_that_the_variant_does_not_hold_fails_closed(self, temp_db):
        self._seed(temp_db)
        cfg = _cfg(variants=[BuildVariant(name="a"), BuildVariant(name="b")], default_variant="a",
                   default_image_name="stage0")

        config_hash, err = resolve_build(temp_db, "proj", cfg)

        assert config_hash is None
        assert err is not None and "app" in err and "mcuboot" in err, err

    def test_an_esp_idf_variant_gets_its_application(self, temp_db, tmp_path):
        out_dir = BuildLayout(tmp_path).out_dir("esp32s3")
        _seed(temp_db, [
            ("h-app", "esp32s3", APP, out_dir / "compile_commands.json"),
            ("h-boot", "esp32s3", "bootloader", out_dir / "bootloader" / "compile_commands.json"),
        ])
        cfg = _cfg("esp-idf", variants=[BuildVariant(name="esp32s3")], default_variant="esp32s3")

        config_hash, err = resolve_build(temp_db, "proj", cfg, project_root=tmp_path)

        assert err is None, err
        assert config_hash == "h-app"


class TestActiveBuild:
    def test_the_tools_without_a_selector_get_the_application(self, temp_db, tmp_path):
        """smart_search and semantic_search answer for this build, and get_active_build reports it."""
        _two_images(temp_db, tmp_path, application_last=False)

        row, refusal = active_build(temp_db, "proj", _cfg("esp-idf"), tmp_path)

        assert refusal is None
        assert row is not None and row["config_hash"] == "h-app"

    def test_several_variants_without_a_default_give_the_newest_build(self, temp_db):
        """The tools without a selector keep the newest build, as they always did."""
        _seed(temp_db, [("h-a", "a", "", Path("/tmp/a")), ("h-b", "b", "", Path("/tmp/b"))])

        row, refusal = active_build(temp_db, "proj", _cfg(), None)

        assert refusal is None
        assert row is not None and row["config_hash"] == "h-b"

    def test_an_application_that_is_not_indexed_gives_no_build(self, temp_db, tmp_path):
        """The first index run makes the bootloader in minutes and the application in hours.

        The tools without a selector answered from the bootloader in that
        time, while every other tool refused.
        """
        out_dir = BuildLayout(tmp_path).out_dir("")
        _seed(temp_db, [("h-boot", "", "bootloader", out_dir / "bootloader" / "compile_commands.json")])

        row, refusal = active_build(temp_db, "proj", _cfg("esp-idf"), tmp_path)

        assert row is None
        assert refusal is not None and "not indexed" in refusal

    def test_default_image_for_discovery(self, temp_db, tmp_path):
        _two_images(temp_db, tmp_path)
        rows = temp_db.execute("SELECT * FROM build_configs").fetchall()

        assert default_image(_cfg("esp-idf"), tmp_path, "", rows) == APP


class TestSingleBuildImages:
    def test_the_application_is_indexed_last(self, tmp_path):
        """get_active_config gives the newest build, and the daemon reads that build."""
        from fw_context_mcp.cli._index import _single_build_images

        out_dir = BuildLayout(tmp_path).out_dir("")
        _esp_idf_build(out_dir)

        images = _single_build_images(tmp_path, _cfg("esp-idf"), "esp-idf", out_dir / "compile_commands.json", False)

        assert images == [
            ("bootloader", out_dir / "bootloader" / "compile_commands.json"),
            (APP, out_dir / "compile_commands.json"),
        ]

    def test_a_file_of_the_user_is_one_program(self, tmp_path):
        from fw_context_mcp.cli._index import _single_build_images

        _esp_idf_build(BuildLayout(tmp_path).out_dir(""))
        own = tmp_path / "compile_commands.json"

        assert _single_build_images(tmp_path, _cfg("esp-idf"), "esp-idf", own, True) == [("", own)]

    def test_a_build_of_one_program_has_no_image_name(self, tmp_path):
        from fw_context_mcp.cli._index import _single_build_images

        out_dir = BuildLayout(tmp_path).out_dir("")
        out_dir.mkdir(parents=True)
        cc = out_dir / "compile_commands.json"
        cc.write_text("[]", encoding="utf-8")

        assert _single_build_images(tmp_path, _cfg("cmake"), "cmake", cc, False) == [("", cc)]

    def test_a_database_of_no_image_is_an_error(self, tmp_path, capsys):
        from fw_context_mcp.cli._index import _single_build_images

        _esp_idf_build(BuildLayout(tmp_path).out_dir(""))
        other = tmp_path / "elsewhere.json"

        assert _single_build_images(tmp_path, _cfg("esp-idf"), "esp-idf", other, False) is None
        assert "none of them" in capsys.readouterr().err


class TestBuildVariants:
    def test_a_built_esp_idf_variant_has_the_images_that_discovery_finds(self, tmp_path, monkeypatch):
        """A run that builds and a run that reads the build must give one variant the same images.

        The build path recorded the application under the image "" and no
        bootloader, and discovery named both.  Each change of run kind
        changed the config_hash, reindexed the application and retired the
        build of the run before.
        """
        from fw_context_mcp.cli import _index
        from fw_context_mcp.indexer import build as build_module

        def fake_build(project_root, cfg):
            out_dir = BuildLayout(project_root).out_dir(cfg.variant_name)
            _esp_idf_build(out_dir)
            return out_dir / "compile_commands.json"

        monkeypatch.setattr(build_module, "generate_compile_commands", fake_build)
        build_cfg = _cfg("esp-idf", variants=[BuildVariant(name="esp32s3")]).build
        variants = build_cfg.variants
        builder = ESPIDFBuildSystem()

        built = _index._build_variants(tmp_path, build_cfg, builder, variants)
        found = _index._discover_existing_cc(tmp_path, variants, build_cfg, builder)

        assert {(v, i, cc) for v, i, cc, _ in built} == {(v, i, cc) for v, i, cc, _ in found}
        assert {i for _, i, _, _ in built} == {APP, "bootloader"}


class TestRunSingle:
    def _run(self, tmp_path, monkeypatch, args, run_fn):
        """Run _run_single over an ESP-IDF build in *tmp_path*, with *run_fn* for the indexer."""
        from fw_context_mcp.cli import _index
        from fw_context_mcp.indexer import runner

        out_dir = BuildLayout(tmp_path).out_dir("")
        _esp_idf_build(out_dir)
        cc = out_dir / "compile_commands.json"
        monkeypatch.setattr(_index, "_resolve_compile_commands", lambda *a, **k: (cc, False))
        monkeypatch.setattr(_index, "_validate_and_fix_artifacts", lambda *a, **k: (cc, [], True))
        monkeypatch.setattr(_index, "_post_index_optimize", lambda *a, **k: None)
        monkeypatch.setattr(runner, "run", run_fn)
        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        return _index._run_single(args, _cfg("esp-idf"), tmp_path, "proj", db_path, "esp-idf", False, {})

    @staticmethod
    def _args(**kw) -> argparse.Namespace:
        return argparse.Namespace(image=kw.get("image"), exclude_image=kw.get("exclude_image", []),
                                  variant=None, variants=None)

    def test_a_failed_bootloader_does_not_keep_the_application_out(self, tmp_path, monkeypatch, capsys):
        indexed: list[str] = []

        def run_fn(*, image="", **kwargs):
            if image == "bootloader":
                raise ValueError("parse failure")
            indexed.append(image)
            return "h" * 16

        code = self._run(tmp_path, monkeypatch, self._args(), run_fn)

        assert indexed == [APP]
        assert code == 1
        assert "image=bootloader" in capsys.readouterr().err

    def test_a_failed_image_retires_no_build(self, tmp_path, monkeypatch):
        """The build from before the images can be the only complete one."""
        retired: list = []
        monkeypatch.setattr(
            "fw_context_mcp.indexer._postprocess.cleanup_retired_builds",
            lambda *a, **k: retired.append(a) or [],
        )

        def run_fn(*, image="", **kwargs):
            raise ValueError("system failure")

        assert self._run(tmp_path, monkeypatch, self._args(), run_fn) == 1
        assert retired == []

    def test_a_run_that_indexed_every_image_retires_the_old_builds(self, tmp_path, monkeypatch):
        retired: list = []
        monkeypatch.setattr(
            "fw_context_mcp.indexer._postprocess.cleanup_retired_builds",
            lambda *a, **k: retired.append(a[4]) or [],
        )

        assert self._run(tmp_path, monkeypatch, self._args(), lambda **k: "h" * 16) == 0
        assert retired == [{("", "bootloader"), ("", APP)}]

    def test_the_image_option_on_a_build_of_one_program_is_an_error(self, tmp_path, monkeypatch, capsys):
        from fw_context_mcp.cli import _index
        from fw_context_mcp.indexer import runner

        out_dir = BuildLayout(tmp_path).out_dir("")
        out_dir.mkdir(parents=True)
        cc = out_dir / "compile_commands.json"
        cc.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(_index, "_resolve_compile_commands", lambda *a, **k: (cc, False))
        monkeypatch.setattr(_index, "_validate_and_fix_artifacts", lambda *a, **k: (cc, [], True))
        monkeypatch.setattr(runner, "run", lambda **k: pytest.fail("nothing may be indexed"))

        code = _index._run_single(
            self._args(image="app"), _cfg("cmake"), tmp_path, "proj", tmp_path / "index.db", "cmake", False, {},
        )

        assert code == 1
        assert "one program" in capsys.readouterr().err


class TestDiscovery:
    def test_active_image_of_a_build_without_variants(self, tmp_path):
        """get_active_build reports the image that a query without ``image`` gets."""
        from fw_context_mcp.mcp.handlers.maintenance import _build_variant_discovery

        out_dir = BuildLayout(tmp_path).out_dir("")
        builds = [
            {"variant": "", "image": "bootloader", "board": "", "config_hash": "h-boot",
             "compile_commands_path": str(out_dir / "bootloader" / "compile_commands.json")},
            {"variant": "", "image": APP, "board": "", "config_hash": "h-app",
             "compile_commands_path": str(out_dir / "compile_commands.json")},
        ]

        discovery = _build_variant_discovery(_cfg("esp-idf"), builds, tmp_path)

        assert discovery["multi"] is False
        assert {img["name"] for img in discovery["images"]} == {APP, "bootloader"}
        assert discovery["active_image"] == APP



class TestRunMulti:
    def test_a_variant_with_a_failed_image_keeps_its_builds(self, tmp_path, monkeypatch):
        """Its build without an image name, from before the images, can be its only complete application."""
        from fw_context_mcp.cli import _index
        from fw_context_mcp.indexer import runner

        cc_list = [
            ("a", "bootloader", tmp_path / "a-boot.json", ""),
            ("a", APP, tmp_path / "a-app.json", ""),
            ("b", "bootloader", tmp_path / "b-boot.json", ""),
            ("b", APP, tmp_path / "b-app.json", ""),
        ]
        monkeypatch.setattr(_index, "_discover_existing_cc", lambda *a, **k: list(cc_list))
        monkeypatch.setattr(_index, "_post_index_optimize", lambda *a, **k: None)

        def run_fn(*, variant="", image="", **kwargs):
            if (variant, image) == ("a", APP):
                raise ValueError("parse failure")
            return "h" * 16

        monkeypatch.setattr(runner, "run", run_fn)
        built: list = []
        monkeypatch.setattr(
            "fw_context_mcp.indexer._postprocess.cleanup_retired_builds",
            lambda conn, pid, db_dir, variants, pairs: built.append(pairs) or [],
        )
        cfg = _cfg("esp-idf", variants=[BuildVariant(name="a"), BuildVariant(name="b")])
        args = argparse.Namespace(build=False, background=False, no_index=False, image=None,
                                  exclude_image=[], variant=None, variants=None)
        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()

        _index._run_multi(args, cfg, tmp_path, "proj", db_path, "esp-idf",
                          {"vendor_paths": [], "project_paths": []})

        assert built == [{("b", "bootloader"), ("b", APP)}]

    def test_an_unknown_image_name_is_an_error(self, capsys):
        from fw_context_mcp.cli._index import _unknown_images

        cc_list = [("", "bootloader", Path("b"), ""), ("", APP, Path("a"), "")]
        args = argparse.Namespace(image=None, exclude_image=["bootlader"])

        assert "bootlader" in _unknown_images(cc_list, args)
        assert _unknown_images(cc_list, argparse.Namespace(image=APP, exclude_image=[])) == ""


class TestCheckedBuild:
    def test_the_cli_checks_the_build_that_get_active_build_reports(self, temp_db, tmp_path, monkeypatch):
        """The newest build is the bootloader, and the checks must read the application."""
        import fw_context_mcp.cli._index as index_mod
        import fw_context_mcp.config as config_mod

        _two_images(temp_db, tmp_path, application_last=False)
        monkeypatch.setattr(config_mod, "derive_project_id", lambda root: "proj")

        row = index_mod._checked_build(temp_db, tmp_path, _cfg("esp-idf"))

        assert row is not None and row["config_hash"] == "h-app"


class TestDiscoveryCompletedOnly:
    def test_a_build_that_is_still_indexing_is_not_the_active_image(self, tmp_path):
        from fw_context_mcp.mcp.handlers.maintenance import _build_variant_discovery

        out_dir = BuildLayout(tmp_path).out_dir("")
        builds = [
            {"variant": "", "image": APP, "board": "", "config_hash": "h-app",
             "manifest_verification": "indexing",
             "compile_commands_path": str(out_dir / "compile_commands.json")},
            {"variant": "", "image": "bootloader", "board": "", "config_hash": "h-boot",
             "manifest_verification": "full",
             "compile_commands_path": str(out_dir / "bootloader" / "compile_commands.json")},
        ]

        discovery = _build_variant_discovery(_cfg("esp-idf"), builds, tmp_path)

        assert discovery["active_image"] is None
