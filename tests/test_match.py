"""Assignment tests, including the ADR-0002 uniqueness rail. Hermetic."""

import unittest

from pipeline.catalog import Candidate, Catalog
from pipeline.load import CourseRow
from pipeline.match import Thresholds, assign, score_all

ABER = "Aberystwyth University"
SITE = "https://www.aber.ac.uk"

DS = "https://courses.aber.ac.uk/undergraduate/data-science/"
DS_IY = "https://courses.aber.ac.uk/undergraduate/data-science-iy/"
HUB = "https://www.aber.ac.uk/en/study-with-us/subjects/data-science/"
ANTH = "https://courses.aber.ac.uk/undergraduate/anthropology/"
MATHS = "https://courses.aber.ac.uk/undergraduate/mathematics/"


def catalog(*cands):
    return Catalog(ABER, list(cands), strategy="listing")


def row(rid, name):
    return CourseRow(rid, name, ABER, SITE)


class TestSharingRail(unittest.TestCase):
    """Distinct courses take distinct URLs; Variant Siblings may share one."""

    def setUp(self):
        self.rows = [
            row("1", "Data Science BSc (Hons)"),
            row("2", "Data Science (with integrated year in industry) BSc (Hons)"),
        ]
        self.cat = catalog(
            Candidate("Data Science (BSc, 3 years)", DS, "ug"),
            Candidate("Data Science (with integrated year in industry) "
                      "(BSc, 4 years)", DS_IY, "ug"),
        )

    def test_each_row_gets_its_own_url(self):
        res = assign(self.rows, self.cat, ABER)
        urls = {r.row.id: r.url for r in res}
        self.assertEqual(urls["1"], DS)
        self.assertEqual(urls["2"], DS_IY)

    def test_distinct_courses_do_not_share(self):
        # These two are NOT Variant Siblings: "integrated year in industry" is
        # stripped as a delivery variant, but they still resolve to separate
        # pages the Site publishes, so each must hold its own.
        res = assign(self.rows, self.cat, ABER)
        used = [r.url for r in res if r.url]
        self.assertEqual(len(used), len(set(used)))

    def test_both_reach_confident(self):
        res = assign(self.rows, self.cat, ABER)
        self.assertTrue(all(r.status == "confident" for r in res))

    def test_identical_rows_share_rather_than_starve(self):
        """Under ADR-0004 identical rows share; under ADR-0002 one was starved.

        This is the behaviour change the sharing rail exists to produce. Dedupe
        in `run.py` still matters — it saves scoring cycles — but it is no
        longer what prevents a duplicate row from being left empty.
        """
        rows = [row("1", "Data Science BSc (Hons)"),
                row("2", "Data Science BSc (Hons)")]
        cat = catalog(Candidate("Data Science (BSc, 3 years)", DS, "ug"))
        res = assign(rows, cat, ABER)
        self.assertEqual([r.url for r in res], [DS, DS])
        for r in res:
            self.assertIn("variant_sibling_share=2", r.flags)

    def test_placement_variant_shares_the_base_page(self):
        """The requirement that prompted ADR-0004."""
        rows = [row("1", "Anthropology BA (Hons)"),
                row("2", "Anthropology with Placement BA (Hons)")]
        cat = catalog(Candidate("Anthropology BA (Hons)", ANTH, "ug"))
        res = assign(rows, cat, ABER)
        self.assertEqual([r.url for r in res], [ANTH, ANTH])

    def test_non_sibling_is_denied_and_records_why(self):
        """The counterexample Score cannot separate: both pairs score 0.775."""
        rows = [row("1", "Anthropology BA (Hons)"),
                row("2", "Archaeology and Anthropology BA (Hons)")]
        cat = catalog(Candidate("Anthropology BA (Hons)", ANTH, "ug"))
        res = {r.row.id: r for r in assign(rows, cat, ABER)}
        self.assertEqual(res["1"].url, ANTH)
        self.assertEqual(res["2"].url, "")
        self.assertIn("share_denied_stem_mismatch", res["2"].flags)
        self.assertIn(f"denied_url={ANTH}", res["2"].flags)
        self.assertIn("denied_held_by=1", res["2"].flags)

    def test_award_class_mismatch_is_not_a_sibling(self):
        # A page holding a BSc and an MSc of one subject is a multi-course
        # page, not one course in two variants.
        rows = [row("1", "Mathematics BSc (Hons)"),
                row("2", "Mathematics MSc")]
        cat = catalog(Candidate("Mathematics BSc (Hons)", MATHS, "ug"))
        res = {r.row.id: r for r in assign(rows, cat, ABER)}
        self.assertEqual(res["1"].url, MATHS)
        self.assertEqual(res["2"].url, "")

    def test_share_group_is_capped(self):
        from pipeline.match import SHARE_CAP
        rows = [row(str(i), "Data Science BSc (Hons)")
                for i in range(SHARE_CAP + 4)]
        cat = catalog(Candidate("Data Science (BSc, 3 years)", DS, "ug"))
        res = assign(rows, cat, ABER)
        filled = [r for r in res if r.url]
        self.assertEqual(len(filled), SHARE_CAP)
        denied = [r for r in res if not r.url]
        self.assertEqual(len(denied), 4)
        for r in denied:
            self.assertIn("share_denied_cap_reached", r.flags)

    def test_the_116_way_collapse_cannot_reform(self):
        """The real failure in the source sheet, as a regression test.

        116 courses were collapsed onto one Master of Laws page. Our scorer
        rates the victims 0.130-0.141 against a 0.55 floor, so they never even
        reach the sharing rule.
        """
        page = "https://www.herts.ac.uk/courses/master-of-laws-llm-with-placement-year"
        rows = [row("1", "LLM Master of Laws (with Placement Year)"),
                row("2", "BA (Hons) 2D Digital Animation (4 Years with Placement)"),
                row("3", "BA (Hons) Creative Writing (4 Years with Placement)")]
        cat = Catalog("Hertfordshire",
                      [Candidate("Master of Laws LLM with Placement Year",
                                 page, "pg")],
                      strategy="listing")
        res = {r.row.id: r for r in assign(rows, cat, "University of Hertfordshire")}
        self.assertEqual(res["1"].url, page)
        self.assertEqual(res["2"].url, "")
        self.assertEqual(res["3"].url, "")


