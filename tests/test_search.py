"""Search fallback: what gets asked, and what a result is allowed to become."""

import io
import json
import tempfile
import unittest
import unittest.mock

from pipeline.search import (MAX_CONSECUTIVE_ERRORS, SEARCH_FOUND,
                             SERPER_ENDPOINT, FixtureProvider, NullProvider,
                             SearchProviderError, SerperProvider, build_query,
                             _post_json, is_searchable, on_site,
                             search_rows, searchable_rows)
from pipeline.load import _host_of
from pipeline.triage import ShareIndex


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
    def test_the_query_is_the_name_scoped_to_the_site(self):
        self.assertEqual(
            build_query("Diploma of Business", "ANT College", "ant.edu.au"),
            "Diploma of Business site:ant.edu.au")

    def test_the_name_is_not_quoted(self):
        """Measured: the exact phrase halves usable yield (10/40 -> 5/40).

        A site wording the course slightly differently returns nothing at all.
        """
        self.assertNotIn('"', build_query("Diploma of Business", "", "x.edu"))

    def test_the_institution_is_not_included(self):
        """Redundant after `site:`, and it is the legal name.

        "A2 Education Pty Ltd" rarely appears in a course page's own text, so
        including it filtered out real pages.
        """
        q = build_query("Diploma", "A2 Education Pty Ltd", "a2.edu.au")
        self.assertNotIn("Pty Ltd", q)

    def test_whitespace_is_collapsed(self):
        self.assertEqual(build_query("  Diploma   of  Business ", "", "x.edu"),
                         "Diploma of Business site:x.edu")

    def test_a_missing_site_still_produces_a_query(self):
        self.assertEqual(build_query("X", "Inst", ""), "X")


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


SITE = "https://courses.aber.ac.uk"
# `_host_of` reduces a website to its registrable domain, which is what the
# query is scoped to; the pages themselves sit on a course subdomain, and
# `on_site` compares registrable domains so both forms belong to the Site.
HOST = _host_of(SITE)
PAGES = "courses.aber.ac.uk"
DS = f"https://{PAGES}/undergraduate/data-science"
DS_STUB = f"https://{PAGES}/about/open-days"
ANTH = f"https://{PAGES}/undergraduate/anthropology"
AGGREGATOR = "https://www.coursefinder.example/uk/data-science"


def row(rid, name, url="", status="no_match", flags="", prior=""):
    """An output-CSV row dict, shaped as `run_phase2` reads them."""
    return {"id": rid, "name": name, "institution_name": "Aberystwyth",
            "course_url": url, "matched_status": status, "website": SITE,
            "row_flags": flags, "matched_score": "", "match_margin": "",
            "match_evidence": "", "prior_course_url": prior,
            "prior_matched_status": "", "url_change": "none"}


def run(rows, fixtures):
    """Search *rows* with canned *fixtures*, returning the stats."""
    provider = FixtureProvider(fixtures)
    return search_rows(rows, provider, ShareIndex(rows)), provider


def query(name):
    return build_query(name, "Aberystwyth", HOST)


class TestRanking(unittest.TestCase):
    """The engine's order is evidence about the engine, not about the course."""

    def test_the_best_scoring_hit_wins_not_the_first(self):
        rows = [row("1", "Data Science")]
        # The stub is returned first; the course page is returned second.
        run(rows, {query("Data Science"): [DS_STUB, DS]})
        self.assertEqual(rows[0]["course_url"], DS)

    def test_a_single_good_hit_is_adopted(self):
        rows = [row("1", "Data Science")]
        stats, _ = run(rows, {query("Data Science"): [DS]})
        self.assertEqual(stats.adopted, 1)
        self.assertEqual(rows[0]["course_url"], DS)


class TestRejection(unittest.TestCase):
    def test_an_aggregator_is_rejected_before_it_is_scored(self):
        rows = [row("1", "Data Science")]
        stats, _ = run(rows, {query("Data Science"): [AGGREGATOR]})
        self.assertEqual(stats.adopted, 0)
        self.assertEqual(stats.rejected_off_site, 1)
        self.assertEqual(rows[0]["course_url"], "")

    def test_a_hit_below_the_floor_is_rejected(self):
        """A plausible wrong URL is worse than a blank (ADR-0001)."""
        rows = [row("1", "Data Science")]
        stats, _ = run(rows, {query("Data Science"): [DS_STUB]})
        self.assertEqual(stats.adopted, 0)
        self.assertEqual(stats.rejected_by_gate, 1)
        self.assertEqual(rows[0]["course_url"], "")
        self.assertEqual(rows[0]["matched_status"], "no_match")

    def test_no_results_is_not_an_error(self):
        rows = [row("1", "Data Science")]
        stats, _ = run(rows, {})
        self.assertEqual((stats.adopted, stats.no_results), (0, 1))


