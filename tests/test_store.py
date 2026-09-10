"""Store and URL-history tests. Hermetic — a temporary database per test."""

import os
import tempfile
import unittest

from pipeline.catalog import Candidate
from pipeline.load import CourseRow
from pipeline.match import MatchResult
from pipeline.store import Store, new_run_id

# Canonical form: clean_url() strips the trailing slash, and the store
# canonicalises before writing history so a slash is not read as a move.
DS = "https://courses.aber.ac.uk/undergraduate/data-science"
DS_NEW = "https://courses.aber.ac.uk/undergraduate/data-science-bsc"


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

    def test_run_id_is_readable_and_filename_safe(self):
        """It doubles as the log filename prefix, so it must read as a date."""
        import re
        rid = new_run_id()
        self.assertRegex(rid, r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-[0-9a-f]{6}$")
        self.assertFalse(set(rid) & set('/:\\*?"<>|'), rid)

    def test_ids_sort_chronologically_within_the_format(self):
        earlier = "2026-09-01T10-00-00Z-aaaaaa"
        later = "2026-09-02T10-00-00Z-aaaaaa"
        self.assertLess(earlier, later)

    def test_the_two_formats_do_not_sort_against_each_other(self):
        """Documents why `prune_old_runs` orders by mtime rather than by name.

        "-" (0x2D) precedes "0" (0x30), so every readable id sorts before every
        compact one regardless of date. Sorting run ids to find the oldest
        would delete the newest logs first.
        """
        self.assertLess("2026-09-02T10-00-00Z-new", "20260801T100000Z-old")

    # The keys `process_site` actually produces. An earlier fixture invented a
    # `diagnosis` key, which is why it never noticed that the column was null
    # on every row ever written.
    HEALTHY = {"strategy": "listing", "candidates": 809, "healthy": True,
               "failure_reason": "", "notes": ["a note"]}
    BLOCKED = {"strategy": "none", "candidates": 0, "healthy": False,
               "failure_reason": "blocked", "notes": ["refused every probe"]}

    def _record(self, site_key, health):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_site(rid, site_key, site_key, f"https://{site_key}",
                               health)
        return self.store.conn.execute(
            "SELECT * FROM catalogs WHERE site_key = ?", (site_key,)).fetchone()

    def test_site_health_is_recorded(self):
        row = self._record("aber.ac.uk", self.HEALTHY)
        self.assertEqual(row["candidates"], 809)
        self.assertEqual(row["healthy"], 1)
        self.assertEqual(row["strategy"], "listing")

    def test_why_a_site_failed_is_stored_not_dropped(self):
        """The field you query to answer "which sites are unreachable"."""
        row = self._record("monash.edu", self.BLOCKED)
        self.assertEqual(row["healthy"], 0)
        self.assertEqual(row["diagnosis"], "blocked")

    def test_blocked_sites_are_queryable(self):
        self._record("monash.edu", self.BLOCKED)
        self._record("aber.ac.uk", self.HEALTHY)
        blocked = self.store.conn.execute(
            "SELECT site_key FROM catalogs WHERE diagnosis = 'blocked'"
        ).fetchall()
        self.assertEqual([r["site_key"] for r in blocked], ["monash.edu"])


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
        """Drift is measured against the sheet, so the sheet must have one.

        Previously this passed by comparing against the last row written,
        which is what made the database disagree with the CSV.
        """
        r1 = new_run_id()
        self.store.start_run(r1, "x.csv", {})
        self.store.record_results(r1, [result()])
        r2 = new_run_id()
        self.store.start_run(r2, "x.csv", {})
        moved = result(url=DS_NEW)
        moved.row.prior_course_url = DS
        changed = self.store.record_results(r2, [moved])
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


class TestBaselineSeeding(StoreCase):
    """The sheet's own URLs are each course's first history entry.

    Without this, history began at our first run: 24,294 rows and zero courses
    with more than one URL, so a course whose URL we changed looked as though
    it had always had ours.
    """

    def _sheet(self, url=DS, date="2026-06-24"):
        return [{"id": "1", "course_url": url, "processed_date": date,
                 "matched_status": "matched"}]

    def test_a_sheet_url_becomes_the_first_history_row(self):
        self.assertEqual(self.store.seed_baseline(self._sheet()), 1)
        hist = self.store.history_for("1")
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["url"], DS)

    def test_it_is_stamped_with_the_sheets_own_date(self):
        # Inventing "now" would make the history look precise but be wrong
        # about when the URL was established.
        self.store.seed_baseline(self._sheet(date="2026-07-14"))
        self.assertTrue(
            self.store.history_for("1")[0]["first_seen"].startswith("2026-07-14"))

    def test_seeding_twice_does_not_duplicate(self):
        self.store.seed_baseline(self._sheet())
        self.assertEqual(self.store.seed_baseline(self._sheet()), 0)
        self.assertEqual(len(self.store.history_for("1")), 1)

    def test_a_blank_sheet_url_seeds_nothing(self):
        self.assertEqual(self.store.seed_baseline(
            [{"id": "1", "course_url": "", "processed_date": "2026-06-24"}]), 0)

    def test_the_baseline_is_canonicalised(self):
        """A trailing slash alone must not read as the course having moved."""
        self.store.seed_baseline(self._sheet(url=DS + "/"))
        self.assertEqual(self.store.history_for("1")[0]["url"], DS)


