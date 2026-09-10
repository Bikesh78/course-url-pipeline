"""Prior-URL triage: the gate, the adoption rules, and the sharing guard."""

import unittest

from pipeline.load import _host_of
from pipeline.statuses import MANUALLY_ASSIGNED, SEARCH_FOUND
from pipeline.triage import (CARRIED_OVER, ShareIndex, add_flag,
                             classify_change, gate_score, occupant_url,
                             triage_rows)

SITE = "https://courses.aber.ac.uk"
DS = "https://courses.aber.ac.uk/undergraduate/data-science"
DS_OTHER = "https://courses.aber.ac.uk/undergraduate/data-science-iy"
ANTH = "https://courses.aber.ac.uk/undergraduate/anthropology"
SECTION_PAGE = "https://courses.aber.ac.uk/undergraduate"


def row(rid, name, url="", status="no_match", website=SITE, flags=""):
    return {"id": rid, "name": name, "institution_name": "Aberystwyth",
            "course_url": url, "matched_status": status, "website": website,
            "row_flags": flags, "matched_score": "", "match_margin": "",
            "match_evidence": ""}


def source(rid, url="", status="unmatched"):
    return {"id": rid, "course_url": url, "matched_status": status}


class TestClassifyChange(unittest.TestCase):
    """`url_change` answers only 'did the delivered answer change'."""

    def test_neither_side_has_one(self):
        self.assertEqual(classify_change("", ""), "none")

    def test_we_added_one(self):
        self.assertEqual(classify_change("", DS), "added")

    def test_we_dropped_one(self):
        self.assertEqual(classify_change(DS, ""), "dropped")

    def test_identical(self):
        self.assertEqual(classify_change(DS, DS), "unchanged")

    def test_trailing_slash_is_not_a_change(self):
        # 729 rows in the full sheet differed only by this.
        self.assertEqual(classify_change(DS + "/", DS), "unchanged")

    def test_whitespace_is_not_a_change(self):
        self.assertEqual(classify_change(DS, DS + " "), "unchanged")

    def test_a_www_prefix_is_not_a_change(self):
        # 123 rows differed by nothing else.
        self.assertEqual(
            classify_change("https://www.ccs.edu.au/theology/bachelor",
                            "https://ccs.edu.au/theology/bachelor"),
            "unchanged")

    def test_a_scheme_upgrade_is_not_a_change(self):
        self.assertEqual(
            classify_change("http://x.edu.au/a", "https://x.edu.au/a"),
            "unchanged")

    def test_a_different_subdomain_is_still_a_change(self):
        # Only `www.` is treated as cosmetic; a course subdomain is not.
        self.assertEqual(
            classify_change("https://x.edu.au/a", "https://courses.x.edu.au/a"),
            "changed")

    def test_a_genuinely_different_page(self):
        self.assertEqual(classify_change(DS, DS_OTHER), "changed")


class TestGate(unittest.TestCase):
    def test_a_matching_slug_scores_high(self):
        self.assertGreater(gate_score("Data Science BSc (Hons)", DS, "Aber"),
                           0.55)

    def test_an_unrelated_slug_scores_low(self):
        self.assertLess(gate_score("Veterinary Nursing FdSc", DS, "Aber"), 0.55)

    def test_no_url_scores_zero(self):
        self.assertEqual(gate_score("Data Science BSc", "", "Aber"), 0.0)