class TestSharingGuard(unittest.TestCase):
    """A search hit answers to the same sharing rule as a crawled URL.

    Each pair here is chosen so the *gate* passes and the sharing rule is what
    decides — a name that fails the gate would never reach this check, so the
    obvious pairings prove nothing about sharing.
    """

    # Scores 0.679 against the Data Science page, and is not a Variant Sibling
    # of it: a separate degree, not the same course delivered differently.
    NON_SIBLING = "Data Science and Statistics"
    # Scores 0.575 and *is* a Variant Sibling.
    SIBLING = "Data Science (Placement Year)"

    def test_a_hit_held_by_a_non_sibling_is_refused(self):
        rows = [row("1", "Data Science", url=DS, status="verified"),
                row("2", self.NON_SIBLING)]
        stats, _ = run(rows, {query(self.NON_SIBLING): [DS]})
        self.assertEqual(stats.rejected_by_sharing, 1)
        self.assertEqual(rows[1]["course_url"], "")
        self.assertIn("search_denied_sharing", rows[1]["row_flags"])

    def test_a_hit_held_by_a_variant_sibling_is_allowed(self):
        rows = [row("1", "Data Science", url=DS, status="verified"),
                row("2", self.SIBLING)]
        stats, _ = run(rows, {query(self.SIBLING): [DS]})
        self.assertEqual(stats.adopted, 1)
        self.assertEqual(rows[1]["course_url"], DS)

    def test_two_courses_cannot_both_take_one_hit(self):
        """The index must see an adoption made earlier in the same run."""
        rows = [row("1", "Data Science"), row("2", self.NON_SIBLING)]
        stats, _ = run(rows, {query("Data Science"): [DS],
                              query(self.NON_SIBLING): [DS]})
        self.assertEqual((stats.adopted, stats.rejected_by_sharing), (1, 1))
        self.assertEqual(rows[0]["course_url"], DS)
        self.assertEqual(rows[1]["course_url"], "")


class TestWhatAnAdoptedRowCarries(unittest.TestCase):
    def setUp(self):
        self.rows = [row("1", "Data Science")]
        run(self.rows, {query("Data Science"): [DS]})
        self.r = self.rows[0]

    def test_the_status_marks_it_as_found_by_search(self):
        self.assertEqual(self.r["matched_status"], SEARCH_FOUND)

    def test_a_search_result_is_never_verified(self):
        """Verification means we fetched the page; a ranking cannot stand in."""
        self.assertNotEqual(self.r["matched_status"], "verified")

    def test_the_row_is_flagged(self):
        self.assertIn("url_from_search", self.r["row_flags"])

    def test_the_score_is_recorded_and_clears_the_floor(self):
        self.assertGreaterEqual(float(self.r["matched_score"]), 0.55)

    def test_there_is_no_margin(self):
        """Margin compares Candidates ranked by the same evidence."""
        self.assertEqual(self.r["match_margin"], "")

    def test_the_evidence_names_search_and_the_provider(self):
        self.assertIn("search result", self.r["match_evidence"])
        self.assertIn("FixtureProvider", self.r["match_evidence"])

    def test_provenance_is_recomputed_not_left_stale(self):
        """Triage writes `url_change` before this stage runs."""
        self.assertEqual(self.r["url_change"], "added")

    def test_provenance_reads_unchanged_when_search_finds_the_prior_url(self):
        rows = [row("1", "Data Science", prior=DS)]
        rows[0]["url_change"] = "dropped"
        run(rows, {query("Data Science"): [DS]})
        self.assertEqual(rows[0]["url_change"], "unchanged")


