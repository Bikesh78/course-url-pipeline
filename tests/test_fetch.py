"""Fetcher cache, politeness and domain-grouping tests. No network."""

import gzip
import json
import os
import tempfile
import time
import unittest

from pipeline.fetch import FetchResult, Fetcher, domain_of, registrable

URL = "https://courses.aber.ac.uk/undergraduate/data-science/"


class TestDomainGrouping(unittest.TestCase):
    def test_course_subdomain_shares_apex_budget(self):
        self.assertEqual(registrable("courses.aber.ac.uk"), "aber.ac.uk")
        self.assertEqual(registrable("www.aber.ac.uk"), "aber.ac.uk")

    def test_two_part_tld(self):
        self.assertEqual(registrable("bond.edu.au"), "bond.edu.au")
        self.assertEqual(registrable("www.curtin.edu.au"), "curtin.edu.au")

    def test_canadian_provincial_tld(self):
        self.assertEqual(registrable("www.conestogac.on.ca"), "conestogac.on.ca")

    def test_plain_two_label_host(self):
        self.assertEqual(registrable("codecore.ca"), "codecore.ca")

    def test_domain_of(self):
        self.assertEqual(domain_of(URL), "courses.aber.ac.uk")


class TestCache(unittest.TestCase):
    def test_written_entries_are_gzipped(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(cache_dir=d)
            f._write_cache(FetchResult(URL, 200, URL, "<h1>Data Science</h1>"))
            gz, plain = f._cache_paths(URL)
            self.assertTrue(os.path.exists(gz))
            self.assertFalse(os.path.exists(plain))
            with gzip.open(gz, "rt", encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["status"], 200)

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(cache_dir=d)
            f._write_cache(FetchResult(URL, 200, URL, "<h1>Data Science</h1>"))
            got = f._read_cache(URL)
            self.assertIsNotNone(got)
            self.assertTrue(got.from_cache)
            self.assertEqual(got.text, "<h1>Data Science</h1>")

    def test_legacy_uncompressed_entry_is_still_read(self):
        """A cache written before compression must stay warm, not be refetched."""
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(cache_dir=d)
            _, plain = f._cache_paths(URL)
            os.makedirs(os.path.dirname(plain), exist_ok=True)
            with open(plain, "w", encoding="utf-8") as fh:
                json.dump({"url": URL, "status": 200, "final_url": URL,
                           "text": "legacy", "error": ""}, fh)
            got = f._read_cache(URL)
            self.assertIsNotNone(got)
            self.assertEqual(got.text, "legacy")

    def test_corrupt_entry_is_treated_as_a_miss(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(cache_dir=d)
            gz, _ = f._cache_paths(URL)
            os.makedirs(os.path.dirname(gz), exist_ok=True)
            with open(gz, "wb") as fh:
                fh.write(b"not gzip at all")
            self.assertIsNone(f._read_cache(URL))

    def test_miss_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(Fetcher(cache_dir=d)._read_cache(URL))


class TestOfflineMode(unittest.TestCase):
    def test_offline_never_hits_the_network(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(cache_dir=d, offline=True)
            res = f.get(URL)
            self.assertFalse(res.ok)
            self.assertIn("offline", res.error)

    def test_offline_still_serves_the_cache(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(cache_dir=d, offline=True)
            f._write_cache(FetchResult(URL, 200, URL, "cached body"))
            res = f.get(URL)
            self.assertTrue(res.ok)
            self.assertEqual(res.text, "cached body")


class TestInputGuards(unittest.TestCase):
    def test_non_http_scheme_is_rejected_without_a_request(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(cache_dir=d)
            for bad in ("mailto:a@b.c", "", "42cdc3a0-faed-43fd-95fa-37cb46a8b094",
                        "javascript:void(0)"):
                res = f.get(bad)
                self.assertFalse(res.ok)
                self.assertEqual(res.error, "not an http url")


class TestPoliteness(unittest.TestCase):
    def test_requests_to_one_domain_are_spaced(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(cache_dir=d, delay=0.25)
            key = "aber.ac.uk"
            start = time.monotonic()
            f._wait_turn(key)
            f._wait_turn(key)
            self.assertGreaterEqual(time.monotonic() - start, 0.25)

    def test_different_domains_do_not_wait_on_each_other(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(cache_dir=d, delay=0.5)
            start = time.monotonic()
            f._wait_turn("aber.ac.uk")
            f._wait_turn("curtin.edu.au")
            self.assertLess(time.monotonic() - start, 0.4)


if __name__ == "__main__":
    unittest.main()
