"""``fw-context cleanup`` — remove what an older fw-context wrote and no version uses.

The work is ``housekeeping.clean``; this module parses the command line and
prints the report.  ``fw-context index`` and ``doctor --fix`` call the same
function, thus the command is for a user who wants it now, or ``--dry-run``
to see the list first.
"""

from __future__ import annotations

import argparse


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Remove (or with ``--dry-run`` list) the obsolete files, directories and config keys.

    Exit codes: 0 = done or nothing to do, 1 = a path could not be cleaned.
    """
    from ..housekeeping import clean
    from ..utils import resolve_project_root

    try:
        root = resolve_project_root(args.project)
    except OSError:
        root = None
    report = clean(root, dry_run=args.dry_run, quiet=True)
    verb = "would remove" if args.dry_run else "removed"
    for line in report.lines():
        print(f"{verb}: {line}")
    for path, reason in report.failures:
        print(f"cannot clean {path}: {reason}")
    if report.empty and not report.failures:
        print("nothing to clean")
    return 1 if report.failures else 0
