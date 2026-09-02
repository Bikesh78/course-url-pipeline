"""Chunking tests. The load-bearing property is that no Site is ever split."""

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.split_by_site import pack_sites, sha256_of  # noqa: E402
from run import chunk_suffix  # noqa: E402

SOURCE = "final_courses.csv"


class TestPacking(unittest.TestCase):
    """Bin packing: whole Sites only, remainder spread, bin count respected."""

    def test_every_site_lands_in_exactly_one_bin(self):
        order = [f"s{i}" for i in range(50)]
        rows = {h: (i % 7) + 1 for i, h in enumerate(order)}
        bins = pack_sites(order, rows, 5)
        flat = [h for b in bins for h in b]
        self.assertEqual(sorted(flat), sorted(order))
        self.assertEqual(len(flat), len(set(flat)))

    def test_no_more_bins_than_requested(self):
        order = [f"s{i}" for i in range(50)]
        rows = {h: 1 for h in order}
        self.assertLessEqual(len(pack_sites(order, rows, 5)), 5)

    def test_remainder_is_spread_not_dumped_in_the_last_bin(self):
        """A fixed target puts the whole remainder in the final chunk."""
        order = [f"s{i}" for i in range(100)]
        rows = {h: 10 for h in order}
        bins = pack_sites(order, rows, 10)
        sizes = [sum(rows[h] for h in b) for b in bins]
        self.assertLess(max(sizes), 2 * min(sizes),
                        f"bins are lopsided: {sizes}")

    def test_a_site_larger_than_the_target_gets_its_own_bin(self):
        # The 5,083-row visa-code site cannot be split, so it must overshoot
        # rather than be divided.
        order = ["big", "a", "b", "c"]
        rows = {"big": 1000, "a": 5, "b": 5, "c": 5}
        bins = pack_sites(order, rows, 4)
        holder = next(b for b in bins if "big" in b)
        self.assertEqual(holder, ["big"])

    def test_bins_are_never_empty(self):
        order = [f"s{i}" for i in range(6)]
        rows = {h: 100 for h in order}
        for b in pack_sites(order, rows, 6):
            self.assertTrue(b)

    def test_fewer_sites_than_bins_does_not_crash(self):
        bins = pack_sites(["a", "b"], {"a": 1, "b": 1}, 10)
        self.assertEqual(sorted(h for b in bins for h in b), ["a", "b"])


class TestChunkOutputNaming(unittest.TestCase):
    """Chunked runs must not overwrite each other's outputs."""

    def test_suffix_is_derived_from_the_chunk_file(self):
        self.assertEqual(chunk_suffix("chunks/final_courses.003.csv"), ".003")
        self.assertEqual(chunk_suffix("/abs/path/x.017.csv"), ".017")

    def test_the_whole_sheet_gets_no_suffix(self):
        self.assertEqual(chunk_suffix("final_courses.csv"), "")
        self.assertEqual(chunk_suffix(""), "")

    def test_two_chunks_produce_different_default_paths(self):
        a = f"courses_filled{chunk_suffix('chunks/final_courses.001.csv')}.csv"
        b = f"courses_filled{chunk_suffix('chunks/final_courses.002.csv')}.csv"
        self.assertNotEqual(a, b)


class TestStalenessGuard(unittest.TestCase):
    """A chunk cut from a superseded sheet must say so."""

    def _manifest(self, d, source, digest):
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"source": source, "source_sha256": digest}, fh)

    def test_warns_when_the_source_has_changed(self):
        import io
        import contextlib
        from run import check_chunk_freshness
        with tempfile.TemporaryDirectory() as d:
            source = os.path.join(d, "sheet.csv")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("id,name\n1,x\n")
            self._manifest(d, source, "0" * 64)          # deliberately wrong
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                check_chunk_freshness(os.path.join(d, "final_courses.001.csv"))
            self.assertIn("different version", err.getvalue())

    def test_silent_when_the_source_matches(self):
        import io
        import contextlib
        from run import check_chunk_freshness
        with tempfile.TemporaryDirectory() as d:
            source = os.path.join(d, "sheet.csv")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("id,name\n1,x\n")
            self._manifest(d, source, sha256_of(source))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                check_chunk_freshness(os.path.join(d, "final_courses.001.csv"))
            self.assertEqual(err.getvalue(), "")

    def test_no_manifest_is_not_an_error(self):
        from run import check_chunk_freshness
        with tempfile.TemporaryDirectory() as d:
            check_chunk_freshness(os.path.join(d, "final_courses.001.csv"))


