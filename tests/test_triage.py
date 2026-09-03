"""Prior-URL triage: the gate, the adoption rules, and the sharing guard."""

import unittest

from pipeline.triage import (CARRIED_OVER, classify_change, gate_score,
                             triage_rows)

SITE = "https://courses.aber.ac.uk"
DS = "https://courses.aber.ac.uk/undergraduate/data-science"
DS_OTHER = "https://courses.aber.ac.uk/undergraduate/data-science-iy"
ANTH = "https://courses.aber.ac.uk/undergraduate/anthropology"


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


if __name__ == "__main__":
    unittest.main()
