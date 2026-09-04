"""Catalog extraction unit tests. Hermetic — no network."""

import unittest

import pipeline.catalog as catalog_module

from pipeline.fetch import FetchResult
from pipeline.catalog import (
    build_catalog,
    HUB_PATHS, SCHEMA_VERSION, Candidate, Catalog, _AUTH_PATH,
    _drop_institution_names, _slug_to_name,
    classify_probes, clean_url, extract_links, host_is_refusing,
    level_from_url,
    looks_like_course_name, looks_like_soft_404, page_heading, page_title,
    url_specificity,
)

# Canonical form: clean_url() strips the trailing slash, so a fixture keyed
# with one would never be found.
ANTH = "https://courses.aber.ac.uk/undergraduate/anthropology"

# Observed seeds for this shape: the university's own course page (which
# refuses) and its International College (which works).
NEWCASTLE_PRIORS = [
    "https://www.newcastle.edu.au/degrees/",
    "https://internationalcollege.newcastle.edu.au/",
]

LISTING = ("<html><body>"
           "<a href='/study/undergraduate/business/'>Business BA (Hons)</a>"
           "<a href='/study/undergraduate/law/'>Law LLB (Hons)</a>"
           "</body></html>")

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


class TestMultiCoursePages(unittest.TestCase):
    """One Institution page can list several courses (ADR-0004)."""

    def test_candidate_identity_includes_the_variant_stem(self):
        a = Candidate("Anthropology BA (Hons)", ANTH, "ug")
        b = Candidate("Anthropology with Placement BA (Hons)", ANTH, "ug")
        c = Candidate("Archaeology and Anthropology BA (Hons)", ANTH, "ug")
        # Siblings collapse to one Candidate...
        self.assertEqual(a.key, b.key)
        # ...but a genuinely different course on the same page does not.
        self.assertNotEqual(a.key, c.key)

    def test_merge_keeps_several_courses_from_one_url(self):
        from pipeline.catalog import _merge
        first = [Candidate("Anthropology BA (Hons)", ANTH, "ug")]
        second = [Candidate("Archaeology and Anthropology BA (Hons)", ANTH, "ug")]
        merged = _merge(first, second)
        self.assertEqual(len(merged), 2)
        self.assertEqual({c.url for c in merged}, {ANTH})

    def test_merge_still_collapses_the_same_course(self):
        from pipeline.catalog import _merge
        terse = [Candidate("Anthropology", ANTH, "ug")]
        fuller = [Candidate("Anthropology BA (Hons)", ANTH, "ug")]
        merged = _merge(terse, fuller)
        self.assertEqual(len(merged), 1)
        # The longer anchor text wins.
        self.assertEqual(merged[0].name, "Anthropology BA (Hons)")


class _PageFetcher:
    """Serves canned pages keyed by exact URL, recording what was requested."""

    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def get(self, url):
        """Return the canned page for *url*, or a 404."""
        self.asked.append(url)
        status, text = self.pages.get(url, (404, ""))
        return FetchResult(url, status, url, text)

    def sitemaps_from_robots(self, url):
        """No sitemaps in this stub."""
        return []


class TestPriorUrlHarvesting(unittest.TestCase):
    """Prior URLs are seed material, named by the page, never by the claimer."""

    def test_name_comes_from_the_page_not_the_caller(self):
        from pipeline.catalog import harvest_prior_urls
        f = _PageFetcher({ANTH: (200, "<h1>Anthropology BA (Hons)</h1>")})
        got = harvest_prior_urls(f, [ANTH], {"aber.ac.uk"})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].name, "Anthropology BA (Hons)")
        self.assertEqual(got[0].source, "prior")

    def test_falls_back_to_title_without_a_heading(self):
        from pipeline.catalog import harvest_prior_urls
        f = _PageFetcher({ANTH: (200, "<title>Anthropology BA</title>")})
        got = harvest_prior_urls(f, [ANTH], {"aber.ac.uk"})
        self.assertEqual(got[0].name, "Anthropology BA")

    def test_dead_prior_url_yields_nothing(self):
        from pipeline.catalog import harvest_prior_urls
        got = harvest_prior_urls(_PageFetcher({}), [ANTH], {"aber.ac.uk"})
        self.assertEqual(got, [])

    def test_offsite_prior_url_is_not_fetched(self):
        from pipeline.catalog import harvest_prior_urls
        f = _PageFetcher({})
        harvest_prior_urls(f, ["https://shorelight.com/x/"], {"aber.ac.uk"})
        self.assertEqual(f.asked, [])

    def test_a_navigation_heading_is_rejected(self):
        from pipeline.catalog import harvest_prior_urls
        f = _PageFetcher({ANTH: (200, "<h1>Apply now</h1>")})
        self.assertEqual(harvest_prior_urls(f, [ANTH], {"aber.ac.uk"}), [])


