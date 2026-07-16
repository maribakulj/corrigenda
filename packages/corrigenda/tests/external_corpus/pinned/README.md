# Pinned offline corpus (blocking tier)

Real Gallica ALTO pages committed to the repository so a small,
immutable slice of the external corpus runs in the DEFAULT test suite —
offline, on every merge, with no `external_corpus` marker and no
self-skip. The fetched `.cache/` tier stays non-blocking; this tier is
the guarantee that at least some never-seen-in-development OCR output
gates every change.

## Populating (maintainer, deliberate act)

1. Run `python tests/external_corpus/fetch.py` on a machine with
   Gallica access and let it print/verify the SHA-256 pins.
2. Copy 2–3 representative pages from `.cache/` into this directory —
   at least one multi-column periodical page and one monography page.
   Keep the `<ark>_p<NNNN>.alto.xml` names.
3. Record each file below (ark, page, sha256, fetch date). Source:
   gallica.bnf.fr / Bibliothèque nationale de France — public-domain
   documents; keep this attribution.
4. Never replace a pinned file silently: a re-pin is a reviewed change
   explaining why (e.g. legitimate Gallica re-OCR).

The dev corpus (`examples/`, ark bpt6k3265015q) must NEVER appear here —
this tier only means something if the code was written blind to it.

## Contents

| file | sha256 | fetched |
|---|---|---|
| _(empty — populate per the steps above)_ | | |
