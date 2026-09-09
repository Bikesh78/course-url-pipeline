#!/usr/bin/env python3
"""Collapse `url_history` rows that are the same page written twice.

Why these rows exist
--------------------
`url_history` keys a row by (course, URL) *string*, but two strings can be one
page: a trailing slash, a `www.` prefix or a scheme upgrade is not a move. Two
write paths disagreed about canonicalisation — phase 1 recorded the URL exactly
as extracted, phase 2 ran it through `clean_url` — so the same page was filed
twice for the same course. Baseline seeding from the sheet added more of the
same.

Measured on the database this was written for: 53,967 rows, of which 9,338
collapse across 9,287 courses, leaving 44,629. Until they are collapsed,
`history_for()` overstates how many URLs a course has held and the `drifted()`
query overstates how much has moved.

The write paths are fixed, so this is a one-off repair rather than maintenance.
It is idempotent: a second run finds nothing to do.

What is kept when rows collapse
-------------------------------
One row per page, holding:

* `url` — the canonical (`clean_url`) form
* `first_seen` — the **earliest**, so the history's start date survives
* `last_seen`, `last_verified` — the **latest**, so verification is not lost
* `status`, `first_run`, `last_run` — from the row seen most recently, since
  that is the run whose answer currently stands

Safety
------
Deletes rows, so it refuses to touch a database without `--write`, and
refuses `--write` without either `--backup` (the default, taken first) or an
explicit `--no-backup`.

Usage
-----
    python tools/dedupe_url_history.py                        # report only
    python tools/dedupe_url_history.py --write                # backup, then do it
    python tools/dedupe_url_history.py --db /tmp/copy.db --write --no-backup
"""

from __future__ import annotations

import argparse
import collections
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.catalog import clean_url  # noqa: E402
from pipeline.store import DEFAULT_DB  # noqa: E402
from pipeline.triage import same_page  # noqa: E402

FIELDS = ("course_id", "url", "first_seen", "last_seen", "last_verified",
          "status", "first_run", "last_run")


def _groups(rows: list[dict]) -> list[list[dict]]:
    """Partition one course's rows into same-page groups, order preserved."""
    groups: list[list[dict]] = []
    for row in rows:
        for group in groups:
            if row["url"] == group[0]["url"] or same_page(row["url"],
                                                          group[0]["url"]):
                group.append(row)
                break
        else:
            groups.append([row])
    return groups


def _merge(group: list[dict]) -> dict:
    """The single row a same-page group collapses to."""
    # `last_seen` decides which run's answer stands, so sort by it rather than
    # trusting the order rows came out of the table in.
    newest = max(group, key=lambda r: r["last_seen"] or "")
    verified = [r["last_verified"] for r in group if r["last_verified"]]
    return {
        "course_id": newest["course_id"],
        "url": clean_url(newest["url"]),
        "first_seen": min(r["first_seen"] or "" for r in group),
        "last_seen": newest["last_seen"],
        "last_verified": max(verified) if verified else None,
        "status": newest["status"],
        "first_run": min(group, key=lambda r: r["first_seen"] or "")["first_run"],
        "last_run": newest["last_run"],
    }


def plan(conn: sqlite3.Connection) -> tuple[list[tuple[str, list[dict], dict]],
                                            int]:
    """Work out what would change. Returns (per-course work, total rows)."""
    conn.row_factory = sqlite3.Row
    by_course: dict[str, list[dict]] = collections.defaultdict(list)
    total = 0
    for r in conn.execute(f"SELECT {', '.join(FIELDS)} FROM url_history"):
        by_course[r["course_id"]].append(dict(r))
        total += 1

    work = []
    for course_id, rows in by_course.items():
        for group in _groups(rows):
            merged = _merge(group)
            # Worth rewriting either because rows collapse, or because the one
            # surviving row is not stored in canonical form.
            if len(group) > 1 or group[0]["url"] != merged["url"]:
                work.append((course_id, group, merged))
    return work, total


def apply(conn: sqlite3.Connection, work) -> None:
    """Replace each group with its merged row, in one transaction."""
    for course_id, group, merged in work:
        conn.executemany(
            "DELETE FROM url_history WHERE course_id = ? AND url = ?",
            [(course_id, r["url"]) for r in group])
        conn.execute(
            "INSERT INTO url_history (course_id, url, first_seen, last_seen, "
            "last_verified, status, first_run, last_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(merged[f] for f in FIELDS))
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--write", action="store_true",
                    help="apply the changes; without it, report only")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the .bak copy taken before writing")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    work, total = plan(conn)
    collapsing = sum(len(g) - 1 for _, g, _ in work)
    recanonicalised = sum(1 for _, g, m in work
                          if len(g) == 1 and g[0]["url"] != m["url"])

    print(f"{args.db}: {total} url_history rows")
    print(f"  courses with same-page duplicates : "
          f"{sum(1 for _, g, _ in work if len(g) > 1)}")
    print(f"  rows that collapse                : {collapsing}")
    print(f"  rows only needing canonical form  : {recanonicalised}")
    print(f"  rows after                        : {total - collapsing}")

    if not work:
        print("  nothing to do")
        return 0
    if not args.write:
        print("\n  report only — pass --write to apply")
        for course_id, group, merged in work[:5]:
            print(f"\n  course {course_id}")
            for r in group:
                print(f"    - {r['url']}  (first_seen {r['first_seen']})")
            print(f"    => {merged['url']}  (first_seen {merged['first_seen']})")
        return 0

    if not args.no_backup:
        backup = args.db + ".bak"
        shutil.copy2(args.db, backup)
        print(f"\n  backed up to {backup}")

    apply(conn, work)
    after = conn.execute("SELECT COUNT(*) FROM url_history").fetchone()[0]
    print(f"  done — {after} rows remain")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
