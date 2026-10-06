"""Each variant of ``[[build.variants]]`` gets its own build and its own database.

Without sysbuild, ``_run_multi`` builds every variant first and indexes them
after that.  Each build once returned one shared ``compile_commands.json``,
thus each variant was indexed from the database of the LAST variant.  Now
each variant builds into its own output directory,
``.fw-context/build/<variant>/out`` (see ``build_layout``), and the variant
discovery (``_discover_existing_cc``) reads the same directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.indexer.build import (
    BuildConfig,
    BuildVariant,
    generate_compile_commands,
)
from fw_context_mcp.indexer.build_layout import BuildLayout
from fw_context_mcp.utils import cc_output_path


class _BoardBuilder:
    """A backend whose database names the board of the variant it built."""

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        target = cc_output_path(project_root, cfg)
        target.write_text(json.dumps([{"file": f"{cfg.board}.c"}]), encoding="utf-8")
        return target


@pytest.fixture
def board_backend(monkeypatch):
    """Put ``_BoardBuilder`` in both registries under the name ``fake``."""
    import fw_context_mcp.indexer.build as build_mod
    import fw_context_mcp.indexer.builders as builders_pkg

    registry = SimpleNamespace(
        get=lambda name: _BoardBuilder if name == "fake" else None,
        keys=lambda: ["fake"],
    )
    monkeypatch.setattr(build_mod, "_builder_registry", registry)
    monkeypatch.setattr(builders_pkg, "registry", registry)


def test_the_variants_are_indexed_from_their_own_database(
    monkeypatch, tmp_path: Path, board_backend
):
    """THE regression test: two variants, two databases, two different contents."""
    import fw_context_mcp.indexer.runner as runner_mod
    from fw_context_mcp.cli import _index as index_mod

    seen: dict[str, tuple[Path, list]] = {}

    def _run(**kw) -> str:
        path = Path(kw["compile_commands"])
        seen[kw["variant"]] = (path, json.loads(path.read_text(encoding="utf-8")))
        return "0" * 64

    monkeypatch.setattr(runner_mod, "run", _run)

    build = BuildConfig(
        system="fake",
        variants=[BuildVariant(name="alpha", board="a"), BuildVariant(name="beta", board="b")],
    )
    cfg = SimpleNamespace(build=build)
    args = SimpleNamespace(build=True, no_index=False)
    run_kwargs = {"vendor_paths": [], "project_paths": []}

    index_mod._run_multi(args, cfg, tmp_path, "pid", tmp_path / "index.db", "fake", run_kwargs)

    layout = BuildLayout(tmp_path)
    assert seen == {
        "alpha": (layout.out_dir("alpha") / "compile_commands.json", [{"file": "a.c"}]),
        "beta": (layout.out_dir("beta") / "compile_commands.json", [{"file": "b.c"}]),
    }


def test_the_discovery_finds_what_a_variant_build_wrote(tmp_path: Path, board_backend):
    """A run without ``--build`` must find the file that a build wrote."""
    from fw_context_mcp.cli import _index as index_mod

    variant = BuildVariant(name="alpha", board="a")
    build = BuildConfig(system="fake", variants=[variant])
    written = generate_compile_commands(tmp_path, BuildConfig(system="fake", board="a", variant_name="alpha"))

    found = index_mod._discover_existing_cc(tmp_path, [variant], build, _BoardBuilder())

    assert [(name, image, path) for name, image, path, _ in found] == [("alpha", "", written)]


def test_sysbuild_is_chosen_for_each_variant(monkeypatch, tmp_path: Path, board_backend):
    """sysbuild is a [build] key that a variant can override.

    The choice of the build command and the discovery of the images must read
    the same config, or the set of (variant, image) changes between a run
    with --build and a run without it.
    """
    import fw_context_mcp.indexer.runner as runner_mod
    from fw_context_mcp.cli import _index as index_mod

    multi_built: list[list[str]] = []

    class _SysbuildBoardBuilder(_BoardBuilder):
        def build_multi(self, project_root, cfg, variants):
            multi_built.append([v.name for v in variants])
            return [(v.name, "app", tmp_path / f"{v.name}.json") for v in variants]

    import fw_context_mcp.indexer.build as build_mod
    import fw_context_mcp.indexer.builders as builders_pkg

    registry = SimpleNamespace(get=lambda name: _SysbuildBoardBuilder if name == "fake" else None, keys=lambda: ["fake"])
    monkeypatch.setattr(build_mod, "_builder_registry", registry)
    monkeypatch.setattr(builders_pkg, "registry", registry)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(runner_mod, "run", lambda **kw: seen.append((kw["variant"], kw["image"])) or "0" * 64)

    build = BuildConfig(
        system="fake",
        variants=[
            BuildVariant(name="multi", board="a", overrides={"sysbuild": True}),
            BuildVariant(name="single", board="b"),
        ],
    )
    index_mod._run_multi(
        SimpleNamespace(build=True, no_index=False), SimpleNamespace(build=build), tmp_path, "pid",
        tmp_path / "index.db", "fake", {"vendor_paths": [], "project_paths": []},
    )

    assert multi_built == [["multi"]]
    assert sorted(seen) == [("multi", "app"), ("single", "")]


def test_a_variant_without_a_build_is_reported(tmp_path: Path, board_backend, capsys):
    from fw_context_mcp.cli import _index as index_mod

    variant = BuildVariant(name="alpha", board="a")
    found = index_mod._discover_existing_cc(
        tmp_path, [variant], BuildConfig(system="fake", variants=[variant]), _BoardBuilder()
    )

    assert found == []
    assert "no build artifacts for variant 'alpha'" in capsys.readouterr().err


def test_the_build_without_variants_has_its_own_directory(tmp_path: Path, board_backend):
    result = generate_compile_commands(tmp_path, BuildConfig(system="fake", board="a"))

    assert result == BuildLayout(tmp_path).out_dir("") / "compile_commands.json"


def test_a_variant_name_that_cannot_be_a_directory_stops_the_run_before_any_build(
    monkeypatch, tmp_path: Path, board_backend, capsys
):
    """The name is a directory name, and the config is wrong as a whole."""
    import fw_context_mcp.indexer.runner as runner_mod
    from fw_context_mcp.cli import _index as index_mod

    seen: list[str] = []
    monkeypatch.setattr(runner_mod, "run", lambda **kw: seen.append(kw["variant"]) or "0" * 64)

    build = BuildConfig(
        system="fake",
        variants=[BuildVariant(name="nrf/dev", board="a"), BuildVariant(name="beta", board="b")],
    )
    args = SimpleNamespace(build=True, no_index=False)
    run_kwargs = {"vendor_paths": [], "project_paths": []}

    assert index_mod._run_multi(
        args, SimpleNamespace(build=build), tmp_path, "pid", tmp_path / "index.db", "fake", run_kwargs
    ) == 1
    assert seen == []
    assert not (tmp_path / ".fw-context").exists()
    assert "'nrf/dev'" in capsys.readouterr().err


def test_two_variant_names_of_one_directory_stop_the_run(
    monkeypatch, tmp_path: Path, board_backend, capsys
):
    """macOS and Windows file systems do not tell case apart."""
    from fw_context_mcp.cli import _index as index_mod

    build = BuildConfig(
        system="fake",
        variants=[BuildVariant(name="Dev", board="a"), BuildVariant(name="dev", board="b")],
    )
    args = SimpleNamespace(build=True, no_index=False)

    assert index_mod._run_multi(
        args, SimpleNamespace(build=build), tmp_path, "pid", tmp_path / "index.db", "fake",
        {"vendor_paths": [], "project_paths": []},
    ) == 1
    assert "differ only in case" in capsys.readouterr().err
