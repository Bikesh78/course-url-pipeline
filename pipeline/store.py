"""Persistent run state and URL history, in stdlib SQLite.

Why a database in an otherwise file-based project
-------------------------------------------------
Course URLs drift: an Institution restructures its catalogue and a URL that
resolved last quarter 404s this one. Answering "what URL did this course have in
June" from CSV files means keeping and diffing 52,781-row snapshots by hand.
`url_history` answers it with a query.

`sqlite3` is in the standard library, so ADR-0003's no-pip-install guarantee is
untouched. The CSVs remain the deliverable and are generated from these tables,
so nothing downstream changes.

What this deliberately does *not* store
---------------------------------------
Fetched pages. The gzipped page cache under `.cache/` stays on disk: it works,
it is warm, and moving it would risk the property that makes reruns nearly free
while buying no coverage.

Concurrency
-----------
Writes happen on the main thread only — `run.py` collects worker results in its
`as_completed` loop and hands them here afterwards — so there is no writer
queue and no contention. WAL mode is still enabled so a reader (a query while a
run is going) does not block the writer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

DEFAULT_DB = "pipeline.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started     TEXT NOT NULL,
    finished    TEXT,
    input_path  TEXT,
    args        TEXT,
    rows        INTEGER,
    filled      INTEGER
);

CREATE TABLE IF NOT EXISTS sites (
    site_key     TEXT PRIMARY KEY,
    display_name TEXT,
    website      TEXT,
    last_run     TEXT
);

CREATE TABLE IF NOT EXISTS catalogs (
    site_key   TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    strategy   TEXT,
    candidates INTEGER,
    healthy    INTEGER,
    diagnosis  TEXT,
    notes      TEXT,
    PRIMARY KEY (site_key, run_id)
);

CREATE TABLE IF NOT EXISTS row_results (
    course_id  TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    site_key   TEXT,
    url        TEXT,
    score      REAL,
    margin     REAL,
    live_score REAL,
    status     TEXT,
    flags      TEXT,
    PRIMARY KEY (course_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_row_results_run ON row_results (run_id);
CREATE INDEX IF NOT EXISTS idx_row_results_status ON row_results (status);

-- Append-only. One row per (course, URL) ever assigned, so the sequence of
-- rows for a course is its URL history.
CREATE TABLE IF NOT EXISTS url_history (
    course_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    last_verified TEXT,
    status        TEXT,
    first_run     TEXT,
    last_run      TEXT,
    PRIMARY KEY (course_id, url)
);

CREATE INDEX IF NOT EXISTS idx_url_history_course ON url_history (course_id);
"""


