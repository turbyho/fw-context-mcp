"""LLM profiles: the config names each backend in a profile of the global config, and a project selects one.

Before, ``[llm]`` named the backend directly (``ollama_url``, ``model``,
``chat_api_base``, ``chat_api_key`` as plain text), in each of the three
config files: a committed ``config.toml`` could send the source code to a
host that the repository selects, and a typo of a key was ignored without
a word.  Now:

* a profile is in the global config only (``[llm.profiles.<name>]``), and
  a project selects one with ``[llm] profile``;
* an unknown key of a profile is an error;
* an old ``[llm]`` backend key is an error that says where it goes, and
  ``init`` / ``doctor --fix`` convert it (``_llm_profile_migration``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp._llm_profile_migration import migrate_llm_profiles
from fw_context_mcp.config import settings
from fw_context_mcp.config._profiles import set_profile
from fw_context_mcp.config.settings import ConfigError, load


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A global directory of its own (config and secrets.toml), and a project directory with a .fw-context folder.

    The keyring of the session fails each call (conftest), thus a stored
    key goes to secrets.toml of this directory.
    """
    from fw_context_mcp.llm import secrets

    global_path = tmp_path / "home" / "config.toml"
    global_path.parent.mkdir()
    monkeypatch.setattr(settings, "_GLOBAL_CONFIG_PATH", global_path)
    monkeypatch.setenv("FW_CONTEXT_HOME", str(global_path.parent))
    monkeypatch.setattr(secrets, "_found", {})
    monkeypatch.setattr(secrets, "_told", set())
    settings._config_cache.clear()
    (tmp_path / "proj" / ".fw-context").mkdir(parents=True)
    return tmp_path


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    settings._config_cache.clear()


def _global(home: Path) -> Path:
    return home / "home" / "config.toml"


def _local(home: Path) -> Path:
    return home / "proj" / ".fw-context" / "local.toml"


def _project(home: Path) -> Path:
    return home / "proj"


TWO_PROFILES = """\
[llm]
profile = "local"

[llm.profiles.local]
provider = "ollama"
url = "http://localhost:11434"
model = "qwen2.5-coder:14b"
num_ctx = 8192

[llm.profiles.deepseek]
provider = "openai_compatible"
url = "https://api.deepseek.com"
model = "deepseek-flash"
api_key_env = "TEST_DEEPSEEK_KEY"
"""


# ── The selection ──


def test_the_global_profile_fills_the_backend_fields(home):
    _write(_global(home), TWO_PROFILES)

    llm = load(_project(home)).llm

    assert (llm.profile, llm.provider, llm.model, llm.num_ctx) == ("local", "ollama", "qwen2.5-coder:14b", 8192)
    assert llm.ollama_url == "http://localhost:11434" and llm.chat_api_base is None


def test_a_project_selects_an_api_profile_and_its_key_variable(home):
    """load names the variable; the chat client reads the key at the first request (llm.secrets)."""
    _write(_global(home), TWO_PROFILES)
    _write(_local(home), '[llm]\nprofile = "deepseek"\n')

    llm = load(_project(home)).llm

    assert (llm.profile, llm.provider, llm.model) == ("deepseek", "openai_compatible", "deepseek-flash")
    assert (llm.chat_api_base, llm.chat_api_format) == ("https://api.deepseek.com", "openai")
    assert (llm.api_key_env, llm.chat_api_key) == ("TEST_DEEPSEEK_KEY", None)
    # The embeddings stay on the local Ollama.
    assert llm.ollama_url == settings._DEFAULT_OLLAMA_URL


def test_without_profiles_the_defaults_are_a_local_ollama(home):
    _write(_global(home), "[llm]\nanalyze_symbols = true\n")

    llm = load(_project(home)).llm

    assert (llm.profile, llm.provider, llm.ollama_url) == ("", "ollama", "http://localhost:11434")


