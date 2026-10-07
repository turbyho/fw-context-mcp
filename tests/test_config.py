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
    transient_defines_problems,
)
from fw_context_mcp.indexer.config_hash import DEFAULT_TRANSIENT_DEFINES


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
        import fw_context_mcp.config.settings as settings

        fake_home = tmpdir / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        # The fake home does not move _GLOBAL_CONFIG_PATH, which the module
        # computes at import.  Point it to a temp file that does not exist
        # yet, thus load() creates it there and not in the real home.
        monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", Path(fake_home) / "global.toml")

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


class TestRetiredQueryDriverKeys:
    """``[index] query_driver``, ``query_driver_extra`` and ``query_driver_auto`` are retired.

    They were an allowlist of the compilers that the index run could ask
    for their system headers.  fw-context now asks the compiler of the build
    in all cases, thus an old config must load without an error, and a
    warning names each file that still has a retired key.
    """

    def _project(self, tmpdir, monkeypatch, committed: str = "", local: str = "", global_text: str = "") -> Path:
        import fw_context_mcp.config.settings as settings

        fake_home = tmpdir / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        # The module computes _GLOBAL_CONFIG_PATH at import, thus the fake
        # home does not move it.  Without this line, load() reads the global
        # config of the operator.
        global_cfg = Path(fake_home) / "global.toml"
        global_cfg.write_text(global_text)
        monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_cfg)
        # The warning is given once per file and process; each test starts clean.
        monkeypatch.setattr(settings, "_retired_warned", set())
        proj_dir = Path(tmpdir / "project")
        (proj_dir / ".fw-context").mkdir(parents=True)
        (proj_dir / ".fw-context" / "config.toml").write_text(committed)
        if local:
            (proj_dir / ".fw-context" / "local.toml").write_text(local)
        return proj_dir

    def test_the_config_has_no_query_driver_field(self):
        assert not hasattr(Config().index, "query_driver")

    @pytest.mark.parametrize("layer", ["committed", "local", "global_text"])
    def test_a_retired_key_is_ignored_with_a_warning(self, tmpdir, monkeypatch, caplog, layer):
        files = {layer: '[index]\nquery_driver = ["**"]\nquery_driver_auto = false\nvendor_paths = ["x"]\n'}
        cfg = load(self._project(tmpdir, monkeypatch, **files))
        assert cfg.index.vendor_paths == ["x"], "the other [index] keys of the file stay"
        err = caplog.text
        assert "query_driver, query_driver_auto" in err
        assert "has no effect" in err

    def test_the_warning_is_given_once_per_file(self, tmpdir, monkeypatch, caplog):
        import fw_context_mcp.config.settings as settings

        proj = self._project(tmpdir, monkeypatch, local='[index]\nquery_driver_extra = ["~/x/**"]\n')
        load(proj)
        settings._config_cache.clear()  # force a second parse of the same file
        load(proj)
        assert caplog.text.count("query_driver_extra") == 1

    def test_a_retired_key_in_a_variant_is_not_an_unknown_build_key(self, tmpdir, monkeypatch, caplog):
        """A variant copies a non-[index] key into its [build] overrides.

        Without the removal, ``build_variant_config`` said "unknown [build]
        key", which is the wrong message for a retired key.
        """
        committed = '[[build.variants]]\nname = "a"\nquery_driver = ["**"]\n'
        cfg = load(self._project(tmpdir, monkeypatch, committed))
        variant = cfg.build.variants[0]
        assert "query_driver" not in variant.overrides
        assert "has no effect" in caplog.text

    def test_a_malformed_build_table_does_not_crash_the_removal(self, tmp_path, monkeypatch):
        """``build = "x"`` is a typo; the removal must leave it to the rest of the loader."""
        import fw_context_mcp.config.settings as settings

        monkeypatch.setattr(settings, "_retired_warned", set())
        data = {"build": "x", "index": {"query_driver": ["**"]}}
        settings._drop_retired_keys(data, tmp_path / "local.toml")
        assert data == {"build": "x", "index": {}}

    def test_an_old_toolchains_file_gets_a_warning_and_stays(self, tmpdir, monkeypatch, caplog):
        proj = self._project(tmpdir, monkeypatch)
        toolchains = proj / ".fw-context" / "toolchains.toml"
        toolchains.write_text('[index]\nquery_driver_extra = ["~/x/bin/gcc"]\n')
        load(proj)
        assert "toolchains.toml" in caplog.text
        assert toolchains.is_file(), "a load of the config removes nothing; the cleanup does"

    def test_a_committed_pre_build_gives_no_warning(self, tmpdir, monkeypatch, caplog):
        """The build runs the code of the repository in all cases.

        Thus a warning on a committed ``pre_build`` or ``command`` protected
        nothing, and it is gone.  A non-local ``ollama_url`` still warns.
        """
        committed = '[build]\npre_build = "make gen"\ncommand = "make"\n'
        load(self._project(tmpdir, monkeypatch, committed))
        assert "SECURITY" not in caplog.text

    def test_a_committed_remote_ollama_url_still_warns(self, tmpdir, monkeypatch, caplog):
        committed = '[llm]\nollama_url = "http://198.51.100.7:11434"\n'
        load(self._project(tmpdir, monkeypatch, committed))
        assert "SECURITY: ollama_url" in caplog.text


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
    def test_a_section_that_is_not_a_table_is_ignored(self, tmpdir, monkeypatch, caplog, layer, bad):
        good = '[project]\nname = "kept"\n'
        files = {"committed": "", "local": "", "global_text": ""}
        files[layer] = bad + "\n" + good
        cfg = self._load(tmpdir, monkeypatch, **files)
        assert cfg.project.name == "kept", "the other sections of the file still apply"
        assert "must be a table" in caplog.text

    def test_a_string_section_is_not_read_as_a_substring(self, tmpdir, monkeypatch):
        """``_apply_section`` tested ``key in section``; on a string that is a substring test."""
        cfg = self._load(tmpdir, monkeypatch, local='index = "vendor_paths"\n')
        assert cfg.index.vendor_paths == []


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


