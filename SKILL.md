---
name: literature-research
description: Build and maintain evidence-grounded literature libraries through reproducible multi-source search, staged screening, lawful PDF retrieval, full-text verification, synthesis, and auditable exports. Use for sustained literature projects; do not use for a single citation lookup or unsupported claims of exhaustive coverage.
metadata:
  short-description: Build auditable, PDF-grounded literature libraries
---

# Literature Research

Create a reproducible literature project whose claims can be traced to source metadata and, for final inclusion, local full text.

## Operating contract

- Start from an explicit YAML profile. Copy `assets/research_profile.yaml` and adapt the topic, dates, terms, sources, inclusion rules, and PDF policy.
- Preserve provenance for every record: source, query, DOI, title, year, journal, screening status, PDF acquisition method, and evidence passage.
- Keep `candidate`, `pending_pdf`, `pending_fulltext`, `included`, `peripheral`, and `excluded` distinct. Only `included` means full-text verified.
- Retrieve full text only from lawful open-access, publisher, repository, or user-authorized institutional sources. Never bypass access controls.
- Never print or commit credentials, browser profiles, PDFs, databases, or machine-local runtime paths.
- Do not claim exhaustive, WOS, or SCI coverage until `audit-search` shows no unresolved truncation and the required source credentials were configured.
- Treat automated screening and summaries as triage. Preserve evidence and surface uncertain cases for human review.

## Run the workflow

Run commands from the skill directory. First check the environment:

```bash
python scripts/lit.py doctor --profile path/to/profile.yaml
```

Then use the stages needed by the request:

```bash
python scripts/lit.py init --name PROJECT --profile path/to/profile.yaml
python scripts/lit.py search --profile path/to/profile.yaml --sources openalex,crossref --depth balanced
python scripts/lit.py screen --profile path/to/profile.yaml --stage abstract
python scripts/lit.py fetch --profile path/to/profile.yaml --scope screened --require-pdf
python scripts/lit.py ingest --profile path/to/profile.yaml --verify-pdf
python scripts/lit.py screen --profile path/to/profile.yaml --stage fulltext --require-pdf
python scripts/lit.py summarize --profile path/to/profile.yaml --group-by direction
python scripts/lit.py export --profile path/to/profile.yaml --format markdown,csv,bibtex --include-pdf-manifest
python scripts/lit.py audit-pdfs --profile path/to/profile.yaml
python scripts/lit.py audit-search --profile path/to/profile.yaml
```

Use `--depth quick` for exploration, `balanced` for routine work, `deep` for broader recall, and `exhaustive` only when the user explicitly accepts the time, rate-limit, and review costs. An exhaustive local plan is not proof of database completeness.

If users already have lawful PDFs, prefer `import-pdfs` over downloading them again:

```bash
python scripts/lit.py import-pdfs --profile path/to/profile.yaml --pdf-dir path/to/downloads --verify-pdf
```

Use `ask --evidence` only over ingested full text, and cite the returned paper identity and passage rather than presenting generated synthesis as source text.

## Choose the relevant guidance

- Read `references/configuration.md` when creating or changing a profile or runtime configuration.
- Read `references/source_strategy.md` before changing source priority, query shapes, or coverage claims.
- Read `references/pdf_policy.md` before fetching, importing, or auditing PDFs.
- Read `references/screening_schema.md` before changing statuses, evidence rules, or screening modes.
- Read `references/institutional_access.md` only when using WebVPN, EZproxy, MFA, or a persistent browser profile.

## Delivery

For a finished project, report:

- the exact search window, sources, queries, depth, caps, and unresolved coverage gaps;
- counts by screening status, with `included` explicitly labeled as final PDF/full-text verified;
- `papers.csv`, coverage audit files, PDF manifests, missing-PDF queue, BibTeX, and the evidence-grounded report;
- limitations caused by missing credentials, inaccessible full text, weak metadata, automated screening, or source truncation.

Do not use pending or peripheral records to silently fill a final evidence set.

