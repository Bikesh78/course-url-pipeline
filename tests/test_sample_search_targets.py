"""Choosing a paid search batch. Hermetic — a temporary CSV and database."""

import contextlib
import csv
import io
import os
import sqlite3
import tempfile
import unittest

from pipeline.store import Store
from tools.sample_search_targets import candidates, main, read_ids, spread

COLUMNS = ["id", "name", "institution_name", "course_url", "matched_status",
           "website", "row_flags"]


def row(rid, site, status="no_catalog", url="", inst="Example College",
        flags=""):
    return {"id": rid, "name": f"Course {rid}", "institution_name": inst,
            "course_url": url, "matched_status": status,
            "website": f"https://{site}", "row_flags": flags}


class SamplerCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.results = os.path.join(self._dir.name, "phase2.csv")
        self.db = os.path.join(self._dir.name, "t.db")
        Store(self.db).close()

    def write_rows(self, rows):
        with open(self.results, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(rows)

    def set_diagnosis(self, site, diagnosis, healthy=0):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT OR REPLACE INTO catalogs (site_key, run_id, strategy, "
            "candidates, healthy, diagnosis, notes) "
            "VALUES (?, 'r1', 's', 0, ?, ?, '')", (site, healthy, diagnosis))
        conn.commit()
        conn.close()

    def run_tool(self, *extra):
        """Run the sampler, swallowing its report."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["--results", self.results, "--db", self.db, *extra])
        return code, out.getvalue()


class TestSpread(unittest.TestCase):
    """One row per Site before a second, so a batch is not one institution.

    The first 500 `no_catalog` rows of the real sheet in file order cover only
    66 of 716 Sites, with monash.edu alone holding 563.
    """

    def rows(self, per_site, sites=("a.edu", "b.edu", "c.edu")):
        out = []
        for site in sites:
            for i in range(per_site):
                out.append(row(f"{site}-{i}", site))
        return out

    def test_one_row_per_site_comes_first(self):
        picked = spread(self.rows(10), limit=3)
        self.assertEqual(sorted(r["website"] for r in picked),
                         ["https://a.edu", "https://b.edu", "https://c.edu"])

    def test_a_second_row_is_only_taken_once_every_site_has_one(self):
        picked = spread(self.rows(10), limit=4)
        sites = [r["website"] for r in picked]
        self.assertEqual(len(set(sites[:3])), 3)
        self.assertEqual(sites[3], sites[0], "the walk resumes at the first site")

    def test_it_stops_at_the_limit(self):
        self.assertEqual(len(spread(self.rows(10), limit=7)), 7)

    def test_it_cannot_exceed_what_is_available(self):
        self.assertEqual(len(spread(self.rows(1), limit=99)), 3)

    def test_per_site_caps_the_depth(self):
        picked = spread(self.rows(10), limit=99, per_site=2)
        self.assertEqual(len(picked), 6)

    def test_the_same_input_draws_the_same_batch(self):
        rows = self.rows(10)
        self.assertEqual([r["id"] for r in spread(rows, 5)],
                         [r["id"] for r in spread(rows, 5)])

    def test_a_lopsided_population_is_still_spread(self):
        """One site with 100 rows must not swallow the budget."""
        rows = [row(f"big-{i}", "big.edu") for i in range(100)]
        rows += [row("small-0", "small.edu")]
        picked = spread(rows, limit=2)
        self.assertEqual({r["website"] for r in picked},
                         {"https://big.edu", "https://small.edu"})


class TestFiltering(SamplerCase):
    def test_status_is_honoured(self):
        rows = [row("1", "a.edu", status="no_catalog"),
                row("2", "a.edu", status="no_match")]
        self.assertEqual(
            [r["id"] for r in candidates(rows, {"no_catalog"}, set(), {}, set())],
            ["1"])

    def test_filled_rows_are_never_candidates(self):
        rows = [row("1", "a.edu", url="https://a.edu/x")]
        self.assertEqual(candidates(rows, set(), set(), {}, set()), [])

    def test_inactive_records_are_never_candidates(self):
        rows = [row("1", "a.edu", inst="(Inactive) Example College")]
        self.assertEqual(candidates(rows, set(), set(), {}, set()), [])

    def test_occupation_codes_are_never_candidates(self):
        rows = [row("1", "a.edu", flags="occupation_code_not_course")]
        self.assertEqual(candidates(rows, set(), set(), {}, set()), [])

    def test_diagnosis_selects_the_cohort(self):
        rows = [row("1", "a.edu"), row("2", "b.edu")]
        diag = {"a.edu": "no_hub", "b.edu": "blocked"}
        self.assertEqual(
            [r["id"] for r in candidates(rows, set(), {"no_hub"}, diag, set())],
            ["1"])

    def test_excluded_ids_are_dropped(self):
        rows = [row("1", "a.edu"), row("2", "a.edu")]
        self.assertEqual(
            [r["id"] for r in candidates(rows, set(), set(), {}, {"1"})],
            ["2"])


class TestBatchFiles(SamplerCase):
    def test_it_writes_one_id_per_line(self):
        self.write_rows([row("1", "a.edu"), row("2", "b.edu")])
        target = os.path.join(self._dir.name, "batch.txt")
        self.run_tool("--limit", "2", "--out", target)
        self.assertEqual(read_ids([target]), {"1", "2"})

    def test_a_later_batch_excluding_the_first_is_disjoint(self):
        self.write_rows([row(str(i), "a.edu") for i in range(6)])
        one = os.path.join(self._dir.name, "b1.txt")
        two = os.path.join(self._dir.name, "b2.txt")
        self.run_tool("--limit", "3", "--out", one)
        self.run_tool("--limit", "3", "--exclude", one, "--out", two)
        self.assertEqual(read_ids([one]) & read_ids([two]), set())
        self.assertEqual(len(read_ids([one]) | read_ids([two])), 6)

    def test_appending_does_not_redraw_what_the_file_holds(self):
        """The second cohort must not duplicate the first."""
        self.write_rows([row(str(i), "a.edu") for i in range(4)])
        target = os.path.join(self._dir.name, "batch.txt")
        self.run_tool("--limit", "2", "--out", target)
        self.run_tool("--limit", "2", "--append", target)
        with open(target, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        self.assertEqual(len(lines), len(set(lines)), "ids were duplicated")
        self.assertEqual(len(lines), 4)

    def test_report_only_writes_nothing(self):
        self.write_rows([row("1", "a.edu")])
        code, text = self.run_tool("--limit", "1")
        self.assertEqual(code, 0)
        self.assertIn("report only", text)

    def test_asking_for_a_diagnosis_without_a_catalogs_table_fails_loudly(self):
        """Silently ignoring the filter would spend on the wrong cohort."""
        self.write_rows([row("1", "a.edu")])
        empty = os.path.join(self._dir.name, "absent.db")
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--results", self.results, "--db", empty,
                         "--diagnosis", "no_hub", "--limit", "1"])
        self.assertEqual(code, 2)

    def test_the_two_cohort_workflow_reaches_the_asked_for_size(self):
        rows = [row(f"h{i}", f"hub{i}.edu") for i in range(8)]
        rows += [row(f"b{i}", f"blk{i}.edu") for i in range(4)]
        self.write_rows(rows)
        for i in range(8):
            self.set_diagnosis(f"hub{i}.edu", "no_hub")
        for i in range(4):
            self.set_diagnosis(f"blk{i}.edu", "blocked")
        target = os.path.join(self._dir.name, "batch.txt")
        self.run_tool("--diagnosis", "no_hub", "--limit", "6", "--out", target)
        self.run_tool("--diagnosis", "blocked", "--limit", "2",
                      "--append", target)
        ids = read_ids([target])
        self.assertEqual(len(ids), 8)
        self.assertEqual(len({i for i in ids if i.startswith("h")}), 6)
        self.assertEqual(len({i for i in ids if i.startswith("b")}), 2)


if __name__ == "__main__":
    unittest.main()
