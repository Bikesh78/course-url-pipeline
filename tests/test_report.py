"""Coverage-report diagnosis tests. Hermetic — no network, no real run."""

import csv
import os
import tempfile
import unittest

from pipeline.catalog import Candidate
from pipeline.load import CourseRow
from pipeline.match import MatchResult
from pipeline.report import (OUTPUT_COLUMNS, PHASE_2_STATUSES,
                             phase_for, write_coverage_report,
                             write_filled_csv)
from pipeline.search import SEARCH_FOUND
from pipeline.triage import CARRIED_OVER

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


class TestPhaseColumn(unittest.TestCase):
    """`phase` names which phase produced the delivered URL.

    It carries nothing `matched_status` does not already carry -- across the
    full sheet, every row phase 2 actually decided is `carried_over` or
    `search_found`, with zero exceptions. It exists so a reader can filter
    `phase == 2` without first learning that vocabulary, which makes the
    invariant below the whole justification for the column: the moment it can
    disagree with `matched_status`, it is a bug rather than a convenience.
    """

    URL = "https://courses.aber.ac.uk/undergraduate/data-science"

    def test_every_phase_2_status_maps_to_2(self):
        for status in PHASE_2_STATUSES:
            self.assertEqual(phase_for(status, self.URL), "2", status)

    def test_extraction_statuses_map_to_1(self):
        for status in ("verified", "probable", "ambiguous", "url_dead"):
            self.assertEqual(phase_for(status, self.URL), "1", status)

    def test_a_row_with_no_url_is_blank_whatever_its_status(self):
        """No URL means no phase delivered one; `1` would overstate it."""
        for status in ("no_match", "no_catalog", "verified", "carried_over"):
            self.assertEqual(phase_for(status, ""), "", status)
            self.assertEqual(phase_for(status, "   "), "", status)

    def test_an_unknown_status_is_treated_as_extraction(self):
        self.assertEqual(phase_for("something_new", self.URL), "1")

    def test_the_statuses_come_from_the_constants_not_literals(self):
        """Renaming a status must not leave this mapping stale."""
        self.assertEqual(set(PHASE_2_STATUSES), {CARRIED_OVER, SEARCH_FOUND})

    def test_it_is_appended_after_the_provenance_columns(self):
        """Appended, not inserted; `phase1_*` were later appended after it."""
        self.assertGreater(OUTPUT_COLUMNS.index("phase"),
                           OUTPUT_COLUMNS.index("url_change"))


class TestPhaseNeverDisagreesWithStatus(unittest.TestCase):
    """The invariant, over a written file rather than in the abstract."""

    def written(self, results):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, "filled.csv")
        write_filled_csv(results, path)
        with open(path, encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def result(self, rid, url, status):
        row = CourseRow(rid, "Data Science BSc", "Aberystwyth",
                        "https://www.aber.ac.uk")
        cand = Candidate("Data Science", url, "ug") if url else None
        # `url` is derived from the Candidate, not assignable.
        return MatchResult(row=row, candidate=cand, score=0.9, margin=0.4,
                           status=status)

    def test_a_filled_extraction_row_is_phase_1(self):
        got = self.written([self.result("1", TestPhaseColumn.URL, "confident")])
        self.assertEqual(got[0]["matched_status"], "verified")
        self.assertEqual(got[0]["phase"], "1")

    def test_an_unfilled_row_is_blank(self):
        got = self.written([self.result("1", "", "no_match")])
        self.assertEqual(got[0]["phase"], "")

    def test_no_written_row_can_contradict_its_status(self):
        results = [self.result("1", TestPhaseColumn.URL, "confident"),
                   self.result("2", TestPhaseColumn.URL, "probable"),
                   self.result("3", "", "no_catalog")]
        for row in self.written(results):
            expected = phase_for(row["matched_status"], row["course_url"])
            self.assertEqual(row["phase"], expected)


if __name__ == "__main__":
    unittest.main()