def test_an_ollama_chat_on_another_server_keeps_the_embeddings_on_their_own(home):
    _write(_global(home), TWO_PROFILES.replace('profile = "local"', 'profile = "gpu"\nembed_profile = "local"') + """
[llm.profiles.gpu]
provider = "ollama"
url = "http://gpu-box:11434"
model = "qwen3:14b"
""")

    llm = load(_project(home)).llm

    assert (llm.chat_api_base, llm.chat_api_format) == ("http://gpu-box:11434", "ollama")
    assert llm.ollama_url == "http://localhost:11434"


def test_with_a_chat_on_an_api_the_ollama_tuning_comes_from_the_embeddings_profile(home):
    """Before, keep_alive and max_concurrent came only from an Ollama chat profile: the embeddings got the defaults."""
    tuned = TWO_PROFILES.replace('num_ctx = 8192', 'num_ctx = 8192\nkeep_alive = "30s"\nmax_concurrent = 3')
    _write(_global(home), tuned.replace('profile = "local"', 'profile = "deepseek"\nembed_profile = "local"'))

    llm = load(_project(home)).llm

    assert (llm.provider, llm.keep_alive, llm.ollama_max_concurrent) == ("openai_compatible", "30s", 3)


def test_load_does_not_look_for_the_api_key(home, monkeypatch):
    """A keyring lookup takes about 0.1 s, and load runs for each MCP query: the chat client looks."""
    from fw_context_mcp.llm import secrets

    looked: list[str] = []
    monkeypatch.setattr(secrets, "get_api_key", lambda *args, **kwargs: looked.append("x"))
    _write(_global(home), TWO_PROFILES)
    _write(_local(home), '[llm]\nprofile = "deepseek"\n')

    load(_project(home))

    assert looked == []


@pytest.mark.parametrize(("local", "message"), [
    ('[llm]\nprofile = "nope"\n', "'nope' is not a profile"),
    ('[llm]\nembed_profile = "deepseek"\n', "is an API profile: \\[llm\\] embed_model must name"),
])
def test_a_selection_of_a_missing_or_wrong_profile_names_the_file(home, local, message):
    _write(_global(home), TWO_PROFILES)
    _write(_local(home), local)

    with pytest.raises(ConfigError, match=message) as caught:
        load(_project(home))
    assert caught.value.path == _local(home)


def test_two_profiles_without_a_selection_is_an_error(home):
    _write(_global(home), TWO_PROFILES.replace('profile = "local"\n', ""))

    with pytest.raises(ConfigError, match="profile is not set"):
        load(_project(home))


# ── The checks ──


@pytest.mark.parametrize(("extra", "message"), [
    ('modle = "x"\n', "modle is not a key of a profile"),
    ('api_key_env = "X"\n', "api_key_env is not a key of a profile of provider ollama"),
    ('provider = "soap"\n', "provider must be one of"),
])
def test_a_wrong_key_of_a_profile_is_an_error(home, extra, message):
    text = TWO_PROFILES.replace('num_ctx = 8192\n', 'num_ctx = 8192\n' + extra).replace(
        'provider = "ollama"\n' + 'url = "http://localhost:11434"\nmodel = "qwen2.5-coder:14b"\nnum_ctx = 8192\nprovider = "soap"\n',
        'provider = "soap"\nurl = "http://localhost:11434"\nmodel = "qwen2.5-coder:14b"\nnum_ctx = 8192\n')
    _write(_global(home), text)

    with pytest.raises(ConfigError, match=message):
        load(_project(home))


def test_a_tuning_value_of_a_profile_of_the_wrong_type_is_a_warning_and_the_default(home):
    _write(_global(home), TWO_PROFILES.replace("num_ctx = 8192", 'num_ctx = "big"'))

    cfg = load(_project(home))

    assert cfg.llm.num_ctx == settings.LLMConfig().num_ctx
    assert any("num_ctx" in warning for warning in cfg.config_warnings)


def test_a_profile_in_a_project_file_is_an_error(home):
    _write(_global(home), TWO_PROFILES)
    _write(_local(home), '[llm.profiles.evil]\nprovider = "ollama"\nurl = "http://attacker:11434"\nmodel = "m"\n')

    with pytest.raises(ConfigError, match="in the global config only"):
        load(_project(home))