class TestTransientDefines:
    """``[index] transient_defines``: one list for config_hash and flags_hash."""

    _load = TestMalformedSection._load

    def test_the_default_is_the_list_of_six_names(self):
        assert _from_dict({}).index.transient_defines == list(DEFAULT_TRANSIENT_DEFINES)
        assert len(DEFAULT_TRANSIENT_DEFINES) == 6

    def test_the_global_template_holds_the_default(self):
        """``init`` writes the active keys of the template into the global config."""
        import tomllib

        from fw_context_mcp.config.settings import _GLOBAL_DEFAULTS

        template = tomllib.loads(_GLOBAL_DEFAULTS)
        assert template["index"]["transient_defines"] == list(DEFAULT_TRANSIENT_DEFINES)

    def test_a_project_list_replaces_the_global_list(self, tmpdir, monkeypatch):
        cfg = self._load(
            tmpdir, monkeypatch,
            global_text='[index]\ntransient_defines = ["MBED_BUILD_TIMESTAMP", "BUILD_NUMBER"]\n',
            committed='[index]\ntransient_defines = ["MY_BUILD_STAMP"]\n',
        )
        assert cfg.index.transient_defines == ["MY_BUILD_STAMP"]

    def test_an_empty_project_list_makes_every_macro_count(self, tmpdir, monkeypatch):
        cfg = self._load(
            tmpdir, monkeypatch,
            global_text='[index]\ntransient_defines = ["MBED_BUILD_TIMESTAMP"]\n',
            committed="[index]\ntransient_defines = []\n",
        )
        assert cfg.index.transient_defines == []

    def test_a_correct_config_has_no_problem(self):
        cfg = _from_dict({"index": {"transient_defines": ["MBED_BUILD_TIMESTAMP", "_X1"]}})
        assert transient_defines_problems(cfg) == []

    @pytest.mark.parametrize("entry", ["-DMBED_BUILD_TIMESTAMP", "BUILD_NUMBER=1", "1ABC", "", 5])
    def test_an_entry_that_is_not_a_macro_name_is_a_problem(self, entry):
        """Such an entry matches no -D, and each build would parse every unit again."""
        cfg = _from_dict({"index": {"transient_defines": ["BUILD_ID", entry]}})
        problems = transient_defines_problems(cfg)
        assert len(problems) == 1
        assert repr(entry) in problems[0]

    def test_the_key_in_a_variant_is_a_problem(self):
        """The variant table would drop the key without a word."""
        cfg = _from_dict({
            "build": {"variants": [
                {"name": "dev", "transient_defines": ["X"]},
                {"name": "rel"},
            ]},
        })
        problems = transient_defines_problems(cfg)
        assert len(problems) == 1
        assert "'dev'" in problems[0]

    def test_the_index_run_gets_the_list_of_the_config(self, tmp_path):
        """runner.run has a default for callers without a config; the CLI must override it."""
        from types import SimpleNamespace

        from fw_context_mcp.cli._index import _build_run_kwargs

        cfg = _from_dict({"build": {"system": "makefile"}, "index": {"transient_defines": ["MY_STAMP"]}})
        args = SimpleNamespace(name=None, no_refs=False, force=False)
        kwargs = _build_run_kwargs(args, cfg, tmp_path, "pid", [], [], None)
        assert kwargs["transient_defines"] == ["MY_STAMP"]

    @pytest.mark.parametrize("value", ["{ X = 1 }", "5", "true"])
    def test_a_value_that_is_not_a_list_keeps_the_default(self, tmpdir, monkeypatch, caplog, value):
        """``list(value)`` made ``["X"]`` of a table, and a number stopped the load."""
        cfg = self._load(tmpdir, monkeypatch, committed=f"[index]\ntransient_defines = {value}\n")
        assert cfg.index.transient_defines == list(DEFAULT_TRANSIENT_DEFINES)
        assert "[index] transient_defines must be a list or a string" in caplog.text

    def test_a_string_is_a_list_of_one_name(self):
        cfg = _from_dict({"index": {"transient_defines": "MY_STAMP"}})
        assert cfg.index.transient_defines == ["MY_STAMP"]


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
