"""The list of the models of Ollama in the global config (``config._model_list``).

``data/ollama_models.toml`` of each release holds the entries of
fw-context; a sync copies them into the global config and keeps each
entry that the user changed or added (decision of the user, 2026-10-21).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import tomlkit

from fw_context_mcp.config import _model_list as model_list
from fw_context_mcp.config import settings
from fw_context_mcp.deps._checks import check_llm_models
from fw_context_mcp.deps._fixes import fix_llm_models

GLOBAL = '# my config\n[llm]\nprofile = "local"  # the profile\n\n[llm.profiles.local]\nprovider = "ollama"\nmodel = "m"\n'


@pytest.fixture
def global_path(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(GLOBAL, encoding="utf-8")
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)
    settings._config_cache.clear()
    return path


def _shipped(monkeypatch, chat: dict, embedding: dict | None = None) -> None:
    monkeypatch.setattr(model_list, "shipped", lambda: {"models": chat, "embed_models": embedding or {}})


def _sync(path: Path, version: str = "1.0") -> model_list.SyncReport:
    return model_list.sync_global_config(path, version=version)


def _table(path: Path, table: str = "models") -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))["llm"].get(table, {})


def test_the_list_of_this_release_loads_and_has_both_roles():
    entries = model_list.shipped()

    assert entries["models"]["gemma4:26b"]["quality"] == 3.73 and entries["models"]["gemma4:26b"]["moe"] is True
    assert entries["models"]["minimax-m3.1-flash:cloud"]["remote"] is True
    assert entries["embed_models"]["qwen3-embedding:8b"]["dimension"] == 4096
    assert not model_list.problems({table: dict(rows) for table, rows in entries.items()}, global_file=True)


def test_a_first_sync_adds_each_entry_and_keeps_the_comments(global_path: Path):
    report = _sync(global_path)

    text = global_path.read_text(encoding="utf-8")
    assert "# my config" in text and "# the profile" in text
    shipped = model_list.shipped()
    assert len(report.added) == len(shipped["models"]) + len(shipped["embed_models"])
    entry = _table(global_path)["gemma4:26b"]
    assert entry["source"] == "fw-context 1.0" and entry["fingerprint"] == model_list.fingerprint(entry)
    assert not settings.config_value_problems(tomllib.loads(text), global_file=True)


def test_a_second_sync_changes_nothing(global_path: Path):
    _sync(global_path)
    text = global_path.read_text(encoding="utf-8")

    report = _sync(global_path, version="1.1")

    assert not report.changes and not report.kept and global_path.read_text(encoding="utf-8") == text


def test_a_new_release_updates_an_entry_that_the_user_did_not_change_and_keeps_enabled(global_path, monkeypatch):
    _shipped(monkeypatch, {"a:1b": {"quality": 3.0}})
    _sync(global_path)
    document = tomlkit.parse(global_path.read_text(encoding="utf-8"))
    document["llm"]["models"]["a:1b"]["enabled"] = False
    global_path.write_text(tomlkit.dumps(document), encoding="utf-8")
    _shipped(monkeypatch, {"a:1b": {"quality": 3.5}})

    report = _sync(global_path, version="1.1")

    entry = _table(global_path)["a:1b"]
    assert report.updated == ["models a:1b"]
    assert (entry["quality"], entry["source"], entry["enabled"]) == (3.5, "fw-context 1.1", False)


def test_an_entry_that_the_user_changed_stays_and_the_report_names_the_new_value(global_path, monkeypatch):
    _shipped(monkeypatch, {"a:1b": {"quality": 3.0}})
    _sync(global_path)
    global_path.write_text(global_path.read_text(encoding="utf-8").replace("quality = 3.0", "quality = 4.2"),
                           encoding="utf-8")
    _shipped(monkeypatch, {"a:1b": {"quality": 3.5}})

    report = _sync(global_path, version="1.1")

    assert _table(global_path)["a:1b"]["quality"] == 4.2 and not report.changes
    assert report.kept == ["models a:1b: a new value of fw-context is quality = 3.5"]


def test_an_entry_of_the_user_is_never_changed(global_path, monkeypatch):
    global_path.write_text(GLOBAL + '\n[llm.models."a:1b"]\nquality = 4.9\n', encoding="utf-8")
    _shipped(monkeypatch, {"a:1b": {"quality": 3.0}})

    report = _sync(global_path)

    assert _table(global_path)["a:1b"] == {"quality": 4.9} and not report.changes and not report.kept


def test_an_entry_that_a_new_release_does_not_have_goes_unless_the_user_changed_it(global_path, monkeypatch):
    _shipped(monkeypatch, {"a:1b": {"quality": 3.0}, "b:1b": {"quality": 2.0}})
    _sync(global_path)
    global_path.write_text(global_path.read_text(encoding="utf-8").replace("quality = 2.0", "quality = 2.5"),
                           encoding="utf-8")
    _shipped(monkeypatch, {})

    report = _sync(global_path, version="1.1")

    assert report.removed == ["models a:1b"] and list(_table(global_path)) == ["b:1b"]
    assert report.kept == ["models b:1b: fw-context does not list it any more"]


def test_a_removed_entry_comes_back_and_enabled_false_is_the_way_to_take_a_model_out(global_path, monkeypatch):
    """A removal cannot be told from an entry that is not added yet."""
    _shipped(monkeypatch, {"a:1b": {"quality": 3.0}})
    _sync(global_path)
    document = tomlkit.parse(global_path.read_text(encoding="utf-8"))
    del document["llm"]["models"]["a:1b"]
    global_path.write_text(tomlkit.dumps(document), encoding="utf-8")

    assert _sync(global_path).added == ["models a:1b"]


@pytest.mark.parametrize(("text", "removable"), [
    ('[llm.models."a:1b"]\nquality = "high"\n', True),
    ('[llm.models."a:1b"]\nqualty = 3.0\n', False),
    ('[llm.embed_models."e:1b"]\ndimension = 1.5\n', True),
    ('[llm]\nmodels = 3\n', False),
], ids=["wrong type", "unknown key", "dimension not an integer", "not a table"])
def test_a_value_that_fw_context_cannot_use_is_a_warning_not_an_error(text, removable):
    """The list selects no backend: the strictness follows the harm."""
    found = settings.config_value_problems(tomllib.loads(text), global_file=True)

    assert len(found) == 1 and not found[0].critical and found[0].removable is removable


def test_the_list_in_a_project_file_is_a_warning():
    found = settings.config_value_problems(tomllib.loads('[llm.models."a:1b"]\nquality = 3.0\n'), global_file=False)

    assert [problem.message for problem in found] == ["[llm.models] is in the global config only"]
    assert not found[0].critical


def test_a_bad_entry_does_not_stop_the_load_of_the_config(global_path: Path):
    global_path.write_text(GLOBAL + '\n[llm.models."a:1b"]\nquality = "high"\n', encoding="utf-8")

    cfg = settings.load(None)

    assert any("quality must be a number" in warning for warning in cfg.config_warnings)


def test_doctor_reports_a_list_that_is_not_there_and_its_fix_writes_it(global_path: Path):
    before = check_llm_models()

    ok, message = fix_llm_models(None)

    assert before.status == "degraded" and before.fix_cmd == "fw-context doctor --fix --only llm-models"
    assert ok and "added models gemma4:26b" in message
    after = check_llm_models()
    assert after.status == "ok" and not after.critical


def test_doctor_without_a_global_config_has_a_fix_that_makes_it(tmp_path: Path, monkeypatch):
    path = tmp_path / "missing" / "config.toml"
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", path)

    assert check_llm_models().fix_cmd is not None
    ok, _message = fix_llm_models(None)
    assert ok and "gemma4:26b" in _table(path)


def test_doctor_of_a_global_config_that_is_not_toml_has_no_fix(global_path: Path):
    global_path.write_text("[llm\n", encoding="utf-8")

    result = check_llm_models()

    assert result.status == "degraded" and result.fix_cmd is None and global_path.read_text() == "[llm\n"


def test_init_in_a_dry_run_writes_nothing(global_path: Path, capsys):
    from fw_context_mcp.cli._init import _sync_model_list

    _sync_model_list(dry_run=True)

    assert global_path.read_text(encoding="utf-8") == GLOBAL and "[dry-run]" in capsys.readouterr().out


def test_init_writes_the_list(global_path: Path, capsys):
    from fw_context_mcp.cli._init import _sync_model_list

    _sync_model_list(dry_run=False)

    assert "gemma4:26b" in _table(global_path) and "added" in capsys.readouterr().out


def test_doctor_fix_rechecks_the_list_after_its_fix(global_path: Path):
    """The recheck did not know llm-models: doctor --fix reported an error after a fix that worked."""
    from fw_context_mcp.deps import run_fixes

    [after] = run_fixes([check_llm_models()])

    assert after.name == "llm-models" and after.status == "ok"


@pytest.mark.parametrize("text", [
    GLOBAL.replace('[llm]\nprofile = "local"  # the profile\n', '[llm]\nprofile = "local"\nmodels."mine:1b".quality = 4.0\n'),
    'llm = {profile = "local"}\n',
    "llm = 3\n",
], ids=["dotted keys", "inline [llm]", "[llm] not a table"])
def test_a_shape_that_the_sync_cannot_change_stays_valid_and_is_reported(global_path: Path, text):
    """A dotted table got its new entries at the top level; two syncs made a file that is not TOML."""
    global_path.write_text(text, encoding="utf-8")

    first = _sync(global_path)
    second = _sync(global_path)

    tomllib.loads(global_path.read_text(encoding="utf-8"))  # still TOML
    assert first.skipped and second.skipped
    if "mine:1b" in text:
        assert _table(global_path)["mine:1b"] == {"quality": 4.0}


def test_a_date_in_an_entry_of_fw_context_does_not_stop_the_sync(global_path, monkeypatch):
    _shipped(monkeypatch, {"a:1b": {"quality": 3.0}})
    _sync(global_path)
    global_path.write_text(global_path.read_text(encoding="utf-8").replace(
        "quality = 3.0", "quality = 3.0\nchecked = 2026-10-21"), encoding="utf-8")

    report = _sync(global_path)  # no exception: the fingerprint of a date

    assert _table(global_path)["a:1b"]["checked"].isoformat() == "2026-10-21" and not report.changes


@pytest.mark.parametrize("change", ["remove", "wrong type"])
def test_an_entry_of_fw_context_with_a_removed_or_wrong_value_is_of_the_user(global_path, monkeypatch, change):
    """One rule: a fingerprint that does not match is a change of the user, also a removal or a typo.

    The way back to the entry of fw-context is a removal of the entry: the next sync adds it again.
    """
    _shipped(monkeypatch, {"a:1b": {"quality": 3.0, "weights_gb": 4.7}})
    _sync(global_path)
    text = global_path.read_text(encoding="utf-8")
    text = text.replace("weights_gb = 4.7\n", "" if change == "remove" else 'weights_gb = "big"\n')
    global_path.write_text(text, encoding="utf-8")

    report = _sync(global_path)

    assert not report.changes and not report.kept, "the release changed nothing: no new value to name"
    document = tomlkit.parse(global_path.read_text(encoding="utf-8"))
    del document["llm"]["models"]["a:1b"]
    global_path.write_text(tomlkit.dumps(document), encoding="utf-8")
    assert _sync(global_path).added == ["models a:1b"] and _table(global_path)["a:1b"]["weights_gb"] == 4.7


def test_a_changed_entry_names_a_new_value_only_when_the_release_changed_it(global_path, monkeypatch):
    _shipped(monkeypatch, {"a:1b": {"quality": 3.0}})
    _sync(global_path)
    global_path.write_text(global_path.read_text(encoding="utf-8").replace("quality = 3.0", "quality = 4.2"),
                           encoding="utf-8")

    assert _sync(global_path).kept == []
    _shipped(monkeypatch, {"a:1b": {"quality": 3.5}})
    assert _sync(global_path).kept == ["models a:1b: a new value of fw-context is quality = 3.5"]


def test_an_update_keeps_the_comments_of_the_entry(global_path, monkeypatch):
    _shipped(monkeypatch, {"a:1b": {"quality": 3.0}})
    _sync(global_path)
    text = global_path.read_text(encoding="utf-8").replace('[llm.models."a:1b"]\n', '[llm.models."a:1b"]\n# mine\n')
    global_path.write_text(text, encoding="utf-8")
    _shipped(monkeypatch, {"a:1b": {"quality": 3.5}})

    assert _sync(global_path).updated == ["models a:1b"]
    assert "# mine" in global_path.read_text(encoding="utf-8") and _table(global_path)["a:1b"]["quality"] == 3.5


def test_llm_that_another_table_splits_gets_the_list(global_path):
    """[llm], then [index], then [llm.profiles.local]: valid TOML, and tomlkit writes the list there."""
    global_path.write_text('[llm]\nprofile = "local"\n\n[index]\nx = 1\n\n[llm.profiles.local]\n'
                           'provider = "ollama"\nmodel = "m"\n', encoding="utf-8")

    report = _sync(global_path)
    _sync(global_path)

    assert report.added and not report.skipped and "gemma4:26b" in _table(global_path)


@pytest.mark.parametrize("text", ['llm = {profile = "local"}\n', "[llm]\nmodels = 3\n", '[[llm.models]]\nquality = 3.0\n'],
                         ids=["inline [llm]", "models not a table", "array of tables"])
def test_a_shape_that_the_sync_cannot_change_is_reported_by_doctor_and_init(global_path: Path, capsys, text):
    from fw_context_mcp.cli._init import _sync_model_list

    global_path.write_text(text, encoding="utf-8")

    result = check_llm_models()
    _sync_model_list(dry_run=False)

    assert result.status == "degraded" and "not changed" in result.message
    assert "is not changed" in capsys.readouterr().out
    if text.startswith("llm ="):  # nothing can be written: no fix
        assert result.fix_cmd is None and global_path.read_text(encoding="utf-8") == text


def test_a_change_of_enabled_only_keeps_the_entry_unchanged_also_with_other_order_and_a_comment(global_path,
                                                                                             monkeypatch):
    _shipped(monkeypatch, {"a:1b": {"quality": 3.0, "weights_gb": 4.7}})
    _sync(global_path)
    entry = _table(global_path)["a:1b"]
    global_path.write_text(GLOBAL + f'\n[llm.models."a:1b"]\n# mine\nenabled = false\nfingerprint = "{entry["fingerprint"]}"\n'
                           f'weights_gb = 4.7\nsource = "{entry["source"]}"\nquality = 3.0\n', encoding="utf-8")
    _shipped(monkeypatch, {"a:1b": {"quality": 3.5, "weights_gb": 4.7}})

    report = _sync(global_path, version="1.1")

    assert report.updated == ["models a:1b"] and _table(global_path)["a:1b"]["enabled"] is False


def test_the_list_in_a_project_file_is_not_removed_by_doctor():
    """A committed config.toml must not lose it without a word: the user moves it."""
    found = settings.config_value_problems(tomllib.loads('[llm.models."a:1b"]\nquality = 3.0\n'), global_file=False)

    assert not found[0].removable


def test_the_messages_of_a_value_of_the_list_name_what_is_true(global_path: Path, caplog):
    """The messages of the profiles ("selects the LLM backend", "a lower file or the default") do not apply."""
    from fw_context_mcp.housekeeping import _by_hand

    global_path.write_text(GLOBAL + '\n[llm.models."a:1b"]\nqualty = 3.0\n', encoding="utf-8")
    settings._value_warned.clear()

    settings.load(None)
    [problem] = settings.config_value_problems(tomllib.loads(global_path.read_text()), global_file=True)

    assert "does not use the value" in caplog.text and "Correct it by hand." in caplog.text
    assert "which key you meant" in _by_hand(problem) and "LLM backend" not in _by_hand(problem)


@pytest.mark.parametrize("text", ['llm.profile = "local"\nindex.db_dir = "/data/idx"\n',
                                  'llm.profile = "local"\nllm.analyze_symbols = false\n'],
                         ids=["another table after", "another llm key after"])
def test_a_sync_that_would_move_a_value_of_the_user_writes_nothing(global_path: Path, text):
    """After a dotted key at the top level, tomlkit put the new tables before the next lines of the file."""
    global_path.write_text(text, encoding="utf-8")

    report = _sync(global_path)

    assert report.skipped and not report.changes and global_path.read_text(encoding="utf-8") == text


def test_an_entry_of_fw_context_in_dotted_keys_is_updated_or_reported_without_a_crash(global_path, monkeypatch):
    """A delete of a key from a dotted table raised IndexError in tomlkit: init stopped."""
    old = {"quality": 3.0, "weights_gb": 4.7}
    global_path.write_text(GLOBAL + '\n[llm.models]\n'
                           + "".join(f'"a:1b".{key} = {value}\n' for key, value in old.items())
                           + f'"a:1b".source = "fw-context 1.0"\n"a:1b".fingerprint = "{model_list.fingerprint(old)}"\n',
                           encoding="utf-8")
    _shipped(monkeypatch, {"a:1b": {"quality": 3.5}})

    report = _sync(global_path, version="1.1")

    tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert report.updated == ["models a:1b"] or report.skipped


def test_an_entry_of_fw_context_in_top_level_dotted_keys_is_not_half_written(global_path, monkeypatch):
    """tomlkit wrote the update of such an entry without source and fingerprint: it read as the user's."""
    old = {"quality": 3.0}
    text = (f'llm.models."a:1b".quality = 3.0\nllm.models."a:1b".source = "fw-context 1.0"\n'
            f'llm.models."a:1b".fingerprint = "{model_list.fingerprint(old)}"\n')
    global_path.write_text(text, encoding="utf-8")
    _shipped(monkeypatch, {"a:1b": {"quality": 3.5}})

    report = _sync(global_path, version="1.1")

    entry = _table(global_path)["a:1b"]
    assert report.skipped or (entry["source"] == "fw-context 1.1" and entry["quality"] == 3.5)