def test_an_old_backend_key_says_where_it_goes(home):
    _write(_global(home), TWO_PROFILES)
    _write(_local(home), '[llm]\nmodel = "qwen3:14b"\n')

    with pytest.raises(ConfigError, match=r"\[llm\] model is a key of an LLM profile now"):
        load(_project(home))


def test_a_required_key_of_the_wrong_type_is_one_problem_that_the_fix_keeps():
    """Before, ``model = 5`` gave "needs model" and "model must be a string"; the fix removed the model,
    and "needs model" stayed."""
    layer = {"llm": {"profiles": {"p": {"provider": "ollama", "model": 5}}}}

    problems = settings.config_value_problems(layer, global_file=True)

    assert [(p.keys, p.removable) for p in problems] == [(("llm", "profiles", "p", "model"), False)]


@pytest.mark.parametrize(("key", "value", "removable"), [
    ("url", "http://[::1", False),  # a removal would send the requests to http://localhost:11434
    ("ulr", "http://gpu:11434", False),  # a typo of url: the same
    ("num_ctx", "big", True),  # a tuning value: its default keeps the backend
])
def test_the_fix_removes_only_a_tuning_value_of_a_profile(key, value, removable):
    layer = {"llm": {"profiles": {"p": {"provider": "ollama", "model": "m", key: value}}}}

    problems = settings.config_value_problems(layer, global_file=True)

    assert [p.removable for p in problems] == [removable]


# ── The writer ──


def test_set_profile_keeps_the_comments_and_replaces_the_whole_profile(home):
    _write(_global(home), "# my notes\n" + TWO_PROFILES)

    set_profile("local", {"provider": "ollama", "model": "gemma4:12b"}, select=True)

    text = _global(home).read_text(encoding="utf-8")
    assert text.startswith("# my notes\n")
    assert "num_ctx = 8192" not in text, "a key of the earlier profile must not stay behind"
    settings._config_cache.clear()
    assert load(_project(home)).llm.model == "gemma4:12b"


def test_set_profile_writes_nothing_for_a_profile_with_a_problem(home):
    _write(_global(home), TWO_PROFILES)
    before = _global(home).read_text(encoding="utf-8")

    with pytest.raises(ConfigError, match="needs url"):
        set_profile("api", {"provider": "openai_compatible", "model": "x"})
    with pytest.raises(ValueError, match="profile name"):
        set_profile("a b", {"provider": "ollama", "model": "x"})
    assert _global(home).read_text(encoding="utf-8") == before


# ── The migration of the old keys ──


OLD_GLOBAL = """\
[index]
db_dir = "~/.fw-context/index"

[llm]
enabled = true
ollama_url = "http://localhost:11434"
model = "qwen2.5-coder:14b"
num_ctx = 16384
embed_model = "qwen3-embedding:8b"
"""

OLD_LOCAL = """\
[build]
[llm]
# enabled = false   # disable Ollama, return raw prompts for the agent to answer
# ollama_url = "http://localhost:11434"
# model = "qwen2.5-coder:14b"   # minimum 14B recommended — see global config for options
# num_ctx = 16384
# timeout = 600.0   # HTTP timeout for Ollama requests in seconds (default 600)
# debug_log = "~/.fw-context/llm-debug.jsonl"   # write LLM prompts/responses to JSONL
# embed_model = "mxbai-embed-large:latest"   # embedding model for semantic search
enabled = true
model = "qwen2.5-coder:14b"
# analyze_symbols = true    # generate structured symbol descriptions
"""


