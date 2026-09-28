# Contributing

Keep changes evidence-oriented, domain-neutral by default, and compatible with the status and provenance contract in `SKILL.md`.

Before submitting a change:

1. Add or update a behavior-focused test.
2. Run `python -m unittest discover -s tests -v`.
3. Run `python scripts/lit.py doctor` and `python scripts/lit.py --help`.
4. Confirm that no secrets, local paths, PDFs, databases, browser profiles, or generated research outputs are staged.
5. Document changes to profile fields or output semantics in the relevant reference file.

Do not add retrieval methods that bypass paywalls or access controls. New source adapters should respect source terms, rate limits, and provenance requirements.