class TestAdoption(unittest.TestCase):
    def test_blank_row_adopts_a_good_prior(self):
        rows = [row("1", "Data Science BSc (Hons)")]
        st = triage_rows(rows, {"1": source("1", DS, "matched")})
        self.assertEqual(rows[0]["course_url"], DS)
        self.assertEqual(rows[0]["matched_status"], CARRIED_OVER)
        self.assertIn("url_from_source_sheet", rows[0]["row_flags"])
        self.assertEqual(st.adopted_blank, 1)

    def test_blank_row_rejects_a_prior_that_fails_the_gate(self):
        """6,339 such rows come from the sheet's own low_confidence band."""
        rows = [row("1", "Veterinary Nursing FdSc")]
        st = triage_rows(rows, {"1": source("1", DS, "low_confidence")})
        self.assertEqual(rows[0]["course_url"], "")
        self.assertEqual(st.rejected_by_gate, 1)
        self.assertEqual(st.adopted, 0)

    def test_dead_url_falls_back_to_the_prior(self):
        rows = [row("1", "Data Science BSc (Hons)", DS_OTHER, "url_dead")]
        st = triage_rows(rows, {"1": source("1", DS, "matched")})
        self.assertEqual(rows[0]["course_url"], DS)
        self.assertEqual(st.adopted_dead, 1)

    def test_ambiguous_yields_to_a_better_prior_matched(self):
        # The one cell of the chunk-003 rule that survived re-measurement:
        # prior beat us 1,613 to 498 here.
        rows = [row("1", "Anthropology BA (Hons)", DS, "ambiguous")]
        st = triage_rows(rows, {"1": source("1", ANTH, "matched")})
        self.assertEqual(rows[0]["course_url"], ANTH)
        self.assertEqual(st.adopted_weak, 1)

    def test_verified_is_never_given_up(self):
        # Ours beat a low_confidence prior 8:1 and led even against matched.
        rows = [row("1", "Data Science BSc (Hons)", DS, "verified")]
        st = triage_rows(rows, {"1": source("1", ANTH, "matched")})
        self.assertEqual(rows[0]["course_url"], DS)
        self.assertEqual(st.adopted, 0)

    def test_probable_is_not_given_up(self):
        # Measured a coin flip (334:311); swapping would lose as often as win.
        rows = [row("1", "Anthropology BA (Hons)", DS, "probable")]
        st = triage_rows(rows, {"1": source("1", ANTH, "matched")})
        self.assertEqual(rows[0]["course_url"], DS)
        self.assertEqual(st.adopted, 0)

    def test_ambiguous_does_not_yield_to_a_low_confidence_prior(self):
        # Ours beat low_confidence 1,321:1,105 in this cell.
        rows = [row("1", "Anthropology BA (Hons)", DS, "ambiguous")]
        st = triage_rows(rows, {"1": source("1", ANTH, "low_confidence")})
        self.assertEqual(rows[0]["course_url"], DS)
        self.assertEqual(st.adopted, 0)

    def test_no_prior_leaves_the_row_alone(self):
        rows = [row("1", "Data Science BSc (Hons)", DS, "probable")]
        st = triage_rows(rows, {"1": source("1")})
        self.assertEqual(rows[0]["course_url"], DS)
        self.assertEqual(rows[0]["url_change"], "added")
        self.assertEqual(st.adopted, 0)


class TestSharingGuard(unittest.TestCase):
    """Adoption must not import the source sheet's URL collapse."""

    def test_adoption_denied_when_the_holder_is_not_a_sibling(self):
        # Real shape: the sheet gave the Endodontics page to Orthodontics too.
        endo = "https://x.edu.au/doctor-of-clinical-dentistry-endodontics"
        rows = [
            row("1", "Doctor of Clinical Dentistry (Endodontics)", endo,
                "verified", website="https://x.edu.au"),
            row("2", "Doctor of Clinical Dentistry (Orthodontics)",
                website="https://x.edu.au"),
        ]
        st = triage_rows(rows, {"1": source("1"), "2": source("2", endo,
                                                              "matched")})
        self.assertEqual(rows[1]["course_url"], "")
        self.assertIn("adoption_denied_sharing", rows[1]["row_flags"])
        self.assertEqual(st.rejected_by_sharing, 1)

    def test_adoption_allowed_between_variant_siblings(self):
        url = "https://x.edu.au/anthropology"
        rows = [
            row("1", "Anthropology BA (Hons)", url, "verified",
                website="https://x.edu.au"),
            row("2", "Anthropology with Placement BA (Hons)",
                website="https://x.edu.au"),
        ]
        st = triage_rows(rows, {"1": source("1"),
                                "2": source("2", url, "matched")})
        self.assertEqual(rows[1]["course_url"], url)
        self.assertEqual(st.rejected_by_sharing, 0)

    def test_a_site_with_no_holder_is_free_to_adopt(self):
        rows = [row("1", "Anthropology BA (Hons)", website="https://x.edu.au")]
        st = triage_rows(rows, {"1": source("1",
                                            "https://x.edu.au/anthropology",
                                            "matched")})
        self.assertEqual(st.adopted_blank, 1)


