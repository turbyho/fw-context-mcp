"""Interactive fallback prompts for ``fw-context init``.

Invoked only when auto-detection fails or a required parameter is missing
(e.g. Zephyr board, Arduino FQBN, LLM backend).  Every prompt has a
default (Enter = default).  ``non_interactive`` mode short-circuits all
prompts — no questions are asked in CI/pipe mode.

Idempotence: when a value is already configured, the flow shows the current
value with a "Change? [y/N]" prompt (default N = keep).  Re-running ``init``
and holding Enter therefore walks every section without modifying anything.
"""

from __future__ import annotations

from pathlib import Path

_BUILD_SYSTEM_CHOICES: list[tuple[str, str]] = [
    ("mbed-os", "Mbed OS 5/6"),
    ("zephyr", "Zephyr RTOS"),
    ("platformio", "PlatformIO"),
    ("cmake", "generic CMake"),
    ("esp-idf", "ESP-IDF"),
    ("arduino", "Arduino CLI"),
    ("keil-mdk", "Keil MDK"),
    ("iar-ewarm", "IAR EWARM"),
    ("makefile", "bare Makefile"),
    ("stm32cubeide", "STM32CubeIDE — detect-only"),
    ("ti-ccs", "TI Code Composer Studio — detect-only"),
]


def _input(prompt: str) -> str:
    """Read a trimmed line from stdin; EOF is an empty answer, Ctrl-C stops the command.

    WHY Ctrl-C stops: before, it was an empty answer, thus the default of
    the question ran — "Keep the profile?" removed it, and the profile of
    the project removed the keys of ``local.toml``.  The CLI turns the
    interrupt into exit code 130 (``cli.main``).
    """
    try:
        return input(prompt).strip()
    except EOFError:
        return ""
    except KeyboardInterrupt:
        print()
        raise


def prompt_build_system(non_interactive: bool, *, current: str | None = None) -> str | None:
    """Ask which build system to use (numbered list). Returns key or None.

    When *current* is given, it is marked in the menu and an empty answer
    keeps it — a re-run of ``init`` can walk the section with Enter only.
    """
    if non_interactive:
        return None
    print("\n  What build system does this project use?")
    for i, (key, label) in enumerate(_BUILD_SYSTEM_CHOICES, start=1):
        marker = "  (current)" if key == current else ""
        print(f"  {i:>2}. {key} ({label}){marker}")
    print("   0. none / manual")
    answer = _input("  > ")
    try:
        n = int(answer)
    except ValueError:
        return current
    if n == 0:
        return None
    if 1 <= n <= len(_BUILD_SYSTEM_CHOICES):
        return _BUILD_SYSTEM_CHOICES[n - 1][0]
    return current


def _configure_ncs_version(
    project_root: Path, *, dry_run: bool, non_interactive: bool,
    current: str | None = None,
) -> str | None:
    """Pick an NCS version and write the activation script for it.

    *current* is the ``[build] activate`` already in the config, when there is
    one.  It is offered first and kept by default, so a re-run of init shows
    what the project uses — a hand-written script included — and only moves
    off it when asked.

    Returns the script path, or None when there is nothing to configure —
    no NCS installs, or no answer in a non-interactive run.  The caller then
    falls back to the generic detection and prompt.
    """
    from ..indexer.builders.zephyr import ZephyrBuildSystem

    if current:
        # A configured environment is the answer until someone changes it;
        # do not let the environment scan below silently override it.
        installs = ZephyrBuildSystem.list_installed_ncs()
        version = prompt_ncs_version(
            installs, default=None, non_interactive=non_interactive, current=current
        )
        if version is None or version == current:
            print(f"  Build activate: {current} (already configured)")
            return current
        return _write_ncs_script(project_root, installs, version, dry_run=dry_run)

    # Upstream Zephyr sets its own paths — sourcing zephyr-env.sh or
    # activating the workspace exports ZEPHYR_BASE and
    # ZEPHYR_SDK_INSTALL_DIR.  When they are already there, use them; there
    # is nothing to choose between.  Only NCS needs asking, because its
    # environment does not exist until nrfutil is told which version to make.
    try:
        zephyr_sdk = ZephyrBuildSystem.zephyr_sdk_from_environment()
    except Exception:  # noqa: BLE001 — detection is best-effort at init
        zephyr_sdk = None
    if zephyr_sdk is not None and zephyr_sdk.usable and zephyr_sdk.env_script:
        print(f"  [ok] Zephyr SDK from the environment: {zephyr_sdk.path}")
        return str(zephyr_sdk.env_script)

    try:
        installs = ZephyrBuildSystem.list_installed_ncs()
    except Exception:  # noqa: BLE001 — detection is best-effort at init
        return None
    if not installs:
        return None

    preferred = ZephyrBuildSystem.preferred_ncs_version(project_root)
    version = prompt_ncs_version(
        installs, default=preferred, non_interactive=non_interactive
    )
    if version is None:
        print(
            "  No SDK version chosen — set [build] activate yourself, or "
            "re-run init interactively."
        )
        return None

    return _write_ncs_script(project_root, installs, version, dry_run=dry_run)


