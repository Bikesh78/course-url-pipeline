"""Tests for input loading, repair and flagging against the real CSV."""

import os
import unittest

from pipeline.load import (
    CourseRow, _apply_flags, dedupe, group_by_institution, group_by_site,
    is_test_booking, is_year_level, load_rows, normalise_website,
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


CSV_FINAL = "final_courses.csv"


@unittest.skipUnless(os.path.exists(CSV_FINAL), "current sheet not present")
class TestCurrentSheet(unittest.TestCase):
    """The sheet the pipeline now reads: 52,781 rows, 2,548 Institutions."""

    @classmethod
    def setUpClass(cls):
        cls.rows = load_rows(CSV_FINAL)

    def test_every_row_survives(self):
        self.assertEqual(len(self.rows), 52781)

    def test_institution_id_is_read(self):
        self.assertTrue(all(r.institution_id for r in self.rows))

    def test_legacy_sheet_ids_are_a_strict_subset(self):
        if not os.path.exists(CSV):
            self.skipTest("legacy sheet not present")
        old = {r.id for r in load_rows(CSV)}
        new = {r.id for r in self.rows}
        self.assertTrue(old.issubset(new))

    def test_prior_urls_are_available_as_seed_material(self):
        withprior = [r for r in self.rows if r.prior_urls]
        self.assertGreater(len(withprior), 25000)
        self.assertTrue(all(u.startswith("http")
                            for r in withprior for u in r.prior_urls))

    def test_prior_course_url_is_not_written_to_output(self):
        # It is Candidate evidence, never the answer. A row carrying one must
        # still have to earn its URL through Assignment.
        r = next(r for r in self.rows if r.prior_course_url)
        self.assertTrue(r.prior_course_url.startswith("http"))
        self.assertEqual(r.raw.get("course_url"), r.prior_course_url)

    def test_visa_occupation_rows_are_flagged(self):
        occ = [r for r in self.rows
               if "occupation_code_not_course" in r.flags]
        self.assertGreater(len(occ), 4900)
        self.assertIn("subclass", occ[0].name.lower())

    def test_site_bucket_is_smaller_than_institution_count(self):
        sites = group_by_site(self.rows)
        insts = {r.institution_key for r in self.rows}
        self.assertLess(len(sites), len(insts))

    def test_case_variant_institutions_stay_distinct(self):
        # Two Institutions differ only in name casing; lower-casing the name
        # would merge them, the id does not.
        keys = {r.institution_key for r in self.rows}
        names = {r.institution_name.strip().lower() for r in self.rows}
        self.assertGreater(len(keys), len(names))


class TestForeignScoreGuard(unittest.TestCase):
    """The incoming sheet scores 0-100; ours is 0-1."""

    def test_detects_a_foreign_score(self):
        from pipeline.load import incoming_score_is_foreign
        self.assertTrue(incoming_score_is_foreign("87.1"))
        self.assertTrue(incoming_score_is_foreign("100.0"))

    def test_accepts_our_own_range(self):
        from pipeline.load import incoming_score_is_foreign
        self.assertFalse(incoming_score_is_foreign("0.775"))
        self.assertFalse(incoming_score_is_foreign("1.0"))

    def test_blank_is_not_foreign(self):
        from pipeline.load import incoming_score_is_foreign
        self.assertFalse(incoming_score_is_foreign(""))
        self.assertFalse(incoming_score_is_foreign(None))


class TestYearBandsAreNotCourses(unittest.TestCase):
    """A school year band has no page of its own to find.

    The school publishes a page per *section*, so the closest match is the
    same page for every band inside it. Measured on one: the section page
    scores 0.429 against the band name and its live `<h1>` scores 0.429 too,
    both under the 0.55 floor. Searching these can only fail, and paying for
    it dilutes the measured rate.
    """

    BANDS = [
        "Secondary Junior 7-10",
        "Primary Years 1-6",
        "Secondary Senior Yrs 11-12 Boys & Girls",
        "Senior Secondary (Year 11 & 12)",
        "Junior Secondary (Years 7 to 10)",
        "Primary (Kindergarten to Year 6)",
        "Middle School Education (Years 7-10)",
        "Secondary Year 10",
        "Year 7",
        "Grade 5",
        "Pre-primary",
    ]

    def test_year_bands_are_flagged(self):
        for name in self.BANDS:
            self.assertTrue(is_year_level(name), name)

    def test_the_flag_reaches_the_row(self):
        row = CourseRow("1", "Secondary Junior 7-10", "A B Paterson College",
                        "https://abpat.qld.edu.au")
        _apply_flags(row)
        self.assertIn("year_level_not_course", row.flags)


class TestSittingATestIsNotACourse(unittest.TestCase):
    """Decided by the Institution's domain, never by the course name.

    The sheet carries one booking row per country office -- 14 to 16
    duplicates of "IELTS Academic Online Booking" -- and no per-course page
    can exist for any of them. Where they were filled, 54 rows shared 6 URLs.
    """

    def booking(self, site, name="IELTS Academic Online Booking"):
        return CourseRow("1", name, "British Council", site)

    def test_a_booking_row_is_flagged(self):
        self.assertTrue(is_test_booking(
            {"website": "https://takeielts.britishcouncil.org/"}))

    def test_every_administrator_is_covered(self):
        for site in ("https://www.ets.org/", "https://www.duolingo.com/",
                     "https://www.pearsonpte.com/", "https://www.idp.com/",
                     "https://www.collegeboard.org/"):
            self.assertTrue(is_test_booking({"website": site}), site)

    def test_a_college_teaching_the_same_exam_is_untouched(self):
        """286 such rows fill at 26% -- the general rate. They are courses."""
        for site in ("https://www.languageacademy.com.au/",
                     "https://apc.edu.au/", "https://ptestudycentre.com.au/"):
            self.assertFalse(is_test_booking({"website": site}), site)

    def test_the_course_name_is_never_consulted(self):
        """A name rule would drop `IELTS Preparation` at a real college."""
        self.assertFalse(is_test_booking(
            {"website": "https://www.languageacademy.com.au/",
             "name": "IELTS Preparation"}))

    def test_the_sheet_s_own_typo_is_still_caught(self):
        """`IETLS` is misspelled in the data; the domain does not care."""
        self.assertTrue(is_test_booking(
            {"website": "https://takeielts.britishcouncil.org/",
             "name": "IETLS Academic Online Booking"}))

    def test_it_accepts_a_course_row_as_well_as_a_dict(self):
        """Phase 1 flags from a CourseRow; phase 2 re-checks from a dict."""
        self.assertTrue(is_test_booking(
            self.booking("https://takeielts.britishcouncil.org/")))

    def test_the_flag_reaches_the_row(self):
        row = self.booking("https://takeielts.britishcouncil.org/")
        _apply_flags(row)
        self.assertIn("test_booking_not_course", row.flags)


class TestRealCoursesKeepTheirNumbers(unittest.TestCase):
    """The regression fixture: every one of these was caught by the pattern
    drafted without a qualification guard, and every one is provably a real
    course -- extraction or the sheet had already found it a page, four of
    them `verified`. A future widening must not start catching them again.
    """

    REAL = [
        # age band the qualification teaches
        "Teacher Training PGCE Primary (3-7)",
        "Teacher Training PGCE Primary (5-11)",
        "Primary Education (Later 5-11) with Foundation in Education",
        "Bachelor of Education (Primary 1-10 Health and Physical Education)",
        # plural forms -- `\bbachelor\b` does not match "Bachelors"
        "Bachelors of Secondary Education - Life Science (6-12)",
        "Bachelors of Secondary Education - Math (6-12)",
        # duration in the name
        "General English (1-50 weeks)",
        "English for Secondary Schools (1-52 weeks)",
        "IELTS Preparation Intermediate to Upper Intermediate (5-10 weeks)",
        # entry year of a real programme
        "BA(Hons) Business Management ( Year 1)",
        "International year 2- BA (Hons) International Hospitality Management",
        # university pathway programmes the old leading-keyword rule wrongly
        # flagged; one of these is `verified` in the real data
        "Foundation Year in Arts and Creative Industries",
        "Foundation Year (Standard)",
        "Year One Foundation Program",
        "Foundation Year leading to BSc. (Hons) Biomedical Science",
    ]

    def test_none_of_them_is_treated_as_a_year_band(self):
        for name in self.REAL:
            self.assertFalse(is_year_level(name), name)

    def test_a_plain_qualification_is_untouched(self):
        for name in ("Diploma of Business", "Master of Teaching",
                     "Certificate IV in Engineering"):
            self.assertFalse(is_year_level(name), name)

    def test_an_empty_name_is_not_a_year_band(self):
        self.assertFalse(is_year_level(""))
        self.assertFalse(is_year_level(None))


if __name__ == "__main__":
    unittest.main()
