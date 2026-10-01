"""Tests for fw_context_mcp.config.settings."""

from pathlib import Path

import pytest

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
        base = {"index": {"db_dir": "/a", "vendor_paths": ["third_party"]}}
        override = {"index": {"db_dir": "/b"}}
        result = _deep_merge(base, override)
        assert result["index"]["db_dir"] == "/b"
        assert result["index"]["vendor_paths"] == ["third_party"]  # preserved

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
                "vendor_paths": ["third_party", "vendor_libs"],
                "project_paths": ["src/old_hal"],
            }
        })
        assert cfg.index.db_dir == Path("/tmp/db")
        assert cfg.index.compile_commands == Path("build/cc.json")
        assert cfg.index.vendor_paths == ["third_party", "vendor_libs"]
        assert cfg.index.project_paths == ["src/old_hal"]

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
    def test_returns_id_from_config(self, tmpdir):
        """derive_project_id reads [project].id from config.toml."""
        from fw_context_mcp.config.settings import _write_project_id

        test_id = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        _write_project_id(tmpdir, test_id)
        assert derive_project_id(tmpdir) == test_id

    def test_stable_id_from_config(self, tmpdir):
        """derive_project_id returns the same ID on repeated calls."""
        from fw_context_mcp.config.settings import _write_project_id

        test_id = "f6e5d4c3b2a10987a6b5c4d3e2f1a0b9"
        _write_project_id(tmpdir, test_id)
        pid1 = derive_project_id(tmpdir)
        pid2 = derive_project_id(tmpdir)
        assert len(pid1) == 32
        assert pid1 == pid2

    def test_raises_when_no_id(self, tmpdir):
        """derive_project_id raises ProjectNotInitializedError when [project].id is missing."""
        from fw_context_mcp.config.settings import ProjectNotInitializedError

        with pytest.raises(ProjectNotInitializedError):
            derive_project_id(tmpdir)


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
        shared = {"index": {"vendor_paths": ["third_party"]}}
        local = {"index": {"db_dir": "/custom/db/path"}}
        merged = _deep_merge(shared, local)
        assert merged["index"]["vendor_paths"] == ["third_party"]  # preserved
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
vendor_paths = ["third_party"]
""")

        # Create local config
        (fw_dir / "local.toml").write_text("""\
[llm]
model = "my-local-model"
""")

        cfg = load(proj_dir)
        assert cfg.build.board == "nrf52840"
        assert cfg.index.vendor_paths == ["third_party"]
        assert cfg.llm.model == "my-local-model"


class TestQueryDriverTrust:
    """``[index] query_driver`` selects the compilers that the indexer runs.

    A committed config must not set it, or a repository could allow the
    compiler script it commits.
    """

    def _project(self, tmpdir, monkeypatch, committed: str, local: str = "") -> Path:
        fake_home = tmpdir / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        proj_dir = Path(tmpdir / "project")
        (proj_dir / ".fw-context").mkdir(parents=True)
        (proj_dir / ".fw-context" / "config.toml").write_text(committed)
        if local:
            (proj_dir / ".fw-context" / "local.toml").write_text(local)
        return proj_dir

    def test_the_default_allowlist_applies_without_a_setting(self):
        from fw_context_mcp.config.settings import DEFAULT_QUERY_DRIVER

        assert Config().index.query_driver == list(DEFAULT_QUERY_DRIVER)

    def test_the_committed_config_cannot_set_it(self, tmpdir, monkeypatch, capsys):
        from fw_context_mcp.config.settings import DEFAULT_QUERY_DRIVER

        proj = self._project(tmpdir, monkeypatch, '[index]\nquery_driver = ["**"]\nvendor_paths = ["x"]\n')
        cfg = load(proj)
        assert cfg.index.query_driver == list(DEFAULT_QUERY_DRIVER)
        assert cfg.index.vendor_paths == ["x"], "the other [index] keys of the committed config stay"
        assert "query_driver" in capsys.readouterr().err

    def test_local_toml_can_set_it(self, tmpdir, monkeypatch):
        """The operator decided: local.toml is the developer's own file."""
        proj = self._project(tmpdir, monkeypatch, "[index]\n", '[index]\nquery_driver = ["~/tools/**"]\n')
        assert load(proj).index.query_driver == ["~/tools/**"]

    def test_a_variant_cannot_set_it(self, tmpdir, monkeypatch):
        from fw_context_mcp.config.settings import DEFAULT_QUERY_DRIVER

        committed = '[[build.variants]]\nname = "a"\nquery_driver = ["**"]\n'
        cfg = load(self._project(tmpdir, monkeypatch, committed))
        assert cfg.index.query_driver == list(DEFAULT_QUERY_DRIVER)
        assert all("query_driver" not in v.index_overrides for v in cfg.build.variants)

    def test_a_malformed_build_table_does_not_crash_the_trust_check(self, tmp_path):
        """``build = "x"`` is a typo; the check must leave it to the rest of the loader."""
        from fw_context_mcp.config.settings import _drop_committed_trust_keys

        data = {"build": "x", "index": {"query_driver": ["**"]}}
        _drop_committed_trust_keys(data, tmp_path / "local.toml")
        assert data == {"build": "x", "index": {}}

    def test_the_global_config_can_set_it(self, tmpdir, monkeypatch):
        import fw_context_mcp.config.settings as settings

        proj = self._project(tmpdir, monkeypatch, "[index]\n")
        global_cfg = Path(tmpdir) / "global.toml"
        global_cfg.write_text('[index]\nquery_driver = ["~/tools/**"]\n')
        monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)
        assert load(proj).index.query_driver == ["~/tools/**"]