class TestDictHistoryPath(StoreCase):
    """Phase 2 works over CSV rows, not MatchResult objects."""

    def test_a_changed_url_is_recorded_as_drift(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.seed_baseline(
            [{"id": "1", "course_url": DS, "processed_date": "2026-06-24"}])
        changed = self.store.record_url_rows(
            rid, [{"id": "1", "course_url": DS_NEW,
                   "matched_status": "verified",
                   "prior_course_url": DS}])
        self.assertEqual(changed, 1)
        self.assertEqual([h["url"] for h in self.store.history_for("1")],
                         [DS, DS_NEW])

    def test_an_unchanged_url_is_not_drift(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.seed_baseline(
            [{"id": "1", "course_url": DS, "processed_date": "2026-06-24"}])
        self.assertEqual(self.store.record_url_rows(
            rid, [{"id": "1", "course_url": DS,
                   "matched_status": "verified"}]), 0)

    def test_a_trailing_slash_is_not_drift(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.seed_baseline(
            [{"id": "1", "course_url": DS, "processed_date": "2026-06-24"}])
        self.assertEqual(self.store.record_url_rows(
            rid, [{"id": "1", "course_url": DS + "/",
                   "matched_status": "verified"}]), 0)

    def test_blank_rows_are_skipped(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.assertEqual(self.store.record_url_rows(
            rid, [{"id": "1", "course_url": "", "matched_status": "no_match"}]),
            0)
        self.assertEqual(self.store.history_for("1"), [])


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


DS_SLASH = DS + "/"
DS_WWW = DS.replace("https://courses.", "https://www.courses.")


def sheet(course_id="1", url=DS, date="2026-06-24"):
    """One input-sheet row, as `seed_baseline` reads them."""
    return {"id": course_id, "course_url": url, "processed_date": date}


def out_row(course_id="1", url=DS, prior=DS, status="verified"):
    """One output-CSV row, as `record_url_rows` reads them.

    `prior_course_url` is the column phase 1 and phase 2 both write; drift is
    measured against it, so a test that omits it is not testing what runs.
    """
    return {"id": course_id, "course_url": url, "matched_status": status,
            "prior_course_url": prior}


class TestDriftIsMeasuredAgainstTheSheet(StoreCase):
    """Drift answers "does this differ from what the sheet delivered".

    Not "from the last row we happened to write". `seed_baseline` stamps the
    sheet's rows with the sheet's own `processed_date` (June/July) while phase
    1 stamps its own with the time it ran (September), so "the most recent
    row" is phase 1's answer — which is what made the database disagree with
    the CSV, 10,522 against 8,194.
    """

    def _phase1_then_phase2(self, phase1_url, phase2_url, prior=DS):
        r1 = new_run_id()
        self.store.start_run(r1, "x.csv", {})
        self.store.seed_baseline([sheet(url=prior)])
        self.store.record_results(r1, [result(url=phase1_url)])
        r2 = new_run_id()
        self.store.start_run(r2, "x.csv", {})
        return self.store.record_url_rows(
            r2, [out_row(url=phase2_url, prior=prior)])

    def test_restoring_the_sheets_url_after_phase_1_changed_it_is_not_drift(self):
        """The production shape, and the case that was being miscounted.

        Phase 1 found something else, triage adopted the sheet's URL back, so
        the delivered answer equals what the sheet delivered. `url_change`
        reads `unchanged`; drift must agree.
        """
        self.assertEqual(self._phase1_then_phase2(DS_NEW, DS), 0)

    def test_a_genuine_change_from_the_sheet_is_still_drift(self):
        self.assertEqual(self._phase1_then_phase2(DS_NEW, DS_NEW), 1)

    def test_a_trailing_slash_is_not_drift_through_the_results_path(self):
        """`record_results` did not canonicalise; only the dict path did."""
        self.assertEqual(self._phase1_then_phase2(DS_SLASH, DS_SLASH), 0)

    def test_a_www_prefix_is_not_drift(self):
        self.assertEqual(self._phase1_then_phase2(DS_WWW, DS_WWW), 0)

    def test_filling_a_row_the_sheet_left_blank_is_not_drift(self):
        """That is `added`, not `changed` — mixing them is the bug."""
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.assertEqual(self.store.record_url_rows(
            rid, [out_row(url=DS, prior="")]), 0)

    def test_the_results_path_measures_against_the_sheet_too(self):
        """Phase 1 reads the sheet's URL off the raw input row."""
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        r = result(url=DS_NEW)
        r.row.prior_course_url = DS
        self.assertEqual(self.store.record_results(rid, [r]), 1)

    def test_the_results_path_reports_no_drift_when_it_agrees_with_the_sheet(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        r = result(url=DS_SLASH)
        r.row.prior_course_url = DS
        self.assertEqual(self.store.record_results(rid, [r]), 0)


class TestNoSamePageDuplicates(StoreCase):
    """One canonical row per page, whichever phase wrote it.

    9,338 rows in the live database were slash-variants of a row the same
    course already held, because phase 1 wrote URLs as extracted and phase 2
    canonicalised them.
    """

    def test_a_slash_variant_creates_no_second_row(self):
        r1 = new_run_id()
        self.store.start_run(r1, "x.csv", {})
        self.store.record_results(r1, [result(url=DS_SLASH)])
        r2 = new_run_id()
        self.store.start_run(r2, "x.csv", {})
        self.store.record_url_rows(r2, [out_row(url=DS, prior=DS)])
        self.assertEqual(len(self.store.history_for("1")), 1)

    def test_the_stored_url_is_canonical(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_results(rid, [result(url=DS_SLASH)])
        self.assertEqual(self.store.history_for("1")[0]["url"], DS)

    def test_a_genuinely_different_url_still_appends(self):
        r1 = new_run_id()
        self.store.start_run(r1, "x.csv", {})
        self.store.record_results(r1, [result(url=DS)])
        r2 = new_run_id()
        self.store.start_run(r2, "x.csv", {})
        self.store.record_url_rows(r2, [out_row(url=DS_NEW, prior=DS)])
        self.assertEqual(len(self.store.history_for("1")), 2)


class TestSeedingFoldsSamePageRows(StoreCase):
    """Seeding must not re-file a page the write path already collapsed.

    The loop that produced 89 duplicate pairs in the live database, one per
    run: the sheet holds `www.x/y`, extraction finds `x/y`, the fold in
    `_touch_history_values` rewrites the stored row to the latter, and the next
    seeding no longer finds its own `www.` form by exact match and inserts it
    again. Both halves ended up stamped `first_run=source_sheet`, which is why
    the cause was not obvious from the rows.
    """

    def test_a_www_variant_is_not_filed_as_a_second_row(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_url_rows(
            rid, [out_row(url=DS, prior=DS_WWW)])
        seeded = self.store.seed_baseline([sheet(url=DS_WWW)])
        self.assertEqual(seeded, 0)
        self.assertEqual(len(self.store.history_for("1")), 1)

    def test_the_surviving_row_keeps_our_canonical_url(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_url_rows(rid, [out_row(url=DS, prior=DS_WWW)])
        self.store.seed_baseline([sheet(url=DS_WWW)])
        self.assertEqual(self.store.history_for("1")[0]["url"], DS)

    def test_the_earlier_start_date_wins(self):
        """History exists to say when a URL was established."""
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_url_rows(rid, [out_row(url=DS, prior=DS_WWW)])
        self.store.seed_baseline([sheet(url=DS_WWW, date="2026-06-24")])
        self.assertEqual(self.store.history_for("1")[0]["first_seen"],
                         "2026-06-24")

    def test_a_trailing_slash_variant_is_also_folded(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_url_rows(rid, [out_row(url=DS, prior=DS)])
        self.assertEqual(self.store.seed_baseline([sheet(url=DS_SLASH)]), 0)
        self.assertEqual(len(self.store.history_for("1")), 1)

    def test_a_genuinely_different_url_is_still_seeded(self):
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_url_rows(rid, [out_row(url=DS, prior=DS)])
        self.assertEqual(self.store.seed_baseline([sheet(url=DS_NEW)]), 1)
        self.assertEqual(len(self.store.history_for("1")), 2)

    def test_repeated_seeding_stays_idempotent(self):
        """The property the live database lost: no growth per run."""
        rid = new_run_id()
        self.store.start_run(rid, "x.csv", {})
        self.store.record_url_rows(rid, [out_row(url=DS, prior=DS_WWW)])
        for _ in range(4):
            self.store.seed_baseline([sheet(url=DS_WWW)])
        self.assertEqual(len(self.store.history_for("1")), 1)


if __name__ == "__main__":
    unittest.main()
