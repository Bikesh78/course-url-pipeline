"""Tests encoding the real cases that justify the design.

Every fixture here was observed live against the institutions' own sites during
planning; see ADR-0001. If these regress, the pipeline is unsafe to run.
"""

import unittest

from pipeline.normalize import (
    award_classes, award_tokens, level_of, normalize_name, score,
    strip_institution,
)

ABER = "Aberystwyth University"

# Real <title> values fetched from courses.aber.ac.uk during planning.
TITLE_DS = "Aberystwyth University - Data Science 7G73 BSc"
TITLE_DS_IY = ("Aberystwyth University - Data Science "
               "(with integrated year in industry) 7G74 BSc")
TITLE_ENGLIT = "Aberystwyth University - English Literature Q300 BA"

ROW_DS = "Data Science BSc (Hons)"
ROW_DS_IY = "Data Science (with integrated year in industry) BSc (Hons)"
ROW_ENGLIT = "English Literature BA (Hons)"


class TestInstitutionStripping(unittest.TestCase):
    def test_removes_institution_from_title(self):
        self.assertNotIn("aberystwyth", strip_institution(TITLE_DS, ABER).lower())

    def test_keeps_the_course_part(self):
        self.assertIn("Data Science", strip_institution(TITLE_DS, ABER))

    def test_survives_a_title_that_is_only_the_course(self):
        self.assertEqual(strip_institution("Data Science", ABER), "Data Science")


class TestNormalisation(unittest.TestCase):
    def test_strips_ucas_code(self):
        self.assertNotIn("7g73", normalize_name(TITLE_DS))

    def test_strips_hons(self):
        self.assertNotIn("hons", normalize_name(ROW_DS))

    def test_folds_smart_apostrophe(self):
        # The CSV contains U+2019 in names like "Bachelor’s degree".
        self.assertEqual(normalize_name("Bachelor’s degree of Arts"),
                         normalize_name("Bachelor's degree of Arts"))

    def test_integrated_year_phrasing_is_canonicalised(self):
        a = normalize_name("Data Science (with integrated year in industry)")
        b = normalize_name("Data Science with year in industry")
        self.assertIn("integrated year industry", a)
        self.assertIn("integrated year industry", b)


class TestAwardParsing(unittest.TestCase):
    def test_reads_postnominal(self):
        self.assertEqual(award_tokens(ROW_DS), {"bsc"})

    def test_classifies_bachelor_and_masters(self):
        self.assertIn("bachelor", award_classes("Data Science BSc (Hons)"))
        self.assertIn("masters", award_classes("Digital Curation MSc"))

    def test_reads_australian_vet_credentials(self):
        self.assertIn("vet_certificate",
                      award_classes("Certificate III in Early Childhood Education"))
        self.assertIn("adv_diploma",
                      award_classes("Advanced Diploma of Civil Construction"))
        self.assertIn("pgcert",
                      award_classes("Graduate Diploma of Management (Learning)"))

    def test_reads_long_form_awards(self):
        self.assertIn("bachelor",
                      award_classes("Bachelor’s degree (honours) of Arts (BA)"))

    def test_derives_level(self):
        self.assertEqual(level_of(ROW_DS), "ug")
        self.assertEqual(level_of("MSc Digital Curation"), "pg")
        self.assertEqual(level_of("MBA Master of Business Administration"), "pg")

    def test_award_words_are_not_found_inside_subjects(self):
        # "management" must not yield the "ma" postnominal.
        self.assertNotIn("ma", award_tokens("Management and Marketing"))


