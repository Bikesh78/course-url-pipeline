#!/usr/bin/env python3
"""Choose which course rows a paid search batch should cover.

Why sampling is a tool and not a pipeline flag
----------------------------------------------
The selection question changes as results arrive -- which failure diagnosis to
target, how wide to spread, how much to spend -- while the pipeline's job stays
"search these rows". Keeping it here also makes each batch an artefact: the file
records exactly which rows a given spend covered, so a later batch can be drawn
disjoint from it and the money is auditable.

Why round-robin across Sites
----------------------------
File order is not a sample. The first 500 `no_catalog` rows in the full sheet
cover only **66 of 716 Sites**, and single institutions dominate --
monash.edu 563 rows, newcastle.edu.au 519, unimelb.edu.au 494. A batch drawn
that way measures two universities' URL shapes, not the population.

Taking one row per Site before taking a second turns 500 queries into ~500
distinct Sites. `run.py`'s `interleave_by_domain` round-robins for the same
reason, but for throughput; here it is for sample validity. Sites are sorted, so
the walk is deterministic and a later batch continues into second rows per Site
rather than re-drawing.

Sample from the *post-triage* file
----------------------------------
Default `--results` is `out/phase2.csv`, not the phase 1 output. Triage fills
4.3% of the otherwise-eligible pool for free, and a batch drawn before it runs
would spend paid queries on rows that are already answered by the time search
starts -- `searchable_rows` skips them, so the batch would also come out short
of the size asked for. The *run* still reads the phase 1 output; only the
selection looks at what survives triage.

Why filter on the failure diagnosis
-----------------------------------
`no_catalog` covers four different failures, and which one decides whether a
trial result can be *believed*:

    no_hub          4,312 rows / 538 sites   site readable, listing not found
    blocked         3,796 rows /  83 sites   the site refused us
    thin            1,128 rows /   9 sites
    no_candidates     959 rows /  86 sites

A search hit on a `no_hub` site can be confirmed by fetching it. On a `blocked`
site a fetch will likely be refused whether or not the URL is right, so a
result there is unverifiable rather than wrong. Sampling them separately keeps
those two outcomes from being averaged together.

Usage
-----
    # 400 from the cohort a fetch can confirm
    python tools/sample_search_targets.py --diagnosis no_hub --limit 400 \\
        --out out/search_batch.001.txt

    # plus a 100-row probe of whether blocked sites serve individual pages
    python tools/sample_search_targets.py --diagnosis blocked --limit 100 \\
        --append out/search_batch.001.txt

    # a later batch, guaranteed not to overlap
    python tools/sample_search_targets.py --diagnosis no_hub --limit 400 \\
        --exclude out/search_batch.001.txt --out out/search_batch.002.txt
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.load import _host_of  # noqa: E402
from pipeline.report import DEFAULT_OUT_DIR  # noqa: E402
from pipeline.search import is_searchable  # noqa: E402
from pipeline.store import DEFAULT_DB  # noqa: E402


def site_diagnoses(db: str) -> dict[str, str]:
    """Map Site key to why extraction failed, from the `catalogs` table."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    out = {}
    try:
        for key, diagnosis, healthy in conn.execute(
                "SELECT site_key, diagnosis, healthy FROM catalogs"):
            out[key] = diagnosis if not healthy else "healthy"
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return out


def candidates(rows: list[dict], statuses: set[str], diagnoses: set[str],
               diag_by_site: dict[str, str],
               excluded: set[str]) -> list[dict]:
    """Rows eligible for this batch, before the spread is applied."""
    out = []
    for r in rows:
        if r["id"] in excluded or not is_searchable(r):
            continue
        if statuses and (r.get("matched_status") or "").strip() not in statuses:
            continue
        if diagnoses:
            if diag_by_site.get(_host_of(r.get("website", ""))) not in diagnoses:
                continue
        out.append(r)
    return out


def spread(rows: list[dict], limit: int, per_site: int | None = None) -> list[dict]:
    """Round-robin across Sites: one row per Site before a second, and so on.

    Deterministic — Sites in sorted order, rows in file order within a Site —
    so the same arguments draw the same batch, and a later batch continues the
    walk instead of re-drawing from the top.
    """
    by_site: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_site[_host_of(r.get("website", ""))].append(r)

    queues = [by_site[k] for k in sorted(by_site)]
    picked: list[dict] = []
    depth = 0
    while len(picked) < limit and any(len(q) > depth for q in queues):
        if per_site is not None and depth >= per_site:
            break
        for q in queues:
            if len(q) > depth:
                picked.append(q[depth])
                if len(picked) == limit:
                    return picked
        depth += 1
    return picked


def read_ids(paths: list[str]) -> set[str]:
    """Course ids already covered by earlier batches."""
    seen: set[str] = set()
    for path in paths or []:
        try:
            with open(path, encoding="utf-8") as fh:
                seen.update(line.strip() for line in fh if line.strip())
        except OSError:
            print(f"  warning: could not read {path}", file=sys.stderr)
    return seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results",
                    default=os.path.join(DEFAULT_OUT_DIR, "phase2.csv"),
                    help="the result file to sample from; default is the "
                         "post-triage output, so the batch excludes rows "
                         "triage already filled")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="database holding the per-Site extraction diagnosis")
    ap.add_argument("--status", action="append", default=None,
                    help="matched_status to include (repeatable; "
                         "default no_catalog)")
    ap.add_argument("--diagnosis", action="append", default=None,
                    help="extraction failure to include, e.g. no_hub or "
                         "blocked (repeatable; default any)")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--per-site", type=int, default=None,
                    help="at most this many rows from any one Site")
    ap.add_argument("--exclude", action="append", default=None,
                    help="earlier batch file whose ids to skip (repeatable)")
    ap.add_argument("--out", default=None, help="write the batch here")
    ap.add_argument("--append", default=None,
                    help="append to an existing batch file instead")
    args = ap.parse_args(argv)

    with open(args.results, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    statuses = set(args.status or ["no_catalog"])
    diagnoses = set(args.diagnosis or [])
    diag_by_site = site_diagnoses(args.db)
    if diagnoses and not diag_by_site:
        print(f"  cannot filter by diagnosis: no catalogs table in {args.db}",
              file=sys.stderr)
        return 2

    # Appending implies not re-drawing what the file already holds.
    excluded = read_ids((args.exclude or [])
                        + ([args.append] if args.append else []))
    pool = candidates(rows, statuses, diagnoses, diag_by_site, excluded)
    picked = spread(pool, args.limit, args.per_site)

    sites = {_host_of(r.get("website", "")) for r in picked}
    print(f"{args.results}: {len(rows)} rows")
    print(f"  status {sorted(statuses)}"
          + (f", diagnosis {sorted(diagnoses)}" if diagnoses else ""))
    if excluded:
        print(f"  excluding {len(excluded)} ids already covered")
    print(f"  eligible pool                : {len(pool)}")
    print(f"  picked                       : {len(picked)}")
    print(f"  distinct sites in the batch  : {len(sites)}")
    if picked:
        deepest = collections.Counter(
            _host_of(r.get("website", "")) for r in picked).most_common(1)[0]
        print(f"  most rows from one site      : {deepest[1]} ({deepest[0]})")

    target = args.append or args.out
    if not target:
        print("\n  report only — pass --out or --append to write the batch")
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with open(target, "a" if args.append else "w", encoding="utf-8") as fh:
        for r in picked:
            fh.write(r["id"] + "\n")
    total = len(read_ids([target]))
    print(f"\n  {'appended to' if args.append else 'wrote'} {target} "
          f"({total} ids in total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