class TestSourcePreference(unittest.TestCase):
    def test_listing_name_beats_a_page_derived_one(self):
        """A listing name is independent evidence; a page name is not.

        Verification re-reads the page, so a page-derived name would agree with
        itself. Keeping the listing name preserves the corroboration that
        `verified` claims.
        """
        from pipeline.catalog import _merge
        listing = [Candidate("Anthropology (BA, 3 years)", ANTH, "ug",
                             source="listing")]
        prior = [Candidate("Anthropology with Placement BA (Hons)", ANTH,
                           "ug", source="prior")]
        merged = _merge(prior, listing)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].source, "listing")

    def test_prior_name_beats_a_sitemap_slug(self):
        from pipeline.catalog import _merge
        slug = [Candidate("anthropology", ANTH, "ug", source="sitemap")]
        prior = [Candidate("Anthropology BA (Hons)", ANTH, "ug",
                           source="prior")]
        merged = _merge(slug, prior)
        self.assertEqual(merged[0].source, "prior")


class TestUrlHandling(unittest.TestCase):
    def test_drops_fragment_and_tracking(self):
        self.assertEqual(
            clean_url("https://a.ac.uk/courses/x/?utm_source=q&page=2#top"),
            "https://a.ac.uk/courses/x?page=2")

    def test_drops_view_selecting_query_params(self):
        # The same course linked as ?term=2026-27 and ?term=2027-28 must
        # collapse to one Candidate, not compete with itself.
        a = clean_url("https://a.ac.uk/course-structure/ug/x/?term=2026-27")
        b = clean_url("https://a.ac.uk/course-structure/ug/x/?term=2027-28")
        self.assertEqual(a, b)
        self.assertEqual(a, "https://a.ac.uk/course-structure/ug/x")

    def test_keeps_identifying_query_params(self):
        self.assertIn("courseId=123",
                      clean_url("https://a.ac.uk/courses/?courseId=123"))

    def test_trailing_slash_does_not_make_a_second_url(self):
        """`/x` and `/x/` are one page, and were two Candidates.

        729 rows in the full sheet reported a changed URL when only the slash
        differed, and a Share Group could hold the same page twice.
        """
        self.assertEqual(clean_url("https://a.ac.uk/courses/x"),
                         clean_url("https://a.ac.uk/courses/x/"))

    def test_the_root_path_keeps_its_slash(self):
        # Stripping it would yield a bare host rather than a path.
        self.assertEqual(clean_url("https://a.ac.uk/"), "https://a.ac.uk/")
        self.assertEqual(clean_url("https://a.ac.uk"), "https://a.ac.uk/")

    def test_strips_whitespace_from_hrefs(self):
        """Real anchors carry stray whitespace; it reached the output."""
        self.assertEqual(clean_url("https://a.ac.uk/x "),
                         clean_url("https://a.ac.uk/x"))
        self.assertEqual(clean_url("  https://a.ac.uk/x  "),
                         "https://a.ac.uk/x")

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


class TestHostIsRefusing(unittest.TestCase):
    """Did one specific host refuse us? Separate from `classify_probes`.

    `classify_probes` reads 16 *guessed* paths and must tolerate a site whose
    URLs simply differ. This reads a host's own answers, where the signal is
    unambiguous. Fixtures are real per-host counts from a measured run.
    """

    def test_a_host_that_only_ever_refuses(self):
        self.assertTrue(host_is_refusing({403: 45}))          # www.monash.edu
        self.assertTrue(host_is_refusing({403: 60}))          # www.newcastle
        self.assertTrue(host_is_refusing({403: 100}))         # www.herts.ac.uk

    def test_a_healthy_host_is_not_refusing(self):
        self.assertFalse(host_is_refusing({200: 408, 404: 10}))   # www.sydney
        self.assertFalse(host_is_refusing({200: 65, 404: 12}))    # www.aber

    def test_a_404_only_host_is_not_refusing(self):
        """404 says "wrong path", not "no". That is `no_hub`, not blocked."""
        self.assertFalse(host_is_refusing({404: 16}))

    def test_one_refusal_among_successes_is_not_refusing(self):
        # A restricted page on an otherwise open host.
        self.assertFalse(host_is_refusing({200: 100, 403: 3}))

    def test_nothing_observed_is_not_refusing(self):
        self.assertFalse(host_is_refusing({}))

    def test_every_refusal_status_counts(self):
        for code in (401, 403, 429, 451):
            self.assertTrue(host_is_refusing({code: 5}), code)


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