def now_iso() -> str:
    """UTC timestamp, second precision, for every stamp this module writes."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_run_id() -> str:
    """A sortable, readable, unique identifier for one run.

    Doubles as the log filename prefix, which is why it is spelled out rather
    than compact: `2026-09-01T11-24-23Z-a05b40` reads as a date at a glance
    where `20260901T112423Z` does not. Hyphens stand in for the time's colons,
    which are not portable in filenames.

    Lexical order is chronological *within* this format. It is not comparable
    with the older compact format — "2026-09-02..." sorts before "20260901..."
    because "-" precedes "0" — so anything ordering runs by age uses file mtime
    or the `runs.started` column rather than the id. `prune_old_runs` does.
    """
    return (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
            + "-" + uuid.uuid4().hex[:6])


class Store:
    """Run state and URL history for one database file."""

    def __init__(self, path: str = DEFAULT_DB):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ runs
    def start_run(self, run_id: str, input_path: str, args: dict) -> None:
        """Record that a run began. Call once, before any other write."""
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started, input_path, args) "
            "VALUES (?, ?, ?, ?)",
            (run_id, now_iso(), input_path, json.dumps(args, default=str)))
        self.conn.commit()

    def finish_run(self, run_id: str, rows: int, filled: int) -> None:
        """Record that a run completed, with its headline counts."""
        self.conn.execute(
            "UPDATE runs SET finished = ?, rows = ?, filled = ? "
            "WHERE run_id = ?", (now_iso(), rows, filled, run_id))
        self.conn.commit()

    # ------------------------------------------------------------------ sites
    def record_site(self, run_id: str, site_key: str, display_name: str,
                    website: str, health: dict) -> None:
        """Record one site's Catalog outcome for this run."""
        self.conn.execute(
            "INSERT OR REPLACE INTO sites (site_key, display_name, website, "
            "last_run) VALUES (?, ?, ?, ?)",
            (site_key, display_name, website, run_id))
        self.conn.execute(
            "INSERT OR REPLACE INTO catalogs (site_key, run_id, strategy, "
            "candidates, healthy, diagnosis, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (site_key, run_id, health.get("strategy"),
             health.get("candidates"), 1 if health.get("healthy") else 0,
             # `failure_reason`, not `diagnosis`: process_site writes the
             # former, so this column was silently null on every row. The
             # column keeps its name — renaming it would break existing
             # databases, which are created with CREATE TABLE IF NOT EXISTS.
             health.get("failure_reason"),
             json.dumps(health.get("notes") or [])))
        self.conn.commit()

    # ---------------------------------------------------------------- results
    def record_row_results(self, run_id: str, results, site_key_of=None) -> None:
        """Store per-row outcomes only, without touching URL history.

        Called once per site while the run is in flight, so a killed run keeps
        the sites it finished. History is deliberately *not* written here:
        `_touch_history` derives drift by comparing against the last URL
        recorded for a course, so writing history mid-run would make the
        end-of-run drift count read zero for every row.

        Safe to call repeatedly — the primary key is (course_id, run_id) and
        the write is INSERT OR REPLACE, so the end-of-run pass overwrites these
        rows with their post-Verification status.
        """
        self.conn.executemany(
            "INSERT OR REPLACE INTO row_results (course_id, run_id, site_key, "
            "url, score, margin, live_score, status, flags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._result_rows(run_id, results, site_key_of))
        self.conn.commit()

    def _result_rows(self, run_id: str, results, site_key_of=None) -> list:
        """Flatten MatchResults into row_results tuples."""
        rows = []
        for r in results:
            rows.append((
                r.row.id, run_id,
                site_key_of(r) if site_key_of else r.row.site_host,
                r.url or None,
                r.score if r.candidate else None,
                r.margin if r.candidate else None,
                r.live_score,
                r.status,
                ";".join(r.row.flags + r.flags),
            ))
        return rows

    def record_results(self, run_id: str, results, site_key_of=None) -> int:
        """Store this run's per-row outcomes and update URL history.

        Returns the number of Course Rows whose URL differs from the last one
        recorded for them — the drift this run observed.
        """
        stamp = now_iso()
        changed = 0
        rows = []
        for r in results:
            rows.append((
                r.row.id, run_id,
                site_key_of(r) if site_key_of else r.row.site_host,
                r.url or None,
                r.score if r.candidate else None,
                r.margin if r.candidate else None,
                r.live_score,
                r.status,
                ";".join(r.row.flags + r.flags),
            ))
        self.conn.executemany(
            "INSERT OR REPLACE INTO row_results (course_id, run_id, site_key, "
            "url, score, margin, live_score, status, flags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

        for r in results:
            if not r.url:
                continue
            if self._touch_history(r, run_id, stamp):
                changed += 1
        self.conn.commit()
        return changed

    def record_url_rows(self, run_id: str, rows) -> int:
        """Record final URLs from plain dicts, updating history. Returns drift.

        The dict-shaped counterpart to `record_results`, for phase 2, which
        works over an output CSV rather than MatchResult objects. Without it
        phase 2 would seed the sheet's baseline and then never write what it
        decided, leaving `url_history` with one row per course and drift
        unanswerable.
        """
        from pipeline.catalog import clean_url

        stamp = now_iso()
        changed = 0
        for row in rows:
            # Same canonicalisation as the baseline, so the two are comparable
            # whatever shape the result file was written in.
            url = clean_url(row.get("course_url") or "")
            if not url:
                continue
            if self._touch_history_values(
                    row["id"], url, (row.get("matched_status") or "").strip(),
                    run_id, stamp):
                changed += 1
        self.conn.commit()
        return changed

    def _touch_history_values(self, course_id: str, url: str, status: str,
                              run_id: str, stamp: str) -> bool:
        """Update history for one (course, url). True when the URL changed."""
        cur = self.conn.execute(
            "SELECT url FROM url_history WHERE course_id = ? "
            "ORDER BY last_seen DESC LIMIT 1", (course_id,)).fetchone()
        previous = cur["url"] if cur else None
        verified = stamp if status == "verified" else None

        existing = self.conn.execute(
            "SELECT 1 FROM url_history WHERE course_id = ? AND url = ?",
            (course_id, url)).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE url_history SET last_seen = ?, status = ?, "
                "last_run = ?, last_verified = COALESCE(?, last_verified) "
                "WHERE course_id = ? AND url = ?",
                (stamp, status, run_id, verified, course_id, url))
        else:
            self.conn.execute(
                "INSERT INTO url_history (course_id, url, first_seen, "
                "last_seen, last_verified, status, first_run, last_run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (course_id, url, stamp, stamp, verified, status, run_id,
                 run_id))
        return previous is not None and previous != url

    def _touch_history(self, result, run_id: str, stamp: str) -> bool:
        """Update history for one row. True when its URL changed.

        A URL already recorded for this course has its `last_seen` refreshed
        rather than being duplicated, so history rows count *distinct* URLs a
        course has held, not runs.
        """
        cur = self.conn.execute(
            "SELECT url FROM url_history WHERE course_id = ? "
            "ORDER BY last_seen DESC LIMIT 1", (result.row.id,)).fetchone()
        previous = cur["url"] if cur else None
        verified = stamp if result.status == "verified" else None

        existing = self.conn.execute(
            "SELECT 1 FROM url_history WHERE course_id = ? AND url = ?",
            (result.row.id, result.url)).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE url_history SET last_seen = ?, status = ?, "
                "last_run = ?, last_verified = COALESCE(?, last_verified) "
                "WHERE course_id = ? AND url = ?",
                (stamp, result.status, run_id, verified,
                 result.row.id, result.url))
        else:
            self.conn.execute(
                "INSERT INTO url_history (course_id, url, first_seen, "
                "last_seen, last_verified, status, first_run, last_run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (result.row.id, result.url, stamp, stamp, verified,
                 result.status, run_id, run_id))
        return previous is not None and previous != result.url

    # -------------------------------------------------------------- baseline
    def seed_baseline(self, rows, run_id: str = "source_sheet") -> int:
        """Enter the source sheet's own URLs as each course's first history row.

        Without this, `url_history` begins at our first run: it held 24,294
        rows and **zero** courses with more than one URL, so a course whose URL
        we changed looked as though it had always had ours. Drift was therefore
        unanswerable for exactly the rows where it mattered.

        Each baseline row is stamped with the sheet's own `processed_date`
        (2026-06-24, 06-25 or 07-14) rather than "now" — inventing a timestamp
        would make the history look precise while being wrong about when the
        URL was actually established.

        Idempotent: a course already carrying this URL is left alone, so
        re-seeding after a later export does not duplicate rows.
        """
        from pipeline.catalog import clean_url

        seeded = 0
        for row in rows:
            # Canonicalise exactly as our own URLs are, or a trailing slash
            # alone reads as the course having moved — the sheet writes
            # `/x` where extraction writes `/x/`.
            url = clean_url(row.get("course_url") or "")
            if not url:
                continue
            stamp = (row.get("processed_date") or "").strip() or now_iso()
            cur = self.conn.execute(
                "SELECT 1 FROM url_history WHERE course_id = ? AND url = ?",
                (row["id"], url)).fetchone()
            if cur:
                continue
            self.conn.execute(
                "INSERT INTO url_history (course_id, url, first_seen, "
                "last_seen, last_verified, status, first_run, last_run) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
                (row["id"], url, stamp, stamp,
                 (row.get("matched_status") or "").strip() or None,
                 run_id, run_id))
            seeded += 1
        self.conn.commit()
        return seeded

    # ---------------------------------------------------------------- queries
    def history_for(self, course_id: str) -> list[sqlite3.Row]:
        """Every URL this Course Row has held, oldest first."""
        return list(self.conn.execute(
            "SELECT url, first_seen, last_seen, last_verified, status "
            "FROM url_history WHERE course_id = ? ORDER BY first_seen",
            (course_id,)))

    def drifted(self, limit: int = 100) -> list[sqlite3.Row]:
        """Course Rows that have held more than one URL — observed drift."""
        return list(self.conn.execute(
            "SELECT course_id, COUNT(*) AS urls FROM url_history "
            "GROUP BY course_id HAVING urls > 1 "
            "ORDER BY urls DESC LIMIT ?", (limit,)))

    def runs(self, limit: int = 20) -> list[sqlite3.Row]:
        """Recent runs, newest first."""
        return list(self.conn.execute(
            "SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,)))

    def close(self) -> None:
        """Flush and close. Safe to call twice."""
        try:
            self.conn.commit()
            self.conn.close()
        except sqlite3.ProgrammingError:
            pass
