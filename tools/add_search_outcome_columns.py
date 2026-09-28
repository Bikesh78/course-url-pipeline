#!/usr/bin/env python3
"""Split search's blank-and-searched rows into "found nothing" vs "found and rejected".

`url_ignored_for_extraction` already marks rows `is_searchable` skipped
entirely. Among the rest that are still blank, the cache tells the finer
story: a query with zero cached links never had a candidate at all, while one
with links but no adopted `course_url` had candidates that the off-site check,
the quality gate, or the sharing rule turned down. Nothing in the sheet
carries that distinction without re-reading the cache, so this stamps it in.

Usage
-----
    python tools/add_search_outcome_columns.py
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.report import DEFAULT_OUT_DIR  # noqa: E402
from pipeline.search import (_host_of, build_query, cache_path_for,  # noqa: E402
                             is_searchable, links_from_entry,
                             read_cache_entry)
from run import write_rows_atomically  # noqa: E402

NO_RESULTS_COLUMN = "search_no_results"
ALL_REJECTED_COLUMN = "search_all_rejected"


def classify(row: dict) -> str:
    """One of: filled, ignored, pending, no_results, all_rejected."""
    if (row.get("course_url") or "").strip():
        return "filled"
    if not is_searchable(row):
        return "ignored"
    site = _host_of(row.get("website", "") or "")
    if not site:
        return "pending"
    query = build_query(row.get("name", ""), row.get("institution_name", ""),
                        site)
    entry = read_cache_entry(cache_path_for(query))
    if entry is None:
        return "pending"
    return "no_results" if not links_from_entry(entry) else "all_rejected"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results",
                    default=os.path.join(DEFAULT_OUT_DIR, "courses_filled.csv"),
                    help="the result file to stamp")
    ap.add_argument("--out", default=None,
                    help="where to write; defaults to --results, in place")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip keeping a .bak of the file being overwritten")
    args = ap.parse_args(argv)

    out_path = args.out or args.results

    with open(args.results, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    counts: dict[str, int] = {}
    for row in rows:
        outcome = classify(row)
        counts[outcome] = counts.get(outcome, 0) + 1
        row[NO_RESULTS_COLUMN] = "true" if outcome == "no_results" else "false"
        row[ALL_REJECTED_COLUMN] = "true" if outcome == "all_rejected" else "false"

    write_rows_atomically(rows, out_path, backup=not args.no_backup)

    print(f"{args.results}: {len(rows):,} rows")
    for outcome in ("filled", "ignored", "pending", "no_results", "all_rejected"):
        print(f"  {outcome:<12}: {counts.get(outcome, 0):,}")
    print(f"\n  wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
