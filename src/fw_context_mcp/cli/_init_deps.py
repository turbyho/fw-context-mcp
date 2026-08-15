"""Dependency audit + auto-fix + checklist helpers for ``fw-context init``.

Kept separate from ``_init.py`` so ``cmd_init`` stays a thin orchestrator
(cognitive-complexity budget).  Three responsibilities:

1. ``_run_deps_auto_fix`` — full check + auto-fix of pip packages.
2. ``_model_status`` — fresh chat/embed model status (after LLM config).
3. ``_print_checklist`` — the remaining-steps checklist.
"""

from __future__ import annotations

from pathlib import Path

from ..deps import DepCheckResult, run_fixes, run_full_check
from ._init_build import _count_cc_entries

_PIP_DEPS = ("pysqlite3", "sqlite-vec", "libclang-python", "watchfiles", "tomli-w")


def _run_deps_auto_fix(project_root: Path, *, dry_run: bool = False) -> tuple[list[DepCheckResult], list[DepCheckResult]]:
    """Run full check, auto-fix fixable issues, return (before, after) results.

    Passes ``skip_model_pulls=True`` — chat/embed model pulls (multi-GB)
    are owned exclusively by ``prompt_llm_config``; init must never pull a
    model without consent.  Pip packages (pysqlite3, sqlite-vec, libclang,
    watchfiles, tomli-w) are fixed without consent — small and standard.

    In *dry_run* mode, only the check runs (no ``pip install`` side
    effects) — ``after`` equals ``before``.
    """
    before = run_full_check(project_root=project_root)
    after = run_fixes(before, project_root=project_root, skip_model_pulls=True) if not dry_run else list(before)
    _print_deps_summary(after)
    return before, after


def _index_exists(cfg) -> bool:
    """Return True when an index database exists for the loaded config.

    The checklist uses this to decide whether to list ``fw-context index``
    as a remaining step.  An empty ``project.id`` means init has not
    written it yet, so no index path is resolvable and the answer is False.
    """
    if not cfg.project.id:
        return False
    return (cfg.index.db_dir / cfg.project.id / "index.db").exists()


def _print_deps_summary(results: list[DepCheckResult]) -> None:
    """Print a compact dependency summary (one line per check).

    Shown after auto-fix so the user sees the final state, not the noisy
    per-``pip install`` output.  Each status maps to a marker (``[ok]`` /
    ``[..]`` skipped / ``[✗]``) plus a one-line ``run: ...`` hint for
    anything that was not fixed.
    """
    print("── Dependencies ──")
    for r in results:
        if r.status == "ok":
            print(f"  [ok]  {r.name}: {r.message}")
        elif r.status == "skipped":
            print(f"  [..]  {r.name}: {r.message}")
        else:
            cmd = f" — run: {r.fix_cmd}" if r.fix_cmd else ""
            print(f"  [✗]  {r.name}: {r.message}{cmd}")


def _model_status(project_root: Path) -> dict[str, DepCheckResult]:
    """Re-check chat/embed model status against a freshly loaded config.

    Used by the checklist AFTER ``prompt_llm_config`` so the ``[model]``
    entries reflect what was actually pulled interactively — not the
    pre-fix ``after_results`` where models were deliberately left as
    ``missing`` (``skip_model_pulls=True``).
    """
    from ..config import load as load_config
    from ..deps._checks import check_ollama_chat_model, check_ollama_embed_model

    cfg = load_config(project_root=project_root)
    return {
        "chat-model": check_ollama_chat_model(cfg.llm),
        "embed-model": check_ollama_embed_model(cfg.llm),
    }


def _print_checklist(
    after_results: list[DepCheckResult],
    model_results: dict[str, DepCheckResult],
    *,
    build_ok: bool,
    cc_path: Path | None,
    build_system: str | None,
    tools_registered: list[str],
    index_exists: bool,
    build_skipped: bool = False,
    multi_variant: bool = False,
) -> None:
    """Print the remaining-steps checklist, grouped by category.

    Categories: ``[system]`` (sudo packages), ``[model]`` (ollama pull),
    ``[build]`` (compile DB), ``[index]`` (indexing).
    """
    print("\n── Checklist ──")

    # ── Done ──
    done: list[str] = []
    pip_ok = sum(1 for r in after_results if r.name in _PIP_DEPS and r.status == "ok")
    if pip_ok:
        done.append(f"pip dependencies ({pip_ok}/{len(_PIP_DEPS)})")
    if build_ok and cc_path is not None:
        done.append(f"compile_commands.json ({_count_cc_entries(cc_path)} entries)")
    if tools_registered:
        done.append(f"AI tool configuration ({', '.join(tools_registered)})")

    if done:
        print("  Done:")
        for item in done:
            print(f"    ✓ {item}")

    # ── Remaining ──
    remaining: list[tuple[str, str]] = []
    libclang = next((r for r in after_results if r.name == "libclang-so"), None)
    if libclang is not None and libclang.status == "missing":
        cmd = libclang.fix_cmd or "apt install libclang-18-dev"
        remaining.append(("[system]", cmd))

    for name, label in (("chat-model", "chat model"), ("embed-model", "embedding model")):
        r = model_results.get(name)
        if r is not None and r.status == "missing":
            remaining.append(("[model]", r.fix_cmd or f"ollama pull <{label}>"))

    if not build_ok:
        if multi_variant:
            remaining.append(
                ("[build]", "compile_commands.json — multi-variant project: run 'fw-context index --build'")
            )
        elif build_skipped:
            remaining.append(
                ("[build]", "compile_commands.json — re-run init without --skip-build (or fw-context index --build)")
            )
        else:
            remaining.append(("[build]", "compile_commands.json — set build system / build params and re-run init"))

    if not index_exists:
        remaining.append(("[index]", "fw-context index --build"))

    if remaining:
        print("  Remaining:")
        for i, (cat, cmd) in enumerate(remaining, start=1):
            print(f"    {cat:<10} {i}. {cmd}")
        print("\n  (re-run `fw-context init` after fixing remaining items — it is safe to re-run)")
