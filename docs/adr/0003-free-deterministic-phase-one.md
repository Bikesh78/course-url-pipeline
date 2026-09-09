---
status: accepted
---

# Phase 1 is free, deterministic, and stdlib-only; paid tiers are plug-ins

A reader may wonder why a pipeline resolving 10,637 URLs uses no search API and
no LLM. The reason is that Verification turns out not to need judgement: the
course name is printed on the page we fetched, so comparing it to the name we
already hold is string work. Normalised `difflib` scoring separates the exact
near-miss class we care about — the two Data Science variants score 0.865 and
0.951 against their true pages versus 0.457 and 0.514 against each other's, and
an Award-agreement penalty scores `Equine Science BSc (Hons)` at 0.61 against
its BSc page versus 0.32 against the MSc page.

An LLM re-reading 10,637 pages would cost real money to reach a *worse* result,
because it would judge each row in isolation and could not enforce the
Assignment uniqueness constraint of ADR-0002.

## Consequences

Phase 1 spends nothing and installs nothing, so its coverage number is a
measured fact rather than an estimate. Institutions that fail Extraction Health
stay unresolved until Phase 2. Paid capability — search fallback for the long
tail, LLM adjudication of thin Margins, headless rendering for JavaScript
Sites — is deliberately shaped as a plug-in behind the Catalog ladder and the
Assignment adjudication hook, so enabling it later is configuration rather than
a rewrite. The decision to spend is deferred until `coverage_report.md` says
what the gap actually is.

## Clarification (2026-09-09)

The decision above stands unchanged; this note records what it governs, because
the title's "stdlib-only" and the phrase "installs nothing" in Consequences have
been read together as a blanket ban on third-party packages, which was never the
intent. The title is left as written — it describes what Phase 1 in fact needs —
but it is a description, not a prohibition.

What this ADR rules out is **paid and nondeterministic services in Phase 1** — a
search API, an LLM — for the reasons argued above: they cost money to reach a
worse result, and they would make the coverage number an estimate rather than a
measured fact.

It does not rule out ordinary pure-Python libraries. It never did: `bs4` and
`lxml` were already in use when this was written, behind `try/except
ImportError` with a stdlib fallback, and remain compatible with it.

Phase 2's `SerperProvider` is likewise **not** an exception to this ADR but the
plug-in it anticipated ("search fallback for the long tail ... deliberately
shaped as a plug-in ... so enabling it later is configuration rather than a
rewrite"). It is opt-in by name, so Phase 1's guarantee is intact: no run spends
anything unless `--search-provider serper` asks it to.

A future dependency is therefore a normal engineering choice to argue on its
merits, not a decision this ADR forecloses. `SERPER_API_KEY` loading was
written against the standard library for its own reasons — a credential file
that can interpolate is one whose effective value has to be traced rather than
read — and not because a package was unavailable.
