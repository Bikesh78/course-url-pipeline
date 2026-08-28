"""Tests for input loading, repair and flagging against the real CSV."""

import os
import unittest

from pipeline.load import (
    CourseRow, dedupe, group_by_institution, load_rows, normalise_website,
)

CSV = "processed_courses.csv"


@unittest.skipUnless(os.path.exists(CSV), "input CSV not present")
class TestRealInput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_rows(CSV)

    def test_every_input_row_survives(self):
        self.assertEqual(len(self.rows), 10637)

    def test_phantom_institutions_are_not_counted(self):
        # The raw file yields 559 distinct column-2 values, but 4 of them are
        # debris from the malformed rows (" IT", " Asian Studies", ...).
        self.assertEqual(len(group_by_institution(self.rows)), 555)

    def test_dedupe_collapses_repeated_work(self):
        work = dedupe(self.rows)
        self.assertLess(len(work), len(self.rows))
        self.assertGreater(len(self.rows) - len(work), 500)

    def test_exactly_four_rows_needed_repair(self):
        repaired = [r for r in self.rows if "malformed_row_repaired" in r.flags]
        self.assertEqual(len(repaired), 4)

    def test_repaired_rows_recover_their_institution_and_site(self):
        for r in self.rows:
            if "malformed_row_repaired" in r.flags:
                self.assertEqual(r.institution_name,
                                 "Australian National University - ANU")
                self.assertEqual(r.website, "https://www.anu.edu.au")

    def test_repaired_name_is_rejoined(self):
        names = {r.name for r in self.rows
                 if "malformed_row_repaired" in r.flags}
        self.assertIn(
            "Graduate Non-Award (Economics and Commerce, Visual Arts and Music)",
            names)

    def test_unbalanced_parens_are_a_name_flag_not_a_repair(self):
        # "Journalism (Politics BA (Hons)" parses cleanly; only its name is
        # damaged. It must NOT be treated as a structural repair.
        row = next(r for r in self.rows
                   if r.name == "Journalism (Politics BA (Hons)")
        self.assertIn("truncated_name", row.flags)
        self.assertNotIn("malformed_row_repaired", row.flags)
        self.assertEqual(row.institution_name, "Brunel University London")

    def test_no_row_loses_its_id(self):
        self.assertTrue(all(r.id for r in self.rows))

    def test_prior_not_found_notes_are_readable_but_not_binding(self):
        nf = [r for r in self.rows if r.prior_said_not_found]
        self.assertGreater(len(nf), 200)

    def test_prior_note_urls_are_extracted(self):
        withurl = [r for r in self.rows if r.prior_note_url]
        self.assertGreaterEqual(len(withurl), 4)
        self.assertTrue(all(u.prior_note_url.startswith("http")
                            for u in withurl))


class TestWebsiteNormalisation(unittest.TestCase):
    def test_adds_scheme_to_bare_host(self):
        self.assertEqual(normalise_website("www.ashland.edu"),
                         "https://www.ashland.edu")

    def test_upgrades_http_to_https(self):
        self.assertEqual(normalise_website("http://www.ace.vic.edu.au"),
                         "https://www.ace.vic.edu.au")

    def test_rejects_a_uuid(self):
        self.assertEqual(
            normalise_website("42cdc3a0-faed-43fd-95fa-37cb46a8b094"), "")

    def test_rejects_empty(self):
        self.assertEqual(normalise_website(""), "")


class TestWorkKey(unittest.TestCase):
    def test_same_course_at_same_institution_shares_a_key(self):
        a = CourseRow("1", "Data Science BSc (Hons)", "Aber", "https://a")
        b = CourseRow("2", "Data Science  BSc  (Hons)", "aber", "https://a")
        self.assertEqual(a.work_key, b.work_key)

    def test_different_variants_do_not_share_a_key(self):
        a = CourseRow("1", "Data Science BSc (Hons)", "Aber", "https://a")
        b = CourseRow("2", "Data Science (with integrated year in industry) "
                           "BSc (Hons)", "Aber", "https://a")
        self.assertNotEqual(a.work_key, b.work_key)


if __name__ == "__main__":
    unittest.main()