class TestWhoOccupiesAUrl(unittest.TestCase):
    """A phase-2 URL must still count as a holder on the *next* run.

    The index used to be built from `phase1_url`, which is empty for a row
    search or a person answered -- so 153 search URLs and 3 manual ones
    disappeared from it on a re-run and the page became free to hand to a
    second course. Triage decides from phase 1 for idempotency; occupancy is a
    different question and needs the delivered column.
    """

    def test_a_search_url_is_held_by_the_row_that_has_it(self):
        r = row("1", "Data Science BSc (Hons)", DS, SEARCH_FOUND)
        r["phase1_course_url"] = ""
        self.assertEqual(occupant_url(r), DS)

    def test_a_manual_url_is_held_too(self):
        r = row("1", "Secondary Junior 7-10", SECTION_PAGE, MANUALLY_ASSIGNED)
        r["phase1_course_url"] = ""
        self.assertEqual(occupant_url(r), SECTION_PAGE)

    def test_every_other_row_still_reads_phase_1(self):
        """A carried-over URL is triage's to re-derive, not a holder yet."""
        r = row("1", "Data Science BSc (Hons)", SECTION_PAGE, CARRIED_OVER)
        r["phase1_course_url"] = DS
        self.assertEqual(occupant_url(r), DS)

    def test_a_second_course_cannot_take_a_searched_page(self):
        """The measured failure: denied on run 1, accepted on run 2.

        Both rows want one page and they are not Variant Siblings, so whoever
        holds it keeps it -- on every run, not just the one that filled it.
        """
        held = row("1", "Certificate IV in Kitchen Management and Diploma of "
                        "Hospitality Management", DS, SEARCH_FOUND)
        held["phase1_course_url"] = ""
        other = row("2", "Certificate IV in Kitchen Management")
        other["phase1_course_url"] = ""
        index = ShareIndex([held, other])
        self.assertTrue(index.would_break(
            _host_of(SITE), DS, other["name"]))

    def test_a_sibling_may_still_share_it(self):
        """The guard is about the collapse, not about search results."""
        held = row("1", "Anthropology BA (Hons)", ANTH, SEARCH_FOUND)
        held["phase1_course_url"] = ""
        other = row("2", "Anthropology with Placement BA (Hons)")
        other["phase1_course_url"] = ""
        index = ShareIndex([held, other])
        self.assertFalse(index.would_break(
            _host_of(SITE), ANTH, other["name"]))


class TestProvenanceIsAlwaysWritten(unittest.TestCase):
    def test_every_row_gets_all_three_columns(self):
        rows = [row("1", "Data Science BSc (Hons)", DS, "verified"),
                row("2", "Anthropology BA (Hons)")]
        triage_rows(rows, {"1": source("1", DS, "matched"), "2": source("2")})
        for r in rows:
            self.assertIn("prior_course_url", r)
            self.assertIn("prior_matched_status", r)
            self.assertIn("url_change", r)

    def test_change_is_computed_after_adoption_not_before(self):
        """A restored row reads `unchanged`, not `dropped`."""
        rows = [row("1", "Data Science BSc (Hons)")]
        triage_rows(rows, {"1": source("1", DS, "matched")})
        self.assertEqual(rows[0]["url_change"], "unchanged")


