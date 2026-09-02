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
import logging
import time
import urllib.parse

from pipeline.catalog import (SCHEMA_VERSION, Catalog, build_catalog,
                              page_heading, page_title)
from pipeline.fetch import Fetcher, registrable
from pipeline.logging_setup import set_current_site, setup_logging
from pipeline.store import DEFAULT_DB, Store, new_run_id
from pipeline.load import (DEFAULT_INPUT, dedupe, group_by_site, load_rows,
                           normalise_website, site_display_name)
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


def catalog_path(site_key: str) -> str:
    """Where this Institution's cached Catalog lives.

    Relative to the working directory, so `catalogs/` is created wherever
    `run.py` is invoked from.
    """
    return os.path.join(CATALOG_DIR, f"{slugify(site_key)[:120]}.json")


def load_or_build_catalog(fetcher: Fetcher, institution: str, website: str,
                          expected_rows: int, refresh: bool = False,
                          prior_urls: list[str] | None = None) -> Catalog:
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
    cat = build_catalog(fetcher, institution, website, expected_rows,
                        prior_urls=prior_urls)
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
    logging.getLogger("pipeline.verify").debug(
        f"verify {live:.3f} {res.url}",
        extra={"event": "verify.row", "course_id": res.row.id,
               "url": res.url, "live_score": round(live, 4),
               "heading": heading[:120], "status_before": res.status})
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


