---
status: accepted
---

# Declare and pin the environment, and make a degraded parser announce itself

ADR-0003 says Phase 1's coverage number is "a measured fact rather than an
estimate". That was only true of one machine.

`bs4` and `lxml` were never declared anywhere. They arrived as `apt` packages —
`python3-bs4 4.10.0-2`, `python3-lxml 4.8.0-1build1` — in
`/usr/lib/python3/dist-packages`, almost certainly pulled in as a transitive
dependency of some system tool rather than chosen for this project. They were
visible only because `pyenv` is set to `system`, so `python3` resolves to
`/usr/bin/python3`, which has `dist-packages` on its path. A pyenv-built
interpreter has isolated `site-packages` and cannot see that directory at all.

So extraction had three tiers, and which one a run got depended on which
`python3` happened to be invoked:

| tier | condition | link extraction |
|---|---|---|
| best | `bs4` + `lxml` | `BeautifulSoup(html, "lxml")` |
| middle | `bs4`, no `lxml` | `BeautifulSoup(html, "html.parser")` |
| worst | no `bs4` | regex anchor scraper |

None of the three said which it was. The `lxml` attempt sat inside
`extract_links` behind a bare `except Exception`, so a missing parser was
rediscovered and re-swallowed on every page of every Site — invisible, and paid
for repeatedly. The committed 46.1% and 51.1% figures are tier-one numbers;
the same code on pyenv 3.11.9 runs in tier three and would produce different
ones without a word.

## Decision

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`, both
committed. `bs4` and `lxml` are pinned to **the apt versions** — 4.10.0 and
4.8.0 — because those are what produced the committed figures. `requires-python`
is `>=3.10,<3.11`, and the environment is built on `/usr/bin/python3.10`
(CPython 3.10.12), the exact interpreter those figures were measured on, so
that the libraries are the only thing the declaration changes.

The parser is resolved **once, at import**, and exposed as
`catalog.PARSER_TIER`. A tier below the best logs a warning naming what is
missing, and the tier is written into the run log and into
`coverage_report.md` — a coverage figure produced without `lxml` should say so
on its face, not in stderr the reader never saw.

The fallbacks are kept. A machine without `lxml` must still be able to run the
pipeline; degraded means *announced*, not fatal.

This does not revisit ADR-0003. Phase 1 still spends nothing, and the
clarification appended to that ADR already records that its rule governs paid
and nondeterministic services rather than pure-Python libraries.

## Consequences

`uv sync` reproduces the environment, and `uv run python run.py …` reproduces
the results: verified by re-running Phase 2 in the pinned environment and
diffing against the committed `phase2.csv`, which came out byte-identical. That
diff is the acceptance test for any future change to the pin — a difference
means the pin is wrong, not that the output moved.

`pytest` is declared as a dev dependency and now works. It previously existed
on no interpreter on the development machine, so the test counts in commit
messages were unreproducible; the suite is `unittest`-based and runs either way.

No `.python-version` file is created, deliberately: `pyenv` reads that filename
too, and with only `3.11.9` and `system` installed a file naming 3.10 would
make every bare `python3` in this directory fail. The bare-interpreter workflow
keeps working unchanged, and now reports its tier.

Upgrading `bs4` or `lxml` past the apt vintage is deliberately left undone. It
should be a separate change with the coverage effect measured, since bundling
it here would make any difference unattributable.
