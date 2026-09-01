"""Store and URL-history tests. Hermetic — a temporary database per test."""

import os
import tempfile
import unittest

from pipeline.catalog import Candidate
from pipeline.load import CourseRow
from pipeline.match import MatchResult
from pipeline.store import Store, new_run_id

DS = "https://courses.aber.ac.uk/undergraduate/data-science/"
DS_NEW = "https://courses.aber.ac.uk/undergraduate/data-science-bsc/"


def result(course_id="1", url=DS, status="verified", score=1.0):
    row = CourseRow(course_id, "Data Science BSc (Hons)",
                    "Aberystwyth University", "https://www.aber.ac.uk")
    cand = Candidate("Data Science (BSc, 3 years)", url, "ug") if url else None
    return MatchResult(row=row, candidate=cand, score=score, margin=0.4,
                       status=status)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self._dir.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self._dir.cleanup()


class TestRunLifecycle(StoreCase):
    def test_a_run_is_recorded_and_closed(self):
        rid = new_run_id()
        self.store.start_run(rid, "final_courses.csv", {"limit": 12})
        self.store.finish_run(rid, rows=100, filled=40)
        runs = self.store.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], rid)
        self.assertEqual(runs[0]["rows"], 100)
        self.assertEqual(runs[0]["filled"], 40)
        self.assertIsNotNone(runs[0]["finished"])

    def test_run_ids_sort_chronologically(self):
        self.assertLess(new_run_id()[:15], "99999999T999999")

    def test_site_health_is_recorded(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_site(rid, "aber.ac.uk", "Aberystwyth University",
                               "https://www.aber.ac.uk",
                               {"strategy": "listing", "candidates": 809,
                                "healthy": True, "diagnosis": "ok",
                                "notes": ["a note"]})
        row = self.store.conn.execute(
            "SELECT * FROM catalogs WHERE site_key = ?", ("aber.ac.uk",)
        ).fetchone()
        self.assertEqual(row["candidates"], 809)
        self.assertEqual(row["healthy"], 1)
        self.assertEqual(row["strategy"], "listing")


class TestUrlHistory(StoreCase):
    def test_first_run_records_one_history_row(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        changed = self.store.record_results(rid, [result()])
        self.assertEqual(changed, 0, "nothing to drift from on a first run")
        hist = self.store.history_for("1")
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["url"], DS)

    def test_rerunning_the_same_url_does_not_duplicate_history(self):
        """History counts distinct URLs a course has held, not runs."""
        for _ in range(3):
            rid = new_run_id()
            self.store.start_run(rid, "x.csv", {})
            self.store.record_results(rid, [result()])
        self.assertEqual(len(self.store.history_for("1")), 1)

    def test_a_changed_url_is_appended_and_counted_as_drift(self):
        r1 = new_run_id()
        self.store.start_run(r1, "x.csv", {})
        self.store.record_results(r1, [result()])
        r2 = new_run_id()
        self.store.start_run(r2, "x.csv", {})
        changed = self.store.record_results(r2, [result(url=DS_NEW)])
        self.assertEqual(changed, 1)
        hist = self.store.history_for("1")
        self.assertEqual([h["url"] for h in hist], [DS, DS_NEW])

    def test_drifted_lists_courses_with_more_than_one_url(self):
        r1 = new_run_id()
        self.store.start_run(r1, "x.csv", {})
        self.store.record_results(r1, [result()])
        r2 = new_run_id()
        self.store.start_run(r2, "x.csv", {})
        self.store.record_results(r2, [result(url=DS_NEW)])
        drifted = self.store.drifted()
        self.assertEqual(len(drifted), 1)
        self.assertEqual(drifted[0]["course_id"], "1")
        self.assertEqual(drifted[0]["urls"], 2)

    def test_unfilled_rows_get_no_history(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_results(rid, [result(url=None, status="no_match")])
        self.assertEqual(self.store.history_for("1"), [])

    def test_verification_timestamp_only_set_when_verified(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_results(rid, [result(course_id="2",
                                               status="probable")])
        hist = self.store.history_for("2")
        self.assertIsNone(hist[0]["last_verified"])

    def test_a_later_verification_is_remembered(self):
        r1 = new_run_id()
        self.store.start_run(r1, "x.csv", {})
        self.store.record_results(r1, [result(status="probable")])
        r2 = new_run_id()
        self.store.start_run(r2, "x.csv", {})
        self.store.record_results(r2, [result(status="verified")])
        self.assertIsNotNone(self.store.history_for("1")[0]["last_verified"])


class TestResultPersistence(StoreCase):
    def test_row_results_are_queryable_by_status(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_results(rid, [
            result(course_id="1", status="verified"),
            result(course_id="2", status="ambiguous"),
            result(course_id="3", url=None, status="no_match"),
        ])
        counts = dict(self.store.conn.execute(
            "SELECT status, COUNT(*) FROM row_results WHERE run_id = ? "
            "GROUP BY status", (rid,)).fetchall())
        self.assertEqual(counts["verified"], 1)
        self.assertEqual(counts["ambiguous"], 1)
        self.assertEqual(counts["no_match"], 1)

    def test_reopening_the_database_keeps_history(self):
        path = os.path.join(self._dir.name, "persist.db")
        s1 = Store(path)
        rid = new_run_id()
        s1.start_run(rid, "x.csv", {})
        s1.record_results(rid, [result()])
        s1.close()
        s2 = Store(path)
        try:
            self.assertEqual(len(s2.history_for("1")), 1)
        finally:
            s2.close()


if __name__ == "__main__":
    unittest.main()