class TestRefusalOnTheSiteOwnHost(unittest.TestCase):
    """A site whose own host refuses is `blocked`, whatever a satellite does.

    Both fixtures are real shapes from a measured run. Hub probing alone missed
    them, because seeds derived from prior URLs never pass through it and the
    working satellite drowned the refusal in any aggregate count.
    """

    # Faithful to the observed run: the 16 guessed hub paths 404, and only the
    # real course page — reached from a prior URL, never through hub probing —
    # refuses. Making the hub paths 403 instead would let the pre-existing
    # `classify_probes` catch it and the test would pass without the fix.
    NEWCASTLE = [
        (lambda u: "internationalcollege.newcastle.edu.au" in u, 200, LISTING),
        (lambda u: u.rstrip("/").endswith("/degrees"), 403, ""),
        (lambda u: "www.newcastle.edu.au" in u, 404, ""),
    ]

    def test_hub_404s_but_the_real_course_page_refuses(self):
        cat = build_catalog(_StubFetcher(self.NEWCASTLE), "Newcastle",
                            "https://www.newcastle.edu.au", 545,
                            prior_urls=NEWCASTLE_PRIORS)
        self.assertEqual(cat.failure_reason, "blocked")

    def test_without_the_own_host_check_it_would_read_as_no_hub(self):
        """Pins *why* this test exists, so it cannot silently stop testing it.

        Neutralising `host_is_refusing` must change the verdict. If it does not,
        the fixture is being caught by hub-probe classification instead and is
        no longer exercising the fix.
        """
        original = catalog_module.host_is_refusing
        catalog_module.host_is_refusing = lambda counts: False
        try:
            cat = build_catalog(
                _StubFetcher(self.NEWCASTLE), "Newcastle",
                "https://www.newcastle.edu.au", 545,
                prior_urls=NEWCASTLE_PRIORS)
        finally:
            catalog_module.host_is_refusing = original
        self.assertNotEqual(cat.failure_reason, "blocked")

    def test_a_working_satellite_does_not_disguise_the_refusal(self):
        """The satellite yields Candidates; the site is still reported blocked."""
        cat = build_catalog(_StubFetcher(self.NEWCASTLE), "Newcastle",
                            "https://www.newcastle.edu.au", 545,
                            prior_urls=NEWCASTLE_PRIORS)
        self.assertTrue(cat.candidates, "satellite should still contribute")
        self.assertEqual(cat.failure_reason, "blocked")

    def test_a_genuinely_thin_site_is_still_thin(self):
        """The guard: ordinary shortfalls must not be relabelled as refusals."""
        rules = [(lambda u: "www.thin.ac.uk" in u, 200, LISTING)]
        cat = build_catalog(_StubFetcher(rules), "Thin",
                            "https://www.thin.ac.uk", 400)
        self.assertFalse(cat.healthy(400))
        self.assertNotEqual(cat.failure_reason, "blocked")

    def test_a_refusing_satellite_does_not_block_a_healthy_site(self):
        """Sydney's `intranet.` answers 403 nine times; Sydney is not blocked."""
        rules = [
            (lambda u: "intranet.sydney.edu.au" in u, 403, ""),
            (lambda u: "sydney.edu.au" in u, 200, LISTING),
        ]
        cat = build_catalog(_StubFetcher(rules), "Sydney",
                            "https://www.sydney.edu.au", 4,
                            prior_urls=["https://intranet.sydney.edu.au/x/"])
        self.assertNotEqual(cat.failure_reason, "blocked")