def _write_ncs_script(
    project_root: Path, installs: list, version: str, *, dry_run: bool
) -> str | None:
    """Generate the activation script for *version* and return its path."""
    from ..indexer.builders.zephyr import ZephyrBuildSystem

    chosen = next((i for i in installs if i.version == version), None)
    if chosen is None:
        return None

    nrfutil = ZephyrBuildSystem._find_nrfutil()
    if nrfutil is None:
        print("  nrfutil with 'sdk-manager' not found — cannot write the env script.")
        return None

    if dry_run:
        print(f"  [dry-run] Would use nRF Connect SDK {version} ({chosen.path})")
        return None

    script = ZephyrBuildSystem._write_ncs_env_script(
        project_root, nrfutil, version, str(chosen.path.parent)
    )
    print(f"  [ok] nRF Connect SDK: {version} ({chosen.path})")
    return script


def prompt_ncs_version(
    installs: list, *, default: str | None, non_interactive: bool,
    current: str | None = None,
) -> str | None:
    """Ask which nRF Connect SDK version to build against.

    WHY this is asked rather than detected: the SDK version is an argument to
    ``nrfutil sdk-manager toolchain env``, not something discoverable from the
    project.  Before that environment is set up there is no ZEPHYR_BASE and
    no toolchain on PATH, so a machine with several SDKs installed offers no
    signal about which one this project wants.  Guessing "the newest" builds
    the index against different headers and macros than the developer
    compiles with.

    *default* is the version the project's own environment points at, when
    there is one; Enter keeps it.  Non-interactive returns the default, which
    may be None — the caller then declines to configure rather than pick.
    """
    usable = [i for i in installs if i.usable]
    if current is None:
        if not usable:
            return None
        if len(usable) == 1:
            # Nothing to choose.
            return usable[0].version
    if non_interactive:
        # Never switch an unattended run off what is configured.
        return current or default

    print("\n  Which nRF Connect SDK should this project build with?")
    entries: list[tuple[str, str]] = []
    if current is not None:
        entries.append((current, f"{current}  (current)"))
    for install in usable:
        marker = "  (matches this project's environment)" if install.version == default else ""
        entries.append((install.version, f"{install.describe()}{marker}"))
    for index, (_value, line) in enumerate(entries, start=1):
        print(f"  {index:>2}. {line}")

    keep = current or default
    suffix = f" [{keep}]" if keep else ""
    answer = _input(f"  >{suffix} ")
    if not answer:
        return keep
    try:
        choice = int(answer)
    except ValueError:
        # Accept the version string too — "v3.2.3" is what the user sees.
        match = [v for v, _ in entries if v == answer.strip()]
        return match[0] if match else keep
    if 1 <= choice <= len(entries):
        return entries[choice - 1][0]
    return keep


def prompt_text(
    label: str,
    *,
    default: str | None = None,
    non_interactive: bool = False,
) -> str | None:
    """Ask a free-text question. Returns the answer (or default on empty)."""
    if non_interactive:
        return default
    suffix = f" (default: {default})" if default else ""
    answer = _input(f"  {label}{suffix} > ")
    return answer or default


def confirm_change(label: str, current: str, *, non_interactive: bool = False) -> bool:
    """Ask whether to change an already-configured value. Default N (keep)."""
    print(f"  {label}: {current} (already configured)")
    if non_interactive:
        return False
    return _input("  Change? [y/N] > ").lower() in ("y", "yes")