class TestDecidesFromPhase1NotFromWhatWasDelivered(unittest.TestCase):
    """Triage reads `phase1_course_url`, which is what makes it idempotent.

    Reading the delivered column meant "our answer" was whatever the last run
    produced, so re-running adopted 9 more rows and refused 9 fewer by the
    sharing rule -- exactly offsetting, which is how the share index was
    identified as the sole cause.
    """

    def row2(self, rid, name, phase1_url, delivered, status, phase1_status):
        r = row(rid, name, url=delivered, status=status)
        r["phase1_course_url"] = phase1_url
        r["phase1_matched_status"] = phase1_status
        return r

    def test_a_second_pass_reaches_the_same_verdict(self):
        rows = [self.row2("1", "Data Science", "", DS_OTHER, CARRIED_OVER,
                          "no_match")]
        src = {"1": source("1", DS_OTHER, "matched")}
        first = triage_rows([dict(rows[0])], src)
        again = triage_rows(rows, src)
        self.assertEqual((first.adopted, first.kept_ours),
                         (again.adopted, again.kept_ours))

    def test_phase_1_answer_is_preserved_not_overwritten(self):
        rows = [self.row2("1", "Data Science", DS, DS, "ambiguous",
                          "ambiguous")]
        triage_rows(rows, {"1": source("1", DS_OTHER, "matched")})
        self.assertEqual(rows[0]["phase1_course_url"], DS,
                         "phase 1's answer must survive its replacement")

    def test_it_falls_back_to_course_url_when_the_column_is_absent(self):
        """An older result file still triages correctly."""
        r = row("1", "Data Science", url=DS, status="ambiguous")
        self.assertNotIn("phase1_course_url", r)
        triage_rows([r], {"1": source("1", DS, "matched")})
        self.assertEqual(r["course_url"], DS)


class TestManualDecisionsAreNotGivenUp(unittest.TestCase):
    """The failure an overlay exists to avoid.

    112 of the 751 unfilled year-band rows have a gate-clearing prior in the
    sheet. Triage decides from the phase-1 columns, which are empty for these
    rows, so without the guard it reads "we have nothing" and adopts that
    prior over a decision a person made deliberately.
    """

    def manual_row(self):
        r = row("1", "Data Science", url=SECTION_PAGE,
                status=MANUALLY_ASSIGNED)
        r["phase1_course_url"] = ""
        r["phase1_matched_status"] = "no_catalog"
        return r

    def test_a_human_url_survives_a_gate_clearing_prior(self):
        r = self.manual_row()
        stats = triage_rows([r], {"1": source("1", DS, "matched")})
        self.assertEqual(r["course_url"], SECTION_PAGE)
        self.assertEqual(r["matched_status"], MANUALLY_ASSIGNED)
        self.assertEqual(stats.adopted, 0)

    def test_its_provenance_is_still_written(self):
        r = self.manual_row()
        triage_rows([r], {"1": source("1", DS, "matched")})
        self.assertEqual(r["prior_course_url"], DS)
        self.assertEqual(r["url_change"], "changed")


class TestSearchResultsAreNotGivenUp(unittest.TestCase):
    """Triage must not undo work that cost money.

    Phase 1 found nothing for these rows, so triage reading the phase-1
    columns sees "we have nothing" and would adopt a prior instead. 6 of the
    131 rows in the first live trial had a gate-clearing prior.
    """

    def searched_row(self):
        r = row("1", "Data Science", url=DS, status=SEARCH_FOUND)
        r["phase1_course_url"] = ""
        r["phase1_matched_status"] = "no_catalog"
        return r

    def test_a_search_result_survives_a_gate_clearing_prior(self):
        r = self.searched_row()
        stats = triage_rows([r], {"1": source("1", DS_OTHER, "matched")})
        self.assertEqual(r["course_url"], DS)
        self.assertEqual(r["matched_status"], SEARCH_FOUND)
        self.assertEqual(stats.adopted, 0)

    def test_its_provenance_is_still_written(self):
        r = self.searched_row()
        triage_rows([r], {"1": source("1", DS_OTHER, "matched")})
        self.assertEqual(r["prior_course_url"], DS_OTHER)
        self.assertEqual(r["url_change"], "changed")


class TestFlagsAreAppendedOnce(unittest.TestCase):
    """A re-run reached the same verdict and still wrote a different file."""

    def test_a_repeated_flag_is_not_duplicated(self):
        r = {"row_flags": "already_there"}
        add_flag(r, "already_there")
        self.assertEqual(r["row_flags"], "already_there")

    def test_a_new_flag_is_appended(self):
        r = {"row_flags": "one"}
        add_flag(r, "two")
        self.assertEqual(r["row_flags"], "one;two")

    def test_it_works_on_an_empty_field(self):
        r = {"row_flags": ""}
        add_flag(r, "one")
        self.assertEqual(r["row_flags"], "one")

    def test_it_works_on_a_missing_field(self):
        r = {}
        add_flag(r, "one")
        self.assertEqual(r["row_flags"], "one")


if __name__ == "__main__":
    unittest.main()
