"""Measure how much AST walk time is spent on repeat vendor headers.

Counts symbols by their source FILE (not just vendor/project boolean).
Shows: after TU #1, how many files repeat unchanged.

Usage:
    python experiments/measure_skip_headers.py <compile_commands.json> --max-tus N
"""

from __future__ import annotations

import argparse, logging, sys, time
from pathlib import Path
from collections import Counter

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from fw_context_mcp.indexer.compile_commands import parse as parse_cc, _SOURCE_EXTS
from fw_context_mcp.indexer.symbols import extract_all
from fw_context_mcp.indexer.sdk_detect import _build_sdk_excludes
from fw_context_mcp.indexer._embedding import _fmt_dur

log = logging.getLogger(__name__)


def _is_vendor_file(abs_path: str, vendor_patterns: list[str]) -> bool:
    """Check if an absolute path matches any vendor pattern (LIKE-style)."""
    for pat in vendor_patterns:
        # Pattern is like ".pio/libdeps/%" or "mbed-os/%"
        clean = pat.replace("%", "")
        if clean in abs_path:
            return True
    return False


def _source_file_name(abs_path: str, project_root: Path) -> str:
    """Return short display name for a file path."""
    try:
        return str(Path(abs_path).relative_to(project_root))
    except ValueError:
        return Path(abs_path).name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("compile_commands", type=Path)
    ap.add_argument("--max-tus", type=int, default=15)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    cc_path = args.compile_commands.resolve()
    project_root = cc_path.parent
    units = [u for u in parse_cc(cc_path) if u.file.suffix.lower() in _SOURCE_EXTS][:args.max_tus]
    vendor_patterns = list(_build_sdk_excludes(project_root))

    log.info("Vendor patterns: %s", vendor_patterns)

    # ── Collect stats ──
    all_files_seen: set[str] = set()    # all unique source files across all TUs
    files_per_tu: list[dict] = []        # per-TU breakdown
    total_vendor_syms = 0
    total_project_syms = 0

    for i, unit in enumerate(units):
        t0 = time.monotonic()
        result = extract_all(unit, with_refs=False, return_tu=False)
        t_total = time.monotonic() - t0

        # Group symbols by source file
        file_syms: Counter = Counter()
        for sym in result.symbols:
            file_syms[sym.file] += 1

        vendor_syms = 0
        project_syms = 0
        new_files = 0
        repeat_files = 0

        for fpath, count in file_syms.items():
            is_v = _is_vendor_file(fpath, vendor_patterns)
            if is_v:
                vendor_syms += count
            else:
                project_syms += count

            was_seen = fpath in all_files_seen
            if not was_seen:
                new_files += 1
                all_files_seen.add(fpath)
            else:
                repeat_files += 1

        total_vendor_syms += vendor_syms
        total_project_syms += project_syms

        files_per_tu.append({
            "file": unit.file.name,
            "total_syms": len(result.symbols),
            "vendor_syms": vendor_syms,
            "project_syms": project_syms,
            "unique_files": len(file_syms),
            "new_files": new_files,
            "repeat_files": repeat_files,
            "total_s": t_total,
        })

        log.info("[%d/%d] %-30s syms=%5d vendor=%5d project=%5d files=%3d new=%3d repeat=%3d  %.2fs",
                 i + 1, len(units), unit.file.name,
                 len(result.symbols), vendor_syms, project_syms,
                 len(file_syms), new_files, repeat_files, t_total)

    # ── Report ──
    total_syms = total_vendor_syms + total_project_syms
    total_time = sum(r["total_s"] for r in files_per_tu)
    print()
    print(f"Total: {total_syms} symbols in {_fmt_dur(total_time)}")
    print(f"  Vendor (SDK):  {total_vendor_syms} ({total_vendor_syms/total_syms*100:.0f}%)")
    print(f"  Project:       {total_project_syms} ({total_project_syms/total_syms*100:.0f}%)")
    print(f"  Unique files:  {len(all_files_seen)}")
    print()

    # ── Per-TU table ──
    print(f"{'TU':>3} {'File':<30} {'Syms':>5} {'Vendor':>6} {'Proj':>5} "
          f"{'Files':>5} {'New':>4} {'Rpt':>4} {'Time':>6}")
    for i, r in enumerate(files_per_tu):
        print(f"{i+1:3d} {r['file']:<30} {r['total_syms']:>5} {r['vendor_syms']:>6} "
              f"{r['project_syms']:>5} {r['unique_files']:>5} "
              f"{r['new_files']:>4} {r['repeat_files']:>4} {_fmt_dur(r['total_s']):>6}")

    # ── Savings estimate ──
    first = files_per_tu[0]
    rest = files_per_tu[1:]
    rest_time = sum(r["total_s"] for r in rest)
    rest_vendor_pct = sum(r["vendor_syms"] for r in rest) / max(1, sum(r["total_syms"] for r in rest)) * 100
    first_vendor_pct = first["vendor_syms"] / first["total_syms"] * 100

    print(f"\nFirst TU: vendor={first_vendor_pct:.0f}%")
    print(f"Rest TUs: vendor={rest_vendor_pct:.0f}% of {rest_time:.1f}s")
    print(f"Estimated AST walk savings: ~{_fmt_dur(rest_time * rest_vendor_pct / 100)}")


if __name__ == "__main__":
    main()
