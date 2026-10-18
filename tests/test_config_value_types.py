"""A config value of the wrong type gives a ConfigError that names the file and the key.

Before, ``load`` handled such a value in three ways:

- a crash with no file name (``db_dir = 5``, ``variants = 3``,
  ``ollama_url = 5``), which stopped each tool;
- a value of the wrong type in the config (``[project] id = 5``), which
  failed later;
- a warning in the log and the default (``timeout = "x"``, a section that
  is not a table, a variant without a name).

``fw-context doctor`` lists each such value, and ``doctor --fix`` removes
it (``housekeeping.remove_unusable_values``).
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import pytest

import fw_context_mcp.config.settings as settings
from fw_context_mcp.config import ConfigError, load

PID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home" / "config.toml"
    path.parent.mkdir()
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    return path


def _project(tmp_path: Path, committed: str = "", local: str | None = None) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(committed, encoding="utf-8")
    if local is not None:
        (root / ".fw-context" / "local.toml").write_text(local, encoding="utf-8")
    return root


WRONG = [
    ("[project]\nid = 5", "[project] id must be a string, not an integer"),
    ("[project]\nname = false", "[project] name must be a string, not a boolean"),
    ("[index]\ndb_dir = 5", "[index] db_dir must be a string, not an integer"),
    ("[index]\ncompile_commands = []", "[index] compile_commands must be a string, not an array"),
    ('[index]\nmax_symbol_body_lines = "x"', "[index] max_symbol_body_lines must be an integer, not a string that holds no integer"),
    ('[index]\nmax_symbol_body_lines = "1.5"', "[index] max_symbol_body_lines must be an integer, not a string that holds no integer"),
    ('[index]\ndb_dir = "~no-such-user-of-fw-context/x"', "[index] db_dir must be a path whose home directory (~) is known"),
    ('[[build.variants]]\nname = "a"\ndefines = "X"', "[[build.variants]] 'a' defines must be an array of strings, not a string"),
    ("[index]\nmax_symbol_body_lines = 1.5", "[index] max_symbol_body_lines must be an integer, not a number"),
    ("[index]\nindex_refs = 1", "[index] index_refs must be true or false, not an integer"),
    ("[index]\nvendor_paths = [1]", "[index] vendor_paths must be an array of strings, or a string, not an array with an item"),
    ('[build]\nsystem = { x = "y" }', "[build] system must be a string, not a table"),
    ("[build]\nvariants = 3", "[build] variants must be an array of tables ([[build.variants]]), not an integer"),
    ('[build]\nenv = { X = 1 }', "[build] env must be a table of strings, not a table with a value of a different type"),
    ('[build]\nmake_vars = { X = [1] }', "[build] make_vars must be a table of strings and numbers"),
    ('[llm]\nenabled = "maybe"', "[llm] enabled must be true or false, not a string"),
    ('[llm]\ntimeout = "x"', "[llm] timeout must be a number, not a string that holds no number"),
    ("[llm]\nollama_url = 5", "[llm] ollama_url must be a string, not an integer"),
    ('[llm]\nchat_api_base = "http://[::1"', "[llm] chat_api_base is not a valid URL"),
    ('[cache_server]\nbatch_size = "x"', "[cache_server] batch_size must be an integer"),
    ('[updates]\ncheck = 3', "[updates] check must be true or false, not an integer"),
    ('[call_graph]\ndispatch_bridges = { "a" = 1 }', "[call_graph] dispatch_bridges must be a table of strings"),
    ('llm = "x"', "'llm' must be a table ([llm]), not a string"),
    ('[[build.variants]]\ndescription = "x"', "[[build.variants]] entry 1 must have a name"),
    ("[build]\nvariants = [3]", "[[build.variants]] entry 1 must be a table, not an integer"),
    ('[[build.variants]]\nname = "a"\nenv = 5', "[[build.variants]] 'a' env must be a table of strings, not an integer"),
    ('[[build.variants]]\nname = "a"\nimages = [3]', "[[build.variants]] 'a' image 1 must be a table, not an integer"),
    ('[[build.variants]]\nname = "a"\nimages = [{ dir = 5 }]', "[[build.variants]] 'a' image 1 dir must be a string"),
    ('[[build.variants]]\nname = "a"\ntimeout = "x"', "[[build.variants]] 'a' timeout must be a number, not a string"),
    # An override goes to the build as it is: "9" is not a number there.
    ('[[build.variants]]\nname = "a"\ntimeout = "9"', "[[build.variants]] 'a' timeout must be a number, not a string"),
    ('[[build.variants]]\nname = "a"\nindex_refs = "yes"', "[[build.variants]] 'a' index_refs must be true or false, not a string"),
]


@pytest.mark.parametrize(("text", "message"), WRONG)
def test_a_value_of_the_wrong_type_names_the_file_and_the_key(tmp_path, home, text, message):
    root = _project(tmp_path, text + "\n")

    with pytest.raises(ConfigError) as caught:
        load(root)

    assert caught.value.path == root / ".fw-context" / "config.toml"
    assert caught.value.problem.startswith(message), caught.value.problem


@pytest.mark.parametrize("layer", ["global", "local"])
def test_the_error_names_the_file_of_the_value(tmp_path, home, layer):
    text = '[llm]\ntimeout = "x"\n'
    root = _project(tmp_path, local=text if layer == "local" else "")
    if layer == "global":
        home.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load(root)

    assert caught.value.path == (home if layer == "global" else root / ".fw-context" / "local.toml")


@pytest.mark.parametrize("text", [
    "",
    '[index]\nmax_symbol_body_lines = "200"',
    '[llm]\ntimeout = "12.5"\nenabled = "yes"',
    '[index]\nvendor_paths = "vendor"',
    '[build]\nmake_vars = { JOBS = 4, V = "1" }',
    '[cache_server]\nurl = "http://localhost:9"\nbatch_size = "10"\nforce = "no"',
    '[[build.variants]]\nname = "a"\nboard = "b"\ntimeout = 9\nimages = [{ name = "app", dir = "app" }]',
])
def test_a_value_that_load_can_use_loads(tmp_path, home, text):
    """The string forms of an older config still work: they are not a wrong type."""
    load(_project(tmp_path, text + "\n"))


def test_the_string_forms_are_converted(tmp_path, home):
    cfg = load(_project(tmp_path, '[index]\nmax_symbol_body_lines = "200"\n[llm]\ntimeout = "12.5"\nenabled = "off"\n'
                                  '[cache_server]\nurl = "http://localhost:9"\nbatch_size = "10"\nforce = "yes"\n'))

    assert cfg.index.max_symbol_body_lines == 200
    assert (cfg.llm.timeout, cfg.llm.enabled) == (12.5, False)
    assert cfg.cache_server is not None
    assert (cfg.cache_server.batch_size, cfg.cache_server.force) == (10, True)


def test_the_message_holds_no_value(tmp_path, home):
    """A key such as chat_api_key holds a secret, and the message goes to logs and to the LLM."""
    root = _project(tmp_path, local="[llm]\nchat_api_key = 987654321\n")

    with pytest.raises(ConfigError) as caught:
        load(root)

    assert "987654321" not in str(caught.value)


def test_the_message_counts_the_other_values_and_names_the_fix(tmp_path, home):
    root = _project(tmp_path, '[llm]\ntimeout = "x"\nmodel = 5\n')

    with pytest.raises(ConfigError) as caught:
        load(root)

    assert caught.value.problem.endswith("(and 1 more)"), caught.value.problem
    assert "'fw-context doctor --fix --only config'" in str(caught.value), "a full --fix also pulls models"


def test_a_wrong_project_id_is_not_removable(tmp_path, home):
    """Without an ID the project reads as not initialized, and init writes a new ID."""
    root = _project(tmp_path, "[project]\nid = 5\n")

    with pytest.raises(ConfigError) as caught:
        load(root)

    assert not caught.value.removable
    assert "doctor --fix" not in str(caught.value)


class TestRemoval:
    """``housekeeping.remove_unusable_values``: what ``doctor --fix`` removes, and what stays."""

    def test_each_value_goes_and_the_rest_of_the_file_stays(self, tmp_path, home):
        from fw_context_mcp.housekeeping import remove_unusable_values

        text = (
            '# the comment of the team\n[project]\nid = "' + PID + '"\n\n'
            '[llm]\nmodel = "m"  # stays\ntimeout = "x"\n\n'
            '[[build.variants]]\nname = "a"\nenv = { X = 1 }\n\n'
            '[[build.variants]]\ndescription = "no name"\n\n'
            '[[build.variants]]\nname = "c"\ntimeout = "9"\n'
        )
        root = _project(tmp_path, text)
        config = root / ".fw-context" / "config.toml"

        report = remove_unusable_values(root)

        after = config.read_text(encoding="utf-8")
        assert "# the comment of the team" in after and 'model = "m"  # stays' in after
        data = tomllib.loads(after)
        assert [variant["name"] for variant in data["build"]["variants"]] == ["a", "c"]
        assert "timeout" not in data["llm"]
        assert [label for _, label in report.removed_keys] == [
            "[llm] timeout must be a number, not a string that holds no number",
            "[[build.variants]] 'a' env must be a table of strings, not a table with a value of a different type",
            "[[build.variants]] entry 2 must have a name (a string that is not empty)",
            "[[build.variants]] 'c' timeout must be a number, not a string",
        ]
        assert not report.failures
        load(root)
        assert not (config.parent / "config.toml.bak").exists(), "config.toml has git"

    def test_local_toml_gets_a_copy_of_its_old_text(self, tmp_path, home):
        from fw_context_mcp.housekeeping import remove_unusable_values

        text = '[llm]\ntimeout = "x"\n'
        root = _project(tmp_path, local=text)

        remove_unusable_values(root)

        assert (root / ".fw-context" / "local.toml.bak").read_text(encoding="utf-8") == text

    def test_the_project_id_stays_and_is_a_failure(self, tmp_path, home):
        from fw_context_mcp.housekeeping import remove_unusable_values

        root = _project(tmp_path, "[project]\nid = 5\nname = 7\n")

        report = remove_unusable_values(root)

        assert tomllib.loads((root / ".fw-context" / "config.toml").read_text(encoding="utf-8")) == {"project": {"id": 5}}
        assert [reason.split(".")[0] for _, reason in report.failures] == ["[project] id must be a string, not an integer"]

    def test_a_dry_run_changes_nothing(self, tmp_path, home):
        from fw_context_mcp.housekeeping import remove_unusable_values

        text = '[llm]\ntimeout = "x"\n'
        root = _project(tmp_path, text)

        report = remove_unusable_values(root, dry_run=True)

        assert (root / ".fw-context" / "config.toml").read_text(encoding="utf-8") == text
        assert len(report.removed_keys) == 1


class TestDoctor:
    def test_the_check_lists_each_value_and_names_the_fix(self, tmp_path, home):
        from fw_context_mcp.deps import run_full_check

        root = _project(tmp_path, '[llm]\ntimeout = "x"\n', local="[index]\ndb_dir = 5\n")

        [config] = [r for r in run_full_check(project_root=root) if r.name == "config"]

        assert (config.status, config.fix_cmd, config.critical) == ("error", "fw-context doctor --fix --only config", True)
        assert config.message.startswith("config values that fw-context cannot use: "), config.message
        assert "config.toml: [llm] timeout" in config.message and "local.toml: [index] db_dir" in config.message

    def test_a_file_that_is_not_toml_has_no_fix(self, tmp_path, home):
        from fw_context_mcp.deps import run_full_check

        root = _project(tmp_path, "[llm\n")

        [config] = [r for r in run_full_check(project_root=root) if r.name == "config"]

        assert config.status == "error" and config.fix_cmd is None

    def test_doctor_fix_removes_the_values_and_runs_the_skipped_checks(self, tmp_path, home, capsys):
        from fw_context_mcp.cli._doctor import cmd_doctor

        root = _project(tmp_path, f'[project]\nid = "{PID}"\n[llm]\ntimeout = "x"\n')

        cmd_doctor(argparse.Namespace(only="config,db-integrity,disk-space", project=str(root), fix=True, json=False))

        out = capsys.readouterr().out
        load(root)
        assert "timeout" not in (root / ".fw-context" / "config.toml").read_text(encoding="utf-8")
        assert "check config" not in out, "the checks that skipped the config ran after the fix: " + out


def test_the_update_check_keeps_its_default_for_a_value_of_the_wrong_type(home):
    """`check = "maybe"` read as true before, and as false after the change of the switch converter."""
    home.write_text('[updates]\ncheck = "maybe"\n', encoding="utf-8")

    assert settings.load_update_settings().check is settings.UpdatesConfig().check


class TestFixScope:
    """The fix of `config` removes values only when the operator asks for it."""

    def test_init_removes_no_value(self, tmp_path, home):
        from fw_context_mcp.cli._init_deps import _run_deps_auto_fix

        text = '[llm]\ntimeout = "x"\n'
        root = _project(tmp_path, text)

        _, after = _run_deps_auto_fix(root)

        assert (root / ".fw-context" / "config.toml").read_text(encoding="utf-8") == text
        assert [r.status for r in after if r.name == "config"] == ["error"]

    def test_only_without_config_removes_no_value(self, tmp_path, home):
        """The row `config` of `--only db-integrity` names the error, and must not fix it."""
        from fw_context_mcp.cli._doctor import cmd_doctor

        text = '[llm]\ntimeout = "x"\n'
        root = _project(tmp_path, text)

        code = cmd_doctor(argparse.Namespace(only="db-integrity", project=str(root), fix=True, json=False))

        assert (root / ".fw-context" / "config.toml").read_text(encoding="utf-8") == text
        assert code == 2

    def test_an_error_without_a_fix_command_gets_no_fix(self, tmp_path, home, monkeypatch):
        from fw_context_mcp.deps import DepCheckResult, _fixes, run_fixes

        calls = []
        monkeypatch.setitem(_fixes.FIXABLE, "config", lambda result, root: calls.append(result) or (True, ""))
        row = DepCheckResult(name="config", status="error", message="not valid TOML", critical=True)

        assert run_fixes([row], project_root=tmp_path) == [row]
        assert not calls


class TestRemovalLimits:
    def test_one_run_of_the_fixes_keeps_the_first_copy(self, tmp_path, home):
        """The fix of `obsolete-files` wrote its copy after the fix of `config`: the removed value was lost."""
        from fw_context_mcp.deps import run_fixes, run_full_check

        text = '[llm]\ntimeout = "x"\nallow_external_llm = true\n'
        root = _project(tmp_path, local=text)

        run_fixes(run_full_check(project_root=root, subset={"config", "obsolete-files"}), project_root=root)

        local = root / ".fw-context" / "local.toml"
        assert "timeout" not in local.read_text(encoding="utf-8")
        assert "allow_external_llm" not in local.read_text(encoding="utf-8")
        assert (local.parent / "local.toml.bak").read_text(encoding="utf-8") == text

    def test_the_global_config_gets_a_copy(self, tmp_path, home):
        from fw_context_mcp.housekeeping import remove_unusable_values

        text = '[llm]\ntimeout = "x"\n'
        home.write_text(text, encoding="utf-8")

        remove_unusable_values(None)

        assert home.with_name("config.toml.bak").read_text(encoding="utf-8") == text
        assert "timeout" not in home.read_text(encoding="utf-8")

    def test_a_table_that_tomlkit_cannot_remove_is_a_failure(self, tmp_path, home):
        """With a table between the tables of the array, tomlkit removed nothing, and the fix said "removed"."""
        from fw_context_mcp.housekeeping import remove_unusable_values

        text = '[[build.variants]]\nname = "a"\n\n[index]\nindex_refs = true\n\n[[build.variants]]\ndescription = "x"\n'
        root = _project(tmp_path, text)

        report = remove_unusable_values(root)

        assert (root / ".fw-context" / "config.toml").read_text(encoding="utf-8") == text
        assert not report.removed_keys
        assert [reason for _, reason in report.failures] == [
            "[[build.variants]] entry 2 must have a name (a string that is not empty). "
            "Correct it by hand: fw-context cannot remove all of these values from this file, thus it does not change the file",
        ]

    def test_a_file_without_write_permission_names_each_value(self, tmp_path, home):
        import os
        import stat

        from fw_context_mcp.deps import run_full_check

        root = _project(tmp_path, '[llm]\ntimeout = "x"\n')
        config = root / ".fw-context" / "config.toml"
        config.chmod(stat.S_IRUSR)
        if os.access(config, os.W_OK):
            pytest.skip("root writes a file without write permission")

        [row] = [r for r in run_full_check(project_root=root) if r.name == "config"]

        assert "[llm] timeout must be a number" in row.message and "no write permission" in row.message, row.message
        assert row.fix_cmd is None, "the fix cannot remove a value there"


def test_the_check_lists_each_value_also_with_a_wrong_project_id(tmp_path, home):
    """The row gave only the first problem of `load` ("and 1 more") when one value was the project ID."""
    from fw_context_mcp.deps import run_full_check

    root = _project(tmp_path, '[project]\nid = 5\n', local='[llm]\ntimeout = "x"\n')

    [config] = [r for r in run_full_check(project_root=root) if r.name == "config"]

    assert "local.toml: [llm] timeout must be a number" in config.message, config.message
    assert "config.toml: [project] id must be a string" in config.message, config.message
    assert config.fix_cmd == "fw-context doctor --fix --only config", "the fix can remove the timeout"


def test_a_part_of_the_fix_gives_no_command_that_does_nothing(tmp_path, home):
    """The row after a failed fix kept the command of the row before, and the next run removed nothing."""
    import os
    import stat

    from fw_context_mcp.deps import run_fixes, run_full_check

    home.write_text('[llm]\ntimeout = "x"\n', encoding="utf-8")
    root = _project(tmp_path, local='[llm]\nmodel = 5\n')
    local = root / ".fw-context" / "local.toml"
    local.chmod(stat.S_IRUSR)
    if os.access(local, os.W_OK):
        pytest.skip("root writes a file without write permission")

    [row] = [r for r in run_fixes(run_full_check(project_root=root, subset={"config"}), project_root=root)]

    assert row.message.startswith("fix failed: "), row.message
    assert "timeout" not in home.read_text(encoding="utf-8"), "the global config is fixed"
    assert row.fix_cmd is None, row
