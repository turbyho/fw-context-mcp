"""Tests for fw_context_mcp.config.settings."""

from pathlib import Path

from fw_context_mcp.config.settings import Config, _deep_merge, _from_dict, derive_project_id


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
