"""Catalog extraction unit tests. Hermetic — no network."""

import unittest

from pipeline.fetch import FetchResult
from pipeline.catalog import (
    build_catalog,
    SCHEMA_VERSION, Candidate, Catalog, _AUTH_PATH, _slug_to_name,
    classify_probes, clean_url, extract_links, level_from_url,
    looks_like_course_name, page_heading, page_title, url_specificity,
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


class TestProbeClassification(unittest.TestCase):
    """Telling "the site refuses us" apart from "we guessed wrong paths"."""

    def test_acu_shape_is_blocked(self):
        # ACU answers 403 to all 16 hub paths. This is the real observation
        # that motivated the whole distinction.
        self.assertEqual(classify_probes({403: 16}), "blocked")

    def test_all_404_is_a_wrong_guess_not_a_refusal(self):
        self.assertEqual(classify_probes({404: 16}), "no_hub")

    def test_any_working_hub_clears_the_verdict(self):
        # Aberystwyth's real shape: 4 hubs respond, 12 are 404.
        self.assertEqual(classify_probes({200: 4, 404: 12}), "")

    def test_other_refusal_statuses_count(self):
        for code in (401, 429, 451):
            self.assertEqual(classify_probes({code: 10}), "blocked", code)

    def test_threshold_is_a_share_not_unanimity(self):
        # A site that leaks one 404 among its refusals is still blocked.
        self.assertEqual(classify_probes({403: 8, 404: 2}), "blocked")
        self.assertEqual(classify_probes({403: 7, 404: 3}), "no_hub")

    def test_no_resolved_probes_asserts_nothing(self):
        # Every probe failed at transport level: we learned nothing, and must
        # not claim the site blocked us.
        self.assertEqual(classify_probes({}), "")


class TestAuthSeedGuard(unittest.TestCase):
    def test_login_paths_are_recognised(self):
        # Observed: catalogue.abertay.ac.uk/mng/login answered 200 and yielded
        # no Candidates.
        for path in ("/mng/login", "/login/", "/signin", "/sign-in/",
                     "/auth", "/account/"):
            self.assertTrue(_AUTH_PATH.search(path), path)

    def test_ordinary_course_paths_are_not(self):
        for path in ("/undergraduate/data-science/", "/courses/",
                     "/study/accounting/", "/course-structure/ug/fbl/"):
            self.assertIsNone(_AUTH_PATH.search(path), path)


class TestFailureReason(unittest.TestCase):
    def _cat(self, n, reason=""):
        return Catalog("X", [Candidate(f"Course {i} BSc", f"https://x/{i}")
                             for i in range(n)], failure_reason=reason)

    def test_round_trips_through_json_form(self):
        cat = Catalog("X", [Candidate("A", "https://x/a", "ug", "listing")],
                      strategy="listing", failure_reason="thin",
                      seed_yield={"https://x/": 1, "https://y/": 0})
        back = Catalog.from_dict(cat.as_dict())
        self.assertEqual(back.failure_reason, "thin")
        self.assertEqual(back.seed_yield, {"https://x/": 1, "https://y/": 0})

    def test_absent_fields_default_cleanly(self):
        # A v2 catalog read by v3 code must not explode.
        back = Catalog.from_dict({"institution": "X", "candidates": []})
        self.assertEqual(back.failure_reason, "")
        self.assertEqual(back.seed_yield, {})

    def test_schema_version_is_stamped(self):
        self.assertEqual(Catalog("X").as_dict()["schema_version"],
                         SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 3)


class _StubFetcher:
    """Serves canned responses by URL predicate. No network."""

    def __init__(self, rules):
        self.rules = rules            # list of (predicate, status, body)

    def get(self, url):
        for pred, status, body in self.rules:
            if pred(url):
                return FetchResult(url, status, url, body)
        return FetchResult(url, 404, url, "")

    def sitemaps_from_robots(self, url):
        return []


class TestBlockedOutranksSymptoms(unittest.TestCase):
    """A refused site must be reported as refused, not as its side effects.

    ACU answers 403 to every hub path, but its `catalogue.` subdomain — the
    *library*, not the course catalog — answers 200. That incidental seed made
    the Catalog non-empty, so an earlier version filed ACU as `no_candidates`
    and hid the access problem entirely.
    """

    def _build(self):
        rules = [
            (lambda u: "catalogue.acu.edu.au" in u, 200,
             "<html><title>Australian Catholic University</title></html>"),
            (lambda u: "www.acu.edu.au" in u, 403, ""),
        ]
        return build_catalog(_StubFetcher(rules), "ACU",
                             "https://www.acu.edu.au", 201)

    def test_reason_is_blocked_not_no_candidates(self):
        cat = self._build()
        self.assertEqual(cat.failure_reason, "blocked")

    def test_the_incidental_seed_was_still_taken(self):
        # The point is not that the seed is rejected -- it is that it does not
        # disguise the refusal.
        cat = self._build()
        self.assertTrue(cat.seeds)
        self.assertFalse(cat.healthy(201))

    def test_a_site_that_merely_404s_is_not_blocked(self):
        rules = [(lambda u: "courses.x.ac.uk" in u, 200, "<html></html>")]
        cat = build_catalog(_StubFetcher(rules), "X", "https://www.x.ac.uk", 201)
        self.assertNotEqual(cat.failure_reason, "blocked")


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
