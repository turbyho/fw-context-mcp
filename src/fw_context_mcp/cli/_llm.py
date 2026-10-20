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


def cmd_llm(args: argparse.Namespace) -> int:
    """Without a subcommand: tell the subcommands."""
    print("usage: fw-context llm key {set,delete,status} [profile]")
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