class TestNearMissDiscrimination(unittest.TestCase):
    """The failure class the whole design exists to prevent."""

    def test_plain_variant_prefers_its_own_page(self):
        own = score(ROW_DS, TITLE_DS, ABER)
        other = score(ROW_DS, TITLE_DS_IY, ABER)
        self.assertGreater(own, other)
        self.assertGreater(own - other, 0.3, "margin must be decisive")

    def test_integrated_year_variant_prefers_its_own_page(self):
        own = score(ROW_DS_IY, TITLE_DS_IY, ABER)
        other = score(ROW_DS_IY, TITLE_DS, ABER)
        self.assertGreater(own, other)
        self.assertGreater(own - other, 0.3, "margin must be decisive")

    def test_correct_matches_score_high(self):
        for row, title in ((ROW_DS, TITLE_DS), (ROW_DS_IY, TITLE_DS_IY),
                           (ROW_ENGLIT, TITLE_ENGLIT)):
            self.assertGreaterEqual(score(row, title, ABER), 0.80,
                                    f"{row!r} vs {title!r}")

    def test_unrelated_subject_scores_low(self):
        self.assertLess(score(ROW_DS, TITLE_ENGLIT, ABER), 0.35)


class TestAwardGuard(unittest.TestCase):
    """The Equine Science trap: /undergraduate/ 404s, /postgraduate/ 200s."""

    def test_undergraduate_row_prefers_bachelor_page(self):
        bsc = score("Equine Science BSc (Hons)",
                    "Aberystwyth University - Equine Science BSc", ABER)
        msc = score("Equine Science BSc (Hons)",
                    "Aberystwyth University - Equine Science MSc", ABER)
        self.assertGreater(bsc, msc)
        self.assertGreater(bsc - msc, 0.2, "award guard must be decisive")

    def test_level_subtree_disagreement_is_penalised(self):
        same = score("Equine Science BSc (Hons)", "Equine Science", ABER,
                     candidate_level="ug")
        wrong = score("Equine Science BSc (Hons)", "Equine Science", ABER,
                      candidate_level="pg")
        self.assertGreater(same, wrong)

    def test_sibling_postnominal_still_penalised(self):
        # BSc vs BA is a real distinction, though milder than BSc vs MSc.
        exact = score("Data Science BSc", "Data Science BSc", ABER)
        sibling = score("Data Science BSc", "Data Science BA", ABER)
        self.assertGreater(exact, sibling)


class TestTerseNames(unittest.TestCase):
    """Bare-credential rows: 132 rows are just "IELTS", "PTE", "MBA"."""

    def test_bare_credential_reaches_review_not_oblivion(self):
        # Must clear the floor so it lands in the Review Queue rather than
        # being discarded as no_match. Full-string similarity alone scored
        # this 0.167.
        s = score("MBA", "Aberystwyth University - MBA Master of Business "
                         "Administration", ABER)
        self.assertGreater(s, 0.55)

    def test_bare_credential_matches_prep_course(self):
        self.assertGreater(score("IELTS", "IELTS Preparation Course", ""), 0.55)

    def test_containment_does_not_reach_confident_on_its_own(self):
        # A terse name contained in a longer one is suggestive, never
        # confident — it must stay in review.
        self.assertLess(score("IELTS", "IELTS Preparation Course", ""), 0.80)


class TestSupersetDecoy(unittest.TestCase):
    def test_broader_course_does_not_beat_exact_match(self):
        exact = score("Agriculture BSc (Hons)",
                      "Aberystwyth University - Agriculture BSc", ABER)
        superset = score("Agriculture BSc (Hons)",
                         "Aberystwyth University - Agriculture with Animal "
                         "Science BSc", ABER)
        self.assertGreater(exact, superset)
        self.assertGreater(exact - superset, 0.25)


class TestDegenerateInput(unittest.TestCase):
    def test_empty_inputs_score_zero(self):
        self.assertEqual(score("", TITLE_DS, ABER), 0.0)
        self.assertEqual(score(ROW_DS, "", ABER), 0.0)

    def test_bare_credential_names_do_not_crash(self):
        for name in ("IELTS", "PTE", "OSSD", "SACE", "ELICOS", "YEAR 2"):
            self.assertIsInstance(score(name, "IELTS Preparation", ""), float)


if __name__ == "__main__":
    unittest.main()