def test_the_migration_makes_a_profile_and_removes_the_old_lines(home):
    _write(_global(home), OLD_GLOBAL)
    _write(_local(home), OLD_LOCAL)

    report = migrate_llm_profiles(_project(home))

    assert report.failures == []
    settings._config_cache.clear()
    llm = load(_project(home)).llm
    assert (llm.profile, llm.model, llm.num_ctx, llm.embed_model) == ("local", "qwen2.5-coder:14b", 16384,
                                                                       "qwen3-embedding:8b")
    local = _local(home).read_text(encoding="utf-8")
    for gone in ("# ollama_url", "# model =", "# num_ctx", "# timeout", "\nmodel ="):
        assert gone not in local, gone
    for kept in ("# debug_log", "# embed_model", "# analyze_symbols", "enabled = true", "# profile = "):
        assert kept in local, kept
    assert (_local(home).with_name("local.toml.bak")).exists(), "the old text must have a copy"


def test_a_second_migration_changes_nothing(home):
    _write(_global(home), OLD_GLOBAL)
    _write(_local(home), OLD_LOCAL)
    migrate_llm_profiles(_project(home))
    texts = (_global(home).read_text(encoding="utf-8"), _local(home).read_text(encoding="utf-8"))

    report = migrate_llm_profiles(_project(home))

    assert report.removed_keys == [] and report.failures == []
    assert (_global(home).read_text(encoding="utf-8"), _local(home).read_text(encoding="utf-8")) == texts


def test_a_project_with_its_own_model_gets_its_own_profile(home):
    _write(_global(home), OLD_GLOBAL)
    _write(_local(home), '[llm]\nmodel = "gemma4:12b"\n')

    migrate_llm_profiles(_project(home))

    settings._config_cache.clear()
    llm = load(_project(home)).llm
    assert (llm.profile, llm.model, llm.num_ctx) == ("proj", "gemma4:12b", 16384)
    settings._config_cache.clear()
    assert load(None).llm.model == "qwen2.5-coder:14b", "the global default must not change"


def test_an_old_api_base_becomes_an_api_profile(home):
    _write(_global(home), OLD_GLOBAL)
    _write(_local(home), '[llm]\nchat_api_base = "https://api.example.test/v1"\nmodel = "m1"\n')

    migrate_llm_profiles(_project(home))

    settings._config_cache.clear()
    llm = load(_project(home)).llm
    assert (llm.provider, llm.chat_api_base, llm.model) == ("openai_compatible", "https://api.example.test/v1", "m1")


def test_the_embeddings_server_of_a_project_is_selected_in_the_project_only(home):
    """Before, the migration wrote embed_profile into the global config: each project changed its embeddings server."""
    _write(_global(home), OLD_GLOBAL)
    _write(_local(home), '[llm]\nchat_api_base = "http://gpu-box:11434/api"\nollama_url = "http://emb-box:11434"\n')

    migrate_llm_profiles(_project(home))

    settings._config_cache.clear()
    llm = load(_project(home)).llm
    assert (llm.chat_api_base, llm.ollama_url) == ("http://gpu-box:11434", "http://emb-box:11434")
    assert "embed_profile" in _local(home).read_text(encoding="utf-8")
    settings._config_cache.clear()
    assert load(None).llm.ollama_url == "http://localhost:11434", "the global embeddings server must not change"


def test_an_api_key_of_a_project_goes_to_the_key_store_of_its_own_profile(home):
    """The key applied to that project only: the project gets a profile of its own, and no TOML file keeps it."""
    from fw_context_mcp.llm import secrets

    _write(_global(home), OLD_GLOBAL)
    _write(_local(home), '[llm]\nchat_api_base = "https://api.example.test/v1"\nchat_api_key = "sk-plain-test"\n')

    report = migrate_llm_profiles(_project(home))

    assert report.failures == []
    llm = _converted(home)
    assert (llm.profile, llm.chat_api_base) == ("proj", "https://api.example.test/v1")
    assert secrets.stored_api_key("proj") == "sk-plain-test"
    for path in (_global(home), _local(home), _local(home).with_name("local.toml.bak")):
        assert "sk-plain-test" not in path.read_text(encoding="utf-8"), path
    assert all("sk-plain-test" not in label for _path, label in report.removed_keys), "a message must not hold the key"