class TestCatalogLogging(unittest.TestCase):
    """The log must show what was found, bounded, without re-running anything."""

    def _records(self, verbose=False):
        import json, logging, tempfile
        from pipeline.logging_setup import setup_logging
        d = tempfile.mkdtemp()
        path = setup_logging("2026-01-01T00-00-00Z-test", log_dir=d,
                             quiet=True, verbose=verbose)
        rules = [(lambda u: "courses.x.ac.uk" in u, 200, LISTING),
                 (lambda u: True, 200, LISTING)]
        build_catalog(_StubFetcher(rules), "X", "https://www.x.ac.uk", 4)
        logging.shutdown()
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_catalog_built_carries_a_bounded_sample(self):
        """A 4,767-Candidate Catalog must not put 4,767 entries in the log."""
        rec = next(r for r in self._records() if r.get("event") == "catalog.built")
        self.assertIn("sample", rec)
        self.assertLessEqual(len(rec["sample"]), 10)
        self.assertIn("name", rec["sample"][0])
        self.assertIn("url", rec["sample"][0])

    def test_seeds_are_shown_not_just_counted(self):
        rec = next(r for r in self._records() if r.get("event") == "seeds.found")
        self.assertTrue(rec["seeds"], "the actual seed URLs must be logged")
        self.assertEqual(rec["seed_count"], len(rec["seeds"]))

    def test_skipped_links_are_counted_even_though_few_are_named(self):
        """Aggregate, not per link: naming each produced 40,562 records/site."""
        rec = next(r for r in self._records() if r.get("event") == "crawl.done")
        self.assertIn("links_skipped", rec)
        self.assertIsInstance(rec["links_skipped"], dict)

    def test_per_page_detail_only_appears_when_verbose(self):
        quiet = [r for r in self._records() if r.get("event") == "crawl.page"]
        loud = [r for r in self._records(verbose=True)
                if r.get("event") == "crawl.page"]
        self.assertEqual(quiet, [])
        self.assertTrue(loud)


class TestPageBudget(unittest.TestCase):
    """The crawl budget scales with the rows a Site has to resolve."""

    def test_a_tiny_site_gets_the_floor_not_the_ceiling(self):
        from pipeline.catalog import page_budget, MIN_PAGE_BUDGET
        # The median Site has 5 rows. A flat 160-page budget across 2,220 Sites
        # would be ~355,000 fetches, mostly on Sites like this one.
        self.assertEqual(page_budget(5), MIN_PAGE_BUDGET)

    def test_a_large_site_is_capped(self):
        from pipeline.catalog import page_budget, MAX_PAGE_BUDGET
        self.assertEqual(page_budget(4899), MAX_PAGE_BUDGET)

    def test_budget_grows_with_rows_in_between(self):
        from pipeline.catalog import page_budget
        self.assertLess(page_budget(20), page_budget(100))

    def test_zero_and_negative_rows_are_safe(self):
        from pipeline.catalog import page_budget, MIN_PAGE_BUDGET
        self.assertEqual(page_budget(0), MIN_PAGE_BUDGET)
        self.assertEqual(page_budget(-5), MIN_PAGE_BUDGET)


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


