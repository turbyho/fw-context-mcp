"""The LLM setup in two steps: the services that the user uses, then the service of the analysis and of the embeddings.

Decision of the user, 2026-10-22.  Step 1 (``setup_services``): fw-context
detects the local Ollama, but not an Ollama on another computer nor a cloud
service that the user pays for: the user selects and sets up those.  Step
2 (``choose_chat``, ``choose_embeddings``): two independent lists of the
services that are set up, the local Ollama first.  The chat can thus run at
one provider and the embeddings at another.  A cloud service is a
first-class choice: on a machine without a GPU, or with little memory, it
is the only way.  Each row tells the recommended model and where the code
goes.

After a choice in a project, the user selects the scope: this project
(``local.toml``) or each project (the global default).  An Ollama profile
of the chat is also the Ollama profile of this machine
(``llm.profile_state``).  Enter keeps the current setting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import _init_interactive as ui
from . import _llm_wizard as wizard

#: The providers of a profile that can make the embeddings
#: (``config.settings._EMBED_PROVIDERS``).
_EMBED_PROVIDERS = ("ollama", "openrouter", "openai_compatible")


@dataclass(frozen=True)
class Row:
    """One row of a list: a profile, or the local Ollama that has no profile yet (*profile* "")."""

    profile: str
    label: str
    hint: str
    #: Why the profile cannot run (no key, no sign-in); a row with one is not selectable.
    problem: str = ""


def _global_llm() -> dict:
    from ..config import settings

    try:
        return settings.read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm") or {}
    except settings.ConfigError as exc:
        print(f"  [warn] {exc}")
        return {}


def _project_llm(project_root: Path | None, file_name: str) -> dict:
    """Return ``[llm]`` of a file of the project (``config.toml`` or ``local.toml``), read from the file; {}."""
    from ..config import settings

    if project_root is None:
        return {}
    path = project_root / ".fw-context" / file_name
    try:
        return (settings.read_config_file(path).get("llm") or {}) if path.is_file() else {}
    except settings.ConfigError:
        return {}


def _in_project(project_root: Path | None) -> bool:
    return project_root is not None and (project_root / ".fw-context").is_dir()


def global_offers(project_root: Path | None) -> tuple[bool, bool]:
    """Return whether the chat list and the embeddings list give option 0, "the global settings".

    Option 0 removes keys of ``local.toml``.  It is given when
    ``local.toml`` selects the chat (``profile``, or ``enabled`` of an
    older release) or the embeddings (``embed_profile``, ``embed_model``),
    the global config sets a backend key, and ``config.toml`` sets none:
    init writes only ``local.toml``, and a committed setting applies to
    each user.
    """
    from ..config import settings

    if project_root is None:
        return False, False
    # The files themselves, not ``llm_key_sources``: that names only the
    # file that wins for each key, thus a local.toml that sets each key of
    # the global config hid the global settings, and option 0 was gone.
    keys = set(settings.LLM_BACKEND_KEYS)
    if not keys & _global_llm().keys() or keys & _project_llm(project_root, "config.toml").keys():
        return False, False
    local = _project_llm(project_root, "local.toml").keys()
    return bool(local & {"profile", "enabled"}), bool(local & {"embed_profile", "embed_model"})


def _restore_global(previous: str | None) -> None:
    """Put the earlier text *previous* of the global config back, after a step that failed after its write."""
    from ..config import settings
    from ..config._toml_editor import _atomic_write

    path = settings._GLOBAL_CONFIG_PATH
    if previous is None:
        print(f"  [warn] The earlier global config cannot be put back: read {path} before the next command")
        return
    try:
        _atomic_write(path.resolve(), previous)
    except OSError as exc:
        print(f"  [warn] The global config is changed in part ({exc}): read {path} before the next command")


def _ask_scope(project_root: Path | None, what: str) -> str:
    """Return ``"project"`` or ``"global"``: where the choice of *what* goes; without a project, global."""
    if not _in_project(project_root):
        return "global"
    answer = ui._input(f"  Use it for this project only (1, .fw-context/local.toml) or for each project "
                       f"(2, the global default) — {what}? [1] > ").strip()
    return "global" if answer == "2" else "project"


def _write_local(project_root: Path, updates: dict[str, object], clear: list[str]) -> bool:
    """Write *updates* and remove *clear* in ``[llm]`` of ``local.toml``; check the result; False when not done.

    WHY the check: the line editor finds only the line ``[llm]``.  For
    another form of the table (``[llm]  # note``, an inline table) it
    changed nothing, and the step said "[ok]".  The file then gets its
    earlier text back, and the step names what to change by hand.
    """
    from ..config import settings
    from ..config._toml_editor import _atomic_write

    path = project_root / ".fw-context" / "local.toml"
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    try:
        settings._update_local_toml(project_root, updates, clear_keys=clear)
    except (OSError, settings.ConfigError) as exc:
        print(f"  [warn] .fw-context/local.toml cannot be changed ({exc}). Nothing was changed.")
        return False
    llm = _project_llm(project_root, "local.toml")
    wrong = [key for key in clear if key in llm] + [key for key, value in updates.items() if llm.get(key) != value]
    if not wrong:
        return True
    try:
        if previous is not None:
            _atomic_write(path.resolve(), previous)
    except OSError as exc:
        print(f"  [warn] fw-context cannot change {', '.join(wrong)} of .fw-context/local.toml, and cannot write "
              f"its old text back ({exc}). Read the file before the next command.")
        return False
    print(f"  [warn] fw-context cannot change {', '.join(wrong)} of .fw-context/local.toml: its [llm] table has a "
          "form that fw-context does not edit. Nothing was changed. Change the keys yourself.")
    return False


def _local_ollama_runs() -> bool:
    from ..llm._diag import installed_models

    return installed_models(wizard.local_url()) is not None


def _is_local_ollama(profile: Mapping[str, object], cloud: Mapping[str, Mapping[str, str]]) -> bool:
    from ..config.settings import _DEFAULT_OLLAMA_URL, _is_loopback_url
    from ..llm.profile_state import is_cloud_model

    return (profile.get("provider") == "ollama" and not is_cloud_model(profile, cloud)
            and _is_loopback_url(str(profile.get("url") or _DEFAULT_OLLAMA_URL)))


def _host(profile: Mapping[str, object], cloud: Mapping[str, Mapping[str, str]], name: str) -> str:
    """Return where the code of *profile* goes, or "" for this machine."""
    from ..config.settings import _is_loopback_url
    from ..llm.profile_state import profile_state

    host = profile_state(name, profile, cloud=cloud).host
    return "" if _is_loopback_url(host) else host


# ── Step 1: the services ──


def setup_services(project_root: Path | None, cfg) -> None:
    """Show the services, the local Ollama as detected; set up the services that the user selects.

    The setups and the test of each new profile are the ones of the
    service list of ``cli._llm_wizard``.  Enter sets up nothing: the
    services with a profile (✓) stay as they are.
    """
    from ..llm.profile_state import cloud_hosts

    profiles = wizard._profiles()
    cloud = cloud_hosts(profiles)
    print("\n  Step 1 — the services that you use (the local Ollama is detected; the others you select):")
    wizard._print_services(profiles, cloud)
    if _local_ollama_runs() and not any(_is_local_ollama(p, cloud) for p in profiles.values()):
        print("    (the local Ollama runs: step 2 offers it, also without a profile)")
    answer = ui._input("  Set up services: numbers, separated by commas (Enter: none) > ")
    for service in wizard._selected(answer):
        before = wizard._global_text()
        name = wizard._SETUPS[service.key](project_root, cfg, cloud.get(wizard.local_url().rstrip("/"), {}))
        if name:
            wizard._test_new_profile(cfg, name, before)


# ── Step 2: the LLM analysis ──


def _chat_rows(profiles: dict[str, dict], cloud: Mapping[str, Mapping[str, str]]) -> list[Row]:
    """Return a row for each profile, and one for the local Ollama when it runs without a profile."""
    from ..llm.hardware import machine_memory
    from ..llm.profile_state import profile_state
    from ..llm.recommend import CHAT, _number, gather, list_entries, price, quality, rate

    local = None
    if _local_ollama_runs():
        local = gather(CHAT, wizard.local_url(), machine_memory(), measure=False)
    entries, _note = list_entries(CHAT)
    rows = []
    for name, profile in profiles.items():
        model = str(profile.get("model") or "")
        host = _host(profile, cloud, name)
        info = ""
        candidate = local.candidate(model) if local is not None and _is_local_ollama(profile, cloud) else None
        if candidate is not None:
            info = f"  {quality(candidate, CHAT)}  {rate(candidate, CHAT)}"
        elif model in entries and entries[model].get("remote") is True:
            # [llm.models] is a file of the user: a grade that is not a
            # number shows no grade, and no traceback.
            entry = entries[model]
            grade = _number(entry.get("quality"))
            info = f"  {f'{grade:.2f}' if grade is not None else '-'}  {price(entry, CHAT)}".rstrip()
        problem = profile_state(name, profile, cloud=cloud).problem or ""
        if problem:
            # It cannot run yet: the row says what repairs it.
            info += f"  ({problem})"
        rows.append(Row(name, f"{name}: {profile.get('provider')} {model}{f'  ⚠ {host}' if host else ''}",
                        info, problem))
    if local is not None and not any(_is_local_ollama(p, cloud) for p in profiles.values()):
        hint = f"  recommended {local.model.tag}  {quality(local.model, CHAT)}  {rate(local.model, CHAT)}" if (
            local.model is not None) else ""
        rows.insert(0, Row("", "Local Ollama (detected, no profile yet: set it up now)", hint))
    rows.sort(key=lambda row: row.profile != "" and "⚠" in row.label)
    return rows


def choose_chat(project_root: Path | None, cfg, *, offer_global: bool = False) -> None:
    """Ask which service runs the LLM analysis, of the services that are set up; write the selection.

    *offer_global*: ``local.toml`` selects a profile, and option 0 removes
    that selection (the global settings then apply).
    """
    from ..config import settings
    from ..llm.profile_state import cloud_lists, known

    profiles = wizard._profiles()
    cloud = known(cloud_lists(profiles))
    rows = _chat_rows(profiles, cloud)
    current = cfg.llm.profile or cfg.llm.model
    print(f"\n  Step 2 — the LLM analysis: where it runs (Enter: keep {current or 'the current setting'}):")
    if offer_global:
        print("    0. the global settings (removes the profile of .fw-context/local.toml)")
    for number, row in enumerate(rows, 1):
        print(f"    {number}. {row.label}{row.hint}")
    if not rows:
        print("    (no service: set one up in step 1)")
    print("    d. no LLM analysis (the embeddings stay)")
    answer = ui._input("  > ").strip().lower()
    if answer == "":
        print("  [ok] LLM analysis: the current setting stays")
        return
    if answer == "d":
        if project_root is None:
            print("  [skip] No project: the LLM analysis is a setting of a project")
            return
        ui._disable_llm_analysis(project_root, cfg)
        return
    if answer == "0" and offer_global and project_root is not None:
        # ``enabled = false`` of an older release too: else the analysis
        # stays off, and the step said that the global settings apply.
        # ``analyze_symbols`` stays: it selects no backend (option "d").
        if _write_local(project_root, {}, ["profile", "enabled"]):
            print("  [ok] LLM analysis: the global settings apply (removed the profile of .fw-context/local.toml)")
        return
    if not answer.isdigit() or not 1 <= int(answer) <= len(rows):
        print("  [skip] The LLM analysis is not changed")
        return
    row = rows[int(answer) - 1]
    if row.problem:
        print(f"  [skip] The profile {row.profile!r} cannot run: {row.problem}")
        return
    if row.profile:
        # The context of an API model must be known before the profile
        # analyses (decision of the user, 2026-10-22); a GET of the list.
        try:
            problem = wizard.context_problem(settings.profile_llm_config(cfg.llm, row.profile))
        except settings.ConfigError as exc:
            problem = str(exc)
        if problem is not None:
            print(f"  [skip] The profile {row.profile!r} cannot analyse: {problem}")
            return
    name = row.profile
    if not name:
        # The local Ollama without a profile: set it up now.
        before = wizard._global_text()
        written = wizard._SETUPS["local"](project_root, cfg, dict(cloud.get(wizard.local_url().rstrip("/"), {})))
        if written is None or not wizard._test_new_profile(cfg, written, before):
            return
        name = written
    _select_chat(project_root, name)


def _select_chat(project_root: Path | None, name: str) -> None:
    """Make *name* the chat of this project or of each project; an Ollama profile also of this machine."""
    from ..config import settings
    from ..config._profiles import select_global_profile
    from ..llm.configure import configure_llm_core
    from ..llm.profile_state import cloud_lists, is_ollama, known, ollama_profile_name, undecided

    llm = _global_llm()
    profiles = wizard._profiles()
    lists = cloud_lists(profiles)
    # Without a model list, a cloud model looks like a model of this
    # machine: it would become the Ollama profile of this machine.
    problem = undecided(name, profiles, lists, machine_too=False)
    if problem is not None:
        print(f"  [skip] {problem}")
        return
    cloud = known(lists)
    ollama = is_ollama(profiles.get(name) or {}, cloud)
    machine = ollama_profile_name(llm, cloud)
    if ollama and machine not in (None, name) and not ui._confirm(
        f"Make {name!r} the Ollama profile of this machine (each project that uses Ollama)? Now it is "
        f"{machine!r}", default=True,
    ):
        print("  [skip] The Ollama profile of this machine stays; the LLM analysis is not changed")
        return
    scope = _ask_scope(project_root, f"the profile {name!r}")
    if scope == "global" and not ollama and "ollama_profile" not in llm:
        # The key keeps the Ollama profile of this machine: the rule
        # without it needs the model lists.
        problem = undecided(name, profiles, lists, machine_too=True)
        if problem is not None:
            print(f"  [skip] {problem}")
            return
    new_machine = ollama and machine != name
    # For this project, the Ollama profile of this machine changes before
    # the test (``load`` refuses an Ollama profile of a project that is
    # not the one of this machine): a failed test puts this text back.
    before = wizard._global_text()
    try:
        if scope == "global":
            # An external default keeps the Ollama profile of this machine
            # as its key (as fw-context llm default --global).
            keep = machine if not ollama and machine and "ollama_profile" not in llm else None
            select_global_profile(name, ollama_profile=name if ollama else keep)
            print(f"  [ok] Profile {name!r} is the profile of each project that selects none")
        elif new_machine:
            select_global_profile(None, ollama_profile=name)
    except (OSError, settings.ConfigError) as exc:
        print(f"  [warn] The global config is not changed: {exc}")
        return
    if new_machine and scope == "global":
        print(f"  [ok] Profile {name!r} is the Ollama profile of this machine")
    if project_root is None or not _in_project(project_root):
        return
    if scope == "project":
        # A test request, and the notice when the code goes out: the core
        # puts the old local.toml back when the profile does not answer.
        result = configure_llm_core(project_root, {"enabled": True, "profile": name})
        if result.get("status") != "ok":
            if new_machine:
                _restore_global(before)
            print(f"  [warn] {result.get('message', 'the test of the profile failed')}")
            return
        if new_machine:
            print(f"  [ok] Profile {name!r} is the Ollama profile of this machine")
        print(f"  [ok] .fw-context/local.toml selects profile {name!r}")
        if result.get("compliance_warning"):
            print(f"  ⚠ {result['compliance_warning']}")
    else:
        # The key of local.toml would win over the new global default.
        local = _project_llm(project_root, "local.toml").get("profile")
        committed = _project_llm(project_root, "config.toml").get("profile")
        if local and committed in (None, name):
            _write_local(project_root, {}, ["profile"])
        elif local or committed:
            print(f"  [note] .fw-context/{'local.toml' if local else 'config.toml'} of this project selects "
                  f"{local or committed!r}: it stays for this project")
    wizard._analysis_on_again(project_root)


# ── Step 2: the embeddings ──


def _embed_rows(profiles: dict[str, dict], cloud: Mapping[str, Mapping[str, str]]) -> list[Row]:
    """Return a row for each profile that can make the embeddings, and the local Ollama without a profile."""
    from ..llm.hardware import machine_memory
    from ..llm.profile_state import is_cloud_model, profile_state
    from ..llm.recommend import EMBEDDING, gather, price, quality, rate, remote_models

    local = None
    if _local_ollama_runs():
        local = gather(EMBEDDING, wizard.local_url(), machine_memory(), measure=False)
    best_api = remote_models(EMBEDDING, "openrouter")
    rows = []
    for name, profile in profiles.items():
        provider = profile.get("provider")
        if provider not in _EMBED_PROVIDERS or is_cloud_model(profile, cloud):
            continue
        host = _host(profile, cloud, name)
        hint = ""
        if _is_local_ollama(profile, cloud) and local is not None and local.model is not None:
            hint = f"  recommended {local.model.tag}  {quality(local.model, EMBEDDING)}  {rate(local.model, EMBEDDING)}"
        elif provider == "openrouter" and best_api:
            tag, entry = best_api[0]
            hint = f"  recommended {tag}  {float(entry['quality']):.3f}  {price(entry, EMBEDDING)}".rstrip()
        # As in the chat list: a profile without its key cannot be
        # selected, else the index run fails at the embeddings.
        problem = profile_state(name, profile, cloud=cloud).problem or ""
        if problem:
            hint += f"  ({problem})"
        rows.append(Row(name, f"{name}: {provider}{f'  ⚠ {host}' if host else ''}", hint, problem))
    if local is not None and not any(_is_local_ollama(p, cloud) for p in profiles.values()):
        hint = f"  recommended {local.model.tag}  {quality(local.model, EMBEDDING)}" if local.model else ""
        rows.insert(0, Row("", "Local Ollama (detected)", hint))
    rows.sort(key=lambda row: row.profile != "" and "⚠" in row.label)
    return rows


def choose_embeddings(project_root: Path | None, cfg, *, offer_global: bool = False) -> None:
    """Ask which service makes the embeddings, of the services that are set up, and the model; write them.

    *offer_global*: ``local.toml`` sets ``embed_profile`` or
    ``embed_model``, and option 0 removes them.
    """
    from ..llm.profile_state import cloud_lists, known

    profiles = wizard._profiles()
    cloud = known(cloud_lists(profiles))
    rows = _embed_rows(profiles, cloud)
    current = cfg.llm.embed_model + (f" ({cfg.llm.embed_profile})" if cfg.llm.embed_profile else "")
    print(f"\n  Step 2 — the embeddings: where they run (Enter: keep {current or 'the current setting'}):")
    if offer_global:
        print("    0. the global settings (removes embed_profile and embed_model of .fw-context/local.toml)")
    for number, row in enumerate(rows, 1):
        print(f"    {number}. {row.label}{row.hint}")
    if not rows:
        print("    (no service that makes embeddings: set up Ollama, OpenRouter or another API in step 1)")
    answer = ui._input("  > ").strip()
    if answer == "":
        print("  [ok] Embeddings: the current setting stays")
        return
    if answer == "0" and offer_global and project_root is not None:
        if _write_local(project_root, {}, ["embed_profile", "embed_model"]):
            print("  [ok] Embeddings: the global settings apply")
            _note_new_embed_model(cfg.llm.embed_model, ui._reload(project_root, cfg).llm.embed_model)
        return
    if not answer.isdigit() or not 1 <= int(answer) <= len(rows):
        print("  [skip] The embeddings are not changed")
        return
    row = rows[int(answer) - 1]
    if row.problem:
        print(f"  [skip] The profile {row.profile!r} cannot run: {row.problem}")
        return
    name = row.profile
    if not name and cfg.llm.provider == "ollama" and _project_chat_is_remote(cfg):
        # Without embed_profile, the embeddings go to the server of an
        # Ollama chat profile: here another computer.
        print("  [warn] The chat runs on an Ollama of another computer, and no profile names the local Ollama: "
              "set up the local Ollama in step 1 first")
        return
    profile = profiles.get(name) or {"provider": "ollama"}
    if profile.get("provider") == "ollama":
        model = _ollama_embed_model(cfg, name, profile, cloud, project_root)
    elif profile.get("provider") == "openrouter":
        model = _openrouter_embed_model()
    else:
        model = _api_embed_model(name, profile)
    if model is None:
        return
    _select_embeddings(project_root, cfg, name, model)


def _note_new_embed_model(old: str, new: str) -> None:
    """Tell that the next index run embeds each symbol again, before the run and not after it."""
    if old and new and old != new:
        print(f"  [note] The embedding model changes from {old} to {new}: the next index run embeds each symbol "
              "again")


def _project_chat_is_remote(cfg) -> bool:
    from ..config.settings import _is_loopback_url

    return not _is_loopback_url(cfg.llm.ollama_url)


def _ollama_embed_model(cfg, name: str, profile: Mapping[str, object],
                        cloud: Mapping[str, Mapping[str, str]], project_root: Path | None) -> str | None:
    """Return the embedding model of an Ollama server: the local one with a table and a pull, another one of its list."""
    from ..config.settings import _DEFAULT_OLLAMA_URL
    from ..deps._fixes import _ollama_pull
    from ..llm._diag import installed_models
    from ..llm.embedder_factory import uses_ollama
    from ._recommend import table_lines, table_tags

    url = str(profile.get("url") or _DEFAULT_OLLAMA_URL)
    if not name or _is_local_ollama(profile, cloud):
        found = ui._recommendations(url, url, ask_embed=True, ask_chat=False,
                                    keep_alive=cfg.llm.keep_alive, timeout=cfg.llm.timeout)
        recommendation = found["embedding"]
        # The model of the config on this server is the default, thus Enter
        # keeps it; the table shows the recommendation next to it.  Only a
        # model that a config file sets: without one, ``load`` gives the
        # automatic CPU model, and that is no choice of the user.  Only an
        # Ollama model: a sentence-transformers name is no tag to pull.
        set_in_file = any("embed_model" in table for table in (
            _global_llm(), _project_llm(project_root, "config.toml"), _project_llm(project_root, "local.toml")))
        configured = (cfg.llm.embed_model if set_in_file and uses_ollama(cfg.llm)
                      and cfg.llm.ollama_url.rstrip("/") == url.rstrip("/") else "")
        print("\n".join(table_lines(recommendation, configured or None, numbered=True)))
        names = table_tags(recommendation)
        default = configured or (recommendation.model.tag if recommendation.model else ui._FALLBACK_EMBED_MODEL)
    else:
        names = installed_models(url) or []
        print(f"  Models of {url}:" if names else f"  Models of {url}: none (pull an embedding model there)")
        if names:
            print("\n".join(ui.numbered(names)))
        print(f"  ⚠ The source code and each search query go to {url}")
        default = names[0] if names else ""
    model = ui.prompt_model("Embedding model", names, default=default or None) or default
    if not model:
        print("  [skip] Embeddings: no model")
        return None
    if ui._confirm(f"Pull {model} now? (multi-GB)"):
        ok, msg = _ollama_pull(model, url)
        print(f"  [{'ok' if ok else 'warn'}] {msg}")
    return model


def _openrouter_embed_model() -> str | None:
    """Return an embedding model of OpenRouter: the list with its quality and price, the best one as the default."""
    from ..llm.recommend import EMBEDDING, price, remote_models

    models = remote_models(EMBEDDING, "openrouter")
    print("  Embedding models of OpenRouter (quality MRR@10 of the search, price for one million tokens):")
    from ._recommend import NUMBER_OR_NAME

    for number, (tag, entry) in enumerate(models, 1):
        print(f"    {number}. {tag:<40} {float(entry['quality']):.3f}  {price(entry, EMBEDDING)}".rstrip())
    if models:
        print(NUMBER_OR_NAME)
    print("  ⚠ The source code and each search query go to openrouter.ai (hosts with zero data retention)")
    default = models[0][0] if models else "mistralai/codestral-embed-2505"
    return ui.prompt_model("Embedding model", [tag for tag, _entry in models], default=default) or default


def _api_embed_model(name: str, profile: Mapping[str, object]) -> str | None:
    """Return the embedding model of another API; ask for the context of the model when the profile has none."""
    from ..config import settings
    from ..config._profiles import set_profile

    print(f"  ⚠ The source code and each search query go to {profile.get('url')}")
    model = ui.prompt_text("Embedding model of that API") or ""
    if not model:
        print("  [skip] Embeddings: no model")
        return None
    if not profile.get("embed_context"):
        context = ui.prompt_text("Tokens of one input of the model (embed_context)", default="8192") or "8192"
        if not context.isdigit() or int(context) <= 0:
            print(f"  [skip] {context!r} is not a number of tokens")
            return None
        try:
            set_profile(name, {**profile, "embed_context": int(context)})
        except (ValueError, OSError, settings.ConfigError) as exc:
            print(f"  [warn] The profile {name!r} is not written: {exc}")
            return None
    return model


def _select_embeddings(project_root: Path | None, cfg, embed_profile: str, model: str) -> None:
    """Write the server and the model of the embeddings, for this project or for each project."""
    from ..config import settings

    scope = _ask_scope(project_root, f"the embedding model {model}")
    if scope == "project" and project_root is not None:
        # ``embed_profile = ""`` and not a removal for the local Ollama: a
        # removed key lets the embed_profile of the global config or of
        # config.toml apply, and the model would go to that API.
        if not _write_local(project_root, {"embed_model": model, "embed_profile": embed_profile}, []):
            return
        print(f"  [ok] .fw-context/local.toml: embedding model {model}" + (
            f" of profile {embed_profile!r}" if embed_profile else ""))
    else:
        previous = wizard._global_text()
        try:
            settings.update_global_llm_key("embed_profile", embed_profile)
            settings.update_global_llm_key("embed_model", model)
        except (OSError, settings.ConfigError) as exc:
            # The two keys go together: a half change sends the model to
            # the server of the other key.
            _restore_global(previous)
            print(f"  [warn] The global config is not changed: {exc}")
            return
        print(f"  [ok] The embedding model of each project: {model}" + (
            f" of profile {embed_profile!r}" if embed_profile else ""))
        own = _project_llm(project_root, "local.toml").keys() & {"embed_profile", "embed_model"}
        if own and project_root is not None:
            if ui._confirm("This project sets its own embedding model in .fw-context/local.toml. Remove it, "
                           "thus the new global one applies? The next index run then embeds each symbol again",
                           default=False):
                _write_local(project_root, {}, sorted(own))
            else:
                print("  [note] .fw-context/local.toml of this project keeps its own embeddings")
    _note_new_embed_model(cfg.llm.embed_model, ui._reload(project_root, cfg).llm.embed_model if project_root
                          else model)