class TestMalformedSection:
    """A section that is not a table is a typo, and a typo must not crash load().

    ``build = "x"`` made ``_build_variants`` raise AttributeError out of
    ``load()``, which took down every MCP tool.
    """

    def _load(self, tmpdir, monkeypatch, *, committed: str = "", local: str = "", global_text: str = ""):
        import fw_context_mcp.config.settings as settings

        fake_home = tmpdir / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        global_cfg = Path(tmpdir) / "global.toml"
        global_cfg.write_text(global_text)
        monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)
        proj_dir = Path(tmpdir / "project")
        (proj_dir / ".fw-context").mkdir(parents=True)
        (proj_dir / ".fw-context" / "config.toml").write_text(committed)
        (proj_dir / ".fw-context" / "local.toml").write_text(local)
        return load(proj_dir)

    @pytest.mark.parametrize("layer", ["committed", "local", "global_text"])
    @pytest.mark.parametrize("bad", ['build = "x"', "build = []", "index = 3", 'llm = "x"', 'cache_server = "x"'])
    def test_a_section_that_is_not_a_table_is_ignored(self, tmpdir, monkeypatch, capsys, layer, bad):
        good = '[project]\nname = "kept"\n'
        files = {"committed": "", "local": "", "global_text": ""}
        files[layer] = bad + "\n" + good
        cfg = self._load(tmpdir, monkeypatch, **files)
        assert cfg.project.name == "kept", "the other sections of the file still apply"
        assert "must be a table" in capsys.readouterr().err

    def test_a_string_section_is_not_read_as_a_substring(self, tmpdir, monkeypatch):
        """``_apply_section`` tested ``key in section``; on a string that is a substring test."""
        from fw_context_mcp.config.settings import DEFAULT_QUERY_DRIVER

        cfg = self._load(tmpdir, monkeypatch, local='index = "query_driver"\n')
        assert cfg.index.query_driver == list(DEFAULT_QUERY_DRIVER)


class TestVendorProjectPaths:
    def test_vendor_paths_parsing(self):
        cfg = _from_dict({"index": {"vendor_paths": ["third_party", "generated"]}})
        assert cfg.index.vendor_paths == ["third_party", "generated"]

    def test_project_paths_parsing(self):
        cfg = _from_dict({"index": {"project_paths": ["src/old_hal"]}})
        assert cfg.index.project_paths == ["src/old_hal"]

    def test_both_paths_parsing(self):
        cfg = _from_dict({
            "index": {
                "vendor_paths": ["lib"],
                "project_paths": ["lib/muj_modul"],
            }
        })
        assert cfg.index.vendor_paths == ["lib"]
        assert cfg.index.project_paths == ["lib/muj_modul"]


class TestLoadCreatesNoProjectFile:
    """load() reads a missing project file as empty, and never creates one."""

    @pytest.fixture(autouse=True)
    def _isolated_global(self, tmp_path, monkeypatch):
        import fw_context_mcp.config.settings as settings

        monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", tmp_path / "home" / "config.toml")

    def test_an_uninitialized_project_stays_untouched(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()

        cfg = load(project)

        assert not (project / ".fw-context").exists()
        assert not cfg.project.id

    def test_a_project_file_that_appears_or_goes_reloads_the_config(self, tmp_path):
        project = tmp_path / "proj"
        (project / ".fw-context").mkdir(parents=True)
        local = project / ".fw-context" / "local.toml"

        assert load(project).llm.num_ctx != 1234
        local.write_text("[llm]\nnum_ctx = 1234\n", encoding="utf-8")
        assert load(project).llm.num_ctx == 1234
        local.unlink()
        assert load(project).llm.num_ctx != 1234

    def test_get_active_build_creates_nothing_before_init(self, tmp_path):
        from fw_context_mcp.mcp.handlers.maintenance import get_active_build

        project = tmp_path / "proj"
        project.mkdir()

        result = get_active_build(project_root=str(project))

        assert result["status"] == "not_initialized"
        assert not (project / ".fw-context").exists()
