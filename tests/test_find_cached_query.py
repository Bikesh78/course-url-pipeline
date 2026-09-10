"""Looking up a cached search response. Hermetic — no network, no key."""

import contextlib
import csv
import io
import os
import tempfile
import unittest

from pipeline.search import MAX_RESULTS, SerperProvider, cache_path_for
from tools.find_cached_query import main, matching

COLUMNS = ["id", "name", "institution_name", "course_url", "matched_status",
           "website", "row_flags"]

QUERY = "Diploma of Business site:alit.edu.au"


def row(rid, name, site="alit.edu.au", inst="ALIT", status="no_catalog"):
    return {"id": rid, "name": name, "institution_name": inst,
            "course_url": "", "matched_status": status,
            "website": f"https://{site}", "row_flags": ""}


class TestThePathMatchesTheProvider(unittest.TestCase):
    """The tool must look where the provider writes.

    A re-implemented hash would drift and then fail in the worst available
    way: reporting "not cached" for a file sitting right there. So this
    asserts the two cannot disagree, rather than asserting a hash value.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def provider(self, num=MAX_RESULTS):
        return SerperProvider(
            api_key="test-key", cache_dir=self._dir.name, delay=0, num=num,
            transport=lambda *a, **k: (200, {"organic": [
                {"link": "https://alit.edu.au/bsb50120-diploma-of-business"}]}))

    def test_the_tool_resolves_the_file_the_provider_wrote(self):
        p = self.provider()
        p.search(QUERY, "alit.edu.au")
        self.assertTrue(
            os.path.exists(cache_path_for(QUERY, MAX_RESULTS, self._dir.name)),
            "cache_path_for pointed somewhere the provider did not write")

    def test_the_private_method_delegates_to_the_public_one(self):
        p = self.provider()
        self.assertEqual(p._cache_path(QUERY),
                         cache_path_for(QUERY, MAX_RESULTS, self._dir.name))

    def test_num_is_part_of_the_key(self):
        """Five results and ten are different questions."""
        self.assertNotEqual(cache_path_for(QUERY, 5, self._dir.name),
                            cache_path_for(QUERY, 10, self._dir.name))

    def test_a_different_num_is_a_cache_miss(self):
        self.provider(num=5).search(QUERY, "alit.edu.au")
        self.assertFalse(
            os.path.exists(cache_path_for(QUERY, 10, self._dir.name)))

    def test_the_separator_prevents_a_collision(self):
        """`num=5` + "0q" must not land where `num=50` + "q" does."""
        self.assertNotEqual(cache_path_for("0q", 5), cache_path_for("q", 50))

    def test_it_shards_on_the_first_two_characters(self):
        path = cache_path_for(QUERY, MAX_RESULTS, "cache")
        shard = os.path.basename(os.path.dirname(path))
        self.assertEqual(len(shard), 2)
        self.assertTrue(os.path.basename(path).startswith(shard))


class ToolCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.results = os.path.join(self._dir.name, "results.csv")
        self.cache = os.path.join(self._dir.name, "cache")

    def write(self, rows):
        with open(self.results, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(rows)

    def cache_it(self, query, links):
        SerperProvider(
            api_key="k", cache_dir=self.cache, delay=0,
            transport=lambda *a, **k: (
                200, {"organic": [{"link": u} for u in links]})
        ).search(query, "alit.edu.au")

    def run_tool(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["--results", self.results,
                         "--cache-dir", self.cache, *argv])
        return code, out.getvalue()


class TestLookup(ToolCase):
    def test_a_cached_query_reports_its_links(self):
        self.write([row("1", "Diploma of Business")])
        self.cache_it(QUERY, ["https://alit.edu.au/bsb50120-diploma-of-business"])
        code, text = self.run_tool("1")
        self.assertEqual(code, 0)
        self.assertIn("cached      : yes", text)
        self.assertIn("bsb50120-diploma-of-business", text)

    def test_it_says_why_a_link_was_rejected(self):
        """The question is not what came back, but why the row is empty."""
        self.write([row("1", "Diploma of Business")])
        self.cache_it(QUERY, ["https://alit.edu.au/news/2026-open-day"])
        _, text = self.run_tool("1")
        self.assertIn("below 0.55 gate", text)

    def test_it_marks_an_adoptable_link(self):
        self.write([row("1", "Diploma of Business")])
        self.cache_it(QUERY, ["https://alit.edu.au/bsb50120-diploma-of-business"])
        _, text = self.run_tool("1")
        self.assertIn("adoptable", text)

    def test_it_marks_an_off_site_link(self):
        self.write([row("1", "Diploma of Business")])
        self.cache_it(QUERY, ["https://aggregator.example/diploma-of-business"])
        _, text = self.run_tool("1")
        self.assertIn("off-site", text)

    def test_an_uncached_query_is_not_an_error(self):
        """Absence is an answer: the row has simply not been searched."""
        self.write([row("1", "Diploma of Business")])
        code, text = self.run_tool("1")
        self.assertEqual(code, 0)
        self.assertIn("cached      : no", text)

    def test_an_empty_cached_result_says_so(self):
        self.write([row("1", "Diploma of Business")])
        self.cache_it(QUERY, [])
        _, text = self.run_tool("1")
        self.assertIn("returned nothing", text)

    def test_an_unknown_id_exits_nonzero(self):
        self.write([row("1", "Diploma of Business")])
        code, text = self.run_tool("no-such-id")
        self.assertEqual(code, 1)
        self.assertIn("matched", text)


class TestSelection(ToolCase):
    def rows(self, n):
        return [row(str(i), f"Secondary Junior Years 7-10",
                    site=f"school{i}.edu.au") for i in range(n)]

    def test_an_id_selects_exactly_one(self):
        self.assertEqual([r["id"] for r in matching(self.rows(3), {"1"}, None)],
                         ["1"])

    def test_a_name_substring_matches_many(self):
        self.assertEqual(len(matching(self.rows(3), set(), "secondary")), 3)

    def test_the_name_match_is_case_insensitive(self):
        self.assertEqual(len(matching(self.rows(2), set(), "SECONDARY")), 2)

    def test_an_ambiguous_name_is_capped_and_says_so(self):
        """"Secondary Junior" matched 35 real rows across as many schools."""
        self.write(self.rows(12))
        _, text = self.run_tool("--name", "Secondary")
        self.assertIn("12 rows matched; showing 5", text)

    def test_all_lifts_the_cap(self):
        self.write(self.rows(12))
        _, text = self.run_tool("--name", "Secondary", "--all")
        self.assertNotIn("showing 5", text)
        self.assertEqual(text.count("cache file"), 12)

    def test_it_needs_something_to_look_up(self):
        self.write(self.rows(1))
        with self.assertRaises(SystemExit):
            main(["--results", self.results])


if __name__ == "__main__":
    unittest.main()