def test_a_dry_run_changes_no_file(home):
    _write(_global(home), OLD_GLOBAL)
    _write(_local(home), OLD_LOCAL)

    report = migrate_llm_profiles(_project(home), dry_run=True)

    assert report.removed_keys
    assert _global(home).read_text(encoding="utf-8") == OLD_GLOBAL
    assert _local(home).read_text(encoding="utf-8") == OLD_LOCAL


# ── The migration: the values of the layers below, the write as one unit ──

OLD_PROXY_GLOBAL = """\
[llm]
chat_api_base = "http://localhost:4000/v1"
model = "gpt-proxy"
keep_alive = "1h"
ollama_max_concurrent = 4
auto_pull = true
"""


def _config(home: Path) -> Path:
    return home / "proj" / ".fw-context" / "config.toml"


def _converted(home: Path):
    settings._config_cache.clear()
    return load(_project(home)).llm


def test_a_project_with_only_a_tuning_key_keeps_the_api_of_the_global_config(home):
    """Before, the project got an Ollama profile with the model of the API: its chat went to another server."""
    _write(_global(home), OLD_PROXY_GLOBAL)
    _write(_local(home), "[llm]\nnum_ctx = 32768\n")

    assert migrate_llm_profiles(_project(home)).failures == []

    llm = _converted(home)
    assert (llm.provider, llm.chat_api_base, llm.model) == ("openai_compatible", "http://localhost:4000/v1", "gpt-proxy")


def test_with_a_chat_on_an_api_the_ollama_tuning_goes_to_the_embeddings_profile(home):
    """Before, keep_alive, ollama_max_concurrent and auto_pull of an API config were lost."""
    _write(_global(home), OLD_PROXY_GLOBAL)

    assert migrate_llm_profiles(_project(home)).failures == []

    llm = _converted(home)
    assert (llm.keep_alive, llm.ollama_max_concurrent, llm.auto_pull) == ("1h", 4, True)
    assert llm.ollama_url == "http://localhost:11434"


@pytest.mark.parametrize(("global_url", "project_keys", "embeddings"), [
    # The project sends the chat to another Ollama: the embeddings stay on the server of the global config.
    ("http://localhost:11434", 'chat_api_base = "http://gpu-box:11434"', "http://localhost:11434"),
    # The project sends the chat to an API: the embeddings stay on the server of the global config.
    ("http://gpu-box:11434", 'chat_api_base = "https://api.example.test/v1"\nmodel = "m1"', "http://gpu-box:11434"),
])
def test_the_embeddings_of_a_project_stay_on_the_server_of_the_layer_below(home, global_url, project_keys, embeddings):
    """Before, the embeddings server came only from the old keys of the project."""
    _write(_global(home), OLD_GLOBAL.replace("http://localhost:11434", global_url))
    _write(_local(home), f"[llm]\n{project_keys}\n")

    assert migrate_llm_profiles(_project(home)).failures == []

    assert _converted(home).ollama_url == embeddings


def test_an_api_key_of_the_global_config_applies_to_a_project_profile_of_that_api(home):
    """The old merge gave the key of the global config to a project that changed only the model."""
    from fw_context_mcp.llm import secrets

    _write(_global(home), '[llm]\nchat_api_base = "https://api.example.test/v1"\nchat_api_key = "sk-plain-test"\n')
    _write(_config(home), '[project]\n\n[llm]\nmodel = "m2"\n')

    assert migrate_llm_profiles(_project(home)).failures == []

    llm = _converted(home)
    assert (llm.profile, llm.model) == ("proj", "m2")
    assert secrets.stored_api_key("proj") == "sk-plain-test"
    assert secrets.stored_api_key(load(None).llm.profile) == "sk-plain-test"


def test_a_project_profile_of_an_api_gets_a_copy_of_the_stored_key_of_the_profile_below(home):
    from fw_context_mcp.llm import secrets

    _write(_global(home), '[llm]\nprofile = "work"\n\n[llm.profiles.work]\nprovider = "openai_compatible"\n'
                          'url = "https://api.example.test/v1"\nmodel = "m1"\n')
    secrets.set_api_key("work", "sk-plain-test")
    _write(_local(home), '[llm]\nmodel = "m2"\n')

    assert migrate_llm_profiles(_project(home)).failures == []

    assert secrets.stored_api_key("proj") == "sk-plain-test"


