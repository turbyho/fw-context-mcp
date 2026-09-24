"""Each variant of ``[[build.variants]]`` gets its own compilation database.

Without sysbuild, ``_run_multi`` builds every variant first and indexes them
after that.  Each build returned the one canonical ``compile_commands.json``,
thus each variant was indexed from the database of the LAST variant.  The
variant discovery (``_discover_existing_cc``) looked for
``compile_commands.<variant>.json``, and no build wrote it.
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
from fw_context_mcp.utils import CC_OUTPUT_REL, cc_output_path


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

    seen: dict[str, list] = {}

    def _run(**kw) -> str:
        seen[kw["variant"]] = json.loads(Path(kw["compile_commands"]).read_text(encoding="utf-8"))
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

    assert seen == {"alpha": [{"file": "a.c"}], "beta": [{"file": "b.c"}]}


def test_the_discovery_finds_what_a_variant_build_wrote(tmp_path: Path, board_backend):
    """A run without ``--build`` must find the file that a build wrote."""
    from fw_context_mcp.cli import _index as index_mod

    variant = BuildVariant(name="alpha", board="a")
    build = BuildConfig(system="fake", variants=[variant])
    generate_compile_commands(
        tmp_path,
        BuildConfig(system="fake", board="a"),
        output=index_mod._variant_cc_path(tmp_path, "alpha"),
    )

    found = index_mod._discover_existing_cc(tmp_path, [variant], build)

    assert [(name, path.name) for name, _, path, _ in found] == [
        ("alpha", "compile_commands.alpha.json")
    ]


def test_the_default_output_is_still_the_canonical_file(tmp_path: Path, board_backend):
    result = generate_compile_commands(tmp_path, BuildConfig(system="fake", board="a"))

    assert result == tmp_path / CC_OUTPUT_REL


def test_an_output_in_another_directory_is_refused(tmp_path: Path, board_backend):
    """``os.replace`` is atomic only in one directory."""
    with pytest.raises(ValueError, match="not atomic"):
        generate_compile_commands(
            tmp_path, BuildConfig(system="fake"), output=tmp_path / "elsewhere.json"
        )


def test_a_variant_name_with_a_slash_does_not_stop_the_others(
    monkeypatch, tmp_path: Path, board_backend, capsys
):
    """``Path.with_name`` refuses "/", and its ValueError stopped the whole run."""
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

    index_mod._run_multi(
        args, SimpleNamespace(build=build), tmp_path, "pid", tmp_path / "index.db", "fake", run_kwargs
    )

    assert seen == ["beta"]
    assert "variant 'nrf/dev'" in capsys.readouterr().err