class TestWhatIsNeverAsked(unittest.TestCase):
    def test_unsearchable_rows_are_never_queried(self):
        """Querying an ANZSCO occupation code spends money on a certain miss."""
        rows = [row("1", "Cook - 351411 (subclass 186)",
                    flags="occupation_code_not_course"),
                row("2", "Anthropology", url=ANTH, status="verified")]
        stats, provider = run(rows, {})
        self.assertEqual(provider.queries, [])
        self.assertEqual(stats.queried, 0)

    def test_a_row_without_a_website_is_not_queried(self):
        rows = [row("1", "Data Science")]
        rows[0]["website"] = ""
        stats, provider = run(rows, {})
        self.assertEqual(provider.queries, [])
        self.assertEqual(stats.queried, 0)

    def test_an_already_filled_row_is_not_queried(self):
        rows = [row("1", "Data Science", url=DS, status="verified")]
        _, provider = run(rows, {query("Data Science"): [ANTH]})
        self.assertEqual(provider.queries, [])
        self.assertEqual(rows[0]["course_url"], DS)


class TestNullProviderIsANoOp(unittest.TestCase):
    def test_no_vendor_changes_nothing_at_all(self):
        """Inert must mean untouched, not partially written."""
        rows = [row("1", "Data Science")]
        before = dict(rows[0])
        stats = search_rows(rows, NullProvider(), ShareIndex(rows))
        self.assertEqual(rows[0], before)
        self.assertEqual(stats.adopted, 0)