def test_a_dry_run_stores_no_key(home):
    from fw_context_mcp.llm import secrets

    _write(_global(home), OLD_GLOBAL)
    _write(_local(home), '[llm]\nchat_api_base = "https://api.example.test/v1"\nchat_api_key = "sk-plain-test"\n')

    report = migrate_llm_profiles(_project(home), dry_run=True)

    assert any("API key of profile proj" in label for _path, label in report.removed_keys), report.removed_keys
    assert secrets.stored_api_key("proj") is None


def test_a_project_without_local_toml_gets_one(home):
    """Before, the write of the missing local.toml failed after config.toml lost its model."""
    _write(_global(home), OLD_GLOBAL)
    _write(_config(home), '[project]\n\n[llm]\nmodel = "gemma4:12b"\n')

    assert migrate_llm_profiles(_project(home)).failures == []

    llm = _converted(home)
    assert (llm.profile, llm.model) == ("proj", "gemma4:12b")


def test_a_file_that_cannot_be_written_stops_the_run_before_the_first_write(home):
    """Before, local.toml selected the new profile and config.toml kept its keys: a second run inverted the layers."""
    _write(_global(home), OLD_GLOBAL)
    _write(_config(home), '[project]\n\n[llm]\nmodel = "A"\n')
    _write(_local(home), '[llm]\nmodel = "B"\n')
    _config(home).chmod(0o444)

    report = migrate_llm_profiles(_project(home))

    assert {path for path, _reason in report.failures} == {_global(home), _local(home), _config(home)}
    assert _global(home).read_text(encoding="utf-8") == OLD_GLOBAL
    assert _local(home).read_text(encoding="utf-8") == '[llm]\nmodel = "B"\n'
    _config(home).chmod(0o644)
    assert migrate_llm_profiles(_project(home)).failures == []
    assert _converted(home).model == "B", "local.toml must stay above config.toml"


def test_a_project_profile_does_not_become_the_profile_of_each_project(home):
    """A global config without profiles got only the profile of the project: load selected it for each project."""
    _write(_global(home), "[index]\n\n[llm]\n# model = \"qwen2.5-coder:14b\"\n")
    _write(_config(home), '[project]\n\n[llm]\nchat_api_base = "https://api.example.test/v1"\nmodel = "gpt"\n')

    assert migrate_llm_profiles(_project(home)).failures == []

    assert _converted(home).chat_api_base == "https://api.example.test/v1"
    settings._config_cache.clear()
    other = load(None).llm
    assert (other.provider, other.model) == ("ollama", "qwen2.5-coder:14b"), "another project must keep the local Ollama"


@pytest.mark.parametrize(("global_format", "project_base", "endpoint"), [
    ("ollama", "http://llm2.lan:8080", "ollama"),
    ("openai", "http://gpu2:11434", "openai_compatible"),
])
def test_the_explicit_format_of_the_global_config_applies_to_the_url_of_the_project(
        home, global_format, project_base, endpoint):
    """The old merge applied chat_api_format of the global config to chat_api_base of the project."""
    _write(_global(home), f'[llm]\nchat_api_base = "https://proxy.example/x"\nchat_api_format = "{global_format}"\n')
    _write(_local(home), f'[llm]\nchat_api_base = "{project_base}"\n')

    assert migrate_llm_profiles(_project(home)).failures == []

    llm = _converted(home)
    assert llm.provider == endpoint, llm
    assert (llm.chat_api_base or "").startswith(project_base), llm


def test_the_report_says_that_num_ctx_of_an_api_is_not_kept(home):
    _write(_global(home), '[llm]\nchat_api_base = "https://api.example.test/v1"\nmodel = "m1"\nnum_ctx = 8192\n')

    report = migrate_llm_profiles(_project(home))

    assert any("num_ctx is not kept" in label for _path, label in report.removed_keys), report.removed_keys


