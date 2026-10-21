"""The state of an LLM profile for a person: where the source code goes, and whether the profile can run.

The setup (``cli/_llm_wizard.py``), ``fw-context llm list`` and the
question at the start of a run show it.  No function here sends a chat
request: "ready" means that the key or the sign-in is there, not that the
server answers (``fw-context llm test`` does that).

The module also tells the Ollama profile of this machine
(``ollama_profile_name``) and if a project can select a profile
(``project_choice_problem``).  WHY one Ollama profile for each machine
(decision of the user, 2026-10-21): the background runs of the projects
overlap, a change of the model or of the context loads the model again
(3 to 10 s), and two models at the same time share the GPU memory.  An
external profile (an API, the ChatGPT plan, a cloud model of Ollama)
shares no memory of this machine, thus each project selects its own.
An Ollama on another computer counts as Ollama: it shares the memory of
that server.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..config.settings import _DEFAULT_OLLAMA_URL, _is_loopback_url


@dataclass(frozen=True)
class ProfileState:
    """One profile of the global config.

    ``host`` is where the chat sends the source code; ``external`` is True
    when that is not this machine.  ``problem`` is None when the profile
    can run, else a sentence with the command that repairs it.
    """

    name: str
    provider: str
    model: str
    host: str
    external: bool
    problem: str | None


def profile_state(
    name: str, profile: Mapping[str, object], *, cloud: Mapping[str, Mapping[str, str]] | None = None,
) -> ProfileState:
    """Return the state of the profile *name* with the values *profile*.

    *cloud* maps the URL of each loopback Ollama server to its cloud models
    and their host (``cloud_hosts``): a cloud model has a
    loopback URL, and its source code still leaves the machine.  Without
    it, such a model shows its loopback URL.
    """
    provider, model = str(profile.get("provider", "")), str(profile.get("model", ""))
    problem: str | None = None
    if provider == "ollama":
        url = str(profile.get("url") or _DEFAULT_OLLAMA_URL)
        host = (cloud or {}).get(url.rstrip("/"), {}).get(model) if _is_loopback_url(url) else None
        host = host or url
    elif provider == "chatgpt":
        from .chatgpt_plan import API_BASE, load_credentials

        host = API_BASE
        credentials = load_credentials(name)
        if credentials is None or not credentials.signed_in():
            problem = f"not signed in: run 'fw-context llm login {name}'"
    else:
        from .providers import provider_spec
        from .secrets import api_key_source

        spec = provider_spec(provider)
        host = str(profile.get("url") or (spec.base_url if spec else ""))
        if spec is not None:
            own_url = host.rstrip("/") == spec.base_url.rstrip("/")
            env_name = str(profile["api_key_env"]) if "api_key_env" in profile else spec.key_env if own_url else ""
            if api_key_source(name, env_name, spec.name if own_url else "") is None:
                problem = f"no API key: run 'fw-context llm key set {name}'"
    return ProfileState(name, provider, model, host, external=not _is_loopback_url(host), problem=problem)


def cloud_lists(profiles: Mapping[str, Mapping[str, object]]) -> dict[str, dict[str, str] | None]:
    """Return ``{server URL: {model: host}}`` of the cloud models of each loopback Ollama server; None: no list.

    The local Ollama is always asked; each other loopback server of an
    Ollama profile too.  WHY each server: a cloud model is a model of the
    server that the profile names, not of the server of the embeddings.
    None is a server that gives no list (it does not run): a cloud model
    of it cannot be told from a model of this machine.
    """
    from ._diag import remote_hosts

    urls = {_DEFAULT_OLLAMA_URL.rstrip("/")}
    for profile in profiles.values():
        if profile.get("provider") == "ollama":
            url = str(profile.get("url") or _DEFAULT_OLLAMA_URL).rstrip("/")
            if _is_loopback_url(url):
                urls.add(url)
    return {url: remote_hosts(url) for url in sorted(urls)}


def cloud_hosts(profiles: Mapping[str, Mapping[str, object]]) -> dict[str, dict[str, str]]:
    """Return ``cloud_lists`` with no list as no cloud model: for a display, which decides nothing."""
    return {url: hosts or {} for url, hosts in cloud_lists(profiles).items()}


def unknown_servers(profiles: Mapping[str, Mapping[str, object]],
                    lists: Mapping[str, Mapping[str, str] | None]) -> list[str]:
    """Return the loopback servers of the Ollama profiles that give no model list (``cloud_lists``).

    A writer that decides about an Ollama profile stops while this list
    is not empty: a cloud model would look like a model of this machine,
    and the project would send its code out, or could not select it.
    """
    urls = set()
    for profile in profiles.values():
        if profile.get("provider") == "ollama":
            url = str(profile.get("url") or _DEFAULT_OLLAMA_URL).rstrip("/")
            if _is_loopback_url(url) and lists.get(url) is None:
                urls.add(url)
    return sorted(urls)


def known(lists: Mapping[str, Mapping[str, str] | None]) -> dict[str, Mapping[str, str]]:
    """Return *lists* without the servers that give no list, in the form of ``cloud_hosts``."""
    return {url: hosts for url, hosts in lists.items() if hosts is not None}


def is_cloud_model(profile: Mapping[str, object], cloud: Mapping[str, Mapping[str, str]]) -> bool:
    """Return True when *profile* names a cloud model of a loopback Ollama (*cloud*: ``cloud_hosts``)."""
    if profile.get("provider") != "ollama":
        return False
    url = str(profile.get("url") or _DEFAULT_OLLAMA_URL).rstrip("/")
    return _is_loopback_url(url) and profile.get("model") in cloud.get(url, {})


def is_ollama(profile: Mapping[str, object], cloud: Mapping[str, Mapping[str, str]]) -> bool:
    """Return True when *profile* runs its model on an Ollama server: of this machine or of another computer.

    A cloud model of Ollama is not one: it runs on the host of the cloud.
    """
    return profile.get("provider") == "ollama" and not is_cloud_model(profile, cloud)


def _profiles_of(llm: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    profiles = llm.get("profiles")
    if not isinstance(profiles, Mapping):
        return {}
    return {name: profile for name, profile in profiles.items() if isinstance(profile, Mapping)}


def ollama_profile_name(llm: Mapping[str, object], cloud: Mapping[str, Mapping[str, str]]) -> str | None:
    """Return the Ollama profile of this machine from the ``[llm]`` table of the global config, or None.

    ``[llm] ollama_profile`` names it.  Without the key: the global
    ``profile`` when it is an Ollama profile, else the only Ollama
    profile.  None: no Ollama profile, a key that names no Ollama profile
    (``config_value_problems`` reports it), or several Ollama profiles
    and no key that selects one.
    """
    ollama = [name for name, profile in _profiles_of(llm).items() if is_ollama(profile, cloud)]
    named = llm.get("ollama_profile")
    if isinstance(named, str) and named:
        return named if named in ollama else None
    default = llm.get("profile")
    if isinstance(default, str) and default in ollama:
        return default
    return ollama[0] if len(ollama) == 1 else None


def undecided(name: str, profiles: Mapping[str, Mapping[str, object]],
              lists: Mapping[str, Mapping[str, str] | None], *, machine_too: bool) -> str | None:
    """Return why a writer cannot decide about the profile *name* now, or None.

    It cannot when *name* is an Ollama profile of a loopback server that
    gives no model list, or with *machine_too* when any such server gives
    none (the Ollama profile of this machine then is not known).  A
    profile of an API or of the ChatGPT plan needs no list.
    """
    unknown = unknown_servers(profiles, lists)
    profile = profiles.get(name) or {}
    if not unknown or (profile.get("provider") != "ollama" and not machine_too):
        return None
    return (f"Ollama at {', '.join(unknown)} gives no model list, thus fw-context cannot tell a cloud model from a "
            "model of this machine: start Ollama, then repeat")


def project_choice_problem(name: str, llm: Mapping[str, object],
                           cloud: Mapping[str, Mapping[str, str]]) -> str | None:
    """Return why a project cannot select the profile *name*, or None when it can.

    A project selects an external profile or the Ollama profile of this
    machine; another Ollama profile is for one run only (``--llm``).
    """
    profile = _profiles_of(llm).get(name)
    if profile is None or not is_ollama(profile, cloud):
        return None
    machine = ollama_profile_name(llm, cloud)
    if machine == name:
        return None
    if machine is None:
        return (f"the profile {name!r} is an Ollama profile, and the global config selects no Ollama profile of "
                f"this machine: run 'fw-context llm default {name} --ollama'")
    return (f"the profile {name!r} is an Ollama profile, and the Ollama profile of this machine is {machine!r}: "
            f"each project uses that one (one model in the memory). Use --llm {name} for one run, or make it the "
            f"Ollama profile of each project with 'fw-context llm default {name} --ollama'")
