"""``fw-context init`` saves a detected embedding model to the global config, one time.

Before, init wrote ``[llm] embed_model`` to ``local.toml`` of each project
after the LLM step.  Each project then selected its own backend: a change
of the global config did not apply to it, and the option "use the global
settings" of the LLM step was undone in the same run.

The tests give the helper a config whose model is already resolved, thus
no test runs the GPU detection or calls Ollama.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

GLOBAL_TEXT = (
    "[index]\n"
    "\n"
    "[llm]\n"
    'model = "global-chat:1b"\n'
    "# CPU only:\n"
    '# embed_model = "qwen3-embedding:0.6b"\n'
    "# GPU:\n"
    '# embed_model = "qwen3-embedding:8b"\n'
)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A project without ``[llm]`` keys, and a global config of *GLOBAL_TEXT*; returns ``(root, global)``."""
    from fw_context_mcp.config import settings

    global_path = tmp_path / "home" / "config.toml"
    global_path.parent.mkdir()
    global_path.write_text(GLOBAL_TEXT, encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_path)
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text("[project]\n", encoding="utf-8")
    (root / ".fw-context" / "local.toml").write_text('[index]\ndb_dir = "/tmp/x"\n', encoding="utf-8")
    return root, global_path


def _persist(root: Path, *, enabled: bool = True, model: str = "detected:1b") -> None:
    from fw_context_mcp.cli._init import _persist_detected_embed_model

    _persist_detected_embed_model(root, SimpleNamespace(llm=SimpleNamespace(enabled=enabled, embed_model=model)))


def test_a_detected_model_goes_to_the_global_config(paths, capsys):
    root, global_path = paths

    _persist(root)

    text = global_path.read_text(encoding="utf-8")
    assert tomllib.loads(text)["llm"]["embed_model"] == "detected:1b", text
    assert '# embed_model = "qwen3-embedding:0.6b"\n' in text and "# CPU only:\n" in text, (
        "the commented examples and their comments stay"
    )
    assert "llm" not in tomllib.loads((root / ".fw-context" / "local.toml").read_text(encoding="utf-8"))
    assert "(detected) saved to" in capsys.readouterr().out


def test_a_second_init_writes_nothing(paths):
    root, global_path = paths
    _persist(root)
    first = global_path.read_text(encoding="utf-8")

    _persist(root, model="other:1b")

    assert global_path.read_text(encoding="utf-8") == first


@pytest.mark.parametrize("layer", ["global", "local", "project"])
def test_a_model_that_a_config_file_sets_is_not_saved(paths, layer):
    root, global_path = paths
    if layer == "global":
        # GLOBAL_TEXT ends inside its [llm] table.
        target, addition = global_path, 'embed_model = "set:1b"\n'
    else:
        name = "local.toml" if layer == "local" else "config.toml"
        target, addition = root / ".fw-context" / name, '\n[llm]\nembed_model = "set:1b"\n'
    target.write_text(target.read_text(encoding="utf-8") + addition, encoding="utf-8")
    before = {path: path.read_text(encoding="utf-8") for path in (global_path, root / ".fw-context" / "local.toml")}

    _persist(root)

    assert {path: path.read_text(encoding="utf-8") for path in before} == before


def test_nothing_is_saved_when_the_llm_is_off(paths):
    root, global_path = paths

    _persist(root, enabled=False)

    assert global_path.read_text(encoding="utf-8") == GLOBAL_TEXT


def test_a_global_config_that_is_not_valid_toml_is_not_changed(paths, capsys):
    """A line editor cannot see the error, and wrote more text into the file."""
    root, global_path = paths
    global_path.write_text("[llm\n", encoding="utf-8")

    _persist(root)

    assert global_path.read_text(encoding="utf-8") == "[llm\n"
    assert "[warn] The detected embedding model detected:1b is not saved" in capsys.readouterr().out


def test_a_table_header_that_the_editor_does_not_read_is_not_doubled(paths, capsys):
    """Before: the editor appended a second [llm] table, and the global config stopped each project."""
    root, global_path = paths
    text = "[llm]  # my settings\nmodel = \"global-chat:1b\"\n"
    global_path.write_text(text, encoding="utf-8")

    _persist(root)

    assert global_path.read_text(encoding="utf-8") == text
    assert "[warn] The detected embedding model detected:1b is not saved" in capsys.readouterr().out
