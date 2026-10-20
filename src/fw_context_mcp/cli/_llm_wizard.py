"""The setup of the LLM services: one code for ``fw-context init`` (option 2 of the LLM step) and ``fw-context llm setup``.

The profiles belong to the user (the global config); a project selects
one.  Thus the setup:

1. Tells what this machine has: the local Ollama, the GPU memory and the
   RAM (``llm.hardware``).
2. Lists the services, with a mark for each service that has a profile,
   and a warning on each service that sends the source code to another
   host.  The user selects services by numbers, separated by commas: that
   works in each terminal, and needs no new dependency.
3. Sets up each selected service: its model, its API key (hidden, into
   the key store) or its sign-in, and a profile of the global config.
4. Tests each new profile with a request without code: a problem shows
   now, not after an hour of indexing.  When the test fails and the user
   does not keep the profile, the global config gets its earlier text
   back, and a key or a sign-in that no profile uses any more goes.
5. Removes the profiles that the user names, after a question each.
6. In a project: selects the profile of the project (the background
   reindex uses it), the global default, or no LLM analysis.

No price and no grade is shown (decisions of the user, 2026-10-20): a
price is not known without data, and the grades are an internal rating.
The embedding model belongs to the project (option 1 of the LLM step of
``init``), thus the setup does not ask for it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import _init_interactive as ui

#: The prompt of a test request: no source code, a short answer.
_TEST_PROMPT = "Reply with exactly: OK"


@dataclass(frozen=True)
class Service:
    """One entry of the service list; *host* is where the source code goes ("" for this machine)."""

    key: str
    label: str
    host: str


SERVICES: tuple[Service, ...] = (
    Service("local", "Local Ollama", ""),
    Service("remote", "Ollama on another computer", "that computer"),
    Service("ollama_cloud", "Ollama Cloud (through the local Ollama)", "ollama.com"),
    Service("chatgpt", "ChatGPT plan (Sign in with ChatGPT)", "api.openai.com"),
    Service("openrouter", "OpenRouter", "openrouter.ai"),
    Service("deepseek", "DeepSeek", "api.deepseek.com"),
    Service("api", "Another OpenAI-compatible API", "the host of the API"),
)


def local_url() -> str:
    """Return the URL of the Ollama of this machine.

    WHY not ``cfg.llm.ollama_url``: that is the server of the embeddings,
    which ``embed_profile`` can put on another computer.  "Local Ollama"
    and "Ollama Cloud" are services of this machine.
    """
    from ..config.settings import _DEFAULT_OLLAMA_URL

    return _DEFAULT_OLLAMA_URL


def run_wizard(project_root: Path | None, cfg) -> None:
    """Run the setup interactively; *project_root* None skips the selection of a project profile."""
    from ..config.settings import ConfigError

    _machine_report()
    try:
        profiles = _profiles()
    except ConfigError as exc:
        print(f"  [warn] {exc}")
        return
    cloud = cloud_hosts(profiles)
    _print_services(profiles, cloud)
    answer = ui._input("  Set up services: numbers, separated by commas (Enter: none) > ")
    for service in _selected(answer):
        before = _global_text()
        name = _SETUPS[service.key](project_root, cfg, cloud.get(local_url().rstrip("/"), {}))
        if name:
            _test_new_profile(cfg, name, before)
    _remove_profiles()
    if project_root is not None and (project_root / ".fw-context").is_dir():
        select_project_profile(project_root, cfg)


# ── The report and the list ──


def _machine_report() -> None:
    """Print the memory of this machine and its local Ollama."""
    from ..llm._diag import installed_models
    from ..llm.hardware import machine_memory

    memory = machine_memory()
    parts = []
    if memory.gpu_mib:
        parts.append(f"{'unified memory for the GPU' if memory.unified else 'GPU'} {memory.gpu_mib / 1024:.0f} GiB")
    else:
        parts.append("no GPU found")
    if memory.ram_mib:
        parts.append(f"RAM {memory.ram_mib / 1024:.0f} GiB")
    models = installed_models(local_url())
    parts.append(f"local Ollama at {local_url()} ({len(models)} models)" if models is not None
                 else f"no Ollama at {local_url()}")
    print(f"  This machine: {', '.join(parts)}")


def _profiles() -> dict[str, dict]:
    """Return the profiles of the global config, in the order of the file.

    Raises:
        ConfigError: the global config cannot be read.
    """
    from ..config import settings

    llm = settings.read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm")
    profiles = llm.get("profiles") if isinstance(llm, dict) else None
    if not isinstance(profiles, dict):
        return {}
    return {name: profile for name, profile in profiles.items() if isinstance(profile, dict)}


def cloud_hosts(profiles: dict[str, dict]) -> dict[str, dict[str, str]]:
    """Return ``{server URL: {model: host}}`` of the cloud models of each loopback Ollama server.

    The local Ollama is always asked; each other loopback server of an
    Ollama profile too.  WHY each server: a cloud model is a model of the
    server that the profile names, not of the server of the embeddings.
    """
    from ..config.settings import _DEFAULT_OLLAMA_URL, _is_loopback_url
    from ..llm._diag import remote_hosts

    urls = {local_url().rstrip("/")}
    for profile in profiles.values():
        if profile.get("provider") == "ollama":
            url = str(profile.get("url") or _DEFAULT_OLLAMA_URL).rstrip("/")
            if _is_loopback_url(url):
                urls.add(url)
    return {url: remote_hosts(url) or {} for url in sorted(urls)}


def service_of(profile: dict, cloud: dict[str, dict[str, str]]) -> str:
    """Return the key of the service of *profile* (``SERVICES``)."""
    from ..config.settings import _DEFAULT_OLLAMA_URL, _is_loopback_url

    provider = profile.get("provider")
    if provider == "ollama":
        url = str(profile.get("url") or _DEFAULT_OLLAMA_URL).rstrip("/")
        if not _is_loopback_url(url):
            return "remote"
        return "ollama_cloud" if profile.get("model") in cloud.get(url, {}) else "local"
    if provider in ("chatgpt", "openrouter", "deepseek"):
        return str(provider)
    return "api"


def _print_services(profiles: dict[str, dict], cloud: dict[str, dict[str, str]]) -> None:
    by_service: dict[str, list[str]] = {}
    for name, profile in profiles.items():
        by_service.setdefault(service_of(profile, cloud), []).append(name)
    print("  LLM services (✓ = it has a profile; ⚠ = the source code goes to another host):")
    for number, service in enumerate(SERVICES, 1):
        names = by_service.get(service.key, [])
        mark = "✓" if names else " "
        warning = f"  ⚠ to {service.host}" if service.host else ""
        have = f"  profiles: {', '.join(names)}" if names else ""
        print(f"    {number}. {mark} {service.label}{warning}{have}")


def _selected(answer: str) -> list[Service]:
    """Return the services of the numbers of *answer*, in their order; a word that is no number is ignored."""
    chosen: list[Service] = []
    for part in answer.replace(" ", "").split(","):
        if part.isdigit() and 1 <= int(part) <= len(SERVICES) and SERVICES[int(part) - 1] not in chosen:
            chosen.append(SERVICES[int(part) - 1])
    return chosen


# ── The setup of each service ──


def _free_name(name: str, exists: Callable[[str], bool]) -> str | None:
    """Return *name*, or another name when the user does not replace an existing profile; None to skip.

    *exists* tells whether a write of a name replaces another profile.
    """
    while exists(name) and not ui._confirm(
        f"The profile {name!r} exists, and each project that selects it uses it. Replace it?", default=False,
    ):
        name = ui.prompt_text("Another profile name (empty: skip)") or ""
        if not name:
            print("  [skip] The profile is not written")
            return None
    return name


def _write_profile(name: str, values: dict[str, object], *, asked: bool = False) -> str | None:
    """Write the profile *name* with *values*; ask before it replaces another profile (unless *asked*).

    Returns the name that was written, or None.
    """
    from ..config import settings
    from ..config._profiles import replaces_other_values, set_profile

    if not asked:
        free = _free_name(name, lambda candidate: replaces_other_values(candidate, values))
        if free is None:
            return None
        name = free
    try:
        set_profile(name, values)
    except (ValueError, OSError, settings.ConfigError) as exc:
        print(f"  [warn] The profile {name!r} is not written: {exc}")
        return None
    print(f"  [ok] Profile {name!r} saved to {settings._GLOBAL_CONFIG_PATH}")
    return name


def _setup_local(project_root: Path | None, cfg, cloud: dict[str, str]) -> str | None:
    """Local Ollama: the model that the memory recommends, a pull with consent, the profile."""
    found = ui._local_ollama_profile(project_root, cfg, cloud=cloud, ask_embed=False)
    if found is None:
        return None
    name, values, _embed = found
    return _write_profile(name, values)


def _setup_remote(project_root: Path | None, cfg, cloud: dict[str, str]) -> str | None:
    """Ollama on another computer: its URL, a model that it has, the profile."""
    from ..config.settings import url_problem
    from ..llm._diag import installed_models

    url = ui.prompt_text("URL of the Ollama server (for example http://192.168.1.20:11434)") or ""
    if not url:
        print("  [skip] Ollama on another computer: no URL")
        return None
    problem = url_problem(url)
    if problem is not None:
        print(f"  [warn] {url} is not a valid URL ({problem})")
        return None
    models = installed_models(url)
    if models is None:
        print(f"  [warn] {url} gives no Ollama model list: check the URL and that Ollama listens on the network "
              "(OLLAMA_HOST=0.0.0.0 on that computer)")
        return None
    if not models:
        print(f"  [warn] {url} has no model: pull one there ('ollama pull <model>')")
        return None
    print(f"  Models of {url}: {', '.join(models)}")
    print(f"  ⚠ The source code goes to {url}")
    model = ui.prompt_text("Model", default=models[0]) or models[0]
    name = ui.prompt_text("Profile name", default="remote") or "remote"
    return _write_profile(name, {"provider": "ollama", "url": url, "model": model})


def _setup_ollama_cloud(project_root: Path | None, cfg, cloud: dict[str, str]) -> str | None:
    """Ollama Cloud: a cloud model of the local Ollama (``ollama signin``, ``ollama pull <model>:cloud``)."""
    url = local_url()
    print("  Ollama Cloud runs a model on ollama.com through the local Ollama. It needs 'ollama signin' and a")
    print("  cloud model on the local server ('ollama pull <model>:cloud').")
    print("  ⚠ The source code goes to ollama.com")
    if cloud:
        print(f"  Cloud models of the local Ollama: {', '.join(cloud)}")
    else:
        print(f"  The Ollama at {url} has no cloud model yet")
    default = next(iter(cloud), "")
    model = ui.prompt_text("Cloud model", default=default or None) or default
    if not model:
        print("  [skip] Ollama Cloud: no model")
        return None
    name = ui.prompt_text("Profile name", default="ollama-cloud") or "ollama-cloud"
    return _write_profile(name, {"provider": "ollama", "url": url, "model": model})


def _setup_chatgpt(project_root: Path | None, cfg, cloud: dict[str, str]) -> str | None:
    """ChatGPT plan: the name first, then the sign-in in the browser, a model of the plan, the profile.

    WHY the name before the sign-in: the tokens are stored under the name
    of the profile, and a rename after the sign-in left them under the old
    name.
    """
    import webbrowser

    from ..llm.chatgpt_plan import LoginError, list_models, login
    from ..llm.errors import LLMError

    name = _free_name(ui.prompt_text("Profile name", default="chatgpt") or "chatgpt", lambda n: n in _profiles())
    if name is None:
        return None
    print("  ⚠ The source code goes to api.openai.com. The requests use the usage of your ChatGPT plan")
    print("    (ChatGPT Settings → Usage; on ChatGPT Plus the five-hour limit is shared with each other app).")

    def _open(url: str, shown: str) -> None:
        print("  Sign in with ChatGPT in the browser. When no browser opens, open this URL:")
        print(f"    {shown}")
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass  # the user has the URL above

    try:
        login(name, open_url=_open)
        models = list_models(name)
    except (LoginError, LLMError, OSError) as exc:
        print(f"  [warn] ChatGPT plan: {exc}")
        _drop_unused_secrets(name)
        return None
    slugs = [slug for slug, _label in models]
    if not slugs:
        print("  [warn] The plan lists no model")
        _drop_unused_secrets(name)
        return None
    print(f"  Models of the plan: {', '.join(slugs)}")
    default = "gpt-6-luna" if "gpt-6-luna" in slugs else slugs[0]
    model = ui.prompt_text("Model", default=default) or default
    written = _write_profile(name, {"provider": "chatgpt", "model": model}, asked=True)
    if written is None:
        _drop_unused_secrets(name)
    return written


def _setup_provider(provider: str) -> Callable[[Path | None, object, dict[str, str]], str | None]:
    """Return the setup of the provider *provider* of ``llm.providers`` (OpenRouter, DeepSeek)."""

    def _setup(project_root: Path | None, cfg, cloud: dict[str, str]) -> str | None:
        from ..llm.providers import PROVIDERS
        from ..llm.secrets import api_key_source, provider_entry

        spec = PROVIDERS[provider]
        print(f"  ⚠ The source code goes to {spec.base_url}")
        name = ui.prompt_text("Profile name", default=spec.name) or spec.name
        if api_key_source(name, spec.key_env, spec.name) is None:
            print(f"  The API key of {spec.label} goes to the key store of the system; it applies to each "
                  f"{spec.label} profile ({provider_entry(spec.name)})")
            if not ui._store_api_key(provider_entry(spec.name)):
                return None
        model = ui.prompt_text("Model", default=spec.recommended_model or None) or spec.recommended_model
        if not model:
            print(f"  [skip] {spec.label}: no model")
            return None
        return _write_profile(name, {"provider": spec.name, "model": model})

    return _setup


def _setup_api(project_root: Path | None, cfg, cloud: dict[str, str]) -> str | None:
    """Another OpenAI-compatible API: name, URL, key, model (``_init_interactive._custom_api_profile``)."""
    return ui._custom_api_profile()


_SETUPS: dict[str, Callable[[Path | None, object, dict[str, str]], str | None]] = {
    "local": _setup_local,
    "remote": _setup_remote,
    "ollama_cloud": _setup_ollama_cloud,
    "chatgpt": _setup_chatgpt,
    "openrouter": _setup_provider("openrouter"),
    "deepseek": _setup_provider("deepseek"),
    "api": _setup_api,
}


# ── The test, the removal, the profile of the project ──


def test_profile(cfg, name: str) -> str | None:
    """Send a request without code through the profile *name*; return None when it answers, else the problem."""
    from ..config.settings import ConfigError, profile_llm_config
    from ..llm.client import get_chat_client
    from ..llm.errors import LLMError

    try:
        profiles = _profiles()
        if name not in profiles:
            return f"the global config has no profile {name!r} (profiles: {', '.join(profiles) or 'none'})"
        llm = profile_llm_config(cfg.llm, name)
        get_chat_client(llm).generate(_TEST_PROMPT, max_tokens=10)
    except (ConfigError, LLMError, ValueError) as exc:
        return str(exc)
    return None


def _global_text() -> str | None:
    """Return the text of the global config, or None when it cannot be read (then no restore is possible)."""
    from ..config import settings

    try:
        return settings._ensure_global_config().read_text(encoding="utf-8")
    except OSError:
        return None


def _test_new_profile(cfg, name: str, before: str | None) -> None:
    """Test the new profile *name*; when it fails, put the global config of *before* back, unless the user keeps it.

    WHY the earlier text and not a removal: a profile that replaced
    another one would be gone with the earlier values, and each project
    that selects it would stop with an error.
    """
    from ..config import settings
    from ..config._toml_editor import _atomic_write

    problem = test_profile(cfg, name)
    if problem is None:
        print(f"  [ok] Profile {name!r} answers")
        return
    print(f"  [warn] Profile {name!r} does not answer: {problem}")
    if ui._confirm(f"Keep the profile {name!r} anyway?", default=False):
        return
    if before is None:
        print(f"  [warn] The earlier global config cannot be put back: remove the profile with "
              f"'fw-context llm remove {name}'")
        return
    try:
        _atomic_write(settings._GLOBAL_CONFIG_PATH.resolve(), before)
    except OSError as exc:
        print(f"  [warn] The earlier global config cannot be written back ({exc}): remove the profile with "
              f"'fw-context llm remove {name}'")
        return
    print(f"  [ok] The global config has its earlier text: profile {name!r} is not changed")
    _drop_unused_secrets(name)


def _drop_unused_secrets(name: str) -> None:
    """Remove the own key and the ChatGPT sign-in of *name* when no profile of that name is left.

    A sign-in that stays without a profile cannot be revoked with
    ``fw-context llm logout`` any more by a person who does not know it.
    """
    from ..config.settings import ConfigError
    from ..llm import chatgpt_plan
    from ..llm.secrets import SecretStoreError, delete_api_key

    try:
        if name in _profiles():
            return
    except ConfigError:
        return
    try:
        delete_api_key(name)
    except SecretStoreError as exc:
        print(f"  [warn] {exc}")
    credentials = chatgpt_plan.load_credentials(name)
    if credentials is not None and credentials.signed_in() and not chatgpt_plan.logout(name):
        print("  [note] The revocation of the ChatGPT sign-in is not confirmed: disconnect fw-context in "
              "ChatGPT Settings")


def _remove_profiles() -> None:
    """Ask for profiles to remove; remove each one after a question, with its key or sign-in."""
    from ..config.settings import ConfigError

    try:
        names = list(_profiles())
    except ConfigError:
        return
    if not names:
        return
    answer = ui._input(f"  Remove profiles ({', '.join(names)}): names, separated by commas (Enter: none) > ")
    for name in [part.strip() for part in answer.split(",") if part.strip()]:
        if name not in names:
            print(f"  [warn] No profile {name!r}")
            continue
        if ui._confirm(f"Remove the profile {name!r} (and its own key or sign-in)?", default=False):
            remove_profile_and_secrets(name)


def remove_profile_and_secrets(name: str) -> bool:
    """Remove the profile *name*, its own stored key, and the sign-in of a ChatGPT profile; return True when removed.

    The key of a provider (``provider:<name>``) stays: the other profiles
    of that provider use it.
    """
    from ..config import settings
    from ..config._profiles import remove_profile

    try:
        remove_profile(name)
    except (OSError, settings.ConfigError) as exc:
        print(f"  [warn] {exc}")
        return False
    _drop_unused_secrets(name)
    print(f"  [ok] Profile {name!r} removed")
    return True


def _project_selection(project_root: Path) -> tuple[str, str]:
    """Return the profile that the project files select, and the file (``project`` or ``local``); ("", "") for none.

    Read from the files, not from the loaded config: a profile that the
    setup removed is still in the config that was loaded before.
    """
    from ..config import settings

    name, layer = "", ""
    for label, file_name in (("project", "config.toml"), ("local", "local.toml")):
        path = project_root / ".fw-context" / file_name
        try:
            llm = settings.read_config_file(path).get("llm") if path.is_file() else None
        except settings.ConfigError:
            continue
        if isinstance(llm, dict) and isinstance(llm.get("profile"), str) and llm["profile"]:
            name, layer = llm["profile"], label
    return name, layer


def select_project_profile(project_root: Path, cfg) -> None:
    """Ask for the profile of the project: a profile, the global default, or no LLM analysis.

    Only a profile that can run is offered (``profile_state``).  Enter
    keeps the current selection.  When ``config.toml`` (in git) selects a
    profile, the global default is not offered: ``local.toml`` can select
    another profile over it, but cannot remove a selection of
    ``config.toml``.
    """
    from ..config.settings import ConfigError
    from ..llm.profile_state import profile_state

    try:
        profiles = _profiles()
    except ConfigError as exc:
        print(f"  [warn] {exc}")
        return
    cloud = cloud_hosts(profiles)
    states = [profile_state(name, profile, cloud=cloud) for name, profile in profiles.items()]
    ready = [state for state in states if state.problem is None]
    current, layer = _project_selection(project_root)
    if current and current not in profiles:
        print(f"  [warn] The project selects profile {current!r}, which the global config does not have")
        current = "" if layer == "local" else current
    print("  The LLM profile of this project (the background reindex uses it):")
    if layer == "project":
        print(f"    (0. the global default: not offered, .fw-context/config.toml selects {current!r})")
    else:
        print(f"    0. the global default{' — Enter' if not current else ''}")
    for number, state in enumerate(ready, 1):
        warning = f"  ⚠ {state.host}" if state.external else ""
        mark = " — Enter" if state.name == current else ""
        print(f"    {number}. {state.name}: {state.provider} {state.model}{warning}{mark}")
    for state in states:
        if state.problem is not None:
            print(f"       ({state.name}: {state.problem})")
    print("    d. no LLM analysis (the embeddings stay)")
    answer = ui._input("  > ").strip().lower()
    if answer == "d":
        ui._disable_llm_analysis(project_root, cfg)
        return
    if layer != "project" and (answer == "0" or (answer == "" and not current)):
        ui._use_global_llm(project_root, remove_local=layer == "local" or _has_local_llm(project_root),
                           old_embed_model=cfg.llm.embed_model)
        _analysis_on_again(project_root)
        return
    if answer == "":
        print(f"  [ok] LLM: the project keeps profile {current!r}")
        return
    if not answer.isdigit() or not 1 <= int(answer) <= len(ready):
        print("  [skip] The profile of the project is not changed")
        return
    _select(project_root, ready[int(answer) - 1].name)


def _has_local_llm(project_root: Path) -> bool:
    """Return True when ``local.toml`` sets a key of ``LLM_BACKEND_KEYS``."""
    from ..config.settings import llm_key_sources

    return "local" in llm_key_sources(project_root).values()


def _analysis_on_again(project_root: Path) -> None:
    """Remove ``analyze_symbols = false`` of ``local.toml``: a selected backend is a backend that analyses.

    WHY: option "d" wrote it, and a later selection of a profile said
    "[ok]" while the analysis stayed off.
    """
    from ..config import settings

    path = project_root / ".fw-context" / "local.toml"
    try:
        llm = settings.read_config_file(path).get("llm") if path.is_file() else None
    except settings.ConfigError:
        return
    if not isinstance(llm, dict) or llm.get("analyze_symbols") is not False:
        return
    try:
        settings._update_local_toml(project_root, {}, clear_keys=["analyze_symbols"])
    except (OSError, settings.ConfigError) as exc:
        print(f"  [warn] analyze_symbols = false stays in .fw-context/local.toml ({exc})")
        return
    print("  [ok] LLM analysis on again (removed analyze_symbols = false from .fw-context/local.toml)")


def _select(project_root: Path, name: str) -> None:
    from ..llm.configure import configure_llm_core

    result = configure_llm_core(project_root, {"enabled": True, "profile": name})
    if result.get("status") != "ok":
        print(f"  [warn] {result.get('message', 'the test of the profile failed')}")
        return
    print(f"  [ok] .fw-context/local.toml selects profile {name!r}")
    if result.get("compliance_warning"):
        print(f"  ⚠ {result['compliance_warning']}")
    _analysis_on_again(project_root)
