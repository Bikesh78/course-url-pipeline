"""Collapsing same-page `url_history` rows. Hermetic — a temporary database."""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest

from pipeline.store import Store
from tools.dedupe_url_history import main, plan

DS = "https://courses.aber.ac.uk/undergraduate/data-science"
DS_SLASH = DS + "/"
DS_OTHER = "https://courses.aber.ac.uk/undergraduate/data-science-bsc"


class DedupeCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "t.db")
        Store(self.path).close()

    def add(self, course_id, url, first_seen, last_seen=None,
            status="verified", verified=None, run="r1"):
        """Insert a history row directly, bypassing the fixed write paths."""
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO url_history (course_id, url, first_seen, last_seen, "
            "last_verified, status, first_run, last_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (course_id, url, first_seen, last_seen or first_seen, verified,
             status, run, run))
        conn.commit()
        conn.close()

    def rows(self, course_id):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        out = [dict(r) for r in conn.execute(
            "SELECT * FROM url_history WHERE course_id = ? "
            "ORDER BY first_seen", (course_id,))]
        conn.close()
        return out

    def run_tool(self, *extra):
        """Run the tool, swallowing its report so the suite stays readable."""
        with contextlib.redirect_stdout(io.StringIO()):
            return main(["--db", self.path, "--no-backup", *extra])


class TestCollapsing(DedupeCase):
    def test_a_slash_pair_collapses_to_one_canonical_row(self):
        self.add("1", DS, "2026-06-24")
        self.add("1", DS_SLASH, "2026-09-02")
        self.run_tool("--write")
        rows = self.rows("1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], DS)

    def test_the_earliest_first_seen_survives(self):
        """The history's start date is the point of keeping history."""
        self.add("1", DS_SLASH, "2026-09-02")
        self.add("1", DS, "2026-06-24")
        self.run_tool("--write")
        self.assertEqual(self.rows("1")[0]["first_seen"], "2026-06-24")

    def test_the_latest_verification_is_not_lost(self):
        self.add("1", DS, "2026-06-24", verified=None)
        self.add("1", DS_SLASH, "2026-09-02", verified="2026-09-02")
        self.run_tool("--write")
        self.assertEqual(self.rows("1")[0]["last_verified"], "2026-09-02")

    def test_the_newest_rows_status_stands(self):
        self.add("1", DS, "2026-06-24", status="ambiguous")
        self.add("1", DS_SLASH, "2026-09-02", last_seen="2026-09-02",
                 status="verified")
        self.run_tool("--write")
        self.assertEqual(self.rows("1")[0]["status"], "verified")

    def test_a_lone_non_canonical_row_is_rewritten(self):
        self.add("1", DS_SLASH, "2026-06-24")
        self.run_tool("--write")
        self.assertEqual(self.rows("1")[0]["url"], DS)


class TestWhatItLeavesAlone(DedupeCase):
    def test_genuinely_different_urls_are_kept(self):
        self.add("1", DS, "2026-06-24")
        self.add("1", DS_OTHER, "2026-09-02")
        self.run_tool("--write")
        self.assertEqual(len(self.rows("1")), 2)

    def test_the_same_page_under_two_courses_is_not_touched(self):
        """Sharing is a separate question; dedupe is per course."""
        self.add("1", DS, "2026-06-24")
        self.add("2", DS, "2026-06-24")
        self.run_tool("--write")
        self.assertEqual(len(self.rows("1")), 1)
        self.assertEqual(len(self.rows("2")), 1)


class TestSafety(DedupeCase):
    def test_without_write_nothing_changes(self):
        self.add("1", DS, "2026-06-24")
        self.add("1", DS_SLASH, "2026-09-02")
        self.run_tool()
        self.assertEqual(len(self.rows("1")), 2)

    def test_a_second_run_finds_nothing_to_do(self):
        self.add("1", DS, "2026-06-24")
        self.add("1", DS_SLASH, "2026-09-02")
        self.run_tool("--write")
        conn = sqlite3.connect(self.path)
        work, total = plan(conn)
        conn.close()
        self.assertEqual(work, [])
        self.assertEqual(total, 1)

    def test_a_clean_database_is_a_no_op(self):
        self.add("1", DS, "2026-06-24")
        self.run_tool("--write")
        self.assertEqual(len(self.rows("1")), 1)


if __name__ == "__main__":
    unittest.main()
