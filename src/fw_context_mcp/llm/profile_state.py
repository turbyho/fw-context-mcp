"""The state of an LLM profile for a person: where the source code goes, and whether the profile can run.

The setup (``cli/_llm_wizard.py``), ``fw-context llm list`` and the
question at the start of a run show it.  No function here sends a chat
request: "ready" means that the key or the sign-in is there, not that the
server answers (``fw-context llm test`` does that).
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
    and their host (``cli._llm_wizard.cloud_hosts``): a cloud model has a
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
