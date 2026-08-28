"""Assignment tests, including the ADR-0002 uniqueness rail. Hermetic."""

import unittest

from pipeline.catalog import Candidate, Catalog
from pipeline.load import CourseRow
from pipeline.match import Thresholds, assign

ABER = "Aberystwyth University"
SITE = "https://www.aber.ac.uk"

DS = "https://courses.aber.ac.uk/undergraduate/data-science/"
DS_IY = "https://courses.aber.ac.uk/undergraduate/data-science-iy/"
HUB = "https://www.aber.ac.uk/en/study-with-us/subjects/data-science/"


def catalog(*cands):
    return Catalog(ABER, list(cands), strategy="listing")


def row(rid, name):
    return CourseRow(rid, name, ABER, SITE)


class TestUniquenessRail(unittest.TestCase):
    """Two variant rows must not both take the same URL."""

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

    def test_no_url_is_used_twice(self):
        res = assign(self.rows, self.cat, ABER)
        used = [r.url for r in res if r.url]
        self.assertEqual(len(used), len(set(used)))

    def test_both_reach_confident(self):
        res = assign(self.rows, self.cat, ABER)
        self.assertTrue(all(r.status == "confident" for r in res))

    def test_two_rows_one_candidate_leaves_one_unmatched(self):
        """Why dedupe must run before Assignment.

        Identical Course Rows competing for one URL is the pathological case:
        one wins, the other is starved. `run.py` therefore collapses duplicate
        rows into a single representative before calling assign().
        """
        rows = [row("1", "Data Science BSc (Hons)"),
                row("2", "Data Science BSc (Hons)")]
        cat = catalog(Candidate("Data Science (BSc, 3 years)", DS, "ug"))
        res = assign(rows, cat, ABER)
        filled = [r for r in res if r.url]
        self.assertEqual(len(filled), 1)
        starved = next(r for r in res if not r.url)
        self.assertIn("url_claimed_by_stronger_match", starved.flags)


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
