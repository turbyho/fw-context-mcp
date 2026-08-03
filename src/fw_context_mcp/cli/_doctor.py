"""``fw-context doctor`` — audit dependencies and repair broken installs."""

from __future__ import annotations

import argparse


def cmd_doctor(args: argparse.Namespace) -> int:
    """Audit all dependencies.  With ``--fix``, attempt auto-repair.

    Exit codes: 0 = all ok, 1 = warnings, 2 = critical failures.
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
