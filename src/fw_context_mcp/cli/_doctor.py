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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Audit all dependencies.  With ``--fix``, attempt auto-repair.

    Checks executables (compiler, clang, bear, etc.), Python packages
    (libclang bindings), writable directories (cache, db), and optional
    dependencies (ollama for LLM analysis, sqlite-vec for embeddings).

    Exit codes: 0 = all ok, 1 = warnings (non-critical), 2 = critical
    failures that prevent operation.
    """
    from ..deps import exit_code, format_results, run_fixes, run_full_check

    results = run_full_check(project_root=args.project)

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
