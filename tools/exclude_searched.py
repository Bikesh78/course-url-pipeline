#!/usr/bin/env python3
"""List the course ids a new paid search batch should *not* draw.

Why a batch needs an exclusion list at all
------------------------------------------
`tools/sample_search_targets.py` is deterministic by design -- sites sorted,
one row taken per site before a second -- so that a later batch continues where
an earlier one stopped rather than re-drawing it. That only works if the later
batch is told what the earlier one covered, and the batch files from the first
500-row trial were written into `out/scratch/`, which is disposable.

The response cache is a better record anyway, because it survives: an entry
exists for a query if and only if that query was paid for. Rows that were
searched and *filled* are already skipped by `is_searchable`; the ones this tool
catches are the rows that were searched and came back empty or rejected, which
stay searchable forever and would be re-drawn first by the round robin. They
cost nothing to re-query -- the cache answers them -- but a batch made of them
measures nothing, while reporting a full 25 rows queried.

Why year bands are in the same list
-----------------------------------
`is_year_level` (pipeline/load.py) decides a name like "Secondary Junior 7-10"
names no course, and `pipeline/load.py` flags such rows so `is_searchable`
refuses them. But that flagging happens during **phase 1**, while phase 2 reads
an existing result file: `out/courses_filled.csv` carries the flag on 54 rows
against 584 searchable rows the predicate actually matches, because phase 1 has
not re-run since the rule was added. Recomputing the predicate here honours the
decision now instead of waiting for a re-crawl. See ADR-0010 for why these rows
are left to a person rather than to search.

Usage
-----
    python tools/exclude_searched.py --out out/scratch/already_searched.txt

    # what the list is made of, without writing it
    python tools/exclude_searched.py
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.load import _host_of, is_year_level  # noqa: E402
from pipeline.report import DEFAULT_OUT_DIR  # noqa: E402
from pipeline.search import (build_query, cache_path_for,  # noqa: E402
                             searchable_rows)


def already_paid(row: dict) -> bool:
    """Has the query this row would send already been bought?

    Built from the same three functions the run uses, so the answer cannot
    drift from what the provider would actually look up: change `build_query`
    or the cache layout and this follows.
    """
    site = _host_of(row.get("website", ""))
    if not site:
        return False
    query = build_query(row.get("name", ""), row.get("institution_name", ""),
                        site)
    return os.path.exists(cache_path_for(query))


def classify(rows: list[dict]) -> tuple[list[str], int, int, int]:
    """Ids to exclude, plus the counts behind them for the report."""
    excluded, paid, bands, both = [], 0, 0, 0
    for r in searchable_rows(rows):
        is_paid = already_paid(r)
        is_band = is_year_level(r.get("name", ""))
        if not (is_paid or is_band):
            continue
        excluded.append(r["id"])
        paid += is_paid
        bands += is_band
        both += is_paid and is_band
    return excluded, paid, bands, both


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results",
                    default=os.path.join(DEFAULT_OUT_DIR,
                                         "courses_filled.csv"),
                    help="the result file a batch would be drawn from")
    ap.add_argument("--out", default=None,
                    help="write the ids here, one per line; without it the "
                         "tool reports and writes nothing")
    args = ap.parse_args(argv)

    with open(args.results, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    searchable = searchable_rows(rows)
    excluded, paid, bands, both = classify(rows)

    print(f"{args.results}: {len(rows):,} rows")
    print(f"  searchable                   : {len(searchable):,}")
    print(f"  already paid for (cached)    : {paid:,}")
    print(f"  year bands (not courses)     : {bands:,}")
    print(f"  counted in both              : {both:,}")
    print(f"  to exclude                   : {len(excluded):,}")
    print(f"  left for a new batch         : "
          f"{len(searchable) - len(excluded):,}")

    if not args.out:
        print("\n  report only -- pass --out to write the list")
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("".join(f"{i}\n" for i in excluded))
    print(f"\n  wrote {len(excluded):,} ids to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