def test_a_missing_global_config_is_made_before_the_project_is_converted(home):
    """Before, the run did nothing, and the next load refused the old keys of the project."""
    _write(_local(home), '[llm]\nmodel = "gemma4:12b"\n')

    assert migrate_llm_profiles(_project(home)).failures == []

    assert _converted(home).model == "gemma4:12b"


def test_a_valid_local_toml_gets_no_commented_line_alone(home):
    """doctor reported a valid local.toml only because the commented profile line was not in it."""
    _write(_global(home), TWO_PROFILES)
    _write(_local(home), '[llm]\nembed_model = "e:1b"\n')

    report = migrate_llm_profiles(_project(home), dry_run=True)

    assert report.removed_keys == [] and report.failures == []


def test_the_error_of_a_file_with_an_api_key_tells_that_the_key_moves_to_the_keyring(home):
    _write(_local(home), '[llm]\nchat_api_base = "https://api.example.test/v1"\nchat_api_key = "sk-plain-test"\n')

    with pytest.raises(ConfigError) as error:
        load(_project(home))

    assert "moves the API key to the keyring" in str(error.value), error.value
    assert "sk-plain-test" not in str(error.value)


def test_a_valid_config_toml_gets_no_commented_line(home):
    """Before, config.toml got the commented profile line, and doctor reported a valid file."""
    _write(_global(home), TWO_PROFILES)
    _write(_config(home), "[project]\n\n[llm]\nanalyze_symbols = true\n")

    report = migrate_llm_profiles(_project(home), dry_run=True)

    assert report.removed_keys == [] and report.failures == []


# ── init: a shared profile changes only after a question ──


def _no_test_call(monkeypatch):
    from fw_context_mcp.llm import configure

    monkeypatch.setattr(configure, "configure_llm_core", lambda *a, **k: {"status": "ok"})


def test_the_cloud_setup_asks_before_it_replaces_a_profile(home, monkeypatch, capsys):
    """Before, the name "local" replaced the Ollama profile of each project with an external API."""
    from fw_context_mcp.cli import _init_interactive

    _write(_global(home), TWO_PROFILES)
    before = _global(home).read_text(encoding="utf-8")
    _no_test_call(monkeypatch)
    answers = {"Profile name": "local", "API URL": "https://api.example.test/v1", "Model": "m1"}
    monkeypatch.setattr(_init_interactive, "prompt_text", lambda label, **kwargs: answers.get(label, ""))
    questions: list[str] = []
    monkeypatch.setattr(_init_interactive, "_confirm", lambda question, **kwargs: questions.append(question) or False)

    _init_interactive._custom_api_profile()

    assert len(questions) == 1 and "'local' exists" in questions[0], questions
    assert "[skip]" in capsys.readouterr().out
    assert _global(home).read_text(encoding="utf-8") == before


def test_a_no_to_the_shared_profile_keeps_its_model_and_makes_no_profile_of_the_project(home, monkeypatch):
    """One Ollama profile for each machine (2026-10-21): before, the project got its own profile local-proj."""
    from fw_context_mcp.cli import _init_interactive
    from fw_context_mcp.llm import auto_model, ollama

    _write(_global(home), TWO_PROFILES)
    _no_test_call(monkeypatch)
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "ollama_running": True, "installed_models": []})
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(_init_interactive, "prompt_text",
                        lambda label, default=None, **kwargs: "new-chat:1b" if label == "Chat model" else default)
    monkeypatch.setattr(_init_interactive, "_confirm", lambda question, **kwargs: False)

    _init_interactive._prompt_ollama_setup(_project(home), load(_project(home)))

    profiles = settings.read_config_file(_global(home))["llm"]["profiles"]
    assert profiles["local"]["model"] == "qwen2.5-coder:14b"
    assert "local-proj" not in profiles