def process_site(fetcher: Fetcher, site_key: str, rows: list,
                 th: Thresholds, args) -> tuple[list[MatchResult], dict]:
    """Resolve one **site** end to end. The unit of parallelism.

    The bucket is a website host rather than an Institution: 165 hosts are
    shared by 455 Institutions covering 12,927 rows, so per-Institution
    buckets would crawl one site many times and split its Candidates between
    the copies.

    Builds or loads the Catalog, then either fails closed or assigns. Two
    things here are load-bearing and easy to break:

    Failing closed. An unhealthy Catalog yields `no_catalog` for every row and
    no URLs at all, rather than matching against a fragment (ADR-0001).

    Deduping *before* Assignment. It no longer prevents starvation — Variant
    Siblings share a URL now — but it still saves scoring cycles, which matters
    at 52,781 rows.

    Returns (results, health) where health feeds the coverage report.
    """
    # Every record emitted from any module on this thread now carries
    # site_key, without threading it through a dozen signatures.
    set_current_site(site_key)
    institution = site_display_name(rows)
    # The website most rows actually point at, not whichever row happened to
    # come first. A bucket spans hosts: 535 of Newcastle's 545 rows name
    # www.newcastle.edu.au and 10 name its International College, and taking
    # the first row picked the College — so hub probing went to a satellite,
    # and the refusal on the host 98% of the rows belong to was never seen.
    site_votes: dict[str, int] = {}
    for r in rows:
        w = normalise_website(r.website)
        if w:
            site_votes[w] = site_votes.get(w, 0) + 1
    website = max(sorted(site_votes), key=lambda w: site_votes[w]) \
        if site_votes else ""

    # Distinct prior URLs for this site, most-repeated first: a URL several
    # rows already point at is more likely to be a real course page.
    counts: dict[str, int] = {}
    for r in rows:
        for u in r.prior_urls:
            counts[u] = counts.get(u, 0) + 1
    prior = sorted(counts, key=lambda u: (-counts[u], u))

    cat = load_or_build_catalog(fetcher, site_key, website, len(rows),
                                refresh=args.refresh_catalogs,
                                prior_urls=prior)
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
    ap.add_argument("--input", default=DEFAULT_INPUT,
                    help="course sheet to read; the legacy "
                         "processed_courses.csv is still accepted")
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
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="SQLite file for run state and URL history; "
                         "'' disables the store")
    ap.add_argument("--log-dir", default="logs",
                    help="directory for per-run JSONL logs")
    ap.add_argument("--verbose", action="store_true",
                    help="log per-page and per-row detail; ~175MB over a full "
                         "run, so prefer it on a single site")
    ap.add_argument("--keep-logs", type=int, default=20,
                    help="previous runs of logs to retain; 0 keeps everything")
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

    run_id = new_run_id()
    log_path = setup_logging(run_id, args.log_dir, verbose=args.verbose,
                             quiet=args.dry_run, keep_runs=args.keep_logs)
    log = logging.getLogger("run")

    rows = load_rows(args.input)
    by_site = group_by_site(rows)
    # Rows with no usable website cannot be crawled at all; they are reported
    # rather than silently dropped.
    no_site = by_site.pop("", [])
    # Rank by rows that are plausibly courses, not raw row count. The largest
    # bucket in the sheet is 5,083 ANZSCO visa occupation codes on a government
    # site, which no crawl can resolve; ranking on it would spend the first
    # --limit slot on guaranteed waste. Nothing is skipped — a full run still
    # covers every bucket.
    NON_COURSE = {"occupation_code_not_course", "year_level_not_course"}

    def course_rows(rs):
        """Rows in a bucket that are plausibly courses at all."""
        return sum(1 for r in rs if not (NON_COURSE & set(r.flags)))

    ranked = sorted(by_site.items(),
                    key=lambda kv: (-course_rows(kv[1]), -len(kv[1]), kv[0]))

    if args.institution:
        # Match on Institution name for convenience, but still process whole
        # site buckets, since that is what a Catalog covers.
        wanted = {w.lower() for w in args.institution}
        ranked = [(k, v) for k, v in ranked
                  if any(r.institution_name.lower() in wanted for r in v)]
        if not ranked:
            print(f"No Institution matched {args.institution!r}. Try one of:",
                  file=sys.stderr)
            for k, v in sorted(by_site.items(), key=lambda kv: -len(kv[1]))[:10]:
                print(f"  {len(v):5d}  {site_display_name(v)}", file=sys.stderr)
            return 2
    if args.limit:
        ranked = ranked[:args.limit]

    planned_rows = sum(len(v) for _, v in ranked)
    print(f"{len(ranked)} sites, {planned_rows} course rows "
          f"({len(dedupe([r for _, v in ranked for r in v]))} unique work items)"
          + (f"; {len(no_site)} rows have no usable website" if no_site else ""))
    if args.dry_run:
        for site_key, rs in ranked[:40]:
            site = normalise_website(rs[0].website) or "(no usable website)"
            cached = "cached" if os.path.exists(catalog_path(site_key)) else "-"
            label = site_display_name(rs)
            print(f"  {len(rs):5d}  {label[:40]:42s} {site[:40]:42s} {cached}")
        if len(ranked) > 40:
            print(f"  ... and {len(ranked) - 40} more")
        return 0

    store = Store(args.db) if args.db else None
    if store:
        store.start_run(run_id, args.input, vars(args))
    log.info(f"run {run_id}: {len(ranked)} sites, {planned_rows} rows",
             extra={"run_id": run_id, "sites": len(ranked),
                    "rows": planned_rows, "log_file": log_path,
                    "db": args.db or None})

    fetcher = Fetcher(delay=args.delay, offline=args.offline)
    results: list[MatchResult] = []
    health: dict[str, dict] = {}
    started = time.time()
    done = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_site, fetcher, sk, rs, th, args):
                   sk for sk, rs in ranked}
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
            label = site_display_name([r.row for r in res]) if res else inst
            log.info(
                f"  [{done}/{len(ranked)}] {label[:42]:44s} "
                f"cand={h['candidates']:5d} {h['strategy']:16s} "
                f"{'ok ' if h['healthy'] else 'NO '} filled={filled}/{len(res)}",
                extra={"run_id": run_id, "site_key": inst,
                       "institution": label,
                       "candidates": h.get("candidates"),
                       "strategy": h.get("strategy"),
                       "healthy": bool(h.get("healthy")),
                       # `failure_reason`, not `diagnosis`: process_site
                       # writes the former and report.py reads the former, so
                       # the log used a key that never existed and every record
                       # carried a null. It is the field you grep to answer
                       # "why did these sites fail".
                       "failure_reason": h.get("failure_reason"),
                       "rows": len(res), "filled": filled})
            if store:
                website = next((normalise_website(r.row.website) for r in res
                                if normalise_website(r.row.website)), "")
                store.record_site(run_id, inst, label, website, h)

    if not args.no_verify:
        targets = [r for r in results
                   if r.url and "shared_with_duplicate_row" not in r.flags]
        targets = interleave_by_domain(targets)
        log.info(f"verifying {len(targets)} assigned URLs "
                 f"across {len({registrable(urllib.parse.urlsplit(r.url).netloc) for r in targets})} domains",
                 extra={"run_id": run_id, "targets": len(targets)})
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

    drifted = 0
    if store:
        drifted = store.record_results(run_id, results)
        store.finish_run(run_id, len(results),
                         sum(1 for r in results if r.url))

    write_filled_csv(results, args.out)
    n_review = write_review_queue(results, args.review_out)
    n_cal = write_calibration_sample(results, args.calibration_out)
    write_coverage_report(results, health, args.report_out, fetcher.stats)

    filled = sum(1 for r in results if r.url)
    shared = sum(1 for r in results
                 if any(f.startswith("variant_sibling_share") for f in r.flags))
    denied = sum(1 for r in results
                 if any(f.startswith("share_denied") for f in r.flags))
    log.info("")
    log.info(f"wrote {args.out} ({len(results)} rows, {filled} filled "
             f"= {100 * filled / max(1, len(results)):.1f}%)")
    log.info(f"wrote {args.review_out} ({n_review} rows needing review)")
    log.info(f"wrote {args.calibration_out} ({n_cal} rows to label)")
    log.info(f"wrote {args.report_out}")
    log.info(f"sharing: {shared} rows in a Share Group, "
             f"{denied} denied a non-sibling's URL")
    if store:
        log.info(f"store: {args.db} (run {run_id}; {drifted} URLs changed "
                 f"since the previous run)")
    log.info(f"logs: {log_path} (structured) and "
             f"{log_path[:-len('.jsonl')]}.log (readable)")
    log.info(f"fetch: {fetcher.stats.requests} requests, "
             f"{fetcher.stats.cache_hits} cache hits, "
             f"{fetcher.stats.errors} errors, "
             f"{fetcher.stats.blocked_by_robots} robots-blocked")
    log.info(f"elapsed {time.time() - started:.0f}s",
             extra={"run_id": run_id, "rows": len(results), "filled": filled,
                    "shared": shared, "share_denied": denied,
                    "drifted": drifted,
                    "elapsed_s": round(time.time() - started, 1)})
    if store:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
