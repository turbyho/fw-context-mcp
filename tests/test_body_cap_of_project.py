"""``[index] max_symbol_body_lines`` of the project of the query caps the body.

``source._get_max_body_lines`` called ``config.load()`` without a project:
that reads the global config only.  The value in ``config.toml`` or
``local.toml`` of a project had no effect, and a query about one project
got the cap of the global config.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import fw_context_mcp.config.settings as settings

PID = "0123456789abcdef0123456789abcdef"
LONG = "int long_function(int x) {\n" + "".join(f"    x += {n};\n" for n in range(1, 9)) + "    return x;\n}\n"


@pytest.fixture
def project(request, tmp_path, monkeypatch) -> Path:
    """One build of one file; the project caps a body at 3 lines (or ``request.param``), the global config at 50.

    WHY a parameter and not a rewrite of the file in the test: the config
    cache keys on the mtime, and a file system with a coarse mtime keeps
    the old value after a fast rewrite.
    """
    from fw_context_mcp.indexer.runner import run

    cap = getattr(request, "param", 3)

    home = tmp_path / "home" / "config.toml"
    home.parent.mkdir()
    home.write_text("[index]\nmax_symbol_body_lines = 50\n", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", home)
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / "main.c").write_text(LONG, encoding="utf-8")
    index_dir = tmp_path / "index"
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{PID}"\n[index]\nmax_symbol_body_lines = {cap}\n', encoding="utf-8",
    )
    (root / ".fw-context" / "local.toml").write_text(f'[index]\ndb_dir = "{index_dir.as_posix()}"\n', encoding="utf-8")
    cc = root / "compile_commands.json"
    cc.write_text(json.dumps([{"directory": str(root), "file": "main.c", "arguments": ["cc", "-c", "main.c"]}]),
                  encoding="utf-8")
    run(compile_commands=cc, db_path=index_dir / PID / "index.db", project_root=root, project_id=PID,
        index_refs=False, index_embeddings=False, analyze_symbols=False, analyze_overrides=False)
    return root


@pytest.mark.parametrize("tool", ["get_source", "get_symbol_context"])
def test_the_cap_of_the_project_applies(project, tool):
    from fw_context_mcp.mcp.handlers import source

    result = getattr(source, tool)(name="long_function", project_root=str(project))

    lines = result["source"].splitlines()
    assert len(lines) == 3, result["source"]
    assert lines[0].strip().startswith("1") and "long_function" in lines[0]
    assert result["_source_truncated"] is True


@pytest.mark.parametrize("project", [100], indirect=True)
def test_a_body_under_the_cap_is_whole(project):
    from fw_context_mcp.mcp.handlers import source

    result = source.get_source(name="long_function", project_root=str(project))

    assert len(result["source"].splitlines()) == LONG.count("\n"), result["source"]
    assert "_source_truncated" not in result
