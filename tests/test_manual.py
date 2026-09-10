"""Human decisions applied over the automated stages. Hermetic."""

import csv
import os
import tempfile
import unittest

from pipeline.manual import (ManualStats, apply_manual_urls, load_manual_urls,
                             _evidence)
from pipeline.statuses import MANUALLY_ASSIGNED, NEVER_GIVEN_UP, SEARCH_FOUND
from pipeline.triage import ShareIndex

SITE = "https://courses.aber.ac.uk"
DS = "https://courses.aber.ac.uk/undergraduate/data-science"
SECTION = "https://courses.aber.ac.uk/undergraduate"
OFFSITE = "https://education.example.gov/curriculum/years-7-10"


def row(rid, name, url="", status="no_catalog", website=SITE, prior=""):
    return {"id": rid, "name": name, "institution_name": "Aberystwyth",
            "course_url": url, "matched_status": status, "website": website,
            "row_flags": "", "matched_score": "", "match_margin": "",
            "match_evidence": "", "prior_course_url": prior,
            "prior_matched_status": "", "url_change": "none"}


def decision(rid, url=SECTION, note="No course page exists",
             who="bikesh", when="2026-09-10"):
    return {"id": rid, "course_url": url, "note": note,
            "decided_by": who, "decided_at": when}


class TestWhatAnAppliedRowCarries(unittest.TestCase):
    def setUp(self):
        self.rows = [row("1", "Secondary Junior 7-10")]
        self.stats = apply_manual_urls(self.rows, [decision("1")])
        self.r = self.rows[0]

    def test_the_url_is_written(self):
        self.assertEqual(self.r["course_url"], SECTION)

    def test_the_status_says_a_human_decided(self):
        self.assertEqual(self.r["matched_status"], MANUALLY_ASSIGNED)

    def test_it_is_never_verified(self):
        """The pipeline did not fetch the page."""
        self.assertNotEqual(self.r["matched_status"], "verified")

    def test_the_row_is_flagged(self):
        self.assertIn("url_from_human", self.r["row_flags"])

    def test_no_score_is_recorded(self):
        """The gate was overridden; printing what it failed would justify it."""
        self.assertEqual(self.r["matched_score"], "")
        self.assertEqual(self.r["match_margin"], "")

    def test_the_evidence_names_who_and_why(self):
        self.assertIn("bikesh", self.r["match_evidence"])
        self.assertIn("2026-09-10", self.r["match_evidence"])
        self.assertIn("No course page exists", self.r["match_evidence"])

    def test_provenance_is_recomputed(self):
        self.assertEqual(self.r["url_change"], "added")

    def test_it_counts_as_applied(self):
        self.assertEqual((self.stats.applied, self.stats.rejected), (1, 0))


class TestItWinsOverEveryStage(unittest.TestCase):
    """A person looking at the page beats a slug score."""

    def test_it_overrides_a_search_result(self):
        rows = [row("1", "Data Science", url=DS, status=SEARCH_FOUND)]
        apply_manual_urls(rows, [decision("1")])
        self.assertEqual(rows[0]["course_url"], SECTION)
        self.assertEqual(rows[0]["matched_status"], MANUALLY_ASSIGNED)

    def test_it_overrides_a_carried_over_url(self):
        rows = [row("1", "Data Science", url=DS, status="carried_over")]
        apply_manual_urls(rows, [decision("1")])
        self.assertEqual(rows[0]["course_url"], SECTION)

    def test_it_overrides_a_verified_extraction(self):
        rows = [row("1", "Data Science", url=DS, status="verified")]
        apply_manual_urls(rows, [decision("1")])
        self.assertEqual(rows[0]["matched_status"], MANUALLY_ASSIGNED)

    def test_triage_is_told_never_to_take_it_back(self):
        """The 112-row failure: an empty phase-1 answer invites re-adoption."""
        self.assertIn(MANUALLY_ASSIGNED, NEVER_GIVEN_UP)


