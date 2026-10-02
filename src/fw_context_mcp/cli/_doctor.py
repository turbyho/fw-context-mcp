"""``fw-context doctor`` — audit dependencies and repair broken installs.

Checks for required executables (compiler, libclang, Python packages),
writable directories, and configuration consistency.  With ``--fix``,
attempts automatic repair (e.g., installing missing Python packages).

WHY a doctor command: fw-context depends on many external tools (bear,
compilers, clang, libclang Python bindings, ollama).  Users often hit
cryptic errors from missing dependencies.  A single diagnostic command
that explains what's wrong and optionally fixes it reduces support burden.

Exit codes: 0 = all ok, 1 = warnings, 2 = critical failures.
"""

from __future__ import annotations

import argparse


def _allow_toolchains(project: str | None, *, to_stderr: bool) -> None:
    """Add the toolchains of the project to the allowlist in ``toolchains.toml``, and say which.

    WHY without ``--fix``: the allowlist is basic project setup that a user
    does not know about.  A toolchain outside it keeps the guessed headers
    without any visible sign, thus ``doctor`` writes it each time it runs.
    See ``indexer/_allowlist.py``.
    """
    import sys

    from ..indexer._allowlist import allow_project_toolchains
    from ..utils import resolve_project_root

    try:
        root = resolve_project_root(project)
    except (OSError, ValueError):
        # ValueError: an ambiguous project name.  The checks still run and
        # report the project problem themselves.
        return
    if not (root / ".fw-context" / "config.toml").is_file():
        return  # not an initialized project
    try:
        added = allow_project_toolchains(root)
    except (OSError, ValueError) as error:
        # ValueError: a ``command`` field with an unclosed quote.  The
        # checks of doctor must still run.
        print(f"  query-driver: cannot add the toolchains of {root}: {error}", file=sys.stderr)
        return
    out = sys.stderr if to_stderr else sys.stdout
    for glob in added:
        print(f"  query-driver: added {glob} to .fw-context/toolchains.toml", file=out)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Audit all dependencies.  With ``--fix``, attempt auto-repair.

    Checks executables (compiler, clang, bear, etc.), Python packages
    (libclang bindings), writable directories (cache, db), and optional
    dependencies (ollama for LLM analysis, sqlite-vec for embeddings).

    Exit codes: 0 = all ok, 1 = warnings (non-critical), 2 = critical
    failures that prevent operation.
    """
    from ..deps import exit_code, format_results, run_fixes, run_full_check
    from ..deps._checks import CHECK_ORDER

    # --only runs a subset.  WHY: `make install` must install the clang
    # headers without `--fix` pulling the multi-GB Ollama models.
    subset: set[str] | None = None
    if getattr(args, "only", None):
        subset = {name.strip() for name in args.only.split(",") if name.strip()}
        unknown = subset - {name for name, _ in CHECK_ORDER}
        if unknown:
            known = ", ".join(name for name, _ in CHECK_ORDER)
            print(f"fw-context doctor: unknown check(s): {', '.join(sorted(unknown))}. Known: {known}")
            return 2

    _allow_toolchains(args.project, to_stderr=bool(getattr(args, "json", False)))

    results = run_full_check(project_root=args.project, subset=subset)

    if args.fix:
        results = run_fixes(results, project_root=args.project)
        print(format_results(results))
        ok = sum(1 for r in results if r.status == "ok")
        fixed = ok  # approximation — items that were missing and are now ok
        print(f"  ({fixed} items ok after fix)")
    elif args.json:
        import json
        from dataclasses import asdict

        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        print(format_results(results))

    return exit_code(results)
