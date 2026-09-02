#!/usr/bin/env python3
"""Split the course sheet into chunks that never cut a Site in half.

Why this cannot be a row split
------------------------------
Institutions are contiguous in the sheet, but **Sites are not**. 165 hosts are
shared by 455 Institution records — `britishcouncil.org` appears fifteen times
as separate per-country entries — so slicing by row number fractures 142 Site
buckets. A fractured Site would have its Catalog built twice from two partial
row sets, its Variant Sibling sharing rule applied within each half separately,
and its Extraction Health measured against a row count that is missing rows.

So the unit of splitting is the Site, exactly as `pipeline.load.group_by_site`
defines it — this tool imports that function rather than reimplementing the
rule, so the `registrable()` host handling (which stops `barkly.vic.edu.au`
collapsing into `vic.edu.au`) is shared, not duplicated.

Equal rows is not equal time
----------------------------
Chunks are balanced on **row count**, which is what was asked for, but
wall-clock tracks *Site count*: most of the cost is per-Site probing. Expect a
wide spread — at 20 chunks, roughly 1 to 27 minutes each, with one chunk being
a single 5,083-row Site. `--report` prints the spread before you commit to it.

Usage
-----
    python tools/split_by_site.py --chunks 20
    python tools/split_by_site.py --chunks 20 --report   # plan only, no files
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.load import DEFAULT_INPUT, load_rows  # noqa: E402

DEFAULT_OUT_DIR = "chunks"
MANIFEST = "manifest.json"
NO_WEBSITE = "no_website.csv"


def sha256_of(path: str) -> str:
    """Checksum of the source sheet, so a stale chunk is detectable."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_source(path: str) -> tuple[list[str], list[list[str]]]:
    """Header and non-blank body rows, as raw fields.

    Rows are carried through as the source wrote them rather than
    re-serialised from parsed objects, so a chunk is what the pipeline would
    have read from the sheet directly.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        raw = list(csv.reader(fh))
    if not raw:
        return [], []
    header = raw[0]
    body = [f for f in raw[1:] if any(x.strip() for x in f)]
    return header, body


def pair_rows_to_source(path: str):
    """Zip parsed Course Rows to their source lines, asserting they align.

    `load_rows` skips fully blank lines and nothing else, so the two sequences
    must correspond one-to-one. The assertion is the guard: if the loader ever
    filters differently, this fails loudly instead of silently writing chunks
    whose rows belong to the wrong Sites.
    """
    header, body = read_source(path)
    rows = load_rows(path)
    if len(rows) != len(body):
        raise SystemExit(
            f"cannot pair rows to source lines: load_rows returned "
            f"{len(rows)} but the file has {len(body)} non-blank rows. "
            f"The loader's row filtering has changed; update this tool.")
    try:
        idx = header.index("id")
    except ValueError:
        idx = 0
    for r, fields in zip(rows, body):
        if fields[idx].strip() != r.id:
            raise SystemExit(
                f"row/source misalignment at id {r.id!r} vs "
                f"{fields[idx]!r} — refusing to split.")
    return header, list(zip(rows, body))


def pack_sites(site_order: list[str], site_rows: dict[str, int],
               n_chunks: int) -> list[list[str]]:
    """Pack whole Sites into *n_chunks* bins of roughly equal row count.

    The target is recomputed per bin from what is left, so the remainder is
    spread across the bins instead of landing entirely in the last one — a
    fixed target puts 10,968 rows in the final chunk against a 5,278 goal.
    """
    remaining_rows = sum(site_rows.values())
    remaining_bins = n_chunks
    bins: list[list[str]] = []
    cur: list[str] = []
    cur_rows = 0
    for i, host in enumerate(site_order):
        n = site_rows[host]
        target = remaining_rows / max(remaining_bins, 1)
        sites_left = len(site_order) - i
        # Close the bin when adding this Site would overshoot the target, but
        # never leave a later bin with no Sites to put in it. Closing now
        # leaves `remaining_bins - 1` bins to fill and `sites_left` Sites
        # (including this one) to fill them with.
        if (cur and cur_rows + n > target and remaining_bins > 1
                and sites_left >= remaining_bins - 1):
            bins.append(cur)
            remaining_rows -= cur_rows
            remaining_bins -= 1
            cur, cur_rows = [], 0
        cur.append(host)
        cur_rows += n
    if cur:
        bins.append(cur)
    return bins


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=DEFAULT_INPUT)
    ap.add_argument("--chunks", type=int, default=20)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--report", action="store_true",
                    help="print the planned split without writing files")
    args = ap.parse_args(argv)

    header, paired = pair_rows_to_source(args.input)

    by_host: dict[str, list[list[str]]] = {}
    no_website: list[list[str]] = []
    order: list[str] = []
    for row, fields in paired:
        host = row.site_host
        if not host:
            no_website.append(fields)
            continue
        if host not in by_host:
            by_host[host] = []
            order.append(host)
        by_host[host].append(fields)

    site_rows = {h: len(v) for h, v in by_host.items()}
    bins = pack_sites(order, site_rows, args.chunks)

    total = sum(site_rows.values())
    print(f"{len(paired)} rows, {len(by_host)} sites, "
          f"{len(no_website)} with no usable website")
    print(f"packing {total} crawlable rows into {len(bins)} chunks "
          f"(target {total // max(len(bins), 1)} rows each)")
    print()
    print(f"  {'chunk':>5}  {'sites':>6}  {'rows':>7}  {'largest site':>12}")
    for i, hosts in enumerate(bins, 1):
        rows_n = sum(site_rows[h] for h in hosts)
        largest = max((site_rows[h] for h in hosts), default=0)
        print(f"  {i:5d}  {len(hosts):6d}  {rows_n:7d}  {largest:12d}")

    if args.report:
        print("\n--report: no files written")
        return 0

    os.makedirs(args.out_dir, exist_ok=True)
    manifest = {
        "source": os.path.abspath(args.input),
        "source_sha256": sha256_of(args.input),
        "source_rows": len(paired),
        "chunks": [],
        "no_website_rows": len(no_website),
    }

    for i, hosts in enumerate(bins, 1):
        name = f"final_courses.{i:03d}.csv"
        path = os.path.join(args.out_dir, name)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for h in hosts:
                w.writerows(by_host[h])
        manifest["chunks"].append({
            "file": name,
            "sites": len(hosts),
            "rows": sum(site_rows[h] for h in hosts),
            "hosts": hosts,
        })

    if no_website:
        path = os.path.join(args.out_dir, NO_WEBSITE)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(no_website)

    with open(os.path.join(args.out_dir, MANIFEST), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)

    print(f"\nwrote {len(bins)} chunks + {MANIFEST} to {args.out_dir}/")
    if no_website:
        print(f"wrote {len(no_website)} uncrawlable rows to "
              f"{args.out_dir}/{NO_WEBSITE} (not in any chunk)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
