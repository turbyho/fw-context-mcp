"""The setup of the LLM services: one code for the LLM step of ``fw-context init`` and ``fw-context llm setup``.

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
5. Selects the service of the LLM analysis and the service and model of
   the embeddings, from two independent lists (``cli._llm_lists``), for
   this project or for each project.
6. ``llm setup`` only: removes the profiles that the user names, after a
   question each, and a selection of ``local.toml`` of a removed profile.

The lists show the grade and the price of each model of the model list
(``[llm.models]``): a cloud service is a first-class choice (decision of
the user, 2026-10-22), and the user compares it with the local models.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
        _profiles()
    except ConfigError as exc:
        print(f"  [warn] {exc}")
        return
    from ._llm_lists import choose_chat, choose_embeddings, global_offers, setup_services

    # Two steps (decision of the user, 2026-10-22): the services that the
    # user uses, then the service of the analysis and of the embeddings;
    # then the removal of profiles.
    setup_services(project_root, cfg)
    cfg = ui._reload(project_root, cfg)
    chat_offer, embed_offer = global_offers(project_root)
    choose_chat(project_root, cfg, offer_global=chat_offer)
    cfg = ui._reload(project_root, cfg)
    choose_embeddings(project_root, cfg, offer_global=embed_offer)
    _remove_profiles()
    if project_root is not None:
        _forget_removed_profiles(project_root)


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


def service_of(profile: dict, cloud: Mapping[str, Mapping[str, str]]) -> str:
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
    """Local Ollama: the recommended model (``llm.recommend``), a pull with consent, the profile."""
    found = ui._local_ollama_profile(project_root, cfg, cloud=cloud)
    if found is None:
        return None
    name, values = found
    # asked: _local_ollama_profile asked before it changed the shared
    # profile; a new name would make a second Ollama profile.
    written = _write_profile(name, values, asked=True)
    if written is not None:
        ui.make_machine_profile(written)
    return written


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

    from ..llm.chatgpt_plan import LoginError, login
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

    from ..config.settings import LLMConfig
    from ..llm.chatgpt_plan import API_BASE

    try:
        login(name, open_url=_open)
    except (LoginError, LLMError, OSError) as exc:
        print(f"  [warn] ChatGPT plan: {exc}")
        _drop_unused_secrets(name)
        return None
    values = _model_from_list(LLMConfig(provider="chatgpt", profile=name, chat_api_base=API_BASE), "the plan",
                              "gpt-6-luna")
    written = _write_profile(name, {"provider": "chatgpt", **values}, asked=True) if values else None
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
        from ..config.settings import LLMConfig

        llm = LLMConfig(provider=spec.name, profile=name, chat_api_base=spec.base_url, api_key_env=spec.key_env)
        values = _model_from_list(llm, spec.label, spec.recommended_model)
        return _write_profile(name, {"provider": spec.name, **values}) if values else None

    return _setup


#: A model list up to this length is printed whole; a longer one (OpenRouter
#: lists hundreds of models) gives only the recommended model and the count.
_LIST_PRINT_LIMIT = 20


def _model_from_list(llm, label: str, recommended: str) -> dict[str, object] | None:
    """Ask for a model of the model list of the provider of *llm*; return the profile values, or None.

    WHY the list (decision of the user, 2026-10-22): the names of the
    models are the current ones of the API, and the context of the model
    comes from the same list.  A fixed name went out of date: the
    recommended "deepseek-v4-flash" answered, but the list of DeepSeek does
    not hold it, thus its context was not known.  *recommended* is only
    the default, when the list holds it.  The values get ``context`` only
    when the list gives no context of the model (``_context_values``).
    """
    import difflib
    from dataclasses import replace

    from ..llm.model_list import ModelListError, provider_models

    try:
        names = [info.name for info in provider_models(llm)]
    except ModelListError as exc:
        print(f"  [warn] The model list of {label} cannot be read ({exc}): no profile is written")
        return None
    if not names:
        print(f"  [warn] The model list of {label} holds no model: no profile is written")
        return None
    default = recommended if recommended in names else names[0]
    if len(names) <= _LIST_PRINT_LIMIT:
        print(f"  Models of {label}: {', '.join(names)}")
    else:
        print(f"  {label} lists {len(names)} models; the recommended one is {default}")
    model = ui.prompt_text("Model", default=default) or default
    if model not in names:
        close = difflib.get_close_matches(model, names, n=5)
        print(f"  [skip] {model!r} is not in the model list of {label}"
              + (f" (similar names: {', '.join(close)})" if close else ""))
        return None
    context = _context_values(replace(llm, model=model))
    return None if context is None else {"model": model, **context}


def _context_values(llm) -> dict[str, object] | None:
    """Return ``{}`` when the model list gives the context of the model, else ``{"context": n}`` from a question.

    None: the user gave no context, thus the analysis cannot use the
    model.  The question comes only for a model whose list gives no
    context (decision of the user, 2026-10-22: do not ask for a value that
    the API gives).
    """
    from ..llm.model_list import ModelContextError, model_context

    try:
        context = model_context(llm)
    except ModelContextError as exc:
        if exc.not_listed or exc.unreadable:
            # A static context repairs neither: the analysis reads the list
            # at its start, and a list that cannot be read stops it too.
            print(f"  [skip] {exc}")
            return None
        print(f"  The API gives no context of {llm.model}: the LLM analysis needs it")
    else:
        print(f"  Context of {llm.model}: {context} tokens (the model list of the API)")
        return {}
    answer = (ui.prompt_text("Context of the model in tokens") or "").strip()
    if not answer.isdigit() or int(answer) <= 0:
        print(f"  [skip] {answer!r} is not a number of tokens: no profile is written")
        return None
    return {"context": int(answer)}


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


def test_profile(cfg, name: str, *, check_context: bool = True) -> str | None:
    """Send a request without code through the profile *name*; return None when it answers, else the problem.

    *check_context*: first check the model and its context with the model
    list of an API or ChatGPT profile (``context_problem``).
    """
    from ..config.settings import ConfigError, profile_llm_config
    from ..llm.client import get_chat_client
    from ..llm.errors import LLMError

    try:
        profiles = _profiles()
        if name not in profiles:
            return f"the global config has no profile {name!r} (profiles: {', '.join(profiles) or 'none'})"
        llm = profile_llm_config(cfg.llm, name)
        problem = context_problem(llm) if check_context else None
        if problem is not None:
            return problem
        get_chat_client(llm).generate(_TEST_PROMPT, max_tokens=10)
    except (ConfigError, LLMError, ValueError) as exc:
        return str(exc)
    return None


def context_problem(llm) -> str | None:
    """Return why the context of the chat model of an API or ChatGPT profile is not known, or None.

    WHY in the test of a profile: a model that the list of the provider no
    longer holds still answered the test, and the analysis stopped later
    without the context (decision of the user, 2026-10-22: the context
    must be known).  An Ollama profile gives its context through the
    server (``/api/show``), thus None here.
    """
    from ..llm.model_list import LIST_PROVIDERS, ModelContextError, model_context

    if llm.provider not in LIST_PROVIDERS:
        return None
    try:
        model_context(llm)
    except ModelContextError as exc:
        return str(exc)
    return None


def _global_text() -> str | None:
    """Return the text of the global config, or None when it cannot be read (then no restore is possible)."""
    from ..config import settings

    try:
        return settings._ensure_global_config().read_text(encoding="utf-8")
    except OSError:
        return None


def _test_new_profile(cfg, name: str, before: str | None) -> bool:
    """Test the new profile *name*; when it fails, put the global config of *before* back, unless the user keeps it.

    Returns True when the profile stays: it answers, or the user keeps it.

    WHY the earlier text and not a removal: a profile that replaced
    another one would be gone with the earlier values, and each project
    that selects it would stop with an error.
    """
    from ..config import settings
    from ..config._toml_editor import _atomic_write

    # WHY no check of the context: the setup checked it with the model list
    # (``_model_from_list``), and an openai_compatible profile without a
    # context serves the embeddings only.  The list of the LLM analysis
    # checks the context when a profile is selected for the chat.
    problem = test_profile(cfg, name, check_context=False)
    if problem is None:
        print(f"  [ok] Profile {name!r} answers")
        return True
    print(f"  [warn] Profile {name!r} does not answer: {problem}")
    if ui._confirm(f"Keep the profile {name!r} anyway?", default=False):
        return True
    if before is None:
        print(f"  [warn] The earlier global config cannot be put back: remove the profile with "
              f"'fw-context llm remove {name}'")
        return False
    try:
        _atomic_write(settings._GLOBAL_CONFIG_PATH.resolve(), before)
    except OSError as exc:
        print(f"  [warn] The earlier global config cannot be written back ({exc}): remove the profile with "
              f"'fw-context llm remove {name}'")
        return False
    print(f"  [ok] The global config has its earlier text: profile {name!r} is not changed")
    _drop_unused_secrets(name)
    return False


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


def _forget_removed_profiles(project_root: Path) -> None:
    """Remove a selection of ``local.toml`` that names a profile that is gone: else the next load stops.

    The global default then applies.  A selection of ``config.toml`` (in
    git) is only named: init writes only ``local.toml``.
    """
    from ..config import settings

    profiles = _profiles()
    for file_name in ("local.toml", "config.toml"):
        path = project_root / ".fw-context" / file_name
        try:
            llm = (settings.read_config_file(path).get("llm") or {}) if path.is_file() else {}
        except settings.ConfigError:
            continue
        gone = [key for key in ("profile", "embed_profile") if isinstance(llm.get(key), str) and llm[key]
                and llm[key] not in profiles]
        if not gone:
            continue
        if file_name == "config.toml":
            print(f"  [warn] .fw-context/config.toml selects {', '.join(repr(llm[k]) for k in gone)}, which the "
                  "global config does not have: change the file")
            continue
        from ._llm_lists import _write_local

        # The model goes with its profile: else the model of the removed
        # API goes to the Ollama of the global settings.
        clear = gone + (["embed_model"] if "embed_profile" in gone and "embed_model" in llm else [])
        if _write_local(project_root, {}, clear):
            print(f"  [ok] .fw-context/local.toml selected {', '.join(repr(llm[k]) for k in gone)}, which is "
                  "removed: the global settings apply")


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


def _analysis_on_again(project_root: Path) -> None:
    """Remove ``analyze_symbols = false`` and ``enabled = false`` of ``local.toml``: a selected backend analyses.

    WHY: option "d" writes ``analyze_symbols = false``, and an older
    release wrote ``enabled = false``.  A later selection of a profile
    said "[ok]" while the analysis stayed off.
    """
    from ..config import settings

    path = project_root / ".fw-context" / "local.toml"
    try:
        llm = settings.read_config_file(path).get("llm") if path.is_file() else None
    except settings.ConfigError:
        return
    if not isinstance(llm, dict):
        return
    off = [key for key in ("analyze_symbols", "enabled") if llm.get(key) is False]
    if not off:
        return
    from ._llm_lists import _write_local

    if _write_local(project_root, {}, off):
        print(f"  [ok] LLM analysis on again (removed {' and '.join(f'{key} = false' for key in off)} from "
              ".fw-context/local.toml)")