class TestCandidateIdentityIgnoresInstitution(unittest.TestCase):
    """One page must be one Candidate, however the site spelled its name.

    A Candidate's identity is (URL, Variant Stem), and the stem was taken from
    the raw anchor text. So a site that prints the same page as "Student
    Support" in one listing and "Student Support | Webster University" in
    another produced two Candidates for one page. Both then score identically
    against a Course Row, the Margin between them is zero, and the row is filed
    `ambiguous` with nothing to choose between. On the finished sheet, 494 rows
    carried an Institution word their course name lacked and exactly one
    reached `verified`, against a 19.7% baseline.
    """

    def test_two_spellings_of_one_page_collapse(self):
        got = _drop_institution_names([
            Candidate("Bachelor of Animal and Veterinary Bioscience", ANTH),
            Candidate("Bachelor of Animal and Veterinary Bioscience "
                      "- The University of Sydney", ANTH),
        ], "The University of Sydney")
        self.assertEqual(len(got), 1)
        self.assertNotIn("sydney", got[0].name.lower())

    def test_distinct_courses_on_one_page_still_survive(self):
        # The whole point of ADR-0004: a shared page holds several courses, and
        # collapsing on the URL alone would discard all but one. Stripping the
        # Institution must not widen that collapse. (Variant Siblings --
        # "Anthropology" and "Anthropology with Placement" -- do share a
        # Variant Stem and so are one Candidate by design, before and after
        # this change; distinct subjects are the case that must survive.)
        got = _drop_institution_names([
            Candidate("Anthropology - University of Sydney", ANTH),
            Candidate("Archaeology - University of Sydney", ANTH),
        ], "University of Sydney")
        self.assertEqual(len(got), 2)

    def test_a_name_that_is_only_the_institution_is_kept_as_found(self):
        # strip_institution can leave nothing; dropping the name entirely would
        # make the Candidate unscoreable rather than merely unhelpful.
        got = _drop_institution_names(
            [Candidate("Webster University", ANTH)], "Webster University")
        self.assertEqual(got[0].name, "Webster University")

    def test_build_catalog_applies_it(self):
        # The health checks along the ladder count Candidates, so the collapse
        # has to happen as they enter -- not once at the end.
        listing = (
            f'<a href="{ANTH}">Anthropology BA | Aberystwyth University</a>'
            f'<a href="{ANTH}">Anthropology BA</a>')
        f = _PageFetcher({"https://www.aber.ac.uk/courses": (200, listing)})
        cat = build_catalog(f, "Aberystwyth University",
                            "https://www.aber.ac.uk", expected_rows=1)
        urls = [c for c in cat.candidates if c.url == ANTH]
        self.assertEqual(len(urls), 1, [c.name for c in cat.candidates])


class TestSoft404Seeds(unittest.TestCase):
    """A 200 that says "page not found" is not a listing.

    Lambton College answers 200 with "Error - Page not Found | Lambton
    College" for six separate hub probes, and its Catalog still filled with
    1,295 Candidates scraped off that error page's own navigation. Across the
    finished run, 339 of 8,158 HTTP-200 seeds (4.2%, over 36 sites) were these.
    """

    def test_title_signature_is_detected(self):
        for html in (
            "<title>Error - Page not Found | Lambton College</title>",
            "<title>Page Not Found</title>",
            "<html><body><h1>404 Error</h1></body></html>",
            "<title>Oops</title><h1>The page you requested does not exist</h1>",
        ):
            with self.subTest(html=html[:40]):
                self.assertTrue(looks_like_soft_404(html))

    def test_a_listing_that_merely_mentions_the_phrase_is_kept(self):
        # Read from the title and headings, never the body prose: a real
        # listing can mention it in passing and must not be thrown away.
        self.assertFalse(looks_like_soft_404(
            "<title>Undergraduate Courses</title><body>Browse our courses. "
            "If you get a page not found error, contact us.</body>"))
        self.assertFalse(looks_like_soft_404(
            "<title>Courses | Lambton College</title><h1>Programs</h1>"))

    def test_soft_404_is_not_accepted_as_a_seed(self):
        err = "<title>Error - Page not Found | Lambton College</title><nav>" \
              '<a href="/programs/nursing">Nursing</a></nav>'
        pages = {f"https://www.lambton.ca{p}": (200, err) for p in HUB_PATHS}
        f = _PageFetcher(pages)
        seeds, notes, statuses = catalog_module.find_seeds(
            f, "https://www.lambton.ca")
        self.assertEqual(seeds, [])
        self.assertTrue(any("soft 404" in n for n in notes), notes)

    def test_a_site_of_soft_404s_is_no_hub_not_responded(self):
        # The status histogram must record the 404 it is, not the 200 it
        # claims, or the site is filed under a symptom no crawling can fix.
        err = "<title>Page not found</title>"
        pages = {f"https://www.lambton.ca{p}": (200, err) for p in HUB_PATHS}
        _, _, statuses = catalog_module.find_seeds(
            _PageFetcher(pages), "https://www.lambton.ca")
        self.assertNotIn(200, statuses)
        self.assertEqual(classify_probes(statuses), "no_hub")

    def test_a_real_listing_still_becomes_a_seed(self):
        ok = '<title>Courses</title><a href="/programs/nursing">Nursing</a>'
        f = _PageFetcher({"https://www.lambton.ca/courses": (200, ok)})
        seeds, _, statuses = catalog_module.find_seeds(
            f, "https://www.lambton.ca")
        self.assertEqual(seeds, ["https://www.lambton.ca/courses"])
        self.assertEqual(statuses.get(200), 1)
