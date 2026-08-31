"""Output writers: the filled CSV, the Review Queue, and the coverage report."""

from __future__ import annotations

import csv
import os
import random
from collections import Counter, defaultdict

from pipeline.load import INPUT_COLUMNS
from pipeline.match import MatchResult

OUTPUT_COLUMNS = INPUT_COLUMNS + ["match_margin", "live_page_score",
                                  "match_evidence", "row_flags"]

REVIEW_COLUMNS = [
    "id", "institution_name", "name", "matched_status", "matched_score",
    "match_margin", "course_url", "matched_candidate_name",
    "runner_up_score", "runner_up_url", "runner_up_name", "row_flags",
    "prior_note",
]


def _status_for_csv(res: MatchResult) -> str:
    """Map the internal status onto the documented `matched_status` vocabulary."""
    return {"confident": "verified"}.get(res.status, res.status)


def write_filled_csv(results: list[MatchResult], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(OUTPUT_COLUMNS)
        for res in results:
            row = res.row
            w.writerow([
                row.id, row.name, row.institution_name,
                res.url,
                row.notes,
                row.prior_web_search,
                f"{res.score:.4f}" if res.candidate else "",
                _status_for_csv(res),
                row.website,
                f"{res.margin:.4f}" if res.candidate else "",
                f"{res.live_score:.4f}" if res.live_score is not None else "",
                res.evidence,
                ";".join(row.flags + res.flags),
            ])


def write_review_queue(results: list[MatchResult], path: str) -> None:
    """Only the rows a human needs to look at, worst Margin first."""
    queue = [r for r in results if r.needs_review]
    queue.sort(key=lambda r: (r.margin, -r.score))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(REVIEW_COLUMNS)
        for r in queue:
            w.writerow([
                r.row.id, r.row.institution_name, r.row.name,
                _status_for_csv(r), f"{r.score:.4f}", f"{r.margin:.4f}",
                r.url, r.candidate.name if r.candidate else "",
                f"{r.runner_up_score:.4f}" if r.runner_up else "",
                r.runner_up.url if r.runner_up else "",
                r.runner_up.name if r.runner_up else "",
                ";".join(r.row.flags + r.flags),
                r.row.notes or r.row.prior_web_search,
            ])
    return len(queue)


def write_calibration_sample(results: list[MatchResult], path: str,
                             size: int = 150, seed: int = 20260828) -> int:
    """A stratified sample for one-time human labelling.

    Thresholds cannot be fitted from the sheet itself: it contains only four
    usable ground-truth URLs. This sample is the bounded human cost that turns
    `matched_score` from an ordering signal into a calibrated confidence.
    """
    rng = random.Random(seed)
    buckets: dict[str, list[MatchResult]] = defaultdict(list)
    for r in results:
        if r.score >= 0.95:
            buckets["0.95-1.00"].append(r)
        elif r.score >= 0.85:
            buckets["0.85-0.95"].append(r)
        elif r.score >= 0.75:
            buckets["0.75-0.85"].append(r)
        elif r.score >= 0.65:
            buckets["0.65-0.75"].append(r)
        elif r.score >= 0.55:
            buckets["0.55-0.65"].append(r)
        elif r.score > 0:
            buckets["below-floor"].append(r)
    per = max(1, size // max(1, len(buckets)))
    chosen: list[MatchResult] = []
    for band, items in sorted(buckets.items()):
        rng.shuffle(items)
        chosen.extend(items[:per])
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["score_band", "id", "institution_name", "name",
                    "course_url", "matched_candidate_name", "matched_score",
                    "match_margin", "matched_status",
                    "HUMAN_VERDICT_correct_yes_no"])
        for r in chosen:
            band = ("0.95-1.00" if r.score >= 0.95 else
                    "0.85-0.95" if r.score >= 0.85 else
                    "0.75-0.85" if r.score >= 0.75 else
                    "0.65-0.75" if r.score >= 0.65 else
                    "0.55-0.65" if r.score >= 0.55 else "below-floor")
            w.writerow([band, r.row.id, r.row.institution_name, r.row.name,
                        r.url, r.candidate.name if r.candidate else "",
                        f"{r.score:.4f}", f"{r.margin:.4f}",
                        _status_for_csv(r), ""])
    return len(chosen)


def write_coverage_report(results: list[MatchResult], catalog_health: dict,
                          path: str, fetch_stats=None) -> None:
    """The input to the Phase 2 spend decision (ADR-0003)."""
    total = len(results)
    status = Counter(_status_for_csv(r) for r in results)
    filled = sum(1 for r in results if r.url)
    flags = Counter(f for r in results for f in (r.row.flags + r.flags))

    by_inst: dict[str, list[MatchResult]] = defaultdict(list)
    for r in results:
        by_inst[r.row.institution_name].append(r)

    no_catalog_rows = sum(len(v) for k, v in by_inst.items()
                          if not catalog_health.get(k, {}).get("healthy", False))
    disagree = [r for r in results if r.url and r.row.prior_said_not_found]

    lines: list[str] = []
    a = lines.append
    a("# Coverage report")
    a("")
    a("Generated by `run.py`. This is the input to the Phase 2 spend decision "
      "recorded in [ADR-0003](docs/adr/0003-free-deterministic-phase-one.md).")
    a("")
    a("## Headline")
    a("")
    a(f"- Course Rows processed: **{total}**")
    a(f"- `course_url` filled: **{filled}** ({100 * filled / max(1, total):.1f}%)")
    a(f"- Rows in Institutions whose Catalog failed Extraction Health: "
      f"**{no_catalog_rows}** ({100 * no_catalog_rows / max(1, total):.1f}%)")
    a(f"- Rows a previous manual pass called `not found` where a URL was "
      f"found: **{len(disagree)}**")
    starved = sum(1 for r in results
                  if "url_claimed_by_stronger_match" in r.flags)
    a(f"- Rows left blank because their best URL went to a stronger claim: "
      f"**{starved}** — the uniqueness rail of ADR-0002 doing its job. These "
      f"are Course Rows the Institution does not list as separate pages "
      f"(variants such as `X`, `X (Top-Up)`, `X with foundation year`), so a "
      f"search API will not resolve them either.")
    a("")
    a("## Status distribution")
    a("")
    a("| status | rows | share |")
    a("|---|---|---|")
    for k in ("verified", "probable", "ambiguous", "no_match", "no_catalog",
              "url_dead"):
        v = status.get(k, 0)
        a(f"| `{k}` | {v} | {100 * v / max(1, total):.1f}% |")
    a("")
    a("## Row flags")
    a("")
    if flags:
        a("| flag | rows |")
        a("|---|---|")
        for k, v in flags.most_common():
            a(f"| `{k}` | {v} |")
    else:
        a("None raised.")
    a("")
    a("## Extraction Health by Institution")
    a("")
    a("Institutions are the unit of work: the largest 30 carry over half the "
      "rows, so a single failed Catalog is expensive. Sorted by rows at stake.")
    a("")
    a("Candidate count is only a *proxy* for a usable Catalog, so this table "
      "reports the diagnosis alongside it. The distinction that matters is "
      "whether more crawling could help: an Institution that **refuses** the "
      "crawler cannot be fixed by tuning the crawler, and grouping it with "
      "Institutions whose crawl merely landed on the wrong pages sends the "
      "next person after an unwinnable target.")
    a("")
    a("| institution | rows | candidates | strategy | healthy | filled | fill % | diagnosis |")
    a("|---|---|---|---|---|---|---|---|")
    ranked = sorted(by_inst.items(), key=lambda kv: -len(kv[1]))
    crawlable: list[tuple[str, int, float, str]] = []
    unreachable: list[tuple[str, int, float, str]] = []
    for inst, rs in ranked:
        h = catalog_health.get(inst, {})
        f_ = sum(1 for r in rs if r.url)
        pct = 100 * f_ / max(1, len(rs))
        reason = h.get("failure_reason", "")
        if reason == "blocked":
            diagnosis = "**BLOCKED**"
            unreachable.append((inst, len(rs), pct, reason))
        elif reason == "no_website":
            diagnosis = "**NO WEBSITE**"
            unreachable.append((inst, len(rs), pct, reason))
        elif reason == "no_hub":
            diagnosis = "**NO HUB**"
            crawlable.append((inst, len(rs), pct, reason))
        elif reason == "no_candidates":
            diagnosis = "**NO CANDIDATES**"
            crawlable.append((inst, len(rs), pct, reason))
        elif reason == "thin":
            diagnosis = "**THIN**"
            crawlable.append((inst, len(rs), pct, reason))
        elif not h.get("healthy"):
            diagnosis = "no catalog"
        elif pct < 20:
            diagnosis = "**MISTARGETED**"
            crawlable.append((inst, len(rs), pct, "mistargeted"))
        elif pct < 50:
            diagnosis = "partial"
        else:
            diagnosis = "ok"
        a(f"| {inst} | {len(rs)} | {h.get('candidates', 0)} | "
          f"{h.get('strategy', '-')} | {'yes' if h.get('healthy') else 'NO'} | "
          f"{f_} | {pct:.0f}% | {diagnosis} |")
    a("")

    _REASON_TEXT = {
        "mistargeted": "Catalog passed the count check but the crawl reached "
                       "the wrong pages",
        "no_candidates": "hub reachable but nothing extractable — most often a "
                         "JavaScript course finder",
        "thin": "real listings found, but too few of them",
        "no_hub": "site reachable, but no course listing root was located",
        "blocked": "site refuses the crawler",
        "no_website": "no usable website value in the input",
    }

    if crawlable:
        a("### Fixable by crawling")
        a("")
        a("These are the cheapest coverage available: the Catalog, not the "
          "matcher, is what fell short, and no purchase is needed to improve "
          "them.")
        a("")
        for inst, n, pct, reason in sorted(crawlable, key=lambda t: -t[1]):
            why = _REASON_TEXT.get(reason, reason)
            a(f"- **{inst}** — {n} rows, {pct:.0f}% filled. "
              f"{why[:1].upper()}{why[1:]}.")
        a("")

    if unreachable:
        a("### Not fixable by crawling")
        a("")
        a("Crawler tuning cannot help here. A site returning 403 to a "
          "self-identifying crawler is declining automated access; how to "
          "respond is a policy decision — an agreement with the institution, "
          "an official feed, or a different access posture — not a bug fix. "
          "**Do not count these rows as recoverable by crawling.**")
        a("")
        for inst, n, pct, reason in sorted(unreachable, key=lambda t: -t[1]):
            why = _REASON_TEXT.get(reason, reason)
            a(f"- **{inst}** — {n} rows, {pct:.0f}% filled. "
              f"{why[:1].upper()}{why[1:]}.")
        a("")

    # Seeds that produced nothing. This is what exposes a useless probe --
    # a library catalogue, a login shell, a JS app -- as evidence rather than
    # as a guess about what kind of page it was.
    dead_seeds: list[tuple[str, str]] = []
    for inst, _rs in ranked:
        for seed, n in sorted(catalog_health.get(inst, {})
                              .get("seed_yield", {}).items()):
            if n == 0:
                dead_seeds.append((inst, seed))
    if dead_seeds:
        a("### Seeds that yielded nothing")
        a("")
        a("Each of these responded and was crawled but produced no Candidates. "
          "Recurring hosts here indicate a probe worth narrowing; a plausible "
          "course host here indicates a listing shape the crawler cannot read.")
        a("")
        a("| institution | seed |")
        a("|---|---|")
        for inst, seed in dead_seeds:
            a(f"| {inst} | `{seed}` |")
        a("")

    a("## Phase 2 decision")
    a("")
    unresolved = total - filled
    a(f"- **{unresolved}** rows remain unfilled.")
    a(f"- Of those, **{no_catalog_rows}** are in Institutions with no "
      f"machine-readable Catalog — these are what a paid search fallback would "
      f"address.")
    a(f"- **{status.get('ambiguous', 0)}** rows are Ambiguous — these are what "
      f"LLM adjudication or human triage would address.")
    if fetch_stats is not None:
        a("")
        a(f"Fetch: {fetch_stats.requests} requests, "
          f"{fetch_stats.cache_hits} cache hits, {fetch_stats.errors} errors, "
          f"{fetch_stats.blocked_by_robots} blocked by robots.txt.")
    a("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