class TestOverridesAreRecordedNotSilent(unittest.TestCase):
    def test_a_shared_page_is_applied_and_flagged(self):
        """One section page covering a school's year bands is the normal case."""
        rows = [row("1", "Secondary Junior 7-10", url=SECTION,
                    status=MANUALLY_ASSIGNED),
                row("2", "Secondary Senior 11-12")]
        stats = apply_manual_urls(rows, [decision("2")], ShareIndex(rows))
        self.assertEqual(rows[1]["course_url"], SECTION)
        self.assertIn("share_accepted_by_human", rows[1]["row_flags"])
        self.assertEqual(stats.shared_pages, 1)

    def test_a_page_nobody_else_holds_is_not_flagged(self):
        rows = [row("1", "Secondary Junior 7-10")]
        stats = apply_manual_urls(rows, [decision("1")], ShareIndex(rows))
        self.assertNotIn("share_accepted_by_human", rows[0]["row_flags"])
        self.assertEqual(stats.shared_pages, 0)

    def test_an_off_domain_url_is_applied_and_flagged(self):
        """A state curriculum page can legitimately be the right answer."""
        rows = [row("1", "Secondary Junior 7-10")]
        stats = apply_manual_urls(rows, [decision("1", url=OFFSITE)])
        self.assertEqual(rows[0]["course_url"], OFFSITE)
        self.assertIn("url_off_institution_domain", rows[0]["row_flags"])
        self.assertEqual(stats.off_domain, 1)

    def test_an_on_domain_url_is_not_flagged(self):
        rows = [row("1", "Data Science")]
        apply_manual_urls(rows, [decision("1", url=DS)])
        self.assertNotIn("url_off_institution_domain", rows[0]["row_flags"])


class TestMistakesAreLoud(unittest.TestCase):
    """A typo that silently does nothing wastes an afternoon."""

    def test_an_unknown_id_is_reported(self):
        rows = [row("1", "Data Science")]
        stats = apply_manual_urls(rows, [decision("nope")])
        self.assertEqual(stats.unknown_ids, ["nope"])
        self.assertEqual(stats.applied, 0)

    def test_a_non_http_url_is_reported(self):
        rows = [row("1", "Data Science")]
        stats = apply_manual_urls(rows, [decision("1", url="courses.aber.ac.uk")])
        self.assertEqual(len(stats.bad_urls), 1)
        self.assertIn("1", stats.bad_urls[0])
        self.assertEqual(rows[0]["course_url"], "")

    def test_an_empty_url_is_reported(self):
        rows = [row("1", "Data Science")]
        stats = apply_manual_urls(rows, [decision("1", url="")])
        self.assertEqual(len(stats.bad_urls), 1)

    def test_valid_rows_in_the_same_file_still_apply(self):
        rows = [row("1", "Data Science"), row("2", "Anthropology")]
        stats = apply_manual_urls(
            rows, [decision("nope"), decision("2", url=DS)])
        self.assertEqual((stats.applied, stats.rejected), (1, 1))
        self.assertEqual(rows[1]["course_url"], DS)

    def test_a_rejected_entry_leaves_its_row_untouched(self):
        rows = [row("1", "Data Science", url=DS, status="verified")]
        before = dict(rows[0])
        apply_manual_urls(rows, [decision("1", url="not-a-url")])
        self.assertEqual(rows[0], before)


class TestLoading(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "manual_urls.csv")

    def write(self, entries):
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["id", "course_url", "note",
                                               "decided_by", "decided_at"])
            w.writeheader()
            w.writerows(entries)

    def test_a_missing_file_is_a_silent_no_op(self):
        """Most checkouts carry no human decisions at all."""
        self.assertEqual(load_manual_urls(self.path), [])

    def test_entries_are_read(self):
        self.write([decision("1"), decision("2")])
        self.assertEqual(len(load_manual_urls(self.path)), 2)

    def test_blank_id_rows_are_skipped(self):
        self.write([decision("1"), decision("")])
        self.assertEqual([e["id"] for e in load_manual_urls(self.path)], ["1"])

    def test_a_note_with_a_comma_survives(self):
        self.write([decision("1", note="No page, so the section page instead")])
        self.assertIn("No page, so",
                      load_manual_urls(self.path)[0]["note"])


class TestEvidenceText(unittest.TestCase):
    def test_an_unattributed_decision_says_so(self):
        self.assertIn("unattributed", _evidence(decision("1", who="")))

    def test_a_missing_note_is_omitted_cleanly(self):
        text = _evidence(decision("1", note=""))
        self.assertTrue(text.endswith("2026-09-10"), text)

    def test_a_missing_date_is_omitted_cleanly(self):
        self.assertNotIn(" on :", _evidence(decision("1", when="")))


class TestStats(unittest.TestCase):
    def test_rejected_counts_both_kinds(self):
        s = ManualStats(unknown_ids=["a"], bad_urls=["b: ''", "c: ''"])
        self.assertEqual(s.rejected, 3)


if __name__ == "__main__":
    unittest.main()
