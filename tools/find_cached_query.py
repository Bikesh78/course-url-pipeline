#!/usr/bin/env python3
"""Show what the search vendor returned for a course, and why it was rejected.

Why this exists
---------------
The response cache is deliberately un-indexed: a path is derived from the query
by hash and never listed anywhere, so there is no way to look an entry up
without first rebuilding the query the pipeline would have sent. That makes
`ls` and `grep` useless against it and a tool worth having.

Why it prints scores, not just URLs
-----------------------------------
A bare list of links does not answer the question people actually have, which
is *why is this row still empty*. Almost always the answer is that every result
scored below the 0.55 gate -- 216 of 500 rows in the first live trial ended that
way, and a few more were on the wrong domain or refused by the sharing rule. So
each link is shown with its `on_site` verdict and its `gate_score`, which is the
same number the pipeline judged it by.

Usage
-----
    # by course id (unique)
    python tools/find_cached_query.py 65c587e9-9f42-4a50-85e7-3df52be51ffe

    # by name substring -- often ambiguous, so output is capped
    python tools/find_cached_query.py --name "Diploma of Business"
    python tools/find_cached_query.py --name "Secondary Junior" --all
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.load import _host_of  # noqa: E402
from pipeline.match import FLOOR  # noqa: E402
from pipeline.report import DEFAULT_OUT_DIR  # noqa: E402
from pipeline.search import (MAX_RESULTS, build_query,  # noqa: E402
                             cache_path_for, is_searchable, on_site)
from pipeline.triage import gate_score  # noqa: E402

SHOWN_BY_DEFAULT = 5


def matching(rows: list[dict], ids: set[str], name: str | None) -> list[dict]:
    """Rows selected by exact id, or by case-insensitive name substring."""
    if ids:
        return [r for r in rows if r["id"] in ids]
    if name:
        needle = name.lower()
        return [r for r in rows if needle in (r.get("name") or "").lower()]
    return []


def report(row: dict, num: int, cache_dir: str | None) -> None:
    """Print one row's query, its cache entry, and each link's verdict."""
    site = _host_of(row.get("website", ""))
    query = build_query(row.get("name", ""), row.get("institution_name", ""),
                        site)
    path = (cache_path_for(query, num, cache_dir) if cache_dir
            else cache_path_for(query, num))

    print(f'{row.get("name", "")}  [{row["id"]}]')
    print(f'  institution : {row.get("institution_name", "")}')
    print(f'  site        : {site or "(none — never searched)"}')
    print(f'  status      : {row.get("matched_status", "")}'
          f'{"" if is_searchable(row) else "   (not searchable)"}')
    print(f'  query       : {query}')
    print(f'  cache file  : {path}')

    if not os.path.exists(path):
        # Absence is an answer, not a failure: the row has not been searched.
        print("  cached      : no")
        return
    try:
        links = json.load(gzip.open(path, "rt", encoding="utf-8"))["links"]
    except (OSError, ValueError, KeyError, EOFError) as e:
        print(f"  cached      : unreadable ({type(e).__name__})")
        return

    print(f"  cached      : yes, {len(links)} link(s)")
    if not links:
        print("     the vendor returned nothing for this query")
        return
    for url in links:
        onsite = on_site(url, site)
        gate = gate_score(row.get("name", ""), url,
                          row.get("institution_name", ""))
        verdict = ("adoptable" if onsite and gate >= FLOOR
                   else "off-site" if not onsite
                   else f"below {FLOOR} gate")
        print(f"     gate={gate:.3f}  on-site={str(onsite):5s}  {verdict}")
        print(f"        {url}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ids", nargs="*", help="course ids to look up")
    ap.add_argument("--name", default=None,
                    help="match on a course-name substring instead")
    ap.add_argument("--results",
                    default=os.path.join(DEFAULT_OUT_DIR,
                                         "courses_filled.csv"),
                    help="result file to read the course rows from")
    ap.add_argument("--num", type=int, default=MAX_RESULTS,
                    help="result count the query was cached under; part of "
                         "the cache key, so a different value is a different "
                         "file")
    ap.add_argument("--cache-dir", default=None,
                    help="override the cache directory")
    ap.add_argument("--all", action="store_true",
                    help=f"show every match, not just the first "
                         f"{SHOWN_BY_DEFAULT}")
    args = ap.parse_args(argv)

    if not args.ids and not args.name:
        ap.error("give at least one course id, or --name")

    with open(args.results, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    found = matching(rows, set(args.ids), args.name)
    if not found:
        what = ", ".join(args.ids) if args.ids else repr(args.name)
        print(f"no row in {args.results} matched {what}")
        return 1

    # A name substring is routinely ambiguous -- "Secondary Junior" matches 36
    # rows across as many schools -- so say how many and show a few, rather
    # than filling the terminal because the name was not unique.
    shown = found if args.all else found[:SHOWN_BY_DEFAULT]
    if len(shown) < len(found):
        print(f"{len(found)} rows matched; showing {len(shown)}. "
              f"Pass --all for the rest, or a course id to be exact.\n")

    for i, row in enumerate(shown):
        if i:
            print()
        report(row, args.num, args.cache_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
