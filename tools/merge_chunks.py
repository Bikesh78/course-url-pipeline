#!/usr/bin/env python3
"""Combine per-chunk pipeline outputs into whole-sheet results.

Each chunked run writes `courses_filled.NNN.csv` and friends. This concatenates
them, checks the split held, and writes a combined summary.

The checks matter more than the concatenation. A course id appearing in two
chunks, or a site's rows spread across chunks, means `split_by_site.py` did not
do its job — and the symptom downstream would be two Catalogs for one site and
a sharing rule applied to half its rows at a time.

Usage
-----
    python tools/merge_chunks.py
    python tools/merge_chunks.py --chunk-dir chunks --results-dir .
"""

from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHUNK_RE = re.compile(r"\.(\d{3})\.csv$")


def load_manifest(chunk_dir: str) -> dict | None:
    path = os.path.join(chunk_dir, "manifest.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_csv(path: str) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        return list(r.fieldnames or []), list(r)


def merge(pattern: str, out_path: str) -> tuple[int, list[str]]:
    """Concatenate files matching *pattern*; returns (rows, files used)."""
    files = sorted(glob.glob(pattern))
    if not files:
        return 0, []
    header, rows = read_csv(files[0])
    for f in files[1:]:
        h, r = read_csv(f)
        if h != header:
            raise SystemExit(f"{f} has a different header from {files[0]} — "
                             f"were these produced by different versions?")
        rows += r
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    return len(rows), files


def catalog_health(db: str) -> dict:
    """Per-site health from the store, newest run per site."""
    if not os.path.exists(db):
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {}
    try:
        for r in con.execute(
                "SELECT site_key, strategy, candidates, healthy, diagnosis "
                "FROM catalogs ORDER BY run_id"):
            out[r["site_key"]] = dict(r)
    except sqlite3.Error:
        pass
    con.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chunk-dir", default="chunks")
    ap.add_argument("--results-dir", default=".")
    ap.add_argument("--db", default="pipeline.db")
    ap.add_argument("--out", default="courses_filled.csv")
    ap.add_argument("--review-out", default="review_queue.csv")
    ap.add_argument("--report-out", default="coverage_report.md")
    ap.add_argument("--allow-partial", action="store_true",
                    help="write to the canonical output names even when some "
                         "chunks have not been run")
    args = ap.parse_args(argv)

    manifest = load_manifest(args.chunk_dir)

    # Which chunks actually have results, before deciding where to write.
    have = {CHUNK_RE.search(os.path.basename(f)).group(1)
            for f in glob.glob(os.path.join(
                args.results_dir, "courses_filled.[0-9][0-9][0-9].csv"))}
    missing = []
    if manifest:
        expected = {f"{i + 1:03d}" for i in range(len(manifest["chunks"]))}
        missing = sorted(expected - have)

    # A partial merge must not take the canonical filename. Overwriting a
    # complete whole-sheet result with a fraction of it is silent data loss —
    # the file still looks like the answer.
    if missing and not args.allow_partial:
        args.out = args.out.replace(".csv", ".partial.csv")
        args.review_out = args.review_out.replace(".csv", ".partial.csv")
        args.report_out = args.report_out.replace(".md", ".partial.md")
        print(f"{len(missing)} of {len(missing) + len(have)} chunks have no "
              f"results; writing partial output to {args.out} "
              f"(pass --allow-partial to use the canonical names)",
              file=sys.stderr)

    n_filled, files = merge(
        os.path.join(args.results_dir, "courses_filled.[0-9][0-9][0-9].csv"),
        args.out)
    if not files:
        print("no per-chunk results found — has any chunk been run?",
              file=sys.stderr)
        return 1
    n_review, _ = merge(
        os.path.join(args.results_dir, "review_queue.[0-9][0-9][0-9].csv"),
        args.review_out)

    ran = have
    print(f"merged {len(files)} chunk result files -> {args.out} "
          f"({n_filled} rows)")

    # --- checks -----------------------------------------------------------
    _, rows = read_csv(args.out)
    ids = collections.Counter(r["id"] for r in rows)
    dupes = [i for i, n in ids.items() if n > 1]
    print()
    print("checks")
    print(f"  duplicate course ids across chunks : {len(dupes)}"
          + ("  <-- SPLIT IS WRONG" if dupes else "  ok"))

    if manifest:
        total_chunks = len(manifest["chunks"])
        print(f"  chunks run                         : "
              f"{len(ran)}/{total_chunks}"
              + (f"  missing {', '.join(missing)}" if missing else "  ok"))
        if not missing:
            want = manifest["source_rows"] - manifest["no_website_rows"]
            print(f"  rows vs manifest                   : {n_filled}/{want}"
                  + ("  ok" if n_filled == want else "  <-- MISMATCH"))
        # A site's rows must all come from one chunk.
        host_of = {}
        for c in manifest["chunks"]:
            for h in c["hosts"]:
                host_of[h] = c["file"]
        print(f"  sites in manifest                  : {len(host_of)}")

    # --- summary ----------------------------------------------------------
    status = collections.Counter(r.get("matched_status", "") for r in rows)
    filled = sum(1 for r in rows if r.get("course_url"))
    health = catalog_health(args.db)

    lines = ["# Coverage report (merged from chunks)", "",
             f"Merged from {len(files)} chunk result files.", "",
             "## Headline", "",
             f"- Course Rows: **{len(rows)}**",
             f"- `course_url` filled: **{filled}** "
             f"({100 * filled / max(1, len(rows)):.1f}%)"]
    if manifest:
        lines.append(f"- Chunks run: **{len(ran)} of "
                     f"{len(manifest['chunks'])}**")
        if manifest["no_website_rows"]:
            lines.append(f"- Rows excluded as uncrawlable (no website): "
                         f"**{manifest['no_website_rows']}**")
    lines += ["", "## Status distribution", "", "| status | rows | share |",
              "|---|---|---|"]
    for k, v in status.most_common():
        lines.append(f"| `{k or '(blank)'}` | {v} | "
                     f"{100 * v / max(1, len(rows)):.1f}% |")
    if health:
        unhealthy = [k for k, v in health.items() if not v["healthy"]]
        lines += ["", "## Extraction", "",
                  f"- Sites with a Catalog recorded: **{len(health)}**",
                  f"- Sites whose Catalog failed Extraction Health: "
                  f"**{len(unhealthy)}**"]
    lines.append("")
    with open(args.report_out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print()
    print(f"wrote {args.out}, {args.review_out}, {args.report_out}")
    print(f"  filled {filled}/{len(rows)} "
          f"({100 * filled / max(1, len(rows)):.1f}%)")
    return 1 if dupes else 0


if __name__ == "__main__":
    raise SystemExit(main())