class TestPartialMergeGuard(unittest.TestCase):
    """A partial merge must not take the canonical output filename."""

    def _setup(self, d, chunks_present, chunks_total):
        cdir = os.path.join(d, "chunks"); os.makedirs(cdir)
        man = {"source": "x.csv", "source_sha256": "0" * 64, "source_rows": 10,
               "no_website_rows": 0,
               "chunks": [{"file": f"final_courses.{i+1:03d}.csv", "sites": 1,
                           "rows": 1, "hosts": [f"h{i}"]}
                          for i in range(chunks_total)]}
        with open(os.path.join(cdir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(man, fh)
        for i in chunks_present:
            with open(os.path.join(d, f"courses_filled.{i:03d}.csv"), "w",
                      encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["id", "course_url", "matched_status"])
                w.writerow([f"id{i}", "https://x/", "verified"])
        return cdir

    def _run(self, d, cdir, extra=()):
        import subprocess
        return subprocess.run(
            [sys.executable, "tools/merge_chunks.py", "--chunk-dir", cdir,
             "--results-dir", d, "--db", os.path.join(d, "none.db"),
             "--out", os.path.join(d, "courses_filled.csv"),
             "--review-out", os.path.join(d, "review_queue.csv"),
             "--report-out", os.path.join(d, "coverage_report.md"), *extra],
            capture_output=True, text=True)

    def test_incomplete_merge_writes_a_partial_file(self):
        with tempfile.TemporaryDirectory() as d:
            cdir = self._setup(d, [1], 3)
            self._run(d, cdir)
            self.assertTrue(os.path.exists(
                os.path.join(d, "courses_filled.partial.csv")))
            self.assertFalse(os.path.exists(
                os.path.join(d, "courses_filled.csv")),
                "a 1-of-3 merge must not claim the canonical name")

    def test_complete_merge_uses_the_canonical_name(self):
        with tempfile.TemporaryDirectory() as d:
            cdir = self._setup(d, [1, 2, 3], 3)
            self._run(d, cdir)
            self.assertTrue(os.path.exists(
                os.path.join(d, "courses_filled.csv")))

    def test_allow_partial_overrides_the_guard(self):
        with tempfile.TemporaryDirectory() as d:
            cdir = self._setup(d, [1], 3)
            self._run(d, cdir, extra=("--allow-partial",))
            self.assertTrue(os.path.exists(
                os.path.join(d, "courses_filled.csv")))

    def test_duplicate_ids_across_chunks_are_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as d:
            cdir = self._setup(d, [1, 2, 3], 3)
            # Make chunk 2 collide with chunk 1 — the shape a bad split takes.
            with open(os.path.join(d, "courses_filled.002.csv"), "w",
                      encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["id", "course_url", "matched_status"])
                w.writerow(["id1", "https://x/", "verified"])
            r = self._run(d, cdir)
            self.assertIn("SPLIT IS WRONG", r.stdout)
            self.assertEqual(r.returncode, 1)


@unittest.skipUnless(os.path.exists(SOURCE), "source sheet not present")
class TestRealSplit(unittest.TestCase):
    """Run the real splitter and assert the invariants on its output."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        r = subprocess.run(
            [sys.executable, "tools/split_by_site.py", "--chunks", "8",
             "--out-dir", cls._dir.name],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(f"splitter failed: {r.stderr[-800:]}")
        with open(os.path.join(cls._dir.name, "manifest.json"),
                  encoding="utf-8") as fh:
            cls.manifest = json.load(fh)

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def _chunk_rows(self, entry):
        path = os.path.join(self._dir.name, entry["file"])
        with open(path, encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def test_no_site_appears_in_two_chunks(self):
        """The property the whole design rests on."""
        seen = {}
        for c in self.manifest["chunks"]:
            for h in c["hosts"]:
                self.assertNotIn(h, seen,
                                 f"site {h} in both {seen.get(h)} and {c['file']}")
                seen[h] = c["file"]

    def test_no_row_is_lost_or_duplicated(self):
        ids = []
        for c in self.manifest["chunks"]:
            ids += [r["id"] for r in self._chunk_rows(c)]
        nw = os.path.join(self._dir.name, "no_website.csv")
        if os.path.exists(nw):
            with open(nw, encoding="utf-8", newline="") as fh:
                ids += [r["id"] for r in csv.DictReader(fh)]
        with open(SOURCE, encoding="utf-8-sig", newline="") as fh:
            src = [r["id"] for r in csv.DictReader(fh)]
        self.assertEqual(len(ids), len(src))
        self.assertEqual(sorted(ids), sorted(src))

    def test_chunk_rows_match_the_source_rows_field_for_field(self):
        with open(SOURCE, encoding="utf-8-sig", newline="") as fh:
            src = {r["id"]: r for r in csv.DictReader(fh)}
        checked = 0
        for c in self.manifest["chunks"][:2]:
            for row in self._chunk_rows(c):
                self.assertEqual(row, src[row["id"]])
                checked += 1
        self.assertGreater(checked, 100)

    def test_uncrawlable_rows_are_separated_not_padded_into_chunks(self):
        # They would inflate a chunk's row count while costing no crawl time.
        self.assertGreater(self.manifest["no_website_rows"], 0)
        chunk_rows = sum(c["rows"] for c in self.manifest["chunks"])
        self.assertEqual(chunk_rows + self.manifest["no_website_rows"],
                         self.manifest["source_rows"])

    def test_manifest_records_a_checksum_for_staleness_detection(self):
        self.assertEqual(self.manifest["source_sha256"], sha256_of(SOURCE))
        self.assertEqual(len(self.manifest["source_sha256"]), 64)

    def test_each_chunk_carries_the_full_header(self):
        with open(SOURCE, encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh))
        for c in self.manifest["chunks"]:
            path = os.path.join(self._dir.name, c["file"])
            with open(path, encoding="utf-8", newline="") as fh:
                self.assertEqual(next(csv.reader(fh)), header)

    def test_a_chunk_loads_through_the_normal_loader(self):
        """A chunk must be indistinguishable from a small sheet."""
        from pipeline.load import group_by_site, load_rows
        c = self.manifest["chunks"][0]
        rows = load_rows(os.path.join(self._dir.name, c["file"]))
        self.assertEqual(len(rows), c["rows"])
        sites = group_by_site(rows)
        sites.pop("", None)
        self.assertEqual(sorted(sites), sorted(c["hosts"]),
                         "site bucketing inside the chunk differs from the split")


if __name__ == "__main__":
    unittest.main()
