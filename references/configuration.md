# Configuration

## Precedence

Runtime values resolve in this order:

1. command-line arguments;
2. the project profile;
3. a file named by `LIT_RESEARCH_RUNTIME_CONFIG`;
4. ignored `assets/local_runtime.yaml`;
5. environment variables and built-in defaults, where supported.

Credentials are loaded from the process environment or `--env-file`. Runtime YAML must contain paths and workflow preferences only.

## Core profile fields

- `project`: filesystem-safe project identity.
- `topic`: human-readable scope.
- `data_root`: optional output root; blank uses `~/.literature-research/projects`.
- `output_language`: `en` for public generic reports or `zh-CN` for Chinese reports.
- `date_from`, `date_to`: optional ISO dates or years.
- `screening_mode`: `generic` by default.
- `search_terms`: domain/topic terms used for query generation and generic triage.
- `method_terms`: optional methods that must drive the study when `require_method_primary` is true.
- `exclude_terms`: optional literal keyword exclusions for automated triage.
- `include_rules`, `exclude_rules`: semantic rules for human review; the generic classifier does not pretend to execute prose rules.
- `direction_terms`: optional mapping from direction labels to literal keyword lists.
- `custom_queries`: expert-authored source queries run before generated combinations.
- `require_pdf_for_inclusion`: keeps final inclusion PDF-grounded.
- `original_research_only`: routes reviews, perspectives, commentaries, and editorials to `peripheral`.
- `representatives_include_candidates`: when false, final representative lists do not use unresolved candidates as filler.

## Search controls

`search_depth` selects a preset. Individual `search_max_queries`, `search_max_records`, `search_max_per_query`, `search_source_max_pages`, `search_split_years`, and `search_year_chunk_size` values override it. Zero removes a local cap; it does not remove upstream API limits.

Use `custom_queries` for controlled vocabulary, database field syntax, or unfamiliar domains. Inspect `query_coverage_report.csv` and `search_coverage_audit.md` before making coverage claims.

## Screening modes

`generic` uses literal terms from the profile and is the supported public default. The codebase retains specialist modes for reproducibility of earlier projects:

- `environmental_catalysis_theory_ml`
- `spinel_oxide_catalysis`
- `high_throughput_screening`
- `phenol_electrocatalytic_hydrogenation_reduction`
- `electrocatalytic_hydrogenation_reduction`
- `electrocatalytic_hydrogenation_reduction_no_co2rr_nitrate`
- `one_two_electron_wor`

Specialist modes encode domain judgments and should not be selected for unrelated topics. New public workflows should prefer profile data over another hard-coded classifier.

## Local runtime template

Copy `assets/local_runtime.example.yaml` to `assets/local_runtime.yaml`, or store the copy elsewhere and set `LIT_RESEARCH_RUNTIME_CONFIG`. The real local file is gitignored.