def _confirm(question: str, *, default: bool = True) -> bool:
    """Yes/no prompt with a default.  Returns the boolean answer."""
    yn = "Y/n" if default else "y/N"
    answer = _input(f"  {question} [{yn}] > ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


# The name of each config layer, as the LLM step of init gives it.
_LAYER_FILES = {
    "global": "the global config",
    "project": ".fw-context/config.toml",
    "local": ".fw-context/local.toml",
}


def _llm_layers(project_root: Path) -> list[str]:
    """Return the config layers that select the LLM backend, in merge order.

    A layer selects the backend when it sets a key of ``LLM_BACKEND_KEYS``.
    Before, init read only ``local.toml``, thus a project that used a
    complete ``[llm]`` table of the global config got "LLM: not
    configured", and the menu gave no way to keep the global settings.
    """
    from ..config.settings import llm_key_sources

    found = set(llm_key_sources(project_root).values())
    return [layer for layer in _LAYER_FILES if layer in found]


def _llm_summary(cfg) -> str:
    """Summarize the LLM config as a one-line display string.

    Used in the "already configured" line so the user can recognize the
    current backend at a glance before deciding whether to change it.
    """
    llm = cfg.llm
    if not llm.enabled:
        return "disabled"
    server = llm.chat_api_base or llm.ollama_url
    return (f"profile {llm.profile or '(default)'}: {llm.provider} {llm.model} @ {server}, "
            f"embedding {llm.embed_model or 'auto'}")


def prompt_llm_config(project_root: Path, cfg, *, non_interactive: bool) -> None:
    """Configure the LLM backend interactively.  Idempotent.

    The step tells where the backend comes from: the project
    (``config.toml`` or ``local.toml``), the global config, or nowhere.

    * The project selects the backend: a "Change? [y/N]" prompt, with the
      file named.  On "y", the two lists.
    * Else: the two lists (``cli._llm_lists``): where the LLM analysis runs,
      and where the embeddings run (decision of the user, 2026-10-22).
      Enter keeps the current setting of each.

    In a project that selects the chat or the embeddings in ``local.toml``
    and not in ``config.toml``, option 0 of a list removes those keys from
    ``local.toml``, and the global settings then apply.  Option 0 is not
    given when ``config.toml`` selects the backend: init writes only
    ``local.toml``, and a committed setting applies to each user.

    Non-interactive mode writes nothing.  It prints where the settings come
    from, or the manual-edit instructions when nothing selects the backend.
    """
    layers = _llm_layers(project_root)
    project_layers = [layer for layer in layers if layer != "global"]
    from_global = "global" in layers

    if non_interactive:
        if not project_layers and from_global:
            print(f"  LLM: {_llm_summary(cfg)} (from the global config)")
        elif not project_layers:
            print("  LLM: not configured")
            print("    Configure it by running `fw-context init` interactively,")
            print("    or edit .fw-context/local.toml manually (see template).")
        return

    if project_layers:
        where = " and ".join(_LAYER_FILES[layer] for layer in project_layers)
        if not confirm_change("LLM", f"{_llm_summary(cfg)} (set in {where})"):
            return
        print("  LLM: re-configure")
    elif from_global:
        print(f"  LLM: {_llm_summary(cfg)} (from the global config)")
    else:
        print("  LLM: not configured")
    print("  fw-context uses an LLM for the symbol analysis and smart search, and embeddings for the search")
    print("  by meaning. Each runs on this machine, on another computer, or at a cloud service.")
    from ._llm_lists import choose_chat, choose_embeddings, global_offers, setup_services
    from ._llm_wizard import _machine_report

    chat_offer, embed_offer = global_offers(project_root)
    _machine_report()
    setup_services(project_root, cfg)
    cfg = _reload(project_root, cfg)
    choose_chat(project_root, cfg, offer_global=chat_offer)
    cfg = _reload(project_root, cfg)
    choose_embeddings(project_root, cfg, offer_global=embed_offer)


def _reload(project_root: Path | None, cfg):
    """Return the config of *project_root* after a change of the LLM setup; *cfg* when it cannot be loaded."""
    from ..config import load, settings

    settings._config_cache.clear()
    try:
        return load(project_root)
    except settings.ConfigError as exc:
        print(f"  [warn] {exc}")
        return cfg


def _disable_llm_analysis(project_root: Path, cfg) -> None:
    """Turn off the LLM analysis of the project, and keep the embeddings (option "d" of the chat list).

    Writes ``[llm] analyze_symbols = false`` to ``local.toml``.  WHY not
    ``enabled = false``, as before: that also turns off the embeddings and
    the chat of ``smart_search``, but the menu said "embedding still
    works".  WHY no test call (``configure_llm_core``): the option needs
    no chat model, and a test of the chat model failed on a machine
    without one, and put the old settings back.
    """
    from ..config.settings import ConfigError, _update_local_toml

    try:
        _update_local_toml(project_root, {"analyze_symbols": False})
    except (ConfigError, OSError) as exc:
        print(f"  [warn] .fw-context/local.toml cannot be changed ({exc}). Nothing was changed.")
        return
    print("  [ok] LLM analysis off ([llm] analyze_symbols = false) — the embeddings stay")
    if not cfg.llm.enabled:
        print("  [note] The LLM is off ([llm] enabled = false): the embeddings need enabled = true")


def _local_ollama_profile(
    project_root: Path | None, cfg, *, cloud: dict[str, str] | None = None,
) -> tuple[str, dict[str, object]] | None:
    """Ask for the chat model of the local Ollama, pull it with consent; return (profile name, values).

    The default of the prompt is the model of the Ollama profile of this
    machine, else the recommendation.  WHY: before, the default was always
    the recommendation, thus Enter replaced a model of the global config,
    for example a larger chat model, with the recommended one.  Not the
    chat model of an API profile: Ollama does not serve it.

    The chat model goes to the Ollama profile of this machine
    (``_machine_profile_for``), never to a profile of one project.  Its
    other keys stay; the caller writes it.  The recommended model comes
    from the rule of two bands (``llm.recommend``): the measured speed of
    each installed model of the list, after a question, else an estimate
    from the memory of the machine; the table shows each model.  *cloud*
    names the cloud models of the local Ollama, which the list leaves
    out: they do not run on this machine.  The embedding model has its
    own list (``cli._llm_lists.choose_embeddings``).

    Returns None when the user stops at the question about the Ollama
    profile of this machine (``_machine_profile_for``).
    """
    from ..config.settings import _DEFAULT_OLLAMA_URL, _is_loopback_url
    from ..llm.auto_model import _gpu_available
    from ..llm.chat_router import chat_ollama_server
    from ..llm.ollama import check_setup

    gpu = _gpu_available()
    chat_server = chat_ollama_server(cfg.llm) if cfg.llm.provider == "ollama" else None
    ollama_profile = (
        bool(cfg.llm.profile) and chat_server is not None and _is_loopback_url(chat_server)
        and cfg.llm.model not in (cloud or {})
    )
    server = chat_server if ollama_profile and chat_server else _DEFAULT_OLLAMA_URL

    result = check_setup(cfg.llm)
    # `ollama_running`, not the status: "ok" also comes for a setup that
    # needs no Ollama (chat on an API, local embeddings), and it then said
    # "Ollama running" for a server that does not run.
    if not result.get("ollama_running"):
        print(f"  [warn] Ollama cannot be used: {result.get('message') or 'no Ollama server at ' + server}")
    else:
        print(f"  [ok] Ollama running at {server}")
        installed = [model for model in result.get("installed_models") or [] if model not in (cloud or {})]
        if installed:
            print(f"  Installed models: {', '.join(installed)}")
        # "error" with a running server: the Ollama server of chat_api_base
        # gives no model list.  Its message also names a missing model.
        if result.get("status") in ("model_missing", "error") and result.get("message"):
            print(f"  [info] {result['message']}")

    print(f"  Hardware: {'GPU detected' if gpu else 'CPU only (no GPU detected)'}")
    from ._recommend import table_lines

    chat_from_config = ollama_profile and bool(cfg.llm.model)
    found = _recommendations(server, server, ask_embed=False, keep_alive=cfg.llm.keep_alive,
                             timeout=cfg.llm.timeout)
    chat_recommended = found["chat"].model.tag if found["chat"].model else _FALLBACK_CHAT_MODEL
    chat_default = cfg.llm.model if chat_from_config else chat_recommended
    print("\n".join(table_lines(found["chat"], chat_default if chat_from_config else None)))
    fallback = "" if found["chat"].model else " (no recommendation: a small model that runs on each machine)"
    print(f"  Recommended: {chat_recommended}{fallback}")
    if chat_default != chat_recommended:
        print(f"  Current config: {chat_default} — Enter keeps it")

    chat = prompt_text("Chat model", default=chat_default) or chat_default
    found_profile = _machine_profile_for(server, chat)
    if found_profile is None:
        return None
    name, values = found_profile
    chat = str(values["model"])

    from ..deps._fixes import _ollama_pull

    if _confirm(f"Pull {chat} now? (multi-GB)"):
        ok, msg = _ollama_pull(chat, server)
        print(f"  [{'ok' if ok else 'warn'}] {msg}")
    return name, values


def _machine_profile_for(server: str, chat: str) -> tuple[str, dict[str, object]] | None:
    """Return the Ollama profile of this machine for the local Ollama setup, with the chat model *chat*.

    One Ollama profile for each machine (``llm.profile_state``): the setup
    writes the chat model into it, and never makes a profile for one
    project.  The profile is the Ollama profile of this machine when it
    is of the local Ollama, else ``local``.  When the Ollama profile of
    this machine is of another computer, the user makes ``local`` the new
    one, or stops.  The profile is shared: a new chat model in it is the
    chat model of each project, thus a "no" keeps its model.  A profile
    ``local`` that is not of the local Ollama (an API, another computer)
    is never changed: the setup takes a free name.  While a local Ollama
    gives no model list, only ``[llm] ollama_profile`` names the profile
    of this machine: a cloud model would look like a local one.

    Returns None when the user stops.
    """
    from ..config import settings
    from ..config.settings import _DEFAULT_OLLAMA_URL, _is_loopback_url, read_config_file
    from ..llm.profile_state import cloud_lists, is_cloud_model, known, ollama_profile_name, unknown_servers

    llm = read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm") or {}
    raw = llm.get("profiles")
    profiles: dict = raw if isinstance(raw, dict) else {}
    lists = cloud_lists(profiles)
    cloud = known(lists)
    if unknown_servers(profiles, lists):
        named = llm.get("ollama_profile")
        machine = named if isinstance(named, str) and isinstance(profiles.get(named), dict) else None
    else:
        machine = ollama_profile_name(llm, cloud)

    def own(profile: object) -> bool:
        # A cloud model is not of the local Ollama: before, a profile
        # "local" of a cloud model became the Ollama profile of this
        # machine, and its code went to the cloud.  Without a model list a
        # cloud model is not known; the test call of the setup then fails
        # (a cloud model runs through the local Ollama), and the global
        # config gets its earlier text back.
        return (isinstance(profile, dict) and profile.get("provider") == "ollama"
                and _is_loopback_url(str(profile.get("url") or _DEFAULT_OLLAMA_URL))
                and not is_cloud_model(profile, cloud))

    machine_url = str((profiles.get(machine) or {}).get("url") or _DEFAULT_OLLAMA_URL) if machine else ""
    if machine and _is_loopback_url(machine_url):
        name = machine
    else:
        name, number = "local", 1
        while name in profiles and not own(profiles[name]):
            number += 1
            name = f"local-{number}"
        if machine and not _confirm(
            f"The Ollama profile of this machine is {machine!r} ({machine_url}). Make {name!r} (the local Ollama) "
            "the Ollama profile of each project?", default=True,
        ):
            print("  [skip] The Ollama profile of this machine is not changed: 'fw-context llm default <profile> "
                  "--ollama' changes it")
            return None
    earlier = profiles.get(name)
    keep: dict = earlier if own(earlier) and isinstance(earlier, dict) else {"url": server}
    values: dict[str, object] = {**keep, "provider": "ollama", "model": chat}
    from ..config._profiles import replaces_other_values

    if replaces_other_values(name, values) and not _confirm(
        f"The profile {name!r} is shared: each project that uses Ollama gets the chat model {chat}. Change it?",
        default=False,
    ):
        values["model"] = keep.get("model") or chat
        print(f"  [info] The profile {name!r} keeps the chat model {values['model']}")
    return name, values


def make_machine_profile(name: str) -> None:
    """Make *name* the Ollama profile of this machine after the local Ollama setup wrote it, when it is not yet.

    The key ``[llm] ollama_profile`` is written only when the rule without
    it gives another profile (``ollama_profile_name``): a global config
    with one Ollama profile needs no key.
    """
    from ..config import settings
    from ..config._profiles import select_global_profile
    from ..llm.profile_state import cloud_lists, known, ollama_profile_name, unknown_servers

    try:
        llm = settings.read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm") or {}
        raw = llm.get("profiles")
        profiles: dict = raw if isinstance(raw, dict) else {}
        lists = cloud_lists(profiles)
        # Without a list, the rule can give a cloud model: the key then
        # names the profile without a guess.
        if not unknown_servers(profiles, lists) and ollama_profile_name(llm, known(lists)) == name:
            return
        if llm.get("ollama_profile") == name:
            return
        select_global_profile(None, ollama_profile=name)
    except (OSError, settings.ConfigError) as exc:
        print(f"  [warn] {name!r} is not the Ollama profile of this machine ({exc}): run "
              f"'fw-context llm default {name} --ollama'")
        return
    print(f"  [ok] Profile {name!r} is the Ollama profile of this machine: each project that uses Ollama uses it")


#: The models when the rule has no candidate: the memory of the machine is
#: not known (Windows gives no RAM), and no installed model of the list is
#: measured.  A small model runs on each machine; the user selects another.
_FALLBACK_CHAT_MODEL = "qwen2.5-coder:7b"
#: 4b and not the automatic 8b: the same quality, less VRAM and smaller
#: vectors (measured 2026-10-21).  On the CPU, 0.6b: 4b was 4 times slower
#: on the GPU of the measurement (22 against 92 symbols/s).  The fallback
#: only: with a known memory, the rule of two bands decides, and it puts
#: the quality first (decision of the user, 2026-10-21).
_FALLBACK_EMBED_MODEL = "qwen3-embedding:4b"
_FALLBACK_CPU_EMBED_MODEL = "qwen3-embedding:0.6b"


def _recommendations(chat_server: str, embed_server: str, *, ask_embed: bool, keep_alive: str,
                     timeout: float, ask_chat: bool = True) -> dict:
    """Return the recommendation of each role (``llm.recommend.gather``); measure the installed models after a question.

    WHY a question: each installed model of the list without a stored
    speed loads once (5 to 70 s each), and the setup of a machine with
    many models then takes minutes.  Without the measurement, the
    estimate from the memory of the machine applies to them too.
    *ask_chat* False: the embeddings only (the list of the embeddings).
    """
    from ..config.settings import _is_loopback_url
    from ..llm.hardware import machine_memory
    from ..llm.recommend import CHAT, EMBEDDING, gather

    memory = machine_memory()
    roles = ([(CHAT, chat_server)] if ask_chat else []) + ([(EMBEDDING, embed_server)] if ask_embed else [])

    def run(measure: bool) -> dict:
        return {role.name: gather(role, server, memory if _is_loopback_url(server) else None, measure=measure,
                                  keep_alive=keep_alive, timeout=timeout,
                                  progress=lambda text: print(f"  [measure] {text}", flush=True))
                for role, server in roles}

    found = run(False)
    waiting = [c.tag for recommendation in found.values() for c in recommendation.candidates if c.can_measure]
    if waiting and _confirm(f"Measure the speed of {len(waiting)} installed models now? Each one loads one time "
                            "(5-70 s each); the recommendation then compares the measured speeds", default=True):
        found = run(True)
    return found


def _read_secret(prompt: str) -> str:
    """Read a secret from the terminal without an echo (a seam for the tests)."""
    import getpass

    return getpass.getpass(prompt)


def _store_api_key(profile: str) -> bool:
    """Ask for the API key of *profile* and store it; return False when the setup must stop.

    An empty answer stores nothing: a server without a key (a proxy on the
    local network) is valid.  The messages never hold the key.
    """
    from ..llm.secrets import SecretStoreError, secrets_path, set_api_key

    while True:
        value = _read_secret("  API key (no echo; empty: no key) > ").strip()
        if not value:
            return True
        try:
            place = set_api_key(profile, value)
            break
        except ValueError as exc:
            # A space, a control character, a character that is not ASCII:
            # the message holds no key.  Before, the error stopped init.
            print(f"  [warn] {exc}: give the key again")
        except SecretStoreError as exc:
            print(f"  [warn] {exc}. Cloud API setup stopped")
            return False
    where = "the keyring of the system" if place == "keyring" else f"{secrets_path()} (no keyring can be used)"
    print(f"  [ok] The API key is stored in {where}")
    return True


def _custom_api_profile() -> str | None:
    """Write a profile of another OpenAI-compatible API to the global config (the setup of the LLM services).

    Returns its name, or None when nothing is written.
    """
    from ..config import settings
    from ..config._profiles import set_profile

    found = _custom_api_values()
    if found is None:
        return None
    name, values = found
    try:
        set_profile(name, values)
    except (ValueError, OSError, settings.ConfigError) as exc:
        print(f"  [warn] The profile {name!r} is not written: {exc}")
        return None
    print(f"  [ok] Profile {name!r} saved to {settings._GLOBAL_CONFIG_PATH}")
    return name


def _custom_api_values(*, model_prompt: str = "Model", model_default: str = "deepseek-chat",
                       ) -> tuple[str, dict[str, object]] | None:
    """Ask for a profile of provider openai_compatible (name, URL, key, model); store its key; return (name, values).

    *model_prompt* and *model_default*: the question of the model, for a
    chat model or for an embedding model (the list of the embeddings).

    The profile never gets the API key: a key in a TOML file is a secret in
    plain text.  The profile gets the NAME of the environment variable of
    the key, or, without one, the step reads the key without an echo and
    stores it for the profile (``llm.secrets``: the keyring, else
    ``secrets.toml``).
    """
    from ..config._profiles import replaces_other_values
    from ..config.settings import _is_loopback_url, url_problem

    name = prompt_text("Profile name", default="cloud") or "cloud"
    url = prompt_text("API URL") or ""
    if not url:
        print("  [skip] Cloud API setup skipped")
        return None
    # Before `_is_loopback_url`: it raised ValueError for "http://[::1",
    # and init stopped with a traceback.
    problem = url_problem(url)
    if problem is not None:
        print(f"  [warn] {url} is not a valid URL ({problem}). Cloud API setup skipped")
        return None
    key_env = prompt_text("Environment variable with the API key (empty: store the key in the keyring)") or ""
    model = prompt_text(model_prompt, default=model_default) or model_default

    if not _is_loopback_url(url):
        print(f"  ⚠ Source code will be sent to {url}.")
        print("    Ensure this complies with your data security policy.")

    values: dict[str, object] = {"provider": "openai_compatible", "url": url, "model": model, "api_key_env": key_env or None}
    # Each project that selects the profile uses it: a replaced "local"
    # profile sent the source code of each project to this API.
    while replaces_other_values(name, values) and not _confirm(
        f"The profile {name!r} exists, and each project that selects it uses it. Replace it?", default=False,
    ):
        name = prompt_text("Another profile name (empty: skip)") or ""
        if not name:
            print("  [skip] Cloud API setup skipped")
            return None
    # The key before the test call: the call needs it.
    if not key_env and not _store_api_key(name):
        return None
    return name, values


# ── Config-first build resolution ───────────────────────────────────────


def resolve_build_system(
    project_root: Path,
    cfg,
    *,
    non_interactive: bool,
    dry_run: bool,
) -> str | None:
    """Determine the build system, config-first.  Returns the system key or None.

    Reads ``cfg.build.system`` first (already configured → shows
    "already configured" + ``Change? [y/N]``).  Falls back to marker-based
    detection; when that also fails, prompts interactively and writes the
    answer to ``config.toml``.  In non-interactive mode, a failed detection
    returns ``None`` (falls to the checklist).
    """
    from ..indexer.build import detect_build_system
    from ._init_build import _write_build_config_key

    system = cfg.build.system
    if system:
        if dry_run:
            print(f"  Build system: {system} (already configured)")
        elif confirm_change("Build system", system, non_interactive=non_interactive):
            system = prompt_build_system(non_interactive, current=system) or system
            if system and system != cfg.build.system:
                _write_build_config_key(project_root, "system", system)
        return system

    detected = detect_build_system(project_root)
    if detected:
        print(f"  Build system: {detected} (auto-detected)")
        if not dry_run:
            _write_build_config_key(project_root, "system", detected)
        return detected

    print("  Build system: not detected")
    print("  No standard project markers found (.mbed, platformio.ini, CMakeLists.txt, ...)")
    system = prompt_build_system(non_interactive)
    if system and not dry_run:
        _write_build_config_key(project_root, "system", system)
        print(f"  [ok] Build system: {system}")
        print("  [ok] Saved to .fw-context/config.toml")
    return system


def resolve_build_params(
    project_root: Path,
    cfg,
    system: str | None,
    *,
    non_interactive: bool,
    dry_run: bool,
) -> None:
    """Prompt for system-specific required parameters (config-first).

    Only runs for the detected/configured *system*.  Missing required
    values are prompted (and written to ``config.toml``); already-set
    values show "already configured" with a ``Change? [y/N]`` prompt.
    """
    if not system:
        return
    from ._init_build import _write_build_config_key

    b = cfg.build
    if b.variants:
        # Multi-variant: board/target/fqbn/etc. live per-variant in
        # [[build.variants]] — the top-level singleton is not prompted.
        print("  Build variants configured — per-variant board/target lives in [[build.variants]]")
        return

    def _ensure(key: str, label: str, current: str | None) -> None:
        if current:
            if dry_run:
                print(f"  {label}: {current} (already configured)")
            elif confirm_change(label, current, non_interactive=non_interactive):
                val = prompt_text(label, default=current, non_interactive=non_interactive)
                if val and val != current:
                    _write_build_config_key(project_root, key, val)
            return
        val = prompt_text(label, non_interactive=non_interactive)
        if val:
            if not dry_run:
                _write_build_config_key(project_root, key, val)
            print(f"  [ok] {label}: {val}")

    if system == "zephyr":
        _ensure("board", "Board", b.board)
    elif system == "arduino":
        _ensure("fqbn", "Board FQBN", b.fqbn)
    elif system == "keil-mdk":
        _ensure("keil_project", "Path to .uvprojx", b.keil_project)
    elif system == "iar-ewarm":
        _ensure("iar_project", "Path to .ewp", b.iar_project)
    elif system == "bare":
        cur = ",".join(b.source_dirs) if b.source_dirs else None
        if cur:
            if dry_run:
                print(f"  Source dirs: {cur} (already configured)")
            elif confirm_change("Source dirs", cur, non_interactive=non_interactive):
                val = prompt_text("Source dirs (comma-separated)", non_interactive=non_interactive)
                if val:
                    _write_build_config_key(project_root, "source_dirs", [s.strip() for s in val.split(",") if s.strip()])
            return
        val = prompt_text("Source dirs (comma-separated)", non_interactive=non_interactive)
        if val:
            if not dry_run:
                _write_build_config_key(project_root, "source_dirs", [s.strip() for s in val.split(",") if s.strip()])
            print(f"  [ok] Source dirs: {val}")
    elif system == "mbed-os":
        if b.target:
            if dry_run:
                print(f"  Target: {b.target} (already configured)")
            elif confirm_change("Target", b.target, non_interactive=non_interactive):
                val = prompt_text("Target", default=b.target, non_interactive=non_interactive)
                if val and val != b.target:
                    _write_build_config_key(project_root, "target", val)
        else:
            try:
                from ..indexer.build import _parse_mbed_dotfile
                dotfile = _parse_mbed_dotfile(project_root)
            except Exception:
                dotfile = {}
            if dotfile.get("TARGET"):
                print(f"  Target: {dotfile['TARGET']} (from .mbed)")
            else:
                val = prompt_text("Target", non_interactive=non_interactive)
                if val:
                    if not dry_run:
                        _write_build_config_key(project_root, "target", val)
                    print(f"  [ok] Target: {val}")


def resolve_build_env(
    project_root: Path,
    cfg,
    system: str | None,
    *,
    non_interactive: bool,
    dry_run: bool,
) -> None:
    """Auto-detect or prompt for the build environment (config-first).

    Reads ``local.toml [build] python``/``activate``/``idf_path`` first —
    already-configured values are shown and left untouched (idempotent).
    Only when the key is missing is ``detect_environment`` run (or the user
    prompted).  Machine-specific → written to ``local.toml``.
    """
    b = cfg.build
    if b.python or b.activate or b.idf_path:
        if b.python:
            print(f"  Build python: {b.python} (already configured)")
        if b.idf_path:
            print(f"  Build idf_path: {b.idf_path} (already configured)")
        if b.activate and system == "zephyr":
            # Show what is configured — a hand-written script included — as
            # the standing choice, with the installed SDKs behind it so a
            # re-run can switch.  init never asks for a script path: a custom
            # one is set by hand in the config, and this only offers to move
            # off it.
            chosen = _configure_ncs_version(
                project_root, dry_run=dry_run, non_interactive=non_interactive,
                current=b.activate,
            )
            if chosen is not None and chosen != b.activate and not dry_run:
                from ._init import _write_build_key

                _write_build_key(project_root, "activate", chosen)
                print(f"  [ok] Build activate: {chosen}")
            return
        if b.activate:
            print(f"  Build activate: {b.activate} (already configured)")
        return

    if not system:
        return

    from ..indexer.builders import registry as _bregistry
    from ._init import _write_build_key

    builder_cls = _bregistry.get(system)
    detected: dict[str, str | None] = {"python": None, "activate": None}

    # Zephyr/NCS: the SDK version is a choice, not a detectable fact — see
    # prompt_ncs_version.  Ask before falling back to detect_environment,
    # which can only guess when several SDKs are installed.
    if system == "zephyr":
        chosen = _configure_ncs_version(
            project_root, dry_run=dry_run, non_interactive=non_interactive
        )
        if chosen is not None:
            detected["activate"] = chosen

    if detected["activate"] is None and builder_cls and hasattr(builder_cls, "detect_environment"):
        try:
            detected = builder_cls.detect_environment(project_root)
        except Exception:
            detected = {"python": None, "activate": None}

    detected_python = detected.get("python")
    if detected_python:
        if not dry_run:
            _write_build_key(project_root, "python", detected_python)
        print(f"  [ok] Build python: {detected_python} (auto-detected)")
    detected_activate = detected.get("activate")
    if detected_activate:
        if not dry_run:
            _write_build_key(project_root, "activate", detected_activate)
        print(f"  [ok] Build activate: {detected_activate} (auto-detected)")

    if system == "esp-idf" and not detected.get("activate"):
        val = prompt_text(
            "Path to ESP-IDF (directory containing export.sh) (or Enter to skip)",
            non_interactive=non_interactive,
        )
        if val:
            idf_dir = Path(val).expanduser()
            export_sh = idf_dir / "export.sh"
            if not dry_run:
                # idf_path sets IDF_PATH for the build; activate sources the
                # export.sh so idf.py/ninja/toolchain are on PATH.  Both are
                # machine-specific and belong in local.toml.
                _write_build_key(project_root, "idf_path", str(idf_dir))
                if export_sh.exists():
                    _write_build_key(project_root, "activate", str(export_sh))
                    detected["activate"] = str(export_sh)
                else:
                    detected["activate"] = val
            print(f"  [ok] Build idf_path: {idf_dir}")
            if export_sh.exists():
                print(f"  [ok] Build activate: {export_sh}")

    if not detected.get("python") and not detected.get("activate") and system in ("zephyr", "esp-idf"):
        print("  SDK environment script not found.")
        val = prompt_text(
            "Path to your shell init script for the SDK environment (or Enter to skip)",
            non_interactive=non_interactive,
        )
        if val:
            if not dry_run:
                _write_build_key(project_root, "activate", val)
            print(f"  [ok] Build activate: {val}")
