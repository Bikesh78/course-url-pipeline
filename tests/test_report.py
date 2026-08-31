"""Coverage-report diagnosis tests. Hermetic — no network, no real run."""

import os
import tempfile
import unittest

from pipeline.catalog import Candidate
from pipeline.load import CourseRow
from pipeline.match import MatchResult
from pipeline.report import write_coverage_report

URL = "https://x.ac.uk/courses/a/"


def rows(inst, n, filled=0):
    out = []
    for i in range(n):
        r = MatchResult(row=CourseRow(f"{inst}-{i}", f"Course {i} BSc", inst,
                                      "https://x.ac.uk"))
        if i < filled:
            r.candidate = Candidate(f"Course {i}", f"{URL}{i}", "ug")
            r.score, r.status = 1.0, "confident"
        out.append(r)
    return out


def render(results, health):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "coverage.md")
        write_coverage_report(results, health, path)
        with open(path, encoding="utf-8") as fh:
            return fh.read()


class TestBlockedIsSeparatedFromMistargeted(unittest.TestCase):
    """The defect this change exists to fix.

    ACU refuses the crawler (403 on every hub path); Concordia is crawled fine
    but the crawl lands on the wrong pages. Reporting both as one bucket told
    the reader that ACU's rows were "recoverable by crawling", which is false.
    """

    def setUp(self):
        self.results = rows("ACU", 20) + rows("Concordia", 20, filled=1)
        self.health = {
            "ACU": {"candidates": 0, "strategy": "none", "healthy": False,
                    "failure_reason": "blocked", "seed_yield": {}},
            "Concordia": {"candidates": 750, "strategy": "listing",
                          "healthy": True, "failure_reason": "",
                          "seed_yield": {}},
        }
        self.out = render(self.results, self.health)

    def test_blocked_institution_is_labelled_blocked(self):
        self.assertIn("**BLOCKED**", self.out)

    def test_mistargeted_institution_is_labelled_mistargeted(self):
        self.assertIn("**MISTARGETED**", self.out)

    def test_both_sections_exist(self):
        self.assertIn("Fixable by crawling", self.out)
        self.assertIn("Not fixable by crawling", self.out)

    def test_blocked_is_not_listed_as_crawl_fixable(self):
        fixable = self.out.split("Fixable by crawling")[1] \
                          .split("Not fixable by crawling")[0]
        self.assertNotIn("ACU", fixable)
        self.assertIn("Concordia", fixable)

    def test_unreachable_section_names_the_blocked_institution(self):
        unreachable = self.out.split("Not fixable by crawling")[1]
        self.assertIn("ACU", unreachable)

    def test_report_warns_against_counting_blocked_rows_as_recoverable(self):
        self.assertIn("Do not count these rows as recoverable by crawling",
                      self.out)


class TestOtherDiagnoses(unittest.TestCase):
    def _one(self, reason, healthy=False, candidates=0):
        return render(rows("X", 10), {
            "X": {"candidates": candidates, "strategy": "listing",
                  "healthy": healthy, "failure_reason": reason,
                  "seed_yield": {}}})

    def test_no_candidates_is_crawl_fixable(self):
        out = self._one("no_candidates")
        self.assertIn("**NO CANDIDATES**", out)
        self.assertIn("X", out.split("Fixable by crawling")[1])

    def test_thin_is_crawl_fixable(self):
        self.assertIn("**THIN**", self._one("thin", candidates=20))

    def test_no_hub_is_crawl_fixable(self):
        out = self._one("no_hub")
        self.assertIn("**NO HUB**", out)
        self.assertIn("X", out.split("Fixable by crawling")[1])

    def test_no_website_is_not_crawl_fixable(self):
        out = self._one("no_website")
        self.assertIn("**NO WEBSITE**", out)
        self.assertIn("X", out.split("Not fixable by crawling")[1])

    def test_healthy_and_well_filled_gets_no_section(self):
        results = rows("X", 10, filled=8)
        out = render(results, {"X": {"candidates": 50, "strategy": "listing",
                                     "healthy": True, "failure_reason": "",
                                     "seed_yield": {}}})
        self.assertNotIn("Fixable by crawling", out)
        self.assertNotIn("Not fixable by crawling", out)


class TestDeadSeedReporting(unittest.TestCase):
    def test_zero_yield_seeds_are_listed(self):
        out = render(rows("Curtin", 10, filled=5), {
            "Curtin": {"candidates": 867, "strategy": "listing",
                       "healthy": True, "failure_reason": "",
                       "seed_yield": {"https://handbook.curtin.edu.au/": 25,
                                      "https://catalogue.curtin.edu.au/": 0}}})
        self.assertIn("Seeds that yielded nothing", out)
        self.assertIn("catalogue.curtin.edu.au", out)

    def test_productive_seeds_are_not_listed_as_dead(self):
        out = render(rows("Curtin", 10, filled=5), {
            "Curtin": {"candidates": 867, "strategy": "listing",
                       "healthy": True, "failure_reason": "",
                       "seed_yield": {"https://handbook.curtin.edu.au/": 25,
                                      "https://catalogue.curtin.edu.au/": 0}}})
        dead = out.split("Seeds that yielded nothing")[1]
        self.assertNotIn("handbook.curtin.edu.au", dead)

    def test_no_section_when_every_seed_produced_something(self):
        out = render(rows("X", 10, filled=5), {
            "X": {"candidates": 50, "strategy": "listing", "healthy": True,
                  "failure_reason": "", "seed_yield": {"https://x/": 50}}})
        self.assertNotIn("Seeds that yielded nothing", out)


if __name__ == "__main__":
    unittest.main()
