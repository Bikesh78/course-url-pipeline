"""Search fallback: what gets asked, and what a result is allowed to become."""

import unittest

from pipeline.search import (FixtureProvider, NullProvider, build_query,
                             is_searchable, on_site, searchable_rows)


class TestProviders(unittest.TestCase):
    def test_the_default_provider_returns_nothing(self):
        """No vendor configured must be inert, not an error."""
        self.assertEqual(NullProvider().search("anything", "x.edu.au"), [])

    def test_fixture_returns_canned_results(self):
        p = FixtureProvider({"q": ["https://x.edu.au/a"]})
        self.assertEqual(p.search("q", "x.edu.au"), ["https://x.edu.au/a"])

    def test_fixture_records_what_was_asked(self):
        p = FixtureProvider({})
        p.search("some query", "x.edu.au")
        self.assertEqual(p.queries, ["some query"])

    def test_a_miss_is_empty_not_an_exception(self):
        self.assertEqual(FixtureProvider({}).search("absent", "x"), [])


class TestQuery(unittest.TestCase):
    def test_the_course_name_is_quoted_as_a_phrase(self):
        q = build_query("Diploma of Business", "ANT College", "ant.edu.au")
        self.assertIn('"Diploma of Business"', q)

    def test_the_query_is_scoped_to_the_institution_site(self):
        q = build_query("Diploma of Business", "ANT College", "ant.edu.au")
        self.assertIn("site:ant.edu.au", q)

    def test_whitespace_is_collapsed(self):
        q = build_query("  Diploma   of  Business ", "", "x.edu.au")
        self.assertIn('"Diploma of Business"', q)

    def test_a_missing_site_still_produces_a_query(self):
        self.assertIn('"X"', build_query("X", "Inst", ""))


class TestOnSite(unittest.TestCase):
    """A search engine will happily return an aggregator's page."""

    def test_same_registrable_domain_passes(self):
        self.assertTrue(on_site("https://www.x.edu.au/a", "x.edu.au"))

    def test_a_course_subdomain_passes(self):
        self.assertTrue(on_site("https://courses.x.edu.au/a", "www.x.edu.au"))

    def test_an_aggregator_is_rejected(self):
        self.assertFalse(on_site("https://shorelight.com/x", "x.edu.au"))

    def test_empty_inputs_are_rejected(self):
        self.assertFalse(on_site("", "x.edu.au"))
        self.assertFalse(on_site("https://x.edu.au/a", ""))


class TestWhatGetsSearched(unittest.TestCase):
    def test_a_filled_row_is_not_searched(self):
        self.assertFalse(is_searchable({"course_url": "https://x/a"}))

    def test_visa_occupation_rows_are_never_searched(self):
        """5,008 rows; no course page exists, so a query is money for nothing."""
        self.assertFalse(is_searchable(
            {"course_url": "", "row_flags": "occupation_code_not_course"}))

    def test_year_level_rows_are_never_searched(self):
        self.assertFalse(is_searchable(
            {"course_url": "", "row_flags": "year_level_not_course"}))

    def test_a_blank_course_row_is_searched(self):
        self.assertTrue(is_searchable({"course_url": "", "row_flags": ""}))

    def test_selection_filters_the_list(self):
        rows = [{"course_url": "", "row_flags": ""},
                {"course_url": "https://x/a", "row_flags": ""},
                {"course_url": "", "row_flags": "occupation_code_not_course"}]
        self.assertEqual(len(searchable_rows(rows)), 1)


if __name__ == "__main__":
    unittest.main()
