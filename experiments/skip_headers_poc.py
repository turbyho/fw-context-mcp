"""PoC: AST walk with skip_files — skip subtrees of already-processed headers.

Measures:
  A) Current extract_all (full walk every TU)
  B) Modified walk: skips cursor subtrees when source file is already processed

Usage:
    python experiments/skip_headers_poc.py <compile_commands.json> --max-tus N
"""

from __future__ import annotations

import argparse, logging, sys, time
from collections import Counter
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import clang.cindex as cx

from fw_context_mcp.indexer.compile_commands import parse as parse_cc, _SOURCE_EXTS
from fw_context_mcp.indexer.symbols import extract_all, _get_index, _SYMBOL_KINDS, _process_one_symbol
from fw_context_mcp.indexer.sdk_detect import _build_sdk_excludes
from fw_context_mcp.indexer._embedding import _fmt_dur
from fw_context_mcp.indexer.models import Symbol

log = logging.getLogger(__name__)


def _resolve_file(cursor, cwd: Path) -> str:
    """Resolve cursor location to an absolute path, or empty string."""
    loc = cursor.location
    if not loc.file:
        return ""
    p = Path(str(loc.file.name))
    resolved = (cwd / p).resolve() if not p.is_absolute() else p.resolve()
    return str(resolved)


def _walk_skip(root_cursor, skip_files: set[str], cwd: Path):
    """Pre-order AST walk that skips subtrees when cursor's file is in skip_files.

    Uses a manual stack (not walk_preorder) so we can skip pushing children
    of cursors whose source file has already been processed.
    """
    stack = [root_cursor]
    while stack:
        c = stack.pop()
        fname = _resolve_file(c, cwd)

        if fname and fname in skip_files:
            continue  # skip this cursor AND its entire subtree

        yield c

        # Push children in reverse order for pre-order semantics
        children = list(c.get_children())
        for child in reversed(children):
            stack.append(child)


def extract_symbols_skip(
    unit,
    skip_files: set[str],
    with_refs: bool = False,
) -> tuple[list[Symbol], set[str]]:
    """Like extract_all but skips AST subtrees of already-processed files.

    Returns (symbols, newly_seen_files) — the caller should merge
    newly_seen_files into its skip_files set for subsequent TUs.
    """
    tu = _get_index().parse(
        str(unit.file),
        args=unit.clang_args,
        options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )
    cwd = unit.directory

    symbols: list[Symbol] = []
    seen_usrs: dict[str, bool] = {}
    class_cursors: list[cx.Cursor] = []
    newly_seen: set[str] = set()

    from fw_context_mcp.indexer.symbols import _build_anon_usr_to_field
    anon_usr_to_field = _build_anon_usr_to_field(tu.cursor)

    # Use fast native walk when skip_files is empty (first TU or no skips)
    if not skip_files:
        for cursor in tu.cursor.walk_preorder():
            fname = _resolve_file(cursor, cwd)
            if fname:
                newly_seen.add(fname)
            if cursor.kind not in _SYMBOL_KINDS:
                continue
            _process_one_symbol(cursor, symbols, seen_usrs, class_cursors, anon_usr_to_field, log)
    else:
        for cursor in _walk_skip(tu.cursor, skip_files, cwd):
            fname = _resolve_file(cursor, cwd)
            if fname and fname not in skip_files:
                newly_seen.add(fname)
            if cursor.kind not in _SYMBOL_KINDS:
                continue
            _process_one_symbol(cursor, symbols, seen_usrs, class_cursors, anon_usr_to_field, log)

    return symbols, newly_seen


def run_baseline(units):
    """Current: extract_all for each TU. Measure timing."""
    times = []
    total_syms = 0
    for i, unit in enumerate(units):
        t0 = time.monotonic()
        result = extract_all(unit, with_refs=False, return_tu=False)
        elapsed = time.monotonic() - t0
        times.append(elapsed)
        total_syms += len(result.symbols)
        log.info("BASELINE [%d/%d] %s: %d syms %.2fs",
                 i + 1, len(units), unit.file.name, len(result.symbols), elapsed)
    return times, total_syms


def run_skip(units):
    """Modified: first TU full walk, subsequent TUs skip already-seen files."""
    times = []
    total_syms = 0
    skip_files: set[str] = set()

    for i, unit in enumerate(units):
        t0 = time.monotonic()
        symbols, newly_seen = extract_symbols_skip(unit, skip_files, with_refs=False)
        elapsed = time.monotonic() - t0

        skip_files |= newly_seen
        times.append(elapsed)
        total_syms += len(symbols)
        log.info("SKIP    [%d/%d] %s: %d syms (new_files=%d skip_set=%d) %.2fs",
                 i + 1, len(units), unit.file.name,
                 len(symbols), len(newly_seen), len(skip_files), elapsed)

    return times, total_syms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("compile_commands", type=Path)
    ap.add_argument("--max-tus", type=int, default=15)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    cc_path = args.compile_commands.resolve()
    project_root = cc_path.parent
    all_units = [u for u in parse_cc(cc_path) if u.file.suffix.lower() in _SOURCE_EXTS]
    units = all_units[:args.max_tus]
    log.info("TUs: %d", len(units))

    # ── Baseline ──
    log.info("=== BASELINE (current extract_all) ===")
    base_times, base_syms = run_baseline(units)

    # ── Skip headers ──
    log.info("=== SKIP HEADERS ===")
    skip_times, skip_syms = run_skip(units)

    # ── Compare ──
    base_total = sum(base_times)
    skip_total = sum(skip_times)
    syms_match = "✓" if base_syms == skip_syms else f"MISMATCH! base={base_syms} skip={skip_syms}"

    print()
    print(f"{'Method':<40} {'Total':>8} {'Symbols':>10}")
    print("-" * 62)
    print(f"{'Baseline (current)':<40} {_fmt_dur(base_total):>8} {base_syms:>10}")
    print(f"{'Skip already-seen files':<40} {_fmt_dur(skip_total):>8} {skip_syms:>10} {syms_match}")
    print(f"\nSpeedup: {base_total/skip_total:.1f}x  ({_fmt_dur(base_total)} → {_fmt_dur(skip_total)})")

    # Per-TU comparison
    print(f"\n{'TU':>3} {'File':<30} {'Base':>6} {'Skip':>6} {'Faster':>6}")
    for i, (bt, st) in enumerate(zip(base_times, skip_times)):
        faster = bt / st if st > 0 else float("inf")
        print(f"{i+1:3d} {units[i].file.name:<30} {_fmt_dur(bt):>6} {_fmt_dur(st):>6} {faster:>5.1f}x")


if __name__ == "__main__":
    main()
