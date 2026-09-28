# Screening Schema

Statuses:

- `candidate`: discovered but not screened.
- `pending_pdf`: likely relevant, but no local PDF yet.
- `pending_fulltext`: PDF exists but full text has not been parsed or screened.
- `included`: full-text verified and method-primary evidence exists.
- `peripheral`: related, but the requested method is secondary.
- `excluded`: not relevant or fails the requested method rule.

Direction labels:

- `mechanism_dft`
- `reaction_pathway`
- `catalyst_screening`
- `ml_prediction`
- `molecular_simulation`
- `hybrid_theory_experiment`
- `other`

Evidence must be a sentence or short passage from the title, abstract, or full text that supports the status. For final included papers, prefer full-text method or results evidence over abstract-only evidence.

## Generic mode

The public default `screening_mode: generic` treats `search_terms`, `method_terms`, `exclude_terms`, and `direction_terms` as literal keyword configuration. It does not interpret prose in `include_rules` or `exclude_rules`; those rules remain a human-review contract.

Automated decisions are triage. Manually inspect borderline inclusions, exclusions caused by ambiguous terms, duplicate versions, reviews misclassified as original research, and cases where the configured method is mentioned but not central.

Specialist screening modes are documented in `references/configuration.md` and must be selected explicitly.
