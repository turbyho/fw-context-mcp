"""``fw-context llm key`` — store, remove and show the API keys of the LLM profiles.

The work is ``llm.secrets``; this module parses the command line and
prints the result.  No output holds the value of a key: ``status`` names
the place of the key only.

``set`` reads the key without an echo on a terminal (``getpass``), else
one line of stdin, thus a script can give the key through a pipe and not
on the command line, where the process list and the shell history show it.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path


def cmd_llm(args: argparse.Namespace) -> int:
    """Without a subcommand: tell the subcommands."""
    print("usage: fw-context llm setup                         (the setup of the LLM services)")
    print("       fw-context llm list | test <profile> | remove <profile>")
    print("       fw-context llm default <profile> [--global]")
    print("       fw-context llm key {set,delete,status} [profile]")
    print("       fw-context llm {login,logout,models} <profile>   (a profile of provider chatgpt)")
    return 0


def _project_and_config(args: argparse.Namespace):
    """Return (project root or None, config); the root is None outside a project of fw-context.

    Raises:
        ConfigError: a config file cannot be used.
    """
    from ..config import load
    from ..utils import resolve_project_root

    try:
        root: Path | None = resolve_project_root(getattr(args, "project", None))
    except OSError:
        root = None
    if root is not None and not (root / ".fw-context").is_dir():
        root = None
    return root, load(root)


def cmd_llm_setup(args: argparse.Namespace) -> int:
    """Run the setup of the LLM services (the same code as option 2 of the LLM step of ``fw-context init``).

    Exit codes: 0 = done, 1 = no terminal, or a config file cannot be used.
    """
    from ..config.settings import ConfigError
    from ._llm_wizard import run_wizard

    if not sys.stdin.isatty():
        print("[error] 'fw-context llm setup' asks questions: run it in a terminal", file=sys.stderr)
        return 1
    try:
        root, cfg = _project_and_config(args)
    except ConfigError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    run_wizard(root, cfg)
    return 0


def cmd_llm_list(args: argparse.Namespace) -> int:
    """List the profiles: provider, model, where the source code goes, and what a profile needs.

    Exit codes: 0 = listed, 1 = a config file cannot be used.
    """
    from ..config.settings import ConfigError, llm_key_sources
    from ..llm.profile_state import profile_state
    from ._llm_wizard import cloud_hosts

    try:
        root, cfg = _project_and_config(args)
    except ConfigError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    profiles = _profiles()
    if not profiles:
        print("No LLM profile: run 'fw-context llm setup'")
        return 0
    from ..config import settings

    global_default = (settings.read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm") or {}).get("profile", "")
    project_default = cfg.llm.profile if root is not None and llm_key_sources(root).get("profile") in (
        "local", "project") else ""
    cloud = cloud_hosts(profiles)
    for name, profile in profiles.items():
        state = profile_state(name, profile, cloud=cloud)
        marks = [label for label, on in (("global default", name == global_default),
                                         ("this project", name == project_default)) if on]
        where = f"⚠ {state.host}" if state.external else "this machine"
        line = f"{name}: {state.provider} {state.model}, {where}"
        if marks:
            line += f" [{', '.join(marks)}]"
        if state.problem:
            line += f" — {state.problem}"
        print(line)
    return 0


def cmd_llm_test(args: argparse.Namespace) -> int:
    """Send a request without code through a profile.

    Exit codes: 0 = it answers, 1 = it does not, or a config file cannot be used.
    """
    from ..config.settings import ConfigError
    from ._llm_wizard import test_profile

    try:
        _root, cfg = _project_and_config(args)
    except ConfigError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    problem = test_profile(cfg, args.profile)
    if problem is not None:
        print(f"[error] Profile {args.profile!r} does not answer: {problem}", file=sys.stderr)
        return 1
    print(f"[ok] Profile {args.profile!r} answers")
    return 0


def cmd_llm_default(args: argparse.Namespace) -> int:
    """Select the profile of the project (``local.toml``, with a test request), or with ``--global`` of each project.

    Exit codes: 0 = selected, 1 = no such profile, no project, the test
    failed, or a config file cannot be used.
    """
    from ..config import settings
    from ..config._profiles import select_global_profile
    from ..llm.configure import configure_llm_core

    if args.global_default:
        try:
            select_global_profile(args.profile)
        except (OSError, settings.ConfigError) as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 1
        print(f"[ok] Profile {args.profile!r} is the profile of each project that selects none")
        return 0
    try:
        root, _cfg = _project_and_config(args)
    except settings.ConfigError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    if root is None:
        print("[error] No project of fw-context here: run 'fw-context init', or use --global", file=sys.stderr)
        return 1
    if args.profile not in _profiles():
        print(f"[error] The global config has no profile {args.profile!r}", file=sys.stderr)
        return 1
    result = configure_llm_core(root, {"enabled": True, "profile": args.profile})
    if result.get("status") != "ok":
        print(f"[error] {result.get('message', 'the test of the profile failed')}", file=sys.stderr)
        return 1
    print(f"[ok] .fw-context/local.toml selects profile {args.profile!r}")
    if result.get("compliance_warning"):
        print(f"⚠ {result['compliance_warning']}")
    return 0


def cmd_llm_remove(args: argparse.Namespace) -> int:
    """Remove a profile, its own stored key, and the sign-in of a ChatGPT profile.

    Exit codes: 0 = removed, 1 = no such profile, or the global config
    selects it.
    """
    from ._llm_wizard import remove_profile_and_secrets

    if args.profile not in _profiles():
        print(f"[error] The global config has no profile {args.profile!r}", file=sys.stderr)
        return 1
    return 0 if remove_profile_and_secrets(args.profile) else 1


def _plan_profile(name: str) -> bool:
    """Return True when *name* is a profile of provider ``chatgpt`` of the global config; else tell why."""
    profile = _profiles().get(name)
    if profile is None:
        print(f"[error] The global config has no profile {name!r}. Add it first:\n"
              f"  [llm.profiles.{name}]\n  provider = \"chatgpt\"\n  model = \"<model>\"", file=sys.stderr)
        return False
    if profile.get("provider") != "chatgpt":
        print(f"[error] Profile {name!r} has provider {profile.get('provider')!r}: only a profile of provider "
              "chatgpt signs in", file=sys.stderr)
        return False
    return True


def cmd_llm_login(args: argparse.Namespace) -> int:
    """Sign a ``chatgpt`` profile in with ChatGPT in the browser.

    Exit codes: 0 = signed in, 1 = not a chatgpt profile, or the sign-in
    failed.
    """
    import webbrowser

    from ..llm.chatgpt_plan import LoginError, login

    if not _plan_profile(args.profile):
        return 1

    def _open(url: str, shown: str) -> None:
        print("Sign in with ChatGPT in the browser. When no browser opens, open this URL:")
        print(f"  {shown}")
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass  # the user has the URL above

    try:
        credentials = login(args.profile, open_url=_open)
    except LoginError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"[error] the sign-in cannot be stored: {exc}", file=sys.stderr)
        return 1
    print(f"[ok] Profile {args.profile!r} is signed in as {credentials.email or credentials.subject}. "
          "The requests use the usage of the ChatGPT plan (ChatGPT Settings → Usage)")
    return 0


def cmd_llm_logout(args: argparse.Namespace) -> int:
    """Revoke the sign-in of a ``chatgpt`` profile and remove its tokens.

    Exit codes: 0 after the tokens are removed (a revocation that is not
    confirmed gives a note), 1 for a profile that is not a chatgpt profile.
    """
    from ..llm.chatgpt_plan import load_credentials, logout

    # Tokens without a profile (a profile that was removed) can still be
    # revoked: else a valid refresh token stayed on the disk.
    if load_credentials(args.profile) is None and not _plan_profile(args.profile):
        return 1
    if logout(args.profile):
        print(f"[ok] Profile {args.profile!r} is signed out")
    else:
        print(f"[ok] The tokens of profile {args.profile!r} are removed, but the revocation is not confirmed: "
              "disconnect fw-context in ChatGPT Settings")
    return 0


def cmd_llm_models(args: argparse.Namespace) -> int:
    """List the models of the ChatGPT plan of a ``chatgpt`` profile.

    Exit codes: 0 = listed, 1 = not a chatgpt profile, not signed in, or
    the request failed.
    """
    from ..llm.chatgpt_plan import list_models
    from ..llm.errors import LLMError

    if not _plan_profile(args.profile):
        return 1
    try:
        models = list_models(args.profile)
    except LLMError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    for slug, label in models:
        print(f"{slug}  ({label})" if label != slug else slug)
    return 0


def _profiles() -> dict[str, dict]:
    """Return the profiles of the global config; a config that cannot be read gives none, and a warning."""
    from ..config import settings

    try:
        llm = settings.read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm")
    except settings.ConfigError as exc:
        print(f"[warn] {exc}", file=sys.stderr)
        return {}
    profiles = llm.get("profiles") if isinstance(llm, dict) else None
    if not isinstance(profiles, dict):
        return {}
    return {name: profile for name, profile in profiles.items() if isinstance(profile, dict)}


def cmd_llm_key_set(args: argparse.Namespace) -> int:
    """Store the API key of a profile in the keyring, else in ``secrets.toml``.

    Exit codes: 0 = stored, 1 = no key given, a name that is not valid,
    or no place could store it.
    """
    from ..config._profiles import _NAME
    from ..llm.providers import PROVIDERS
    from ..llm.secrets import SecretStoreError, provider_entry, secrets_path, set_api_key

    profile = args.profile
    provider = profile.removeprefix("provider:") if profile.startswith("provider:") else ""
    if provider and provider not in PROVIDERS:
        print(f"[error] {provider!r} is not a provider (providers: {', '.join(PROVIDERS)})", file=sys.stderr)
        return 1
    if provider:
        profile = provider_entry(provider)
    elif not _NAME.fullmatch(profile):
        print(f"[error] profile name {profile!r} must have only letters, digits, '_' and '-'", file=sys.stderr)
        return 1
    elif profile not in _profiles():
        # Not an error: the user can store the key first and write the
        # profile after it.
        print(f"[note] The global config has no profile {profile!r} yet", file=sys.stderr)
    value = getpass.getpass(f"API key of LLM profile {profile!r}: ") if sys.stdin.isatty() else sys.stdin.readline()
    value = value.strip()
    if not value:
        print("[error] No key was given: nothing was stored", file=sys.stderr)
        return 1
    try:
        place = set_api_key(profile, value)
    except SecretStoreError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    where = "the keyring of the system" if place == "keyring" else f"{secrets_path()} (no keyring can be used)"
    print(f"[ok] The API key of profile {profile!r} is stored in {where}")
    return 0


def cmd_llm_key_delete(args: argparse.Namespace) -> int:
    """Remove the API key of a profile from the keyring and from ``secrets.toml``.

    Exit codes: 0 = removed or there was none, 1 = the file could not be written.
    """
    from ..llm.secrets import SecretStoreError, delete_api_key

    try:
        removed = delete_api_key(args.profile)
    except SecretStoreError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    if removed:
        print(f"[ok] Removed the API key of profile {args.profile!r} from: {', '.join(removed)}")
    else:
        print(f"[ok] No stored API key of profile {args.profile!r}")
    return 0


def _plan_status(name: str) -> None:
    """Print the sign-in of a ``chatgpt`` profile: the account, never a token."""
    from ..llm.chatgpt_plan import load_credentials

    credentials = load_credentials(name)
    if credentials is not None and credentials.signed_in():
        print(f"{name}: signed in with ChatGPT as {credentials.email or credentials.subject}")
    else:
        print(f"{name}: not signed in (sign in with 'fw-context llm login {name}')")


def cmd_llm_key_status(args: argparse.Namespace) -> int:
    """Tell for each API profile (or for the given one) which place gives its key.

    Exit code: 0 always; a missing key is a state, not an error of the command.
    """
    from ..llm.providers import provider_spec
    from ..llm.secrets import api_key_source

    profiles = _profiles()
    if args.profile:
        names = [args.profile]
    else:
        names = [name for name, profile in profiles.items() if profile.get("provider") != "ollama"]
        if not names:
            print("No profile of the global config uses an API")
            return 0
    for name in names:
        profile = profiles.get(name, {})
        if profile.get("provider") == "chatgpt":
            _plan_status(name)
            continue
        spec = provider_spec(str(profile.get("provider", "")))
        own_url = spec is not None and str(profile.get("url", spec.base_url)).rstrip("/") == spec.base_url.rstrip("/")
        default_env = spec.key_env if spec is not None and own_url else ""
        env_name = str(profile["api_key_env"]) if "api_key_env" in profile else default_env
        source = api_key_source(name, env_name, spec.name if spec is not None and own_url else "")
        if source is None:
            print(f"{name}: no API key (store one with 'fw-context llm key set {name}')")
            continue
        place, _, owner = source.partition(":")
        if place == "env":
            print(f"{name}: the environment variable {owner}")
            continue
        where = "the keyring of the system" if place == "keyring" else "secrets.toml"
        print(f"{name}: {where}" + (f" (the key of provider {owner})" if owner else ""))
    return 0
