#!/usr/bin/env python3
"""Fill the `course_url` column of processed_courses.csv.

`processed_courses.csv` is read-only. See README.md for usage and CONTEXT.md for
the domain vocabulary.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import dataclasses
import json
import os
import re
import sys
import tempfile
import logging
import csv
import time
import urllib.parse

from pipeline.config import DEFAULT_ENV_FILE, load_dotenv
from pipeline.catalog import (PARSER_TIER, SCHEMA_VERSION, Catalog,
                              build_catalog, describe_parser_tier,
                              page_heading, page_title)
from pipeline.fetch import Fetcher, registrable
from pipeline.logging_setup import set_current_site, setup_logging
from pipeline.manual import (DEFAULT_MANUAL_FILE, apply_manual_urls,
                             load_manual_urls)
from pipeline.search import (SEARCH_FOUND, SERPER_DELAY, FixtureProvider,
                            NullProvider, SearchProviderError, SerperProvider,
                            search_rows, searchable_rows)
from pipeline.statuses import PHASE_2_STATUSES
from pipeline.store import DEFAULT_DB, Store, new_run_id
from pipeline.triage import ShareIndex, add_flag, triage_rows
from pipeline.load import (DEFAULT_INPUT, dedupe, group_by_site,
                           load_rows,
                           normalise_website, site_display_name)
from pipeline.match import FLOOR, MatchResult, Thresholds, assign
from pipeline.normalize import score as score_pair
from pipeline.report import (DEFAULT_OUT_DIR, PHASE_1_COLUMNS, phase_for,
                             write_calibration_sample, write_coverage_report,
                             write_filled_csv, write_review_queue)

CATALOG_DIR = "catalogs"


CHUNK_RE = re.compile(r"\.(\d{3})\.csv$")


def chunk_suffix(input_path: str) -> str:
    """`.003` when reading `chunks/final_courses.003.csv`, else "".

    Chunked runs must not overwrite each other's outputs, and deriving the
    suffix from the input name means the caller does not have to remember to
    pass four `--*-out` paths per chunk.
    """
    m = CHUNK_RE.search(os.path.basename(input_path or ""))
    return f".{m.group(1)}" if m else ""


def check_chunk_freshness(input_path: str) -> None:
    """Warn when a chunk no longer matches the sheet it was cut from.

    This is the cost of physical chunk files: nothing stops `final_courses.csv`
    being replaced while stale chunks sit on disk. The manifest records the
    source checksum so the divergence is at least loud.
    """
    manifest = os.path.join(os.path.dirname(input_path), "manifest.json")
    if not os.path.exists(manifest):
        return
    try:
        with open(manifest, encoding="utf-8") as fh:
            data = json.load(fh)
        source = data.get("source")
        recorded = data.get("source_sha256")
        if not source or not recorded or not os.path.exists(source):
            return
        import hashlib
        h = hashlib.sha256()
        with open(source, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != recorded:
            print(f"WARNING: {input_path} was cut from a different version of "
                  f"{source}. Re-run tools/split_by_site.py before trusting "
                  f"these results.", file=sys.stderr)
    except (OSError, ValueError):
        return


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


def write_rows_atomically(rows, path: str, backup: bool = True) -> None:
    """Write *rows* to *path* via a temporary file, then swap it in.

    Phase 2 writes the file it just read, so a crash mid-write would truncate
    the only result there is. Same pattern as
    `tools/backfill_provenance.py`: write beside the target, `os.replace` it
    into position -- atomic on the same filesystem -- and keep the previous
    version as `.bak` so a bad run is one `mv` from undone.
    """
    fields = list(rows[0].keys()) if rows else []
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        if backup and os.path.exists(path):
            os.replace(path, path + ".bak")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def verify_search_rows(rows, fetcher, log, run_id: str) -> dict:
    """Fetch each search-adopted URL and score the live page against the name.

    The same independent evidence phase 1's `verify` collects: the Candidate
    name there comes from a listing anchor, here from a search result, and in
    both cases the page's own `<h1>`/`<title>` is the check. Costs no API money
    -- only the per-domain politeness delay.

    **A failed fetch is recorded, not treated as a wrong answer.** These rows
    are on Sites extraction could not read, and on a `blocked` Site a 403 says
    nothing about whether the URL is right. Dropping them would discard
    possibly-correct answers and would repeat the conflation `fd94963` fixed
    for extraction diagnosis, so the URL and its `search_found` status stay and
    the outcome is reported instead. What to deliver is then a decision made
    with the numbers rather than baked in here.
    """
    counts = collections.Counter()
    for r in rows:
        url = (r.get("course_url") or "").strip()
        if not url:
            continue
        page = fetcher.get(url)
        if page.status != 200 or not page.text:
            counts[f"http_{page.status}"] += 1
            add_flag(r, f"search_verify_http_{page.status}")
            continue
        heading = page_heading(page.text) or page_title(page.text)
        live = score_pair(r.get("name", ""), heading,
                          r.get("institution_name", ""))
        r["live_page_score"] = f"{live:.4f}"
        counts["fetched"] += 1
        counts["live_ge_floor" if live >= FLOOR else "live_below_floor"] += 1
        log.debug(f"verify-search {live:.3f} {url}",
                  extra={"event": "search.verify", "run_id": run_id,
                         "course_id": r["id"], "url": url,
                         "live_score": round(live, 4),
                         "heading": heading[:120]})
    return counts


def build_provider(args):
    """The search provider this run should use.

    A fixture wins if given, so the offline path stays available even with a
    vendor selected. `null` is the default, so no run spends money unless it
    was asked to by name.
    """
    if args.search_fixture:
        return FixtureProvider(path=args.search_fixture)
    if args.search_provider == "serper":
        return SerperProvider(limit=args.search_limit,
                              delay=args.search_delay)
    return NullProvider()


def run_phase2(args, run_id: str, log) -> int:
    """Prior-URL triage, then search, over an existing phase 1 result file.

    Reads and writes CSV rows as plain dicts rather than reconstructing
    MatchResult objects: phase 2 makes decisions about *two URLs*, and nothing
    it does needs the Candidate graph that phase 1 built.

    No network is required for triage. Search issues requests only if a
    provider is configured; the default returns nothing, so the stage is
    inert rather than broken when no vendor has been chosen.
    """
    # Built before anything else: a misconfigured vendor should fail in the
    # first second, not after triage has worked through 52,703 rows.
    try:
        provider = build_provider(args)
    except SearchProviderError as e:
        log.error(f"search: {e}")
        return 2

    with open(args.input, encoding="utf-8-sig", newline="") as fh:
        source = {r["id"]: r for r in csv.DictReader(fh)}
    with open(args.results, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    # Capture what extraction decided, before triage or search can change it.
    # A file phase 1 wrote already carries these; one written before the
    # columns existed does not, and there the delivered URL *is* phase 1's
    # answer because nothing has touched it yet. Back-filling here rather than
    # asking for a re-crawl is what lets an existing result file become the
    # single result file.
    # Keyed on the column being *absent*, never on it being empty. An empty
    # `phase1_course_url` in a file that has the column is meaningful -- it
    # says extraction found nothing -- and treating that as "needs filling"
    # overwrote it with whatever triage had adopted, which destroyed the
    # distinction and cost idempotency: the second run then saw its own
    # adoptions as phase 1's work and stopped re-deriving them.
    backfilled = 0
    for r in rows:
        if "phase1_course_url" not in r:
            backfilled += 1
            # A row already decided by phase 2 is the one case where the
            # delivered URL is *not* extraction's answer, and this file never
            # recorded what was. Left blank rather than guessed: claiming a
            # carried-over or search-found URL came from extraction would be
            # a lie the rest of the pipeline then trusts.
            if (r.get("matched_status") or "").strip() in PHASE_2_STATUSES:
                r["phase1_course_url"] = ""
                r["phase1_matched_status"] = ""
            else:
                r["phase1_course_url"] = (r.get("course_url") or "").strip()
                r["phase1_matched_status"] = (
                    r.get("matched_status") or "").strip()
    if backfilled:
        log.info(f"captured phase 1's answer for {backfilled} rows "
                 f"({', '.join(PHASE_1_COLUMNS)})",
                 extra={"run_id": run_id, "phase1_backfilled": backfilled})

    before = sum(1 for r in rows if (r.get("course_url") or "").strip())
    log.info(f"phase 2 over {len(rows)} rows from {args.results} "
             f"({before} filled)",
             extra={"run_id": run_id, "rows": len(rows), "filled": before})

    store = Store(args.db) if args.db else None
    if store:
        store.start_run(run_id, args.input, vars(args))
        seeded = store.seed_baseline(source.values())
        log.info(f"url_history: seeded {seeded} baseline rows from the sheet",
                 extra={"run_id": run_id, "seeded": seeded})

    # One index, shared by both stages: a URL triage carries over is a holder
    # by the time search runs, so the two cannot hand one page to two courses.
    index = ShareIndex(rows)

    stats = triage_rows(rows, source, index=index)
    log.info(f"triage: adopted {stats.adopted} prior URLs "
             f"({stats.adopted_blank} blank, {stats.adopted_dead} dead, "
             f"{stats.adopted_weak} weak); rejected {stats.rejected_by_gate} "
             f"by quality gate and {stats.rejected_by_sharing} by the sharing "
             f"rule",
             extra={"run_id": run_id, "adopted": stats.adopted,
                    "rejected_gate": stats.rejected_by_gate,
                    "rejected_sharing": stats.rejected_by_sharing})

    only_ids = None
    if args.search_ids:
        with open(args.search_ids, encoding="utf-8") as fh:
            only_ids = {line.strip() for line in fh if line.strip()}
        log.info(f"search: restricted to {len(only_ids)} ids from "
                 f"{args.search_ids}",
                 extra={"run_id": run_id, "search_batch": args.search_ids,
                        "search_batch_size": len(only_ids)})

    eligible = len(searchable_rows(rows, only_ids))
    # A broken vendor must not cost us triage's work: the stage stops itself,
    # the run says so, and the file is still written with what Stage 1 decided.
    ss = search_rows(rows, provider, index, only_ids=only_ids)
    aborted = ""
    if ss.aborted:
        aborted = f"; ABORTED: {ss.aborted}"
        log.error(f"search: {ss.aborted}")

    detail = provider.report() if hasattr(provider, "report") else ""
    log.info(f"search: {eligible} rows eligible, {ss.adopted} adopted from "
             f"{type(provider).__name__} (rejected {ss.rejected_off_site} "
             f"off-site, {ss.rejected_by_gate} by quality gate, "
             f"{ss.rejected_by_sharing} by the sharing rule; "
             f"{ss.no_results} returned nothing)"
             + (f" [{detail}]" if detail else "")
             + ("" if args.search_fixture or args.search_provider != "null"
                else " — no vendor configured, see docs/PHASE-2.md")
             + aborted,
             extra={"run_id": run_id, "search_targets": eligible,
                    "search_adopted": ss.adopted,
                    "search_off_site": ss.rejected_off_site,
                    "search_rejected_gate": ss.rejected_by_gate,
                    "search_rejected_sharing": ss.rejected_by_sharing,
                    "provider": type(provider).__name__,
                    "search_aborted": bool(aborted)})

    if args.verify_search and ss.adopted:
        adopted = [r for r in rows if r.get("matched_status") == SEARCH_FOUND]
        fetcher = Fetcher(delay=args.delay, offline=args.offline)
        vc = verify_search_rows(adopted, fetcher, log, run_id)
        log.info(f"verify-search: {vc['fetched']} of {len(adopted)} adopted "
                 f"URLs fetched 200 "
                 f"({vc['live_ge_floor']} scored >= {FLOOR} against the "
                 f"course name, {vc['live_below_floor']} below); "
                 + ", ".join(f"{n}x {k}" for k, n in sorted(vc.items())
                             if k.startswith("http_")),
                 extra={"run_id": run_id, **{f"verify_{k}": v
                                             for k, v in vc.items()}})

    # Applied last, so a person's decision wins over both automated stages.
    manual = load_manual_urls(args.manual_urls)
    if manual:
        ms = apply_manual_urls(rows, manual, index)
        log.info(f"manual: {ms.applied} of {len(manual)} decisions applied "
                 f"from {args.manual_urls}"
                 + (f"; {ms.shared_pages} share a page with another course"
                    if ms.shared_pages else "")
                 + (f"; {ms.off_domain} off the institution's domain"
                    if ms.off_domain else ""),
                 extra={"run_id": run_id, "manual_applied": ms.applied,
                        "manual_shared": ms.shared_pages,
                        "manual_off_domain": ms.off_domain})
        # Named, not counted: a mistyped id that silently does nothing is the
        # failure most likely to waste someone's afternoon.
        for cid in ms.unknown_ids:
            log.warning(f"manual: no row has id {cid!r} — decision ignored",
                        extra={"run_id": run_id, "manual_unknown_id": cid})
        for bad in ms.bad_urls:
            log.warning(f"manual: not an http URL — {bad}",
                        extra={"run_id": run_id})

    # Counted once every stage has run, so the headline figure and
    # `finish_run` cover triage, search and the manual overlay alike. Counting
    # it earlier undercounted by exactly the number of manual decisions.
    after = sum(1 for r in rows if (r.get("course_url") or "").strip())

    # Recomputed here rather than inherited: triage and search have changed
    # statuses since phase 1 wrote the column, and recomputing also back-fills
    # it for a result file produced before the column existed. One derivation
    # point means the column cannot disagree with `matched_status`.
    for r in rows:
        r["phase"] = phase_for(r.get("matched_status", ""),
                               r.get("course_url", ""))

    write_rows_atomically(rows, args.out, backup=not args.no_backup)

    drifted = 0
    if store:
        drifted = store.record_url_rows(run_id, rows)
        store.finish_run(run_id, len(rows), after)
        log.info(f"url_history: {drifted} courses now hold a different URL "
                 f"than the sheet delivered",
                 extra={"run_id": run_id, "drifted": drifted})
        store.close()

    log.info("")
    log.info(f"wrote {args.out} ({len(rows)} rows, {after} filled "
             f"= {100 * after / max(1, len(rows)):.1f}%, was "
             f"{100 * before / max(1, len(rows)):.1f}%)")
    # Recounted from the rows, not taken from `TriageStats`: that counter is a
    # snapshot from before search ran, so reporting it hid every row search
    # filled -- a 500-row trial printed triage's tally unchanged while the file
    # correctly recorded 97 rows moving none -> added and 34 dropped -> changed.
    delivered = collections.Counter(
        (r.get("url_change") or "").strip() for r in rows)
    for k, v in delivered.most_common():
        log.info(f"  url_change {k:10s} {v:6d}")

    # The output is written and valid either way — triage's work is not thrown
    # away because a vendor broke — but a scripted run has to be able to tell
    # a clean stage 2 from one that died having filled nothing.
    return 3 if ss.aborted else 0


def resolve_output_paths(args, chunk_tag: str = "") -> None:
    """Fill in any unset output path, and create the directories they need.

    Unset paths land in `--out-dir`, so a run leaves nothing in the repo root:
    a chunked run writes four files per chunk, and 87 of them once accumulated
    alongside the source and the one tracked input.

    A path given explicitly is honoured *exactly* as given and never rooted
    under `--out-dir` — a caller who names a file gets that file. Directories
    are created for whichever paths result, explicit ones included, so a long
    run cannot fail at its final write for a missing directory.

    **Phase 2 defaults to a different primary output.** It reads a phase 1
    result file, so sharing phase 1's default name would make a bare
    `--phase 2` read and overwrite the same file — destroying the input it was
    given. The other three defaults are set but unused: phase 2 writes only
    `--out`.
    """
    # Phase 2 updates the result file in place, so there is one result rather
    # than two files of identical shape where nothing says which to open. The
    # baseline it used to protect now lives in the row itself, in
    # `phase1_course_url` -- see docs/adr/0009.
    if getattr(args, "phase", 1) == 2 and not getattr(args, "out", None):
        args.out = args.results
    defaults = {
        "out": f"courses_filled{chunk_tag}.csv",
        "review_out": f"review_queue{chunk_tag}.csv",
        "report_out": f"coverage_report{chunk_tag}.md",
        "calibration_out": f"calibration_sample{chunk_tag}.csv",
    }
    for attr, name in defaults.items():
        if not getattr(args, attr, None):
            setattr(args, attr, os.path.join(args.out_dir, name))
        parent = os.path.dirname(os.path.abspath(getattr(args, attr)))
        os.makedirs(parent, exist_ok=True)


def build_parser() -> argparse.ArgumentParser:
    """The CLI, separated from `main` so it can be parsed in tests.

    The regression that matters: `start_run` persists `vars(args)` into
    the `runs` table, so a test has to be able to assert that no
    credential appears there.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                    help="directory for generated outputs; ignored for any "
                         "output whose path is given explicitly")
    ap.add_argument("--input", default=DEFAULT_INPUT,
                    help="course sheet to read; the legacy "
                         "processed_courses.csv is still accepted")
    # Left as None so a chunked run can suffix the defaults without
    # overriding a path the caller set explicitly.
    ap.add_argument("--out", default=None)
    ap.add_argument("--review-out", default=None)
    ap.add_argument("--report-out", default=None)
    ap.add_argument("--calibration-out", default=None)
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
    ap.add_argument("--phase", type=int, default=1, choices=(1, 2),
                    help="1 crawls and matches; 2 runs prior-URL triage and "
                         "search over an existing result file")
    ap.add_argument("--results",
                    default=os.path.join(DEFAULT_OUT_DIR,
                                         "courses_filled.csv"),
                    help="phase 2: the phase 1 output to work from")
    ap.add_argument("--search-fixture", default=None,
                    help="phase 2: JSON of canned search results; without a "
                         "vendor configured, search returns nothing")
    ap.add_argument("--search-provider", default="null",
                    choices=("null", "serper"),
                    help="phase 2: search vendor. 'serper' spends real money "
                         "and reads its key from $SERPER_API_KEY")
    ap.add_argument("--manual-urls", default=DEFAULT_MANUAL_FILE,
                    help="phase 2: CSV of human decisions (id, course_url, "
                         "note, decided_by, decided_at) applied last and "
                         "winning over triage and search")
    ap.add_argument("--no-backup", action="store_true",
                    help="phase 2: skip the .bak copy of the file being "
                         "replaced")
    ap.add_argument("--search-ids", default=None,
                    help="phase 2: file of course ids, one per line, to "
                         "restrict search to; see "
                         "tools/sample_search_targets.py")
    ap.add_argument("--verify-search", action="store_true",
                    help="phase 2: fetch each search-adopted URL and score "
                         "the live page against the course name")
    ap.add_argument("--search-limit", type=int, default=None,
                    help="phase 2: cap paid search calls for this run, so a "
                         "live vendor can be tried on part of the sheet")
    ap.add_argument("--search-delay", type=float, default=SERPER_DELAY,
                    help="phase 2: seconds between paid search calls")
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
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                    help="file of KEY=value lines to load into the "
                         "environment; a path, never a secret, so it is safe "
                         "in the persisted run args")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the planned work and exit")
    ap.add_argument("--confident", type=float, default=None)
    ap.add_argument("--floor", type=float, default=None)
    ap.add_argument("--min-margin", type=float, default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: resolve Institutions, verify, and write the outputs.

    Institutions are processed concurrently, then Verification re-fetches every
    assigned URL. That second pass is deliberately re-ordered by
    `interleave_by_domain` — grouped by Institution it parks every worker on a
    single domain lock and runs about twelve times slower.
    """
    ap = build_parser()
    args = ap.parse_args(argv)

    # Before anything reads a credential. Only names not already in the
    # environment are set, so an inline `KEY=... python3 run.py` still wins.
    # The count is logged, never the values.
    loaded = load_dotenv(args.env_file)

    th = Thresholds()
    if args.confident is not None:
        th.confident = args.confident
    if args.floor is not None:
        th.floor = args.floor
    if args.min_margin is not None:
        th.min_margin = args.min_margin

    chunk_tag = chunk_suffix(args.input)
    if chunk_tag:
        check_chunk_freshness(args.input)
    resolve_output_paths(args, chunk_tag)

    run_id = new_run_id()
    log_path = setup_logging(run_id, args.log_dir, verbose=args.verbose,
                             quiet=args.dry_run, keep_runs=args.keep_logs)
    log = logging.getLogger("run")
    if loaded:
        log.info(f"loaded {loaded} setting(s) from {args.env_file}",
                 extra={"env_file": args.env_file, "env_loaded": loaded})

    # Which extraction tier this run got. A degraded run must announce itself:
    # its coverage number is not comparable with an lxml run's.
    tier_note = describe_parser_tier()
    (log.info if PARSER_TIER == "lxml" else log.warning)(
        tier_note, extra={"parser_tier": PARSER_TIER})

    if args.phase == 2:
        return run_phase2(args, run_id, log)

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
                # Persist this site's rows now rather than at end of run, so a
                # killed run keeps what it finished. Both existing runs in the
                # store were killed and hold zero results because the only
                # write was the end-of-run one. Re-written after Verification
                # below, which is why URL history is not touched here.
                store.record_row_results(run_id, res)

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