class TestStatusAssignment(unittest.TestCase):
    def test_below_floor_is_no_match(self):
        rows = [row("1", "Veterinary Nursing FdSc")]
        cat = catalog(Candidate("Data Science (BSc, 3 years)", DS, "ug"))
        res = assign(rows, cat, ABER)
        self.assertEqual(res[0].status, "no_match")
        self.assertEqual(res[0].url, "")

    def test_a_tie_is_ambiguous(self):
        rows = [row("1", "Agriculture BSc (Hons)")]
        cat = catalog(
            Candidate("Agriculture", "https://courses.aber.ac.uk/undergraduate/a/", "ug"),
            Candidate("Agriculture", "https://courses.aber.ac.uk/undergraduate/b/", "ug"),
        )
        res = assign(rows, cat, ABER)
        self.assertEqual(res[0].status, "ambiguous")
        self.assertLess(res[0].margin, Thresholds().min_margin)

    def test_hub_page_never_reaches_confident(self):
        rows = [row("1", "Data Science BSc (Hons)")]
        cat = catalog(Candidate("Data Science", HUB, None))
        res = assign(rows, cat, ABER)
        self.assertIn("hub_page_match", res[0].flags)
        self.assertNotEqual(res[0].status, "confident")

    def test_sitemap_sourced_name_never_reaches_confident(self):
        rows = [row("1", "Data Science BSc (Hons)")]
        cat = catalog(Candidate("data science", DS, "ug", source="sitemap"))
        res = assign(rows, cat, ABER)
        self.assertIn("name_from_slug", res[0].flags)
        self.assertNotEqual(res[0].status, "confident")

    def test_empty_catalog_yields_no_matches(self):
        res = assign([row("1", "Data Science BSc (Hons)")], catalog(), ABER)
        self.assertEqual(res[0].status, "no_match")

    def test_evidence_names_the_runner_up(self):
        rows = [row("1", "Data Science BSc (Hons)")]
        cat = catalog(
            Candidate("Data Science (BSc, 3 years)", DS, "ug"),
            Candidate("Data Science (with integrated year in industry) "
                      "(BSc, 4 years)", DS_IY, "ug"),
        )
        res = assign(rows, cat, ABER)
        self.assertIn("runner-up", res[0].evidence)
        self.assertIn(DS_IY, res[0].evidence)


class TestDisplacement(unittest.TestCase):
    """A row that loses its best URL to a stronger claim must say so."""

    def test_second_choice_is_flagged_and_has_negative_margin(self):
        strong = row("1", "Data Science BSc (Hons)")
        weak = row("2", "Data Science and Statistics BSc (Hons)")
        cat = catalog(
            Candidate("Data Science (BSc, 3 years)", DS, "ug"),
            Candidate("Data Science (with integrated year in industry) "
                      "(BSc, 4 years)", DS_IY, "ug"),
        )
        res = {r.row.id: r for r in assign([strong, weak], cat, ABER)}
        loser = res["2"]
        if loser.url:
            self.assertLess(loser.margin, 0)
            self.assertIn("displaced_took_lower_candidate", loser.flags)

    def test_undisplaced_row_has_no_such_flag(self):
        cat = catalog(Candidate("Data Science (BSc, 3 years)", DS, "ug"))
        res = assign([row("1", "Data Science BSc (Hons)")], cat, ABER)
        self.assertNotIn("displaced_took_lower_candidate", res[0].flags)
        self.assertGreaterEqual(res[0].margin, 0)


