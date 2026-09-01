#!/usr/bin/env python3
"""Fill the `course_url` column of processed_courses.csv.

`processed_courses.csv` is read-only. See README.md for usage and CONTEXT.md for
the domain vocabulary.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import os
import re
import sys
import time
import urllib.parse

from pipeline.catalog import (SCHEMA_VERSION, Catalog, build_catalog,
                              page_heading, page_title)
from pipeline.fetch import Fetcher, registrable
from pipeline.load import (dedupe, group_by_institution, load_rows,
                           normalise_website)
from pipeline.match import MatchResult, Thresholds, assign
from pipeline.normalize import score as score_pair
from pipeline.report import (write_calibration_sample, write_coverage_report,
                             write_filled_csv, write_review_queue)

CATALOG_DIR = "catalogs"


def slugify(name: str) -> str:
    """Institution name to a filesystem-safe stem for its cached Catalog.

    Lossy and not reversible — "Curtin University - CU" becomes
    "curtin-university-cu". Only ever used to name a cache file, so a collision
    between two Institutions would mean one reusing the other's Catalog; the
    120-character truncation in `catalog_path` makes that vanishingly unlikely
    but not impossible.
    """
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def catalog_path(institution: str) -> str:
    """Where this Institution's cached Catalog lives.

    Relative to the working directory, so `catalogs/` is created wherever
    `run.py` is invoked from.
    """
    return os.path.join(CATALOG_DIR, f"{slugify(institution)[:120]}.json")


def load_or_build_catalog(fetcher: Fetcher, institution: str, website: str,
                          expected_rows: int, refresh: bool = False) -> Catalog:
    """Catalogs are cached on disk: extraction is the expensive stage."""
    path = catalog_path(institution)
    if not refresh and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            # A Catalog written by an older schema may be missing a field a
            # safety check depends on, so it is rebuilt rather than used.
            # Rebuilding is cheap: the page cache makes it a no-network replay.
            if data.get("schema_version", 1) >= SCHEMA_VERSION:
                return Catalog.from_dict(data)
        except (OSError, ValueError):
            pass
    cat = build_catalog(fetcher, institution, website, expected_rows)
    os.makedirs(CATALOG_DIR, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cat.as_dict(), fh, indent=1)
    except OSError:
        pass
    return cat


def verify(fetcher: Fetcher, res: MatchResult, th: Thresholds) -> None:
    """Fetch the assigned URL and re-score the live page against the row name.

    This is independent evidence: the Candidate name came from a listing page's
    anchor text, while this comes from the course page's own title. Agreement
    between the two is what `verified` claims.
    """
    if not res.url:
        return
    page = fetcher.get(res.url)
    if page.status != 200 or not page.text:
        res.status = "url_dead"
        res.flags.append(f"verify_http_{page.status}")
        return
    heading = page_heading(page.text) or page_title(page.text)
    live = score_pair(res.row.name, heading, res.row.institution_name)
    res.live_score = live
    if live >= th.confident and res.status == "confident":
        res.status = "confident"
    elif res.status == "confident":
        res.status = "probable"
        res.flags.append("live_title_weaker_than_listing")


def interleave_by_domain(results: list[MatchResult]) -> list[MatchResult]:
    """Reorder work so consecutive items hit different domains.

    The fetcher serialises requests per domain. Handing the thread pool a list
    grouped by Institution therefore parks every worker on one domain lock and
    collapses throughput to a single domain's rate — measured at ~1.25 req/s
    against a possible ~15. Round-robin across domains keeps every worker on a
    different lock.
    """
    buckets: dict[str, list[MatchResult]] = {}
    for r in results:
        key = registrable(urllib.parse.urlsplit(r.url).netloc)
        buckets.setdefault(key, []).append(r)
    order: list[MatchResult] = []
    queues = list(buckets.values())
    while queues:
        for q in list(queues):
            order.append(q.pop(0))
            if not q:
                queues.remove(q)
    return order


def clone_for(res: MatchResult, row) -> MatchResult:
    """Give a duplicate Course Row the same result as its representative."""
    out = dataclasses.replace(res, row=row, flags=list(res.flags))
    out.flags.append("shared_with_duplicate_row")
    return out


def process_institution(fetcher: Fetcher, institution: str, rows: list,
                        th: Thresholds, args) -> tuple[list[MatchResult], dict]:
    """Resolve one Institution end to end. The unit of parallelism.

    Builds or loads the Catalog, then either fails closed or assigns. Two
    things here are load-bearing and easy to break:

    Failing closed. An unhealthy Catalog yields `no_catalog` for every row and
    no URLs at all, rather than matching against a fragment (ADR-0001).

    Deduping *before* Assignment. Duplicate Course Rows would otherwise compete
    for the same URL under the uniqueness rail, and one would be starved of a
    URL that is rightfully both rows' answer.

    Returns (results, health) where health feeds the coverage report.
    """
    website = normalise_website(rows[0].website)
    for r in rows:
        if not website:
            website = normalise_website(r.website)
            if website:
                break

    cat = load_or_build_catalog(fetcher, institution, website, len(rows),
                                refresh=args.refresh_catalogs)
    healthy = cat.healthy(len(rows))
    health = {"candidates": len(cat.candidates), "strategy": cat.strategy,
              "healthy": healthy, "notes": cat.notes, "seeds": cat.seeds,
              "failure_reason": cat.failure_reason,
              "seed_yield": cat.seed_yield}

    if not healthy:
        # Fail closed rather than match against a partial Catalog (ADR-0001).
        out = []
        for r in rows:
            mr = MatchResult(row=r, status="no_catalog")
            mr.flags.append(f"catalog_candidates={len(cat.candidates)}")
            if cat.failure_reason:
                mr.flags.append(f"extraction_{cat.failure_reason}")
            out.append(mr)
        return out, health

    # Dedupe BEFORE Assignment. 704 (institution, name) pairs repeat, and the
    # uniqueness rail would otherwise make identical courses compete for the
    # same URL, starving one of them.
    work = dedupe(rows)
    representatives = [group[0] for group in work.values()]
    results = assign(representatives, cat, institution, th)
    by_key = {res.row.work_key: res for res in results}

    out: list[MatchResult] = []
    for key, group in work.items():
        base = by_key[key]
        out.append(base)
        for extra in group[1:]:
            out.append(clone_for(base, extra))
    return out, health


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: resolve Institutions, verify, and write the outputs.

    Institutions are processed concurrently, then Verification re-fetches every
    assigned URL. That second pass is deliberately re-ordered by
    `interleave_by_domain` — grouped by Institution it parks every worker on a
    single domain lock and runs about twelve times slower.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="processed_courses.csv")
    ap.add_argument("--out", default="courses_filled.csv")
    ap.add_argument("--review-out", default="review_queue.csv")
    ap.add_argument("--report-out", default="coverage_report.md")
    ap.add_argument("--calibration-out", default="calibration_sample.csv")
    ap.add_argument("--institution", action="append", default=None,
                    help="restrict to one Institution (repeatable)")
    ap.add_argument("--limit", type=int, default=None,
                    help="process only the N largest Institutions")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests to one domain")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the live re-fetch of assigned URLs")
    ap.add_argument("--refresh-catalogs", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="use only the fetch cache; never hit the network")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the planned work and exit")
    ap.add_argument("--confident", type=float, default=None)
    ap.add_argument("--floor", type=float, default=None)
    ap.add_argument("--min-margin", type=float, default=None)
    args = ap.parse_args(argv)

    th = Thresholds()
    if args.confident is not None:
        th.confident = args.confident
    if args.floor is not None:
        th.floor = args.floor
    if args.min_margin is not None:
        th.min_margin = args.min_margin

    rows = load_rows(args.input)
    by_inst = group_by_institution(rows)
    ranked = sorted(by_inst.items(), key=lambda kv: -len(kv[1]))

    if args.institution:
        wanted = {w.lower() for w in args.institution}
        ranked = [(k, v) for k, v in ranked if k.lower() in wanted]
        if not ranked:
            print(f"No Institution matched {args.institution!r}. Try one of:",
                  file=sys.stderr)
            for k, v in sorted(by_inst.items(), key=lambda kv: -len(kv[1]))[:10]:
                print(f"  {len(v):5d}  {k}", file=sys.stderr)
            return 2
    if args.limit:
        ranked = ranked[:args.limit]

    planned_rows = sum(len(v) for _, v in ranked)
    print(f"{len(ranked)} institutions, {planned_rows} course rows "
          f"({len(dedupe([r for _, v in ranked for r in v]))} unique work items)")
    if args.dry_run:
        for inst, rs in ranked[:40]:
            site = normalise_website(rs[0].website) or "(no usable website)"
            cached = "cached" if os.path.exists(catalog_path(inst)) else "-"
            print(f"  {len(rs):5d}  {inst[:46]:48s} {site[:44]:46s} {cached}")
        if len(ranked) > 40:
            print(f"  ... and {len(ranked) - 40} more")
        return 0

    fetcher = Fetcher(delay=args.delay, offline=args.offline)
    results: list[MatchResult] = []
    health: dict[str, dict] = {}
    started = time.time()
    done = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_institution, fetcher, inst, rs, th, args):
                   inst for inst, rs in ranked}
        for fut in concurrent.futures.as_completed(futures):
            inst = futures[fut]
            done += 1
            try:
                res, h = fut.result()
            except Exception as e:                       # keep going
                print(f"  !! {inst}: {type(e).__name__}: {e}", file=sys.stderr)
                res, h = [], {"candidates": 0, "strategy": "error",
                              "healthy": False, "notes": [str(e)]}
            results.extend(res)
            health[inst] = h
            filled = sum(1 for r in res if r.url)
            print(f"  [{done}/{len(ranked)}] {inst[:44]:46s} "
                  f"cand={h['candidates']:5d} {h['strategy']:16s} "
                  f"{'ok ' if h['healthy'] else 'NO '} filled={filled}/{len(res)}")

    if not args.no_verify:
        targets = [r for r in results
                   if r.url and "shared_with_duplicate_row" not in r.flags]
        targets = interleave_by_domain(targets)
        print(f"verifying {len(targets)} assigned URLs "
              f"across {len({registrable(urllib.parse.urlsplit(r.url).netloc) for r in targets})} domains...",
              flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(lambda r: verify(fetcher, r, th), targets))
        # Propagate the verified status onto duplicate rows.
        by_key = {r.row.work_key: r for r in targets}
        for r in results:
            if "shared_with_duplicate_row" in r.flags:
                src = by_key.get(r.row.work_key)
                if src is not None:
                    r.status = src.status
                    r.live_score = src.live_score

    results.sort(key=lambda r: (r.row.institution_name, r.row.name, r.row.id))
    write_filled_csv(results, args.out)
    n_review = write_review_queue(results, args.review_out)
    n_cal = write_calibration_sample(results, args.calibration_out)
    write_coverage_report(results, health, args.report_out, fetcher.stats)

    filled = sum(1 for r in results if r.url)
    print()
    print(f"wrote {args.out} ({len(results)} rows, {filled} filled "
          f"= {100 * filled / max(1, len(results)):.1f}%)")
    print(f"wrote {args.review_out} ({n_review} rows needing review)")
    print(f"wrote {args.calibration_out} ({n_cal} rows to label)")
    print(f"wrote {args.report_out}")
    print(f"fetch: {fetcher.stats.requests} requests, "
          f"{fetcher.stats.cache_hits} cache hits, {fetcher.stats.errors} errors, "
          f"{fetcher.stats.blocked_by_robots} robots-blocked")
    print(f"elapsed {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