class FakeTransport:
    """Canned HTTP replies, recording what was sent. No network, no key."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, payload, headers, timeout=None):
        self.calls.append({"url": url, "payload": payload, "headers": headers})
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


def organic(*links):
    return 200, {"organic": [{"link": u} for u in links]}


class SerperTestCase(unittest.TestCase):
    """Shared scaffolding: a throwaway cache and no inter-call delay."""

    def provider(self, *replies, **kw):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = FakeTransport(*replies)
        return SerperProvider(api_key="test-key", cache_dir=self.tmp.name,
                              delay=0, transport=self.transport, **kw)


class TestSerperRequest(SerperTestCase):
    def test_organic_links_are_returned_in_order(self):
        p = self.provider(organic("https://x.edu/a", "https://x.edu/b"))
        self.assertEqual(p.search("q", "x.edu"),
                         ["https://x.edu/a", "https://x.edu/b"])

    def test_results_without_a_link_are_ignored(self):
        p = self.provider((200, {"organic": [{"title": "no link"},
                                             {"link": "https://x.edu/a"}]}))
        self.assertEqual(p.search("q", "x.edu"), ["https://x.edu/a"])

    def test_a_response_with_no_organic_block_is_a_miss(self):
        p = self.provider((200, {}))
        self.assertEqual(p.search("q", "x.edu"), [])

    def test_it_posts_to_serper_with_the_key_in_the_header(self):
        p = self.provider(organic("https://x.edu/a"))
        p.search("data science", "x.edu")
        sent = self.transport.calls[0]
        self.assertEqual(sent["url"], SERPER_ENDPOINT)
        self.assertEqual(sent["payload"]["q"], "data science")
        self.assertEqual(sent["headers"]["X-API-KEY"], "test-key")

    def test_the_key_is_never_put_in_the_payload(self):
        """Payloads are the part most likely to end up in a log."""
        p = self.provider(organic("https://x.edu/a"))
        p.search("q", "x.edu")
        self.assertNotIn("test-key", json.dumps(self.transport.calls[0]["payload"]))


class TestSerperCache(SerperTestCase):
    """Every call costs money, so a repeat run must not re-ask."""

    def test_a_second_identical_query_makes_no_paid_call(self):
        p = self.provider(organic("https://x.edu/a"))
        first = p.search("q", "x.edu")
        second = p.search("q", "x.edu")
        self.assertEqual(first, second)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual((p.paid_calls, p.cache_hits), (1, 1))

    def test_an_empty_result_is_cached_too(self):
        """A query with no answer is a fact worth remembering."""
        p = self.provider((200, {"organic": []}))
        p.search("q", "x.edu")
        self.assertEqual(p.search("q", "x.edu"), [])
        self.assertEqual(len(self.transport.calls), 1)

    def test_a_different_query_is_a_separate_entry(self):
        p = self.provider(organic("https://x.edu/a"))
        p.search("one", "x.edu")
        p.search("two", "x.edu")
        self.assertEqual(len(self.transport.calls), 2)


class TestSerperBudget(SerperTestCase):
    def test_the_limit_stops_paid_calls(self):
        p = self.provider(organic("https://x.edu/a"), limit=1)
        p.search("one", "x.edu")
        self.assertEqual(p.search("two", "x.edu"), [])
        self.assertEqual((p.paid_calls, p.capped), (1, 1))

    def test_the_report_names_the_spend(self):
        p = self.provider(organic("https://x.edu/a"))
        p.search("q", "x.edu")
        self.assertIn("1 paid calls", p.report())


class TestSerperFailsLoudly(SerperTestCase):
    """A paid provider must not be able to fail quietly."""

    def test_a_rejected_key_raises_rather_than_returning_nothing(self):
        p = self.provider((401, {"message": "Unauthorized"}))
        with self.assertRaises(SearchProviderError):
            p.search("q", "x.edu")

    def test_a_forbidden_response_also_raises(self):
        p = self.provider((403, {}))
        with self.assertRaises(SearchProviderError):
            p.search("q", "x.edu")

    def test_a_missing_key_is_caught_at_construction(self):
        with self.assertRaises(SearchProviderError):
            SerperProvider(api_key="")

    def test_rate_limiting_is_retried_then_reported_as_a_miss(self):
        p = self.provider((429, {}))
        self.assertEqual(p.search("q", "x.edu"), [])
        self.assertEqual(len(self.transport.calls), 3)   # initial + 2 retries
        self.assertEqual(p.errors, 1)

    def test_a_recoverable_failure_then_success_is_not_an_error(self):
        p = self.provider((503, {}), organic("https://x.edu/a"))
        self.assertEqual(p.search("q", "x.edu"), ["https://x.edu/a"])
        self.assertEqual(p.errors, 0)

    def test_an_unrecoverable_status_is_not_retried(self):
        """A 400 will fail identically the second time, at the same price."""
        p = self.provider((400, {}))
        p.search("q", "x.edu")
        self.assertEqual(len(self.transport.calls), 1)

    def test_persistent_failure_stops_the_run(self):
        p = self.provider((429, {}))
        for i in range(MAX_CONSECUTIVE_ERRORS - 1):
            p.search(f"q{i}", "x.edu")
        with self.assertRaises(SearchProviderError):
            p.search("last", "x.edu")

    def test_a_failure_is_not_cached(self):
        """Caching a transport failure would make the miss permanent."""
        p = self.provider((429, {}), organic("https://x.edu/a"))
        p.search("q", "x.edu")
        self.assertEqual(p.search("q", "x.edu"), ["https://x.edu/a"])


class TestTheStageSurvivesABrokenProvider(unittest.TestCase):
    """Stage 1's work is not discarded because Stage 2's vendor broke."""

    class Breaks:
        def __init__(self):
            self.n = 0

        def search(self, query, site):
            self.n += 1
            if self.n == 1:
                return [DS]
            raise SearchProviderError("key revoked mid-run")

    def test_rows_adopted_before_the_failure_are_kept(self):
        rows = [row("1", "Data Science"), row("2", "Anthropology")]
        stats = search_rows(rows, self.Breaks(), ShareIndex(rows))
        self.assertEqual(rows[0]["course_url"], DS)
        self.assertEqual(stats.adopted, 1)

    def test_the_abort_is_recorded_not_swallowed(self):
        rows = [row("1", "Data Science"), row("2", "Anthropology")]
        stats = search_rows(rows, self.Breaks(), ShareIndex(rows))
        self.assertIn("key revoked mid-run", stats.aborted)


class TestTheRealTransport(unittest.TestCase):
    """`_post_json` is the only part that touches the network.

    Exercised here by capturing the Request it builds, so the URL, method,
    headers and body are covered without a key or a socket.
    """

    def _capture(self, status=200, body=b'{"organic": []}'):
        captured = {}

        class Resp:
            status = 200

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            captured["timeout"] = timeout
            return Resp()

        return captured, fake_urlopen

    def test_it_builds_a_json_post_with_the_key_header(self):
        captured, fake = self._capture()
        with unittest.mock.patch("urllib.request.urlopen", fake):
            status, body = _post_json(SERPER_ENDPOINT, {"q": "data science"},
                                      {"X-API-KEY": "k"})
        req = captured["req"]
        self.assertEqual(req.full_url, SERPER_ENDPOINT)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.get_header("X-api-key"), "k")
        self.assertEqual(json.loads(req.data.decode()), {"q": "data science"})
        self.assertEqual((status, body), (200, {"organic": []}))

    def test_an_http_error_body_is_decoded_not_raised(self):
        import urllib.error

        def raiser(req, timeout=None):
            raise urllib.error.HTTPError(
                SERPER_ENDPOINT, 401, "Unauthorized", {},
                io.BytesIO(b'{"message": "Unauthorized"}'))

        with unittest.mock.patch("urllib.request.urlopen", raiser):
            status, body = _post_json(SERPER_ENDPOINT, {"q": "x"}, {})
        self.assertEqual(status, 401)
        self.assertEqual(body["message"], "Unauthorized")

    def test_an_unparseable_error_body_still_yields_the_status(self):
        import urllib.error

        def raiser(req, timeout=None):
            raise urllib.error.HTTPError(
                SERPER_ENDPOINT, 500, "Server Error", {},
                io.BytesIO(b"<html>nope</html>"))

        with unittest.mock.patch("urllib.request.urlopen", raiser):
            self.assertEqual(_post_json(SERPER_ENDPOINT, {}, {}), (500, {}))


class TestInactiveRecordsAreNeverQueried(unittest.TestCase):
    """The sheet retires a record by prefixing the *institution* name.

    "(Inactive) Fleming College Toronto". Never the course name -- 0 course
    names carry it against 72 institution names -- so the check has to look at
    the institution.
    """

    def searchable(self, **over):
        r = row("1", "Data Science")
        r.update(over)
        return is_searchable(r)

    def test_an_active_record_is_searchable(self):
        self.assertTrue(self.searchable(institution_name="Fleming College"))

    def test_an_inactive_institution_is_not(self):
        self.assertFalse(
            self.searchable(institution_name="(Inactive) Fleming College"))

    def test_the_marker_is_matched_case_insensitively(self):
        self.assertFalse(
            self.searchable(institution_name="(INACTIVE) Fleming College"))

    def test_an_inactive_course_name_is_also_honoured(self):
        """Defensive: this sheet does not use it, another might."""
        self.assertFalse(self.searchable(name="(Inactive) Data Science"))

    def test_no_paid_query_is_spent_on_one(self):
        rows = [row("1", "Data Science")]
        rows[0]["institution_name"] = "(Inactive) Aberystwyth"
        stats, provider = run(rows, {query("Data Science"): [DS]})
        self.assertEqual(provider.queries, [])
        self.assertEqual((stats.queried, stats.adopted), (0, 0))


class TestBatchRestriction(unittest.TestCase):
    """`--search-ids` spends a fixed budget on a chosen sample."""

    def rows(self):
        return [row("1", "Data Science"), row("2", "Anthropology")]

    def test_only_the_listed_ids_are_queried(self):
        rows = self.rows()
        provider = FixtureProvider({query("Data Science"): [DS],
                                    query("Anthropology"): [ANTH]})
        stats = search_rows(rows, provider, ShareIndex(rows),
                            only_ids={"1"})
        self.assertEqual(stats.queried, 1)
        self.assertEqual(rows[0]["course_url"], DS)
        self.assertEqual(rows[1]["course_url"], "")

    def test_a_row_outside_the_batch_is_left_untouched(self):
        rows = self.rows()
        before = dict(rows[1])
        search_rows(rows, FixtureProvider({query("Anthropology"): [ANTH]}),
                    ShareIndex(rows), only_ids={"1"})
        self.assertEqual(rows[1], before)

    def test_the_batch_narrows_and_never_widens(self):
        """An id in the batch that is not searchable stays skipped."""
        rows = [row("1", "Data Science", url=DS, status="verified")]
        _, provider = run(rows, {})
        self.assertEqual(searchable_rows(rows, {"1"}), [])
        self.assertEqual(provider.queries, [])

    def test_an_unknown_id_in_the_batch_is_harmless(self):
        rows = self.rows()
        stats = search_rows(rows, FixtureProvider({}), ShareIndex(rows),
                            only_ids={"nope"})
        self.assertEqual(stats.queried, 0)


if __name__ == "__main__":
    unittest.main()