def test_the_ollama_setup_leaves_an_old_profile_of_a_project_as_it_is(home, monkeypatch):
    """An Ollama profile of one project (of an older release) stays: --llm can still use it."""
    from fw_context_mcp.cli import _init_interactive
    from fw_context_mcp.llm import auto_model, ollama

    _write(_global(home), TWO_PROFILES + '\n[llm.profiles.local-proj]\nprovider = "ollama"\nmodel = "other:1b"\n')
    _no_test_call(monkeypatch)
    monkeypatch.setattr(ollama, "check_setup", lambda cfg: {"status": "ok", "ollama_running": True, "installed_models": []})
    monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
    monkeypatch.setattr(_init_interactive, "prompt_text",
                        lambda label, default=None, **kwargs: "new-chat:1b" if label == "Chat model" else default)
    monkeypatch.setattr(_init_interactive, "_confirm", lambda question, **kwargs: False)

    _init_interactive._prompt_ollama_setup(_project(home), load(_project(home)))

    profiles = settings.read_config_file(_global(home))["llm"]["profiles"]
    assert profiles["local-proj"]["model"] == "other:1b"
    assert "local-proj-2" not in profiles and profiles["local"]["model"] == "qwen2.5-coder:14b"


# ── The conversion of chat_api_key: the corner cases ──


def test_a_project_api_at_another_url_keeps_the_key_variable_of_the_profile_below(home):
    """The old merge gave the key of the global config to the API of the project, also at another URL."""
    _write(_global(home), '[llm]\nprofile = "work"\n\n[llm.profiles.work]\nprovider = "openai_compatible"\n'
                          'url = "https://api.example.test/v1"\nmodel = "m1"\napi_key_env = "WORK_KEY"\n')
    _write(_local(home), '[llm]\nchat_api_base = "https://other.example.test/v1"\n')

    report = migrate_llm_profiles(_project(home))

    assert report.failures == []
    assert _converted(home).api_key_env == "WORK_KEY"
    assert not any("API key of profile" in label for _path, label in report.removed_keys), report.removed_keys


def test_a_leftover_key_goes_to_the_profile_that_the_project_selects(home):
    """Before, a second profile proj-2 got the key, and the selected profile stayed without one."""
    from fw_context_mcp.llm import secrets

    _write(_global(home), '[llm]\nprofile = "local"\n\n[llm.profiles.local]\nprovider = "ollama"\nmodel = "x"\n\n'
                          '[llm.profiles.proj]\nprovider = "openai_compatible"\nurl = "https://api.example.test/v1"\n'
                          'model = "m1"\n')
    _write(_local(home), '[llm]\nprofile = "proj"\n')
    _write(_config(home), '[project]\n\n[llm]\nchat_api_key = "sk-left-over"\n')

    assert migrate_llm_profiles(_project(home)).failures == []

    assert "proj-2" not in _global(home).read_text(encoding="utf-8")
    assert secrets.stored_api_key("proj") == "sk-left-over"


def test_a_key_of_an_ollama_config_is_removed_without_a_copy(home):
    """The old chat client sent the key only to an OpenAI-compatible API."""
    from fw_context_mcp.llm import secrets

    _write(_global(home), OLD_GLOBAL.replace("enabled = true", 'enabled = true\nchat_api_key = "sk-unused"'))

    report = migrate_llm_profiles(_project(home))

    assert report.failures == []
    assert any("did not send it to an Ollama server" in label for _path, label in report.removed_keys)
    assert secrets.stored_api_key("local") is None
    assert _global(home).read_text(encoding="utf-8").count("[llm.profiles.") == 1


@pytest.fixture(autouse=True)
def _no_cloud_model_list(monkeypatch):
    """No request to the Ollama of the operator: the rule of the Ollama profiles asks for the cloud models.

    The recommendation of the local Ollama setup asks for the installed
    models too.
    """
    from fw_context_mcp.llm import _diag

    monkeypatch.setattr(_diag, "remote_hosts", lambda url: {})
    monkeypatch.setattr(_diag, "installed_models", lambda url: [])
