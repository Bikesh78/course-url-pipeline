"""Keep the code's vocabulary documented where a reader meets it.

Docstring coverage had drifted to 44% of public definitions, including all four
central types (`Candidate`, `Catalog`, `CourseRow`, `MatchResult`), which meant
learning the codebase required asking someone. These tests stop it drifting
back.
"""

import ast
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = sorted(glob.glob(os.path.join(ROOT, "pipeline", "*.py"))) + \
    [os.path.join(ROOT, "run.py")]

# Types whose meaning a reader must not have to reconstruct from usage.
CENTRAL_TYPES = {
    "pipeline/catalog.py": ["Candidate", "Catalog"],
    "pipeline/load.py": ["CourseRow"],
    "pipeline/match.py": ["MatchResult", "Thresholds"],
}


def public_defs(path):
    """Yield (node, qualified_name) for every public def/class in *path*."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        if node.name.startswith("_"):
            continue
        yield node, node.name


def rel(path):
    return os.path.relpath(path, ROOT)


class TestDocstringCoverage(unittest.TestCase):
    def test_every_public_definition_is_documented(self):
        missing = [f"{rel(p)}:{n.lineno} {name}"
                   for p in MODULES for n, name in public_defs(p)
                   if not ast.get_docstring(n)]
        self.assertEqual(missing, [],
                         "undocumented public definitions:\n  " +
                         "\n  ".join(missing))

    def test_module_docstrings_exist(self):
        missing = []
        for p in MODULES:
            with open(p, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            if not ast.get_docstring(tree) and os.path.getsize(p) > 200:
                missing.append(rel(p))
        self.assertEqual(missing, [], f"modules without a docstring: {missing}")


class TestDocstringQuality(unittest.TestCase):
    """A docstring must add something the signature does not."""

    def test_summary_lines_are_sentences(self):
        bad = []
        for p in MODULES:
            for n, name in public_defs(p):
                doc = ast.get_docstring(n)
                if not doc:
                    continue
                first = doc.strip().splitlines()[0].strip()
                if not first.endswith((".", ":", "?", "!")):
                    bad.append(f"{rel(p)}:{n.lineno} {name} -> {first!r}")
        self.assertEqual(bad, [],
                         "summary lines should read as sentences:\n  " +
                         "\n  ".join(bad))

    def test_summaries_are_not_the_name_restated(self):
        # "def domain_of(url)" documented as "Domain of url." tells a reader
        # nothing the signature did not already say.
        bad = []
        for p in MODULES:
            for n, name in public_defs(p):
                doc = ast.get_docstring(n)
                if not doc:
                    continue
                first = doc.strip().splitlines()[0]
                # Split on any non-letter so hyphenated and punctuated words
                # survive. Filtering with str.isalpha() instead drops every
                # word in "Rate-limited, robots-aware, disk-cached fetcher.",
                # leaving an empty list -- and the empty set is a subset of
                # anything, so a good docstring gets flagged.
                words = [w for w in re.split(r"[^a-z]+", first.lower()) if w]
                name_words = set(name.lower().split("_"))
                if words and len(words) <= 3 and set(words) <= name_words:
                    bad.append(f"{rel(p)}:{n.lineno} {name} -> {first!r}")
        self.assertEqual(bad, [],
                         "summaries that only restate the name:\n  " +
                         "\n  ".join(bad))


class TestCentralTypesExplainThemselves(unittest.TestCase):
    """The four types the whole codebase is written in terms of."""

    def test_central_types_have_substantial_docstrings(self):
        thin = []
        for relpath, names in CENTRAL_TYPES.items():
            path = os.path.join(ROOT, relpath)
            found = {n.name: ast.get_docstring(n) or ""
                     for n, _ in public_defs(path)}
            for name in names:
                self.assertIn(name, found, f"{name} missing from {relpath}")
                if len(found[name].split()) < 25:
                    thin.append(f"{relpath} {name}")
        self.assertEqual(thin, [],
                         f"central types need more than a one-liner: {thin}")

    def test_central_types_point_at_the_glossary(self):
        # CONTEXT.md is the single authority for domain terms; the code should
        # reference it rather than restate it and drift.
        for relpath in ("pipeline/catalog.py", "pipeline/load.py"):
            path = os.path.join(ROOT, relpath)
            docs = " ".join(ast.get_docstring(n) or ""
                            for n, _ in public_defs(path))
            self.assertIn("CONTEXT.md", docs,
                          f"{relpath} should cross-reference the glossary")


class TestCrawlerVocabulary(unittest.TestCase):
    """Terms specific to this crawler live in catalog.py, not CONTEXT.md."""

    def setUp(self):
        with open(os.path.join(ROOT, "pipeline", "catalog.py"),
                  encoding="utf-8") as fh:
            self.doc = ast.get_docstring(ast.parse(fh.read())) or ""

    def test_the_heavily_used_terms_are_defined(self):
        # seed appears 39 times in this module, hub 25, listing 18.
        for term in ("hub path", "seed", "listing page", "course page",
                     "ladder"):
            self.assertIn(term, self.doc.lower(),
                          f"{term!r} is used throughout but never defined")

    def test_it_says_listing_is_not_a_detected_page_type(self):
        # The crawler classifies no pages; course pages are often the richest
        # source of Candidates. The docstring must not imply otherwise.
        self.assertIn("not", self.doc.lower().split("listing page**")[1][:400],
                      "docstring should state that listing page is not a "
                      "page type the crawler recognises")


if __name__ == "__main__":
    unittest.main()
