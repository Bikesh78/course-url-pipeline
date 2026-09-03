#!/usr/bin/env python3
"""Add the provenance columns to a result file produced before they existed.

`prior_course_url`, `prior_matched_status` and `url_change` are written by the
pipeline now, but a `courses_filled.csv` produced by an earlier run does not
have them — and re-running phase 1 to get them costs hours of crawling for
information that is already derivable from the source sheet.

What it does *not* do
---------------------
It does not touch `course_url`. Comparison is done through `clean_url`, so a
trailing slash or stray whitespace is correctly reported as *unchanged* without
the delivered value being rewritten. The only edit is three appended columns.

Safety
------
Writes to a temporary file and swaps it in, so an interrupted run cannot leave
a truncated result file. Keeps the original as `<name>.bak` unless told not to.

Usage
-----
    python tools/backfill_provenance.py                       # report only
    python tools/backfill_provenance.py --write               # do it
    python tools/backfill_provenance.py --write --no-backup
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.load import DEFAULT_INPUT  # noqa: E402
from pipeline.report import PROVENANCE_COLUMNS  # noqa: E402
from pipeline.triage import classify_change  # noqa: E402

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def backfill(results_path: str, source_path: str) -> tuple[list[str], list[dict],
                                                           collections.Counter]:
    """Return (fieldnames, rows, change counts) with provenance filled in."""
    with open(source_path, encoding="utf-8-sig", newline="") as fh:
        source = {r["id"]: r for r in csv.DictReader(fh)}
    with open(results_path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        rows = list(reader)

    for col in PROVENANCE_COLUMNS:
        if col not in fields:
            fields.append(col)

    counts: collections.Counter = collections.Counter()
    missing = 0
    for r in rows:
        src = source.get(r["id"])
        if src is None:
            missing += 1
        prior = (src or {}).get("course_url", "").strip()
        r["prior_course_url"] = prior
        r["prior_matched_status"] = (src or {}).get("matched_status", "").strip()
        r["url_change"] = classify_change(prior,
                                          (r.get("course_url") or "").strip())
        counts[r["url_change"]] += 1
    if missing:
        counts["(not in source sheet)"] = missing
    return fields, rows, counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default="courses_filled.csv")
    ap.add_argument("--source", default=DEFAULT_INPUT)
    ap.add_argument("--write", action="store_true",
                    help="without this, only report what would change")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args(argv)

    with open(args.results, encoding="utf-8", newline="") as fh:
        existing = next(csv.reader(fh))
    already = [c for c in PROVENANCE_COLUMNS if c in existing]
    if already:
        print(f"{args.results} already has {', '.join(already)} — "
              f"values will be recomputed.")

    fields, rows, counts = backfill(args.results, args.source)

    print(f"{args.results}: {len(rows)} rows")
    print(f"  columns: {len(existing)} -> {len(fields)}")
    print()
    print("  url_change")
    for k, v in counts.most_common():
        print(f"    {k:24s} {v:6d}  ({100 * v / max(1, len(rows)):5.1f}%)")

    if not args.write:
        print("\n--write not given: nothing was modified")
        return 0

    if not args.no_backup:
        backup = args.results + ".bak"
        shutil.copy2(args.results, backup)
        print(f"\nbacked up original to {backup}")

    # Write beside the target so the replace is atomic on the same filesystem.
    d = os.path.dirname(os.path.abspath(args.results)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, args.results)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"wrote {args.results} with {len(fields)} columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
