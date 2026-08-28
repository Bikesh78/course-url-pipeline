"""Catalog extraction unit tests. Hermetic — no network."""

import unittest

from pipeline.catalog import (
    Candidate, Catalog, _slug_to_name, clean_url, extract_links,
    level_from_url, looks_like_course_name, page_heading, page_title,
    url_specificity,
)

HTML = """
<html><head><title>Aberystwyth University - Data Science 7G73 BSc</title></head>
<body>
<a href="#main">Skip navigation &amp; go straight to the main content.</a>
<h1>Data Science</h1>
<a href="/undergraduate/data-science/">Data Science (BSc, 3 years)</a>
<a href="/undergraduate/data-science-iy/">Data Science (with integrated year in industry) (BSc, 4 years)</a>
<a href="/courses/">Courses</a>
<a href="/courses/undergraduate/request-prospectus/">Request Prospectus</a>
<a href="https://example.org/off-site/">Off Site Course</a>
</body></html>
"""


class TestLinkExtraction(unittest.TestCase):
    def setUp(self):
        self.links = extract_links(HTML, "https://courses.aber.ac.uk/")

    def test_makes_urls_absolute(self):
        urls = [u for _, u in self.links]
        self.assertIn("https://courses.aber.ac.uk/undergraduate/data-science/", urls)

    def test_captures_anchor_text(self):
        texts = [t for t, _ in self.links]
        self.assertIn("Data Science (BSc, 3 years)", texts)

    def test_reads_title_and_heading(self):
        self.assertEqual(page_title(HTML),
                         "Aberystwyth University - Data Science 7G73 BSc")
        self.assertEqual(page_heading(HTML), "Data Science")


class TestCourseNameFilter(unittest.TestCase):
    def test_accepts_a_real_course_name(self):
        self.assertTrue(looks_like_course_name("Data Science (BSc, 3 years)"))

    def test_rejects_navigation(self):
        for nav in ("Courses", "Request Prospectus", "Apply now", "Home",
                    "Undergraduate", "View all courses"):
            self.assertFalse(looks_like_course_name(nav), nav)

    def test_rejects_accessibility_skip_links(self):
        # These pass a word-count test and leaked into the Catalog until
        # excluded explicitly.
        self.assertFalse(looks_like_course_name(
            "Skip navigation & go straight to the main content."))

    def test_rejects_pagination_and_arrows(self):
        for junk in ("2", "»", " > ", "  "):
            self.assertFalse(looks_like_course_name(junk), repr(junk))

    def test_keeps_bare_credentials(self):
        self.assertTrue(looks_like_course_name("MBA"))
        self.assertTrue(looks_like_course_name("IELTS"))


class TestUrlHandling(unittest.TestCase):
    def test_drops_fragment_and_tracking(self):
        self.assertEqual(
            clean_url("https://a.ac.uk/courses/x/?utm_source=q&page=2#top"),
            "https://a.ac.uk/courses/x/?page=2")

    def test_drops_view_selecting_query_params(self):
        # The same course linked as ?term=2026-27 and ?term=2027-28 must
        # collapse to one Candidate, not compete with itself.
        a = clean_url("https://a.ac.uk/course-structure/ug/x/?term=2026-27")
        b = clean_url("https://a.ac.uk/course-structure/ug/x/?term=2027-28")
        self.assertEqual(a, b)
        self.assertEqual(a, "https://a.ac.uk/course-structure/ug/x/")

    def test_keeps_identifying_query_params(self):
        self.assertIn("courseId=123",
                      clean_url("https://a.ac.uk/courses/?courseId=123"))

    def test_rejects_non_http(self):
        self.assertEqual(clean_url("mailto:a@b.c"), "")

    def test_reads_level_from_path(self):
        self.assertEqual(
            level_from_url("https://a.ac.uk/undergraduate/x/"), "ug")
        self.assertEqual(
            level_from_url("https://a.ac.uk/postgraduate/x/"), "pg")
        self.assertIsNone(level_from_url("https://a.ac.uk/about/"))

    def test_course_page_scores_full_specificity(self):
        self.assertEqual(
            url_specificity("https://courses.aber.ac.uk/undergraduate/x/"), 1.0)

    def test_subject_hub_is_demoted(self):
        # A subject hub carries exactly the subject name and so ties with the
        # real course page; demotion is what breaks that tie.
        self.assertLess(
            url_specificity("https://www.aber.ac.uk/en/study-with-us/subjects/agriculture/"),
            1.0)

    def test_compound_subject_hub_is_demoted(self):
        # "/undergraduate-subjects/<x>/" is a hub even though no path segment
        # equals "subjects".
        self.assertLess(url_specificity(
            "https://www.coventry.ac.uk/study-at-coventry/undergraduate-study/"
            "undergraduate-subjects/english-creative-writing/"), 1.0)

    def test_real_course_page_keeps_full_specificity(self):
        self.assertEqual(url_specificity(
            "https://www.coventry.ac.uk/course-structure/ug/fbl/"
            "business-management-ba-hons/"), 1.0)

    def test_slug_becomes_a_weak_name(self):
        self.assertEqual(
            _slug_to_name("https://a.ac.uk/courses/data-science-degree/"),
            "data science")


class TestExtractionHealth(unittest.TestCase):
    def _cat(self, n):
        return Catalog("X", [Candidate(f"Course {i}", f"https://x/{i}")
                             for i in range(n)])

    def test_aberystwyth_shaped_catalog_is_healthy(self):
        self.assertTrue(self._cat(398).healthy(456))

    def test_abertay_shaped_catalog_is_unhealthy(self):
        # 13 candidates for 224 rows: a failed extraction, which must escalate
        # rather than be matched against.
        self.assertFalse(self._cat(13).healthy(224))

    def test_a_tiny_catalog_is_never_healthy(self):
        self.assertFalse(self._cat(2).healthy(2))

    def test_round_trips_through_json_form(self):
        cat = Catalog("X", [Candidate("A", "https://x/a", "ug", "listing")],
                      strategy="listing")
        back = Catalog.from_dict(cat.as_dict())
        self.assertEqual(back.institution, "X")
        self.assertEqual(back.strategy, "listing")
        self.assertEqual(back.candidates[0].url, "https://x/a")
        self.assertEqual(back.candidates[0].level, "ug")


if __name__ == "__main__":
    unittest.main()
