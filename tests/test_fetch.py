"""Fetcher cache, politeness and domain-grouping tests. No network."""

import gzip
import json
import os
import tempfile
import time
import unittest

from pipeline.fetch import (FetchResult, Fetcher, domain_of, registrable,
                            shrink_for_cache)

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

    def test_three_label_public_suffix_keeps_four_labels(self):
        """Victorian school domains: vic.edu.au is a suffix, not a domain.

        Collapsing to vic.edu.au put 144 unrelated schools in one bucket, which
        was harmless as a rate-limit key and wrong as a Catalog key.
        """
        self.assertEqual(registrable("barkly.vic.edu.au"), "barkly.vic.edu.au")
        self.assertEqual(registrable("www.barkly.vic.edu.au"),
                         "barkly.vic.edu.au")
        self.assertEqual(registrable("x.nsw.edu.au"), "x.nsw.edu.au")
        self.assertEqual(registrable("y.catholic.edu.au"), "y.catholic.edu.au")

    def test_lookalike_three_label_domains_are_not_treated_as_suffixes(self):
        # These are real registrable domains with a subdomain, so a blanket
        # "three labels" rule would be wrong in the other direction.
        self.assertEqual(registrable("x.taylors.edu.my"), "taylors.edu.my")
        self.assertEqual(registrable("y.bcu.ac.uk"), "bcu.ac.uk")

    def test_plain_two_label_host(self):
        self.assertEqual(registrable("codecore.ca"), "codecore.ca")

    def test_domain_of(self):
        self.assertEqual(domain_of(URL), "courses.aber.ac.uk")


class TestShrinkForCache(unittest.TestCase):
    """Cached bodies are stripped of markup no extraction step reads.

    Measured at a 62% reduction in cache size, which is what makes a full run
    fit on the available disk. The rules below are the contract: anything a
    future extraction step needs must be added to the carve-out *before* a run,
    because stripped content can only be recovered by refetching.
    """

    PAGE = (
        '<html><head><style>.a{color:red}</style>'
        '<script>var tracking=1;</script>'
        '<script type="application/ld+json">'
        '{"@type":"Course","name":"Data Science BSc"}</script>'
        '</head><body><!-- editorial note --><svg><path d="M0 0"/></svg>'
        '<h1>Data Science</h1>'
        '<a href="/undergraduate/data-science/">Data Science (BSc, 3 years)</a>'
        '</body></html>')

    def test_course_json_ld_survives(self):
        """17% of pages carry schema.org Course data; it must not be lost."""
        out = shrink_for_cache(self.PAGE)
        self.assertIn("application/ld+json", out)
        self.assertIn("Data Science BSc", out)

    def test_ordinary_scripts_and_styles_are_dropped(self):
        out = shrink_for_cache(self.PAGE)
        self.assertNotIn("var tracking", out)
        self.assertNotIn("color:red", out)
        self.assertNotIn("editorial note", out)
        self.assertNotIn("M0 0", out)

    def test_everything_extraction_reads_survives(self):
        out = shrink_for_cache(self.PAGE)
        self.assertIn("<h1>Data Science</h1>", out)          # Verification
        self.assertIn("/undergraduate/data-science/", out)   # crawl_listings
        self.assertIn("Data Science (BSc, 3 years)", out)    # Candidate name

    def test_sitemap_locations_survive(self):
        xml = "<urlset><url><loc>https://x.ac.uk/courses/a/</loc></url></urlset>"
        self.assertIn("<loc>https://x.ac.uk/courses/a/</loc>",
                      shrink_for_cache(xml))

    def test_plain_text_is_left_alone(self):
        """robots.txt is line-structured; collapsing it breaks Sitemap parsing."""
        robots = "User-agent: *\nDisallow: /admin\nSitemap: https://x/s.xml\n"
        self.assertEqual(shrink_for_cache(robots), robots)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(shrink_for_cache(""), "")
        self.assertIsNone(shrink_for_cache(None))

    def test_it_actually_shrinks(self):
        self.assertLess(len(shrink_for_cache(self.PAGE)), len(self.PAGE))

    def test_written_cache_entry_is_stripped(self):
        """The reduction must happen on the write path, not just in theory."""
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(cache_dir=d)
            f._write_cache(FetchResult(URL, 200, URL, self.PAGE))
            got = f._read_cache(URL)
            self.assertNotIn("var tracking", got.text)
            self.assertIn("Data Science BSc", got.text)


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