class TestDomainContainment(unittest.TestCase):
    """A Course Row may only receive a URL on its own Institution's Site."""

    def test_offsite_url_is_dropped(self):
        cat = Catalog(ABER,
                      [Candidate("Data Science BSc",
                                 "https://shorelight.com/aber/data-science/",
                                 "ug")],
                      strategy="listing", domains=["aber.ac.uk"])
        res = assign([row("1", "Data Science BSc (Hons)")], cat, ABER)
        self.assertEqual(res[0].url, "")
        self.assertEqual(res[0].status, "no_match")
        self.assertTrue(any(f.startswith("offsite_url_dropped")
                            for f in res[0].flags))

    def test_course_subdomain_is_allowed(self):
        cat = Catalog(ABER,
                      [Candidate("Data Science (BSc, 3 years)", DS, "ug")],
                      strategy="listing", domains=["aber.ac.uk"])
        res = assign([row("1", "Data Science BSc (Hons)")], cat, ABER)
        self.assertEqual(res[0].url, DS)

    def test_no_recorded_domains_means_no_enforcement(self):
        # Catalogs cached before this check existed carry no domain list; they
        # must keep working rather than silently blanking every URL.
        cat = Catalog(ABER, [Candidate("Data Science (BSc, 3 years)", DS, "ug")],
                      strategy="listing")
        res = assign([row("1", "Data Science BSc (Hons)")], cat, ABER)
        self.assertEqual(res[0].url, DS)


class TestScoreComposition(unittest.TestCase):
    """The Score this module uses is not the one normalize.score() returned.

    `score_all` multiplies by `url_specificity`, a composition that spans two
    modules and is easy to miss when reading either one alone.
    """

    def test_hub_page_scores_below_its_name_similarity(self):
        from pipeline.catalog import url_specificity
        from pipeline.normalize import score as name_score

        cand = Candidate("Data Science", HUB, None)
        cat = catalog(cand)
        scored = score_all([row("1", "Data Science BSc (Hons)")], cat, ABER)
        composed = scored["1"][0][0]
        by_name = name_score("Data Science BSc (Hons)", cand.name, ABER,
                             candidate_level=cand.level)

        self.assertLess(composed, by_name)
        self.assertAlmostEqual(composed, round(by_name * url_specificity(HUB), 4),
                               places=4)

    def test_course_page_is_not_discounted(self):
        from pipeline.normalize import score as name_score

        cand = Candidate("Data Science (BSc, 3 years)", DS, "ug")
        scored = score_all([row("1", "Data Science BSc (Hons)")],
                           catalog(cand), ABER)
        self.assertAlmostEqual(
            scored["1"][0][0],
            name_score("Data Science BSc (Hons)", cand.name, ABER,
                       candidate_level="ug"), places=4)


class TestDemotionPrecedence(unittest.TestCase):
    """Demotions run *after* the confident/ambiguous/probable chain."""

    def test_a_result_that_qualifies_on_score_and_margin_is_still_demoted(self):
        th = Thresholds()
        cat = catalog(Candidate("Data Science", HUB, None),
                      Candidate("Basket Weaving", DS_IY, "ug"))
        res = assign([row("1", "Data Science BSc (Hons)")], cat, ABER)[0]

        # It clears both bars the visible chain tests...
        self.assertGreaterEqual(res.score, th.confident)
        self.assertGreaterEqual(res.margin, th.min_margin)
        # ...and is still not confident, because the URL is a hub page.
        self.assertEqual(res.status, "probable")
        self.assertIn("hub_page_match", res.flags)


class TestDeterminism(unittest.TestCase):
    def test_repeated_runs_produce_identical_assignments(self):
        rows = [row(str(i), n) for i, n in enumerate(
            ["Data Science BSc (Hons)", "Agriculture BSc (Hons)",
             "Data Science (with integrated year in industry) BSc (Hons)"])]
        cat = catalog(
            Candidate("Data Science (BSc, 3 years)", DS, "ug"),
            Candidate("Data Science (with integrated year in industry) "
                      "(BSc, 4 years)", DS_IY, "ug"),
            Candidate("Agriculture (BSc, 3 years)",
                      "https://courses.aber.ac.uk/undergraduate/agri/", "ug"),
        )
        first = [(r.row.id, r.url) for r in assign(rows, cat, ABER)]
        for _ in range(3):
            self.assertEqual([(r.row.id, r.url) for r in assign(rows, cat, ABER)],
                             first)


if __name__ == "__main__":
    unittest.main()
