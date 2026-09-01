"""Name normalisation, Award/Level parsing, and Score computation.

The pipeline's whole claim to correctness rests on this module: a Score here is
what decides whether a Course Row gets a URL. See ADR-0003 for why this is
deterministic string work rather than a model call, and `docs/SCORING.md` for
worked arithmetic on real pairs.

How a Score is computed
-----------------------
`score()` applies these steps in order. Each stage is listed with the reason it
exists, because several look arbitrary until you know what they prevent.

1. **Prepare the text.** Strip the Institution's own name from the Candidate
   (a title is usually "<Institution> - <Course> <code> <Award>", and leaving
   the Institution in depresses a correct match by ~0.2), then normalise both
   sides: drop course codes (7G73, Q300), "(Hons)", durations ("3 years"),
   study modes, stopwords — **and the Award itself**.

   Removing the Award is the non-obvious part. It is reapplied in step 4 as a
   multiplier, so leaving it in the compared text would count it twice and
   inflate two unrelated subjects that happen to share a credential:
   "Equine Science BSc" against "Data Science BSc" scored 0.634 with the Award
   in the text and 0.517 without.

   A name that is *only* an Award ("MBA") normalises to nothing, so those fall
   back to the Award-inclusive text.

2. **Measure three ways.** `seq` (SequenceMatcher, notices order and partial
   words), `tok` (Jaccard on token sets, order-independent), `cov`
   (containment: shared tokens over the shorter side).

3. **Blend into a base**, taking exactly one of two branches:

   - the Candidate is terse (one content token) -> containment blend, and the
     qualifier penalty is deliberately *not* applied, since extra words on the
     other side are unavoidable rather than evidence against the match;
   - otherwise -> symmetric blend, penalised per qualifier the Candidate has
     that the row does not.

   Applying containment to every length asymmetry instead of only to terse
   names scores the "(with integrated year in industry)" decoy 0.73 and
   collapses the variant Margin from 0.46 to 0.27 — it breaks the exact
   property this pipeline exists to protect.

4. **Multiply by Award agreement**, then by **Level agreement**.

5. **Round** to four places and cap at 1.0.

What this module does *not* decide
----------------------------------
The number `score()` returns is **not** the final Score. `score_all()` in
`pipeline/match.py` multiplies it by `url_specificity()` from
`pipeline/catalog.py`, so a `score()` of 1.000 becomes 0.820 for a subject hub
page. Nothing here knows about URLs.

Thresholds — what a Score has to reach to fill a cell — live in
`pipeline/match.py` and are documented in `docs/CALIBRATION.md`. This module
only measures; it never decides.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

# --------------------------------------------------------------------------
# Award vocabulary
# --------------------------------------------------------------------------
# Specific Award tokens. Order matters only for readability; matching is by
# whole token. A mismatch between two *specific* Awards is meaningful:
# "Data Science BSc" and "Data Science BA" are different courses.
_AWARD_TOKENS = {
    # bachelor
    "ba": "bachelor", "bsc": "bachelor", "ba(hons)": "bachelor",
    "beng": "bachelor", "bcom": "bachelor", "bbus": "bachelor",
    "bcompsc": "bachelor", "bnurs": "bachelor", "bmus": "bachelor",
    "bed": "bachelor", "bfa": "bachelor", "llb": "bachelor",
    "bn": "bachelor", "bhsc": "bachelor", "bit": "bachelor",
    "bba": "bachelor", "bdes": "bachelor", "barch": "bachelor",
    "bvsc": "bachelor", "bpharm": "bachelor", "bmid": "bachelor",
    # integrated / undergraduate masters: four-year degrees entered from
    # school, which Institutions list under /undergraduate/. Classing these as
    # taught masters makes the Level guard penalise the correct page.
    "meng": "integrated_masters", "msci": "integrated_masters",
    "mmath": "integrated_masters", "mchem": "integrated_masters",
    "mphys": "integrated_masters", "mbiol": "integrated_masters",
    "mpharm": "integrated_masters", "magr": "integrated_masters",
    "mag": "integrated_masters", "mgeol": "integrated_masters",
    "mcomp": "integrated_masters", "menv": "integrated_masters",
    # taught / research masters
    "ma": "masters", "msc": "masters", "mba": "masters", "mres": "masters",
    "mphil": "masters", "llm": "masters", "mfa": "masters", "mn": "masters",
    "med": "masters", "march": "masters", "mmus": "masters",
    "mpa": "masters", "mph": "masters", "msw": "masters", "mst": "masters",
    # doctorate
    "phd": "doctorate", "dprof": "doctorate", "edd": "doctorate",
    "dba": "doctorate", "md": "doctorate", "jd": "doctorate",
    "dag": "doctorate", "dsc": "doctorate", "dclinpsy": "doctorate",
    "dphil": "doctorate", "engd": "doctorate",
    # foundation / sub-degree
    "fda": "foundation", "fdsc": "foundation", "fdeng": "foundation",
    "hnd": "foundation", "hnc": "foundation",
    # postgraduate certificates
    "pgcert": "pgcert", "pgdip": "pgcert", "pgce": "pgcert",
    "gradcert": "pgcert", "graddip": "pgcert",
}

# Long-form Award phrases, mapped to a class directly. Australian VET
# credentials matter here: a large share of Institutions in this dataset are
# Australian RTOs whose course names are "Certificate III in ..." rather than
# a postnominal.
_AWARD_PHRASES = [
    (r"\badvanced\s+diploma\b", "adv_diploma"),
    (r"\bgraduate\s+diploma\b", "pgcert"),
    (r"\bgraduate\s+certificate\b", "pgcert"),
    (r"\bpostgraduate\s+diploma\b", "pgcert"),
    (r"\bpostgraduate\s+certificate\b", "pgcert"),
    (r"\bcertificate\s+(?:i|ii|iii|iv|1|2|3|4)\b", "vet_certificate"),
    (r"\bdiploma\b", "diploma"),
    (r"\bdoctor(?:ate)?\s+of\b", "doctorate"),
    (r"\bmaster(?:'?s)?(?:\s+degree)?\s+of\b", "masters"),
    (r"\bmaster(?:'?s)?(?:\s+degree)?\s+in\b", "masters"),
    (r"\bbachelor(?:'?s)?(?:\s+degree)?\s+(?:\(honours\)\s+)?of\b", "bachelor"),
    (r"\bbachelor(?:'?s)?(?:\s+degree)?\s+in\b", "bachelor"),
    (r"\bfoundation\s+(?:degree|year)\b", "foundation"),
    (r"\bassociate\s+degree\b", "foundation"),
]

_UNDERGRAD_CLASSES = {"bachelor", "foundation", "integrated_masters",
                      "vet_certificate", "diploma", "adv_diploma"}
_POSTGRAD_CLASSES = {"masters", "doctorate", "pgcert"}

# UCAS / provider course codes. Real examples from this dataset: 7G73, Q300,
# 142L, C801, D406D, 198L. Their shapes vary too much for one regex, so a code
# is identified by character composition instead: a short alphanumeric token
# carrying at least two digits. Two digits is the threshold that keeps genuine
# content ("Year 11", "3D Animation", "Level 5") while dropping codes.
def _is_course_code(token: str) -> bool:
    if not (3 <= len(token) <= 7) or not token.isalnum():
        return False
    return sum(c.isdigit() for c in token) >= 2
_HONS_RE = re.compile(r"\(\s*hons?\.?\s*\)|\bhons\b|\bhonours\b|\bhonors\b",
                      re.IGNORECASE)

# Noise that appears in <title> tags but never distinguishes a course.
_TITLE_NOISE = re.compile(
    r"\b(?:course|courses|degree|degrees|programme|program|study|undergraduate"
    r"|postgraduate|full[- ]time|part[- ]time|entry|apply|overview|home)\b",
    re.IGNORECASE,
)

_SPLIT_TITLE = re.compile(r"\s+[\|–—·:]\s+|\s+-\s+")

# Connective words carry no distinguishing information but inflate token
# overlap: "Accounting and Finance" vs "Accounting and Marketing" would share
# "and". Removed before any token comparison.
_STOPWORDS = {"of", "and", "the", "in", "with", "for", "a", "an", "to", "at",
              "on", "by", "or", "into", "from"}


# --------------------------------------------------------------------------
# Scoring weights
# --------------------------------------------------------------------------
# Every number the Score formula uses lives here rather than inside an
# expression, so that a reader can see what is tuned and a tuner can see what
# it costs. These are *weights*; the *thresholds* that turn a Score into a
# decision (CONFIDENT, FLOOR, MIN_MARGIN) live in `pipeline/match.py` and are
# documented in `docs/CALIBRATION.md`.
#
# All of these were set from measured separations on real pages, not guessed.
# `docs/SCORING.md` shows the arithmetic.

# Base blend. Sequence similarity carries most of the weight because it
# notices word order and partial words; token overlap is the corrective that
# stops a shared prefix dominating.
SEQ_WEIGHT = 0.65
TOKEN_WEIGHT = 0.35

# Containment blend, used only when one side reduces to a single content token
# — the bare-credential rows ("MBA", "IELTS", "PTE"). Full-string similarity
# punishes their length asymmetry: "MBA" against "MBA Master of Business
# Administration" scored 0.193 and was discarded; containment scores it 0.663
# and sends it to review.
TERSE_COVERAGE_WEIGHT = 0.55
TERSE_TOKEN_WEIGHT = 0.45

# Qualifier penalty, applied when the Candidate carries words the Course Row
# never mentions. Without it "Agriculture BSc (Hons)" tied at exactly 1.000
# against both "Agriculture" and "Agriculture (Top-Up)", driving the Margin to
# zero and making a real choice look like a coin flip. The floor stops a long
# official title from ever losing to a terse wrong one.
QUALIFIER_PENALTY_PER_TOKEN = 0.06
QUALIFIER_PENALTY_FLOOR = 0.72

# Award disagreement. This is the guard that stops an undergraduate row taking
# a postgraduate page — see ADR-0001, where /undergraduate/equine-science/ 404s
# while /postgraduate/equine-science/ returns 200. A different Award *class*
# (BSc vs MSc) is a stronger signal of mismatch than a sibling postnominal
# within one class (BSc vs BA), which is why they differ.
AWARD_CLASS_MISMATCH = 0.45
AWARD_SIBLING_MISMATCH = 0.62
AWARD_CLASS_ONLY_MISMATCH = 0.50

# Level disagreement between the row's Award and the Catalog subtree the
# Candidate was found in. Weaker than the Award guard because Level is inferred
# and the two legitimately disagree for integrated masters, which Institutions
# file under /undergraduate/.
LEVEL_MISMATCH = 0.50

# A page title is split on separators, and a segment is discarded as the
# Institution's own name when at least this share of its words are the
# Institution's. Leaving the Institution in place depresses a correct match by
# roughly 0.2.
INSTITUTION_SEGMENT_SHARE = 0.5


def _ascii_fold(s: str) -> str:
    """Collapse smart quotes and accents. The CSV contains U+2019 apostrophes."""
    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


# Spelling variants of the *same* Award. Without canonicalisation these are
# scored as sibling awards and penalised: Aberystwyth's CSV rows say "MAgr"
# while its listing pages say "MAg", which cost the correct page 38% of its
# score and handed the match to a subject hub page instead.
_AWARD_ALIASES = {
    "mag": "magr", "dag": "dagr", "magric": "magr",
    "bscecon": "bsc", "bahons": "ba", "bschons": "bsc",
    "msci": "msci", "mscience": "msci",
    "pgdip": "pgdip", "pgdiploma": "pgdip",
}


def _canonical_award(token: str) -> str:
    return _AWARD_ALIASES.get(token, token)


def award_classes(text: str) -> set[str]:
    """Award classes present in *text* (e.g. {"bachelor"})."""
    s = _ascii_fold(text).lower()
    found = set()
    for pattern, cls in _AWARD_PHRASES:
        if re.search(pattern, s):
            found.add(cls)
    for tok in re.findall(r"[a-z]+", re.sub(r"[^a-z\s]+", " ", s)):
        if tok in _AWARD_TOKENS:
            found.add(_AWARD_TOKENS[tok])
    return found


def award_tokens(text: str) -> set[str]:
    """Specific Award postnominals present in *text* (e.g. {"bsc"})."""
    s = _ascii_fold(text).lower()
    s = re.sub(r"[^a-z\s]+", " ", s)
    return {_canonical_award(t) for t in s.split() if t in _AWARD_TOKENS}


def level_of(text: str) -> str | None:
    """Level implied by *text*: "ug", "pg", or None when undetermined."""
    classes = award_classes(text)
    ug = bool(classes & _UNDERGRAD_CLASSES)
    pg = bool(classes & _POSTGRAD_CLASSES)
    if ug and not pg:
        return "ug"
    if pg and not ug:
        return "pg"
    return None


def strip_institution(title: str, institution_name: str) -> str:
    """Drop the Institution's own name from a page title.

    Titles are overwhelmingly "<Institution> - <Course> <code> <Award>", and
    leaving the Institution in place depresses a correct match by ~0.2.
    """
    if not title:
        return ""
    inst_words = {w for w in re.findall(r"[a-z]{4,}", _ascii_fold(institution_name).lower())}
    parts = [p for p in _SPLIT_TITLE.split(title) if p.strip()]
    if len(parts) > 1 and inst_words:
        kept = []
        for p in parts:
            p_words = set(re.findall(r"[a-z]{4,}", _ascii_fold(p).lower()))
            # Drop a segment only when it is mostly Institution name.
            if (p_words and len(p_words & inst_words) / len(p_words)
                    >= INSTITUTION_SEGMENT_SHARE):
                continue
            kept.append(p)
        if kept:
            parts = kept
    out = " ".join(parts)
    # Also remove a leading/trailing bare mention that survived splitting.
    if inst_words:
        pattern = r"\b" + r"\s+".join(re.escape(w) for w in
                                     _ascii_fold(institution_name).lower().split()) + r"\b"
        out = re.sub(pattern, " ", out, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out).strip()


def normalize_name(text: str, drop_awards: bool = False) -> str:
    """Reduce a course name or page title to its comparable core."""
    if not text:
        return ""
    s = _ascii_fold(text)
    s = _HONS_RE.sub(" ", s)
    s = s.lower()
    s = re.sub(r"\bwith\s+integrated\s+year\s+in\s+industry\b",
               " integrated year industry ", s)
    # Listing pages append study duration and mode to the course name
    # ("Accounting and Finance (BSc, 3 years)"). These vary between the
    # Catalog and the CSV and carry no identity, so they are removed before
    # the integrated-year rule's output is tokenised.
    s = re.sub(r"\b\d+\s*(?:years?|yrs?|months?|semesters?)\b", " ", s)
    s = re.sub(r"\b(?:full|part)[\s-]*time\b", " ", s)
    s = re.sub(r"\byear\s+in\s+industry\b", " integrated year industry ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = [t for t in s.split()
              if t and not _is_course_code(t) and t not in _STOPWORDS]
    if drop_awards:
        tokens = [t for t in tokens if t not in _AWARD_TOKENS]
    return " ".join(tokens)


def _token_overlap(a: str, b: str) -> float:
    """Jaccard on token sets — robust to word order, unlike SequenceMatcher."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _token_coverage(a: str, b: str) -> float:
    """Fraction of the *shorter* name's tokens present in the longer one.

    Needed because many Course Rows are bare credentials ("MBA", "IELTS")
    while the page title spells the award out in full ("MBA Master of Business
    Administration"). Full-string similarity punishes that length asymmetry
    and scored such pairs near 0.17; coverage recognises the containment.
    """
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def score(row_name: str, candidate_name: str, institution_name: str = "",
          candidate_level: str | None = None) -> float:
    """Score how well *candidate_name* matches *row_name*, on 0.0-1.0.

    Blends sequence similarity with token overlap, then penalises Award and
    Level disagreement. Award disagreement is the guard that stops an
    undergraduate row taking a postgraduate page (ADR-0001).
    """
    cand_clean = strip_institution(candidate_name, institution_name) or candidate_name
    # Compare subjects only. Award agreement is handled below as a multiplier,
    # so leaving Award tokens in the compared text would double-count it and
    # inflate the similarity of two different subjects sharing an Award
    # ("Equine Science BSc" vs "Data Science BSc" scored 0.634 that way, 0.517
    # this way).
    a = normalize_name(row_name, drop_awards=True)
    b = normalize_name(cand_clean, drop_awards=True)
    if not a or not b:
        # Fall back to award-inclusive text for names that are nothing but a
        # credential ("MBA", "IELTS"), which would otherwise normalise to "".
        a = normalize_name(row_name)
        b = normalize_name(cand_clean)
    if not a or not b:
        return 0.0

    seq = difflib.SequenceMatcher(None, a, b).ratio()
    tok = _token_overlap(a, b)
    cov = _token_coverage(a, b)
    # Two readings of similarity, whichever is kinder:
    #   - symmetric: the two names describe the same thing at the same length
    #   - containment: one name is a terse form of the other
    # Containment alone is too permissive ("Agriculture" is contained in
    # "Agriculture with Animal Science"), so it is always tempered by Jaccard
    # and can never reach 1.0 on partial overlap.
    base = SEQ_WEIGHT * seq + TOKEN_WEIGHT * tok
    # The containment reading is gated to names that reduce to a single content
    # token — the bare-credential rows ("MBA", "IELTS", "PTE"). Applying it to
    # every length asymmetry destroys the property this pipeline exists to
    # protect: "Data Science" is contained in "Data Science (with integrated
    # year in industry)", and an ungated containment branch scored that decoy
    # 0.73, collapsing the variant Margin from 0.46 to 0.27.
    ta, tb = a.split(), b.split()
    if min(len(ta), len(tb)) == 1:
        # Terse name: containment is the operative reading, and extra tokens on
        # the other side are unavoidable rather than evidence against the
        # match, so the qualifier penalty below is deliberately not applied.
        base = max(base,
                   TERSE_COVERAGE_WEIGHT * cov + TERSE_TOKEN_WEIGHT * tok)
    else:
        # A Candidate carrying qualifiers the Course Row never mentions is a
        # worse match than an exact one, even when it contains every word the
        # row has. Without this, "Agriculture BSc (Hons)" ties at 1.000 against
        # both "Agriculture" and "Agriculture (Top-Up)" and the Margin
        # collapses to zero. Capped so a long official title never loses to a
        # terse wrong one.
        extra = len(set(tb) - set(ta))
        if extra:
            base *= max(QUALIFIER_PENALTY_FLOOR,
                        1.0 - QUALIFIER_PENALTY_PER_TOKEN * extra)

    # Specific Award disagreement (BSc vs BA) — strong signal.
    at, bt = award_tokens(row_name), award_tokens(cand_clean)
    if at and bt and not (at & bt):
        ac, bc = award_classes(row_name), award_classes(cand_clean)
        # Different class entirely (BSc vs MSc) is worse than a sibling
        # postnominal within the same class (BSc vs BA).
        base *= (AWARD_CLASS_MISMATCH if (ac and bc and not (ac & bc))
                 else AWARD_SIBLING_MISMATCH)
    else:
        ac, bc = award_classes(row_name), award_classes(cand_clean)
        if ac and bc and not (ac & bc):
            base *= AWARD_CLASS_ONLY_MISMATCH

    # Level disagreement against the Catalog subtree the Candidate came from.
    row_level = level_of(row_name)
    if candidate_level and row_level and candidate_level != row_level:
        base *= LEVEL_MISMATCH

    return round(min(base, 1.0), 4)
