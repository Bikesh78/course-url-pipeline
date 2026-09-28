#!/usr/bin/env python3
"""Mark rows `is_searchable` never sent to search at all.

Blank `course_url` rows split two ways: most were searched and failed
(`no_catalog`/`no_match`), but some are excluded by `is_searchable` up front --
occupation-code/year-level/test-booking flags, the live year-level/test-booking
re-derivation, or an `(inactive)` marker. Nothing in the sheet distinguishes
"genuinely tried and failed" from "deliberately skipped" without re-running
that guard, so this stamps the answer into a column instead.

Usage
-----
    python tools/add_ignored_column.py
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.report import DEFAULT_OUT_DIR  # noqa: E402
from pipeline.search import is_searchable  # noqa: E402
from run import write_rows_atomically  # noqa: E402

COLUMN = "url_ignored_for_extraction"


def was_ignored(row: dict) -> bool:
    """Was this row excluded from search entirely, rather than tried and failed?"""
    return not (row.get("course_url") or "").strip() and not is_searchable(row)


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

    ignored = 0
    for row in rows:
        flag = was_ignored(row)
        row[COLUMN] = "true" if flag else "false"
        ignored += flag

    write_rows_atomically(rows, out_path, backup=not args.no_backup)

    print(f"{args.results}: {len(rows):,} rows")
    print(f"  {COLUMN} = true  : {ignored:,}")
    print(f"  {COLUMN} = false : {len(rows) - ignored:,}")
    print(f"\n  wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
