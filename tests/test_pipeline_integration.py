"""Orchestrator-level tests. Hermetic — a stub fetcher, no network."""

import argparse
import os
import tempfile
import unittest

from pipeline.catalog import Candidate, Catalog
from pipeline.fetch import FetchResult, registrable
from pipeline.load import CourseRow
from pipeline.match import MatchResult, Thresholds
from run import catalog_path, clone_for, process_institution, slugify, verify

ABER = "Aberystwyth University"
DS = "https://courses.aber.ac.uk/undergraduate/data-science/"


class StubFetcher:
    """Serves canned pages and records what was asked for."""

    def __init__(self, pages: dict[str, tuple[int, str]]):
        self.pages = pages
        self.asked: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.asked.append(url)
        status, text = self.pages.get(url, (404, ""))
        return FetchResult(url, status, url, text)

    def sitemaps_from_robots(self, url: str):
        return []


def args(**kw):
    base = dict(refresh_catalogs=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestSlugging(unittest.TestCase):
    def test_institution_becomes_a_safe_filename(self):
        self.assertEqual(slugify("Curtin University - CU"), "curtin-university-cu")

    def test_path_stays_inside_the_catalog_directory(self):
        p = catalog_path("Australian National University - ANU")
        self.assertTrue(p.startswith("catalogs" + os.sep))
        self.assertTrue(p.endswith(".json"))


class TestCatalogSchemaVersioning(unittest.TestCase):
    """A Catalog missing a field a safety check needs must be rebuilt."""

    def test_stale_catalog_is_rebuilt(self):
        import json
        from pipeline.catalog import SCHEMA_VERSION
        import run as run_mod

        stale = {"institution": ABER, "strategy": "listing", "seeds": [],
                 "candidates": [{"name": "Data Science", "url": DS}]}
        built = {"called": False}

        def fake_build(fetcher, institution, website, expected_rows):
            built["called"] = True
            return Catalog(institution, [Candidate("Data Science", DS, "ug")],
                           strategy="listing", domains=["aber.ac.uk"])

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(stale, fh)
            orig_path, orig_build = run_mod.catalog_path, run_mod.build_catalog
            orig_dir = run_mod.CATALOG_DIR
            run_mod.catalog_path = lambda inst: path
            run_mod.build_catalog = fake_build
            run_mod.CATALOG_DIR = d
            try:
                cat = run_mod.load_or_build_catalog(
                    StubFetcher({}), ABER, "https://www.aber.ac.uk", 10)
            finally:
                run_mod.catalog_path = orig_path
                run_mod.build_catalog = orig_build
                run_mod.CATALOG_DIR = orig_dir

        self.assertTrue(built["called"], "stale catalog was reused")
        self.assertEqual(cat.domains, ["aber.ac.uk"])
        self.assertGreaterEqual(SCHEMA_VERSION, 2)

    def test_current_catalog_is_reused(self):
        import json
        import run as run_mod
        from pipeline.catalog import SCHEMA_VERSION

        current = {"schema_version": SCHEMA_VERSION, "institution": ABER,
                   "strategy": "listing", "seeds": [], "domains": ["aber.ac.uk"],
                   "candidates": [{"name": "Data Science", "url": DS,
                                   "level": "ug", "source": "listing"}]}
        built = {"called": False}

        def fake_build(*a, **k):
            built["called"] = True
            return Catalog(ABER)

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(current, fh)
            orig_path, orig_build = run_mod.catalog_path, run_mod.build_catalog
            run_mod.catalog_path = lambda inst: path
            run_mod.build_catalog = fake_build
            try:
                cat = run_mod.load_or_build_catalog(
                    StubFetcher({}), ABER, "https://www.aber.ac.uk", 10)
            finally:
                run_mod.catalog_path = orig_path
                run_mod.build_catalog = orig_build

        self.assertFalse(built["called"], "current catalog was needlessly rebuilt")
        self.assertEqual(len(cat.candidates), 1)


class TestFailClosed(unittest.TestCase):
    """An unhealthy Catalog must produce no URLs at all (ADR-0001)."""

    def test_unhealthy_catalog_fills_nothing(self):
        rows = [CourseRow(str(i), f"Course {i} BSc (Hons)", "Abertay University",
                          "https://www.abertay.ac.uk") for i in range(224)]
        thin = Catalog("Abertay University",
                       [Candidate("Course 1 BSc", "https://x/1", "ug")] * 1)

        def fake_loader(*a, **k):
            return thin

        import run as run_mod
        original = run_mod.load_or_build_catalog
        run_mod.load_or_build_catalog = fake_loader
        try:
            res, health = process_institution(
                StubFetcher({}), "Abertay University", rows,
                Thresholds(), args())
        finally:
            run_mod.load_or_build_catalog = original

        self.assertFalse(health["healthy"])
        self.assertEqual(len(res), 224)
        self.assertTrue(all(r.status == "no_catalog" for r in res))
        self.assertTrue(all(r.url == "" for r in res))


class TestDuplicateFanOut(unittest.TestCase):
    def test_clone_carries_the_result_and_is_flagged(self):
        base_row = CourseRow("1", "Data Science BSc (Hons)", ABER, "https://a")
        dup_row = CourseRow("2", "Data Science BSc (Hons)", ABER, "https://a")
        res = MatchResult(row=base_row,
                          candidate=Candidate("Data Science", DS, "ug"),
                          score=1.0, margin=0.4, status="confident")
        clone = clone_for(res, dup_row)
        self.assertEqual(clone.url, DS)
        self.assertEqual(clone.row.id, "2")
        self.assertIn("shared_with_duplicate_row", clone.flags)
        # The original must not gain the clone's flag.
        self.assertNotIn("shared_with_duplicate_row", res.flags)


class TestVerification(unittest.TestCase):
    def _result(self, url=DS, status="confident"):
        return MatchResult(
            row=CourseRow("1", "Data Science BSc (Hons)", ABER, "https://a"),
            candidate=Candidate("Data Science (BSc, 3 years)", url, "ug"),
            score=1.0, margin=0.5, status=status)

    def test_agreeing_live_page_keeps_confident(self):
        f = StubFetcher({DS: (200, "<h1>Data Science</h1>")})
        res = self._result()
        verify(f, res, Thresholds())
        self.assertEqual(res.status, "confident")

    def test_dead_url_is_marked(self):
        f = StubFetcher({})               # 404 for everything
        res = self._result()
        verify(f, res, Thresholds())
        self.assertEqual(res.status, "url_dead")

    def test_disagreeing_live_page_is_demoted(self):
        # The listing said "Data Science" but the page is something else: the
        # two independent pieces of evidence disagree, so confidence is lost.
        f = StubFetcher({DS: (200, "<h1>Welsh and Celtic Studies</h1>")})
        res = self._result()
        verify(f, res, Thresholds())
        self.assertEqual(res.status, "probable")
        self.assertIn("live_title_weaker_than_listing", res.flags)

    def test_falls_back_to_title_when_no_heading(self):
        f = StubFetcher({DS: (200,
                              "<title>Aberystwyth University - Data Science "
                              "7G73 BSc</title>")})
        res = self._result()
        verify(f, res, Thresholds())
        self.assertEqual(res.status, "confident")

    def test_unfilled_row_is_not_fetched(self):
        f = StubFetcher({})
        res = MatchResult(row=CourseRow("1", "X BSc", ABER, "https://a"))
        verify(f, res, Thresholds())
        self.assertEqual(f.asked, [])


class TestDomainInterleaving(unittest.TestCase):
    """Work must alternate domains, or every worker blocks on one lock."""

    def _res(self, host, n):
        return [MatchResult(
            row=CourseRow(f"{host}{i}", "X BSc", host, f"https://{host}"),
            candidate=Candidate("X", f"https://{host}/courses/{i}/", "ug"))
            for i in range(n)]

    def test_consecutive_items_differ_in_domain(self):
        from run import interleave_by_domain
        grouped = self._res("aber.ac.uk", 4) + self._res("curtin.edu.au", 4)
        order = interleave_by_domain(grouped)
        hosts = [registrable(r.url.split("/")[2]) for r in order]
        for a, b in zip(hosts, hosts[1:]):
            self.assertNotEqual(a, b, f"adjacent same-domain work: {hosts}")

    def test_nothing_is_lost_or_duplicated(self):
        from run import interleave_by_domain
        grouped = self._res("aber.ac.uk", 5) + self._res("curtin.edu.au", 3)
        order = interleave_by_domain(grouped)
        self.assertEqual(len(order), 8)
        self.assertEqual({r.row.id for r in order},
                         {r.row.id for r in grouped})

    def test_uneven_buckets_drain_cleanly(self):
        from run import interleave_by_domain
        grouped = self._res("a.ac.uk", 1) + self._res("b.ac.uk", 4)
        order = interleave_by_domain(grouped)
        self.assertEqual(len(order), 5)


class TestRateLimitGrouping(unittest.TestCase):
    def test_course_subdomain_shares_the_apex_budget(self):
        # Both must map to one key, or the crawler doubles its request rate
        # against a single Institution.
        self.assertEqual(registrable("courses.aber.ac.uk"),
                         registrable("www.aber.ac.uk"))

    def test_multi_part_tlds_are_handled(self):
        self.assertEqual(registrable("www.curtin.edu.au"), "curtin.edu.au")
        self.assertEqual(registrable("www.conestogac.on.ca"), "conestogac.on.ca")


class TestOutputWriters(unittest.TestCase):
    def test_filled_csv_has_every_row_and_the_new_columns(self):
        from pipeline.report import write_filled_csv, OUTPUT_COLUMNS
        import csv
        results = [
            MatchResult(row=CourseRow("1", "Data Science BSc (Hons)", ABER,
                                      "https://a"),
                        candidate=Candidate("Data Science", DS, "ug"),
                        score=1.0, margin=0.4, status="confident"),
            MatchResult(row=CourseRow("2", "Equine Science BSc (Hons)", ABER,
                                      "https://a"), status="no_match"),
        ]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.csv")
            write_filled_csv(results, path)
            with open(path, encoding="utf-8") as fh:
                got = list(csv.DictReader(fh))
        self.assertEqual(len(got), 2)
        self.assertEqual(list(got[0].keys()), OUTPUT_COLUMNS)
        self.assertEqual(got[0]["course_url"], DS)
        self.assertEqual(got[0]["matched_status"], "verified")
        self.assertEqual(got[1]["course_url"], "")
        self.assertEqual(got[1]["matched_status"], "no_match")
        self.assertEqual(got[1]["matched_score"], "")


if __name__ == "__main__":
    unittest.main()
