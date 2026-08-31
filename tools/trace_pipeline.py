#!/usr/bin/env python3
"""Trace selected Course Rows through every pipeline stage, showing the data.

Diagnostic tool, not part of the pipeline. Reads from the fetch cache by
default so a trace is fast, free and reproducible.

    python tools/trace_pipeline.py --institution "Aberystwyth University" \
        --row "Data Science BSc (Hons)" --out docs/WALKTHROUGH.md
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import os
import sys

# Run from anywhere: the pipeline package lives one level up from tools/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.catalog import (build_catalog, classify_probes, find_seeds,
                              page_heading, page_title, url_specificity)
from pipeline.fetch import Fetcher
from pipeline.load import (dedupe, group_by_institution, load_rows,
                           normalise_website)
from pipeline.match import PREFILTER_MIN, Thresholds, assign, score_all
from pipeline.normalize import (_token_coverage, _token_overlap, award_classes,
                                award_tokens, level_of, normalize_name,
                                score as score_pair, strip_institution)
from pipeline.report import _status_for_csv

OUT = io.StringIO()


def w(line: str = "") -> None:
    OUT.write(line + "\n")


def h(level: int, text: str) -> None:
    w()
    w("#" * level + " " + text)
    w()


def table(headers: list[str], rows: list[list[str]]) -> None:
    w("| " + " | ".join(headers) + " |")
    w("|" + "|".join(["---"] * len(headers)) + "|")
    for r in rows:
        w("| " + " | ".join(str(c) for c in r) + " |")
    w()


def code(text: str, lang: str = "") -> None:
    w(f"```{lang}")
    w(text.rstrip())
    w("```")
    w()


def raw_csv_line(path: str, row_id: str) -> str:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for line in fh:
            if line.startswith(row_id):
                return line.rstrip("\n")
    return "(not found)"


# --------------------------------------------------------------------------


def stage_input(args, targets) -> None:
    h(2, "Stage 0 — Input")
    w("The raw lines as they sit in `processed_courses.csv`. Nine columns plus "
      "two vestigial empty ones left by stray commas in the header.")
    w()
    for row in targets:
        code(raw_csv_line(args.input, row.id), "text")


def stage_load(targets) -> None:
    h(2, "Stage 1 — Load")
    w("`pipeline/load.py` parses, repairs and flags. The **work key** is what "
      "collapses duplicate Course Rows before Assignment; it is deliberately "
      "conservative — only textually identical names merge.")
    w()
    table(["id", "name", "flags", "work key"],
          [[r.id[:8] + "…", r.name, ";".join(r.flags) or "—",
            f"`{r.work_key[1]}`"] for r in targets])

    h(3, "How each name is read")
    w("Normalisation strips course codes, `(Hons)`, durations and stopwords. "
      "The Award is parsed separately, because it is scored as a multiplier "
      "rather than as text — see Stage 5.")
    w()
    table(["name", "subject (awards dropped)", "award tokens", "award classes",
           "level"],
          [[r.name,
            f"`{normalize_name(r.name, drop_awards=True)}`",
            ", ".join(sorted(award_tokens(r.name))) or "—",
            ", ".join(sorted(award_classes(r.name))) or "—",
            level_of(r.name) or "—"] for r in targets])


def stage_seeds(fetcher, institution, website) -> tuple[list[str], dict]:
    h(2, "Stage 2 — Seeds")
    w("`find_seeds()` does not know where an Institution keeps its courses, so "
      "it probes 16 hub paths and 7 course subdomains and keeps whatever "
      "answers 200 — recording the URL *after* redirects.")
    w()
    seeds, notes, statuses = find_seeds(fetcher, website)
    table(["hub probe status", "count"],
          [[str(k), v] for k, v in sorted(statuses.items())])
    verdict = classify_probes(statuses)
    w(f"`classify_probes()` verdict: **{verdict or 'no access problem'}** — "
      f"this is what separates a site that refuses the crawler from one whose "
      f"paths we merely guessed wrong.")
    w()
    w(f"**{len(seeds)} seeds accepted:**")
    w()
    for s in seeds:
        w(f"- `{s}`")
    w()
    if notes:
        w("Notes recorded:")
        w()
        for n in notes:
            w(f"- {n}")
        w()
    return seeds, statuses


def stage_catalog(cat, n_rows) -> None:
    h(2, "Stage 3 — Catalog")
    w("Candidates are **extracted** from the Institution's own listing pages, "
      "never generated (ADR-0001). Each is a `(name, url, level, source)` "
      "record whose identity is its URL.")
    w()
    table(["field", "value"], [
        ["institution", cat.institution],
        ["strategy", f"`{cat.strategy}`"],
        ["pages fetched", cat.pages_fetched],
        ["candidates", len(cat.candidates)],
        ["Course Rows", n_rows],
        ["Extraction Health", "**pass**" if cat.healthy(n_rows) else "**FAIL**"],
        ["failure_reason", f"`{cat.failure_reason}`" if cat.failure_reason else "—"],
        ["domains allowed", ", ".join(f"`{d}`" for d in cat.domains)],
    ])
    h(3, "Yield per seed")
    w("A seed that produced nothing is the evidence that exposes a useless "
      "probe — a library catalogue, a login wall, a JavaScript app — without "
      "having to guess which it is.")
    w()
    table(["seed", "candidates"],
          [[f"`{k}`", v] for k, v in sorted(cat.seed_yield.items(),
                                            key=lambda kv: -kv[1])])
    h(3, "Sample of the extracted Candidates")
    table(["name", "url", "level", "source"],
          [[c.name[:52], f"`…{c.url[-46:]}`", c.level or "—", c.source]
           for c in cat.candidates[:8]])


def explain_score(row_name, cand, institution) -> list[str]:
    """Recompute the score components for display."""
    cleaned = strip_institution(cand.name, institution) or cand.name
    a = normalize_name(row_name, drop_awards=True)
    b = normalize_name(cleaned, drop_awards=True)
    if not a or not b:
        a, b = normalize_name(row_name), normalize_name(cleaned)
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    tok = _token_overlap(a, b)
    cov = _token_coverage(a, b)
    base = 0.65 * seq + 0.35 * tok
    ta, tb = a.split(), b.split()
    terse = min(len(ta), len(tb)) == 1
    if terse:
        base = max(base, 0.55 * cov + 0.45 * tok)
        extra = 0
    else:
        extra = len(set(tb) - set(ta))
    final = score_pair(row_name, cand.name, institution,
                       candidate_level=cand.level)
    spec = url_specificity(cand.url)
    return [f"`{b}`", f"{seq:.3f}", f"{tok:.3f}",
            f"{cov:.3f}" if terse else "—", str(extra) if not terse else "—",
            f"{final:.3f}", f"×{spec:.2f}", f"**{final * spec:.3f}**"]


def stage_match(targets, cat, institution, scored) -> None:
    h(2, "Stage 4 — Scoring")
    w("Every Course Row is scored against the Candidates that share at least "
      "one token with it. Similarity is computed on the **subject only** — "
      "Award agreement is applied afterwards as a multiplier, because scoring "
      "the Award as text double-counts it and inflates two different subjects "
      "that happen to share a credential.")
    w()
    w(f"Prefilter floor: pairs scoring below `{PREFILTER_MIN}` are discarded.")
    for row in targets:
        pairs = scored.get(row.id, [])
        h(3, f"`{row.name}`")
        w(f"Subject compared: `{normalize_name(row.name, drop_awards=True)}` — "
          f"{len(pairs)} Candidate(s) cleared the prefilter.")
        w()
        if not pairs:
            w("_No Candidate shared a token above the prefilter floor._")
            w()
            continue
        rows_out = []
        for s, ci in pairs[:5]:
            c = cat.candidates[ci]
            rows_out.append([c.name[:40]] + explain_score(row.name, c,
                                                          institution))
        table(["candidate", "subject", "seq", "token∩", "cover", "extra",
               "score", "url spec", "final"], rows_out)


def stage_assign(results, targets) -> None:
    h(2, "Stage 5 — Assignment")
    w("Assignment is **constrained**: within one Institution each URL may be "
      "claimed by at most one Course Row (ADR-0002). Triples are sorted by "
      "score and taken greedily, so a stronger claim wins and the loser is "
      "pushed onto its next choice or left blank.")
    w()
    by_id = {r.row.id: r for r in results}
    rows_out = []
    for row in targets:
        res = by_id[row.id]
        rows_out.append([
            row.name[:38],
            f"{res.score:.3f}" if res.candidate else "—",
            # Margin is meaningless without an assignment, and the output CSV
            # leaves it blank in that case, so match that here.
            f"{res.margin:+.3f}" if res.candidate else "—",
            f"{res.runner_up_score:.3f}" if res.runner_up else "—",
            res.status,
            ";".join(res.flags) or "—",
        ])
    table(["row", "score", "margin", "runner-up", "status", "flags"], rows_out)
    w("A **negative margin** is not a bug: it means the row was displaced and "
      "holds a second choice, and such rows sort to the top of the Review "
      "Queue.")
    w()


def stage_verify(fetcher, results, targets, th) -> None:
    h(2, "Stage 6 — Verification")
    w("The Candidate name came from a listing page's anchor text. Verification "
      "fetches the assigned URL and re-scores the row against the **live "
      "page's own** `<h1>`/`<title>`. Agreement between two independent "
      "sources is what `verified` asserts.")
    w()
    by_id = {r.row.id: r for r in results}
    rows_out = []
    for row in targets:
        res = by_id[row.id]
        if not res.url:
            rows_out.append([row.name[:34], "—", "—", "—", res.status])
            continue
        page = fetcher.get(res.url)
        if page.status != 200:
            rows_out.append([row.name[:34], f"HTTP {page.status}", "—", "—",
                             "url_dead"])
            continue
        heading = page_heading(page.text) or page_title(page.text)
        live = score_pair(row.name, heading, row.institution_name)
        res.live_score = live
        verdict = ("holds" if live >= th.confident else "weaker than listing")
        rows_out.append([row.name[:34], heading[:40], f"{live:.3f}", verdict,
                         res.status])
    table(["row", "live page heading", "live score", "verdict", "status"],
          rows_out)


def stage_output(results, targets) -> None:
    h(2, "Stage 7 — Output")
    w("The final row as written to `courses_filled.csv`.")
    w()
    by_id = {r.row.id: r for r in results}
    for row in targets:
        res = by_id[row.id]
        buf = io.StringIO()
        cw = csv.writer(buf)
        cw.writerow(["course_url", "matched_score", "matched_status",
                     "match_margin", "live_page_score", "row_flags"])
        cw.writerow([res.url, f"{res.score:.4f}" if res.candidate else "",
                     _status_for_csv(res),
                     f"{res.margin:.4f}" if res.candidate else "",
                     f"{res.live_score:.4f}" if res.live_score is not None else "",
                     ";".join(row.flags + res.flags)])
        w(f"**{row.name}**")
        code(buf.getvalue().rstrip(), "csv")

    h(3, "How confidence is expressed")
    w("Three separate numbers, deliberately not collapsed into one:")
    w()
    table(["field", "means", "why separate"], [
        ["`matched_score`", "how alike the two names are",
         "a perfect name match can still be the wrong course"],
        ["`match_margin`", "how far ahead of the runner-up",
         "high score with a thin margin is an unresolved choice, not confidence"],
        ["`live_page_score`", "agreement with the live page's own title",
         "independent of the listing anchor text that produced the Candidate"],
    ])
    th = Thresholds()
    w(f"Thresholds: confident ≥ **{th.confident}** *and* margin ≥ "
      f"**{th.min_margin}**; nothing below the floor of **{th.floor}** is "
      f"filled at all. These are reasoned, not yet fitted — see "
      f"`docs/CALIBRATION.md`.")
    w()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="processed_courses.csv")
    ap.add_argument("--institution", default="Aberystwyth University")
    ap.add_argument("--row", action="append", default=None,
                    help="exact course name to trace (repeatable)")
    ap.add_argument("--out", default="docs/WALKTHROUGH.md")
    ap.add_argument("--offline", action="store_true", default=True)
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    rows = load_rows(args.input)
    by_inst = group_by_institution(rows)
    if args.institution not in by_inst:
        print(f"no such institution: {args.institution}", file=sys.stderr)
        return 2
    inst_rows = by_inst[args.institution]
    wanted = set(args.row or [])
    targets = [r for r in inst_rows if r.name in wanted] if wanted \
        else inst_rows[:4]
    if not targets:
        print("none of the requested rows matched", file=sys.stderr)
        return 2

    fetcher = Fetcher(delay=args.delay, offline=args.offline)
    website = normalise_website(inst_rows[0].website)
    th = Thresholds()

    w(f"# Pipeline walkthrough — {args.institution}")
    w()
    w(f"Generated by `tools/trace_pipeline.py` from the fetch cache, so every "
      f"number here is from a real run rather than an illustration. "
      f"{len(inst_rows)} Course Rows belong to this Institution; "
      f"{len(targets)} are traced below, chosen to show different outcomes.")
    w()
    w(f"Institution website: `{website}`")

    stage_input(args, targets)
    stage_load(targets)
    stage_seeds(fetcher, args.institution, website)
    cat = build_catalog(fetcher, args.institution, website, len(inst_rows))
    stage_catalog(cat, len(inst_rows))

    work = dedupe(inst_rows)
    reps = [g[0] for g in work.values()]
    scored = score_all(reps, cat, args.institution)
    stage_match(targets, cat, args.institution, scored)

    results = assign(reps, cat, args.institution, th)
    stage_assign(results, targets)
    stage_verify(fetcher, results, targets, th)
    stage_output(results, targets)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(OUT.getvalue())
    print(f"wrote {args.out} ({len(OUT.getvalue().splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
