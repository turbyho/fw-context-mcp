"""Tests for fw_context_mcp.config.settings."""

from pathlib import Path

from fw_context_mcp.config.settings import (
    Config,
    _deep_merge,
    _ensure_project_local_config,
    _from_dict,
    derive_project_id,
    load,
)


class TestDeepMerge:
    def test_override_scalar(self):
        base = {"a": 1, "b": 2}
        override = {"b": 99}
        assert _deep_merge(base, override) == {"a": 1, "b": 99}

    def test_nested_merge(self):
        base = {"index": {"db_dir": "/a", "source_roots": ["src"]}}
        override = {"index": {"db_dir": "/b"}}
        result = _deep_merge(base, override)
        assert result["index"]["db_dir"] == "/b"
        assert result["index"]["source_roots"] == ["src"]  # preserved

    def test_new_key(self):
        base = {"a": 1}
        override = {"b": 2}
        assert _deep_merge(base, override) == {"a": 1, "b": 2}

    def test_empty_override(self):
        assert _deep_merge({"a": 1}, {}) == {"a": 1}

    def test_empty_base(self):
        assert _deep_merge({}, {"a": 1}) == {"a": 1}


class TestFromDict:
    def test_empty_dict(self):
        cfg = _from_dict({})
        assert isinstance(cfg, Config)
        assert cfg.project.name is None

    def test_project_name(self):
        cfg = _from_dict({"project": {"name": "my-proj"}})
        assert cfg.project.name == "my-proj"

    def test_index_settings(self):
        cfg = _from_dict({
            "index": {
                "db_dir": "/tmp/db",
                "compile_commands": "build/cc.json",
                "source_roots": ["src", "lib"],
                "exclude_paths": ["build", "BUILD"],
            }
        })
        assert cfg.index.db_dir == Path("/tmp/db")
        assert cfg.index.compile_commands == Path("build/cc.json")
        assert cfg.index.source_roots == ["src", "lib"]
        assert cfg.index.exclude_paths == ["build", "BUILD"]

    def test_llm_settings(self):
        cfg = _from_dict({
            "llm": {
                "ollama_url": "http://localhost:9999",
                "model": "deepseek-coder",
                "num_ctx": 4096,
            }
        })
        assert cfg.llm.ollama_url == "http://localhost:9999"
        assert cfg.llm.model == "deepseek-coder"
        assert cfg.llm.num_ctx == 4096

    def test_llm_enabled_default(self):
        """enabled defaults to True."""
        cfg = _from_dict({})
        assert cfg.llm.enabled is True

    def test_llm_enabled_false(self):
        cfg = _from_dict({"llm": {"enabled": False}})
        assert cfg.llm.enabled is False


class TestDeriveProjectId:
    def test_git_repo_produces_stable_id(self):
        """In the fw-context-mcp repo itself, project_id should be stable."""
        pid1 = derive_project_id(Path(__file__).resolve().parent.parent)
        pid2 = derive_project_id(Path(__file__).resolve().parent.parent)
        assert len(pid1) == 16
        assert pid1 == pid2

    def test_non_git_directory(self, tmpdir):
        pid = derive_project_id(tmpdir)
        assert len(pid) == 16

    def test_different_dirs_produce_different_ids(self, tmpdir):
        a = tmpdir / "a"
        b = tmpdir / "b"
        a.mkdir()
        b.mkdir()
        assert derive_project_id(a) != derive_project_id(b)


