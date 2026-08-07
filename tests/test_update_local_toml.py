"""Tests for _update_local_toml() — line-level editing of local.toml [llm] section.

Covers:
- File creation from template when missing
- Updating existing keys in [llm] section
- Adding new keys to [llm] section
- Adding [llm] section when absent
- Preserving comments, other sections, formatting
- Value type formatting (str, bool, int, float)
- None values skipped
- Isolation: only writes local.toml, never global or shared config
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.config.settings import (
    _update_local_toml,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_project(tmp_path: Path, local_toml: str | None = None) -> Path:
    """Create a project root with config.toml and optional local.toml content."""
    root = tmp_path / "proj"
    cfg_dir = root / ".fw-context"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('[project]\n# id = ""\n', encoding="utf-8")
    if local_toml is not None:
        (cfg_dir / "local.toml").write_text(local_toml, encoding="utf-8")
    return root


def _read_local_toml(root: Path) -> str:
    return (root / ".fw-context" / "local.toml").read_text(encoding="utf-8")


# ── File creation ────────────────────────────────────────────────────────────


class TestUpdateLocalTomlCreation:
    def test_creates_file_when_missing(self, tmp_path):
        root = _make_project(tmp_path)
        _update_local_toml(root, {"model": "new-model"})
        assert (root / ".fw-context" / "local.toml").exists()

    def test_created_file_has_llm_section(self, tmp_path):
        root = _make_project(tmp_path)
        _update_local_toml(root, {"model": "new-model"})
        content = _read_local_toml(root)
        assert "[llm]" in content

    def test_creates_fw_context_dir(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        _update_local_toml(root, {"model": "new-model"})
        assert (root / ".fw-context" / "local.toml").exists()

    def test_does_not_touch_config_toml(self, tmp_path):
        root = _make_project(tmp_path)
        config_path = root / ".fw-context" / "config.toml"
        original = config_path.read_text(encoding="utf-8")
        _update_local_toml(root, {"model": "new-model"})
        assert config_path.read_text(encoding="utf-8") == original


# ── Existing [llm] section ───────────────────────────────────────────────────


class TestUpdateLocalTomlExistingLlmSection:
    def test_updates_existing_key_value(self, tmp_path):
        root = _make_project(tmp_path, '[llm]\nmodel = "old"\n')
        _update_local_toml(root, {"model": "new"})
        content = _read_local_toml(root)
        assert 'model = "new"' in content
        assert 'model = "old"' not in content

    def test_adds_new_key_to_section(self, tmp_path):
        root = _make_project(tmp_path, '[llm]\nmodel = "test"\n')
        _update_local_toml(root, {"chat_api_base": "https://api.example.com/v1"})
        content = _read_local_toml(root)
        assert 'chat_api_base = "https://api.example.com/v1"' in content
        assert 'model = "test"' in content

    def test_updates_multiple_keys(self, tmp_path):
        root = _make_project(tmp_path, '[llm]\nmodel = "old"\n')
        _update_local_toml(
            root,
            {
                "model": "new",
                "chat_api_base": "https://api.example.com/v1",
                "auto_pull": True,
            },
        )
        content = _read_local_toml(root)
        assert 'model = "new"' in content
        assert 'chat_api_base = "https://api.example.com/v1"' in content
        assert "auto_pull = true" in content

    def test_preserves_comments(self, tmp_path):
        local = '[llm]\n# my comment\nmodel = "old"\n'
        root = _make_project(tmp_path, local)
        _update_local_toml(root, {"model": "new"})
        content = _read_local_toml(root)
        assert "# my comment" in content

    def test_preserves_other_llm_keys(self, tmp_path):
        local = '[llm]\nmodel = "old"\nnum_ctx = 8192\n'
        root = _make_project(tmp_path, local)
        _update_local_toml(root, {"model": "new"})
        content = _read_local_toml(root)
        assert "num_ctx = 8192" in content

    def test_preserves_other_sections(self, tmp_path):
        local = '[llm]\nmodel = "old"\n\n[index]\ndb_dir = "~/test"\n'
        root = _make_project(tmp_path, local)
        _update_local_toml(root, {"model": "new"})
        content = _read_local_toml(root)
        assert "[index]" in content
        assert 'db_dir = "~/test"' in content

    def test_uncomments_key_if_commented(self, tmp_path):
        local = '[llm]\n# model = "old"\n'
        root = _make_project(tmp_path, local)
        _update_local_toml(root, {"model": "new"})
        content = _read_local_toml(root)
        assert 'model = "new"' in content
        assert '# model = "old"' not in content

    def test_idempotent_same_value(self, tmp_path):
        local = '[llm]\nmodel = "same"\n'
        root = _make_project(tmp_path, local)
        _update_local_toml(root, {"model": "same"})
        content = _read_local_toml(root)
        assert content.count('model = "same"') == 1


# ── No [llm] section ─────────────────────────────────────────────────────────


class TestUpdateLocalTomlNoLlmSection:
    def test_adds_llm_section_when_absent(self, tmp_path):
        root = _make_project(tmp_path, '[index]\ndb_dir = "~/test"\n')
        _update_local_toml(root, {"model": "new"})
        content = _read_local_toml(root)
        assert "[llm]" in content
        assert 'model = "new"' in content

    def test_adds_multiple_keys_new_section(self, tmp_path):
        root = _make_project(tmp_path, '[index]\ndb_dir = "~/test"\n')
        _update_local_toml(root, {"model": "new", "auto_pull": True})
        content = _read_local_toml(root)
        assert 'model = "new"' in content
        assert "auto_pull = true" in content


# ── Value type formatting ────────────────────────────────────────────────────


class TestUpdateLocalTomlValueTypes:
    def test_writes_string_value(self, tmp_path):
        root = _make_project(tmp_path, "[llm]\n")
        _update_local_toml(root, {"chat_api_base": "https://api.example.com"})
        content = _read_local_toml(root)
        assert 'chat_api_base = "https://api.example.com"' in content

    def test_writes_bool_true(self, tmp_path):
        root = _make_project(tmp_path, "[llm]\n")
        _update_local_toml(root, {"auto_pull": True})
        content = _read_local_toml(root)
        assert "auto_pull = true" in content

    def test_writes_bool_false(self, tmp_path):
        root = _make_project(tmp_path, "[llm]\n")
        _update_local_toml(root, {"auto_pull": False})
        content = _read_local_toml(root)
        assert "auto_pull = false" in content

    def test_writes_int_value(self, tmp_path):
        root = _make_project(tmp_path, "[llm]\n")
        _update_local_toml(root, {"num_ctx": 8192})
        content = _read_local_toml(root)
        assert "num_ctx = 8192" in content

    def test_writes_float_value(self, tmp_path):
        root = _make_project(tmp_path, "[llm]\n")
        _update_local_toml(root, {"timeout": 300.0})
        content = _read_local_toml(root)
        assert "timeout = 300.0" in content

    def test_none_value_skips_key(self, tmp_path):
        root = _make_project(tmp_path, '[llm]\nmodel = "existing"\n')
        _update_local_toml(root, {"model": "existing", "chat_api_base": None})
        content = _read_local_toml(root)
        assert "chat_api_base" not in content
        assert 'model = "existing"' in content

    def test_all_none_no_write(self, tmp_path):
        root = _make_project(tmp_path, '[llm]\nmodel = "existing"\n')
        original = _read_local_toml(root)
        _update_local_toml(root, {"chat_api_base": None, "model": None})
        assert _read_local_toml(root) == original


# ── Keys without spaces around = ──────────────────────────────────────────────


class TestUpdateLocalTomlNoSpaces:
    def test_updates_key_value_without_spaces(self, tmp_path):
        root = _make_project(tmp_path, '[llm]\nmodel="old"\n')
        _update_local_toml(root, {"model": "new"})
        content = _read_local_toml(root)
        assert 'model = "new"' in content
        assert 'model="old"' not in content

    def test_updates_key_no_space_before_equals(self, tmp_path):
        root = _make_project(tmp_path, '[llm]\nmodel ="old"\n')
        _update_local_toml(root, {"model": "new"})
        content = _read_local_toml(root)
        assert 'model = "new"' in content
        assert 'model ="old"' not in content

    def test_updates_key_no_space_after_equals(self, tmp_path):
        root = _make_project(tmp_path, '[llm]\nmodel= "old"\n')
        _update_local_toml(root, {"model": "new"})
        content = _read_local_toml(root)
        assert 'model = "new"' in content
        assert 'model= "old"' not in content

    def test_updates_commented_key_without_spaces(self, tmp_path):
        root = _make_project(tmp_path, '[llm]\n#model="old"\n')
        _update_local_toml(root, {"model": "new"})
        content = _read_local_toml(root)
        assert 'model = "new"' in content
        assert '#model="old"' not in content

    def test_no_duplicate_line_when_key_without_spaces(self, tmp_path):
        root = _make_project(tmp_path, '[llm]\nmodel="old"\n')
        _update_local_toml(root, {"model": "new"})
        content = _read_local_toml(root)
        assert content.count("model") == 1  # only one line with "model"

    def test_mixed_space_and_no_space_keys(self, tmp_path):
        local = '[llm]\nmodel="old"\nchat_api_base = "old_url"\n'
        root = _make_project(tmp_path, local)
        _update_local_toml(root, {"model": "new", "chat_api_base": "https://new.example.com/v1"})
        content = _read_local_toml(root)
        assert 'model = "new"' in content
        assert 'chat_api_base = "https://new.example.com/v1"' in content
        assert 'model="old"' not in content
        assert 'chat_api_base = "old_url"' not in content