class TestLocalConfig:
    """Tests for local.toml — the 4th layer of config hierarchy."""

    def test_local_overrides_shared(self):
        """local.toml values should override config.toml values."""
        global_data = {"llm": {"model": "global-model", "ollama_url": "http://localhost:11434"}}
        shared = {"build": {"board": "nrf52840"}}
        local = {"llm": {"model": "local-model"}}
        merged = _deep_merge(global_data, shared)
        merged = _deep_merge(merged, local)
        assert merged["build"]["board"] == "nrf52840"  # preserved from shared
        assert merged["llm"]["model"] == "local-model"  # overridden by local
        assert merged["llm"]["ollama_url"] == "http://localhost:11434"  # preserved from global

    def test_local_missing_is_ok(self):
        """Empty local config should not break anything."""
        global_data = {"llm": {"model": "global-model"}}
        shared = {"build": {"board": "nrf52840"}}
        local: dict = {}
        merged = _deep_merge(global_data, shared)
        merged = _deep_merge(merged, local)
        assert merged["llm"]["model"] == "global-model"
        assert merged["build"]["board"] == "nrf52840"

    def test_local_can_add_new_sections(self):
        """local.toml can add settings not present in shared config.toml."""
        shared: dict = {"build": {"board": "nrf52840"}}
        local = {"llm": {"model": "my-local-model"}}
        merged = _deep_merge(shared, local)
        assert merged["build"]["board"] == "nrf52840"
        assert merged["llm"]["model"] == "my-local-model"

    def test_local_index_db_dir_override(self):
        """local.toml can override db_dir regardless of config.toml."""
        shared = {"index": {"source_roots": ["src"], "exclude_paths": ["build"]}}
        local = {"index": {"db_dir": "/custom/db/path"}}
        merged = _deep_merge(shared, local)
        assert merged["index"]["source_roots"] == ["src"]  # preserved
        assert merged["index"]["exclude_paths"] == ["build"]  # preserved
        assert merged["index"]["db_dir"] == "/custom/db/path"  # from local

    def test_ensure_project_local_config_creates_file(self, tmpdir):
        """_ensure_project_local_config creates local.toml with template."""
        path = _ensure_project_local_config(tmpdir)
        assert path.exists()
        assert path.name == "local.toml"
        content = path.read_text()
        assert "[llm]" in content
        assert "db_dir" in content

    def test_ensure_project_local_config_idempotent(self, tmpdir):
        """Calling _ensure_project_local_config twice doesn't overwrite user edits."""
        path = _ensure_project_local_config(tmpdir)
        path.write_text("# my custom config\n[llm]\nmodel = \"my-model\"\n")
        path2 = _ensure_project_local_config(tmpdir)
        assert path == path2
        assert path.read_text() == "# my custom config\n[llm]\nmodel = \"my-model\"\n"

    def test_load_with_local_toml(self, tmpdir, monkeypatch):
        """load() reads local.toml as the 4th layer."""
        # Point global config to a temp file that doesn't exist yet
        fake_home = tmpdir / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # Create project config (shared)
        proj_dir = tmpdir / "project"
        proj_dir.mkdir()
        fw_dir = proj_dir / ".fw-context"
        fw_dir.mkdir(parents=True)
        (fw_dir / "config.toml").write_text("""\
[build]
board = "nrf52840"

[index]
source_roots = ["src"]
""")

        # Create local config
        (fw_dir / "local.toml").write_text("""\
[llm]
model = "my-local-model"
""")

        cfg = load(proj_dir)
        assert cfg.build.board == "nrf52840"
        assert cfg.index.source_roots == ["src"]
        assert cfg.llm.model == "my-local-model"


class TestSourceRootPaths:
    def test_existing_roots(self, tmpdir):
        cfg = Config()
        cfg.index.source_roots = ["src", "lib"]
        (tmpdir / "src").mkdir()
        paths = cfg.source_root_paths(tmpdir)
        assert len(paths) == 1  # only "src" exists
        assert paths[0] == (tmpdir / "src").resolve()

    def test_no_existing_roots(self, tmpdir):
        cfg = Config()
        cfg.index.source_roots = ["nonexistent"]
        paths = cfg.source_root_paths(tmpdir)
        assert paths == []

    def test_exclude_root_paths(self, tmpdir):
        cfg = Config()
        cfg.index.exclude_paths = ["build", "BUILD"]
        (tmpdir / "build").mkdir()
        (tmpdir / "BUILD").mkdir()
        paths = cfg.exclude_root_paths(tmpdir)
        assert len(paths) == 2
        assert paths[0] == (tmpdir / "build").resolve()
