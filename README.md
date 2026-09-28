# literature-research

An open-source Codex skill and Python CLI for building auditable, PDF-grounded literature libraries. It combines multi-source discovery, staged screening, lawful full-text retrieval, local SQLite storage, evidence extraction, coverage audits, and Markdown/CSV/BibTeX exports.

The tool assists review work; it does not replace expert screening and does not guarantee exhaustive database coverage.

## Features

- Web of Science, OpenAlex, and Crossref metadata search
- configurable quick, balanced, deep, and exhaustive search plans
- DOI-based deduplication and provenance tracking
- staged metadata, abstract, and full-text screening
- legal OA discovery through Unpaywall, publisher links, Europe PMC/PMC, and HAL
- optional user-authorized EZproxy or WebVPN/MFA workflows
- PDF integrity, page-count, extraction, and coverage audits
- Markdown, CSV, JSON, BibTeX, and local evidence-question exports
- domain-neutral profile-driven screening, with explicit legacy specialist modes

## Install as a Codex skill

Clone or copy the repository as `literature-research` inside the Codex skills directory, then enter it:

```bash
cd ~/.codex/skills/literature-research
python -m venv .venv
```

Activate the virtual environment, then install the base dependencies:

```bash
python -m pip install -r requirements.txt
```

For browser-assisted institutional access:

```bash
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
```

On Windows, the skill path is normally `%USERPROFILE%\.codex\skills\literature-research`.

## Quick start

1. Copy `assets/research_profile.yaml` to a project-controlled location and edit it.
2. Optionally copy `.env.example` to `.env` and add only credentials you are authorized to use.
3. Check capabilities without exposing credential values:

```bash
python scripts/lit.py doctor --profile path/to/profile.yaml --env-file path/to/.env
```

4. Run a small, inspectable workflow first:

```bash
python scripts/lit.py init --name my-project --profile path/to/profile.yaml
python scripts/lit.py search --profile path/to/profile.yaml --sources openalex,crossref --depth quick
python scripts/lit.py screen --profile path/to/profile.yaml --stage abstract
python scripts/lit.py fetch --profile path/to/profile.yaml --scope screened --require-pdf
python scripts/lit.py ingest --profile path/to/profile.yaml --verify-pdf
python scripts/lit.py screen --profile path/to/profile.yaml --stage fulltext --require-pdf
python scripts/lit.py audit-search --profile path/to/profile.yaml
python scripts/lit.py audit-pdfs --profile path/to/profile.yaml
python scripts/lit.py export --profile path/to/profile.yaml --format markdown,csv,bibtex --include-pdf-manifest
```

Generated projects default to `~/.literature-research/projects`. Override this with `data_root`, `LIT_RESEARCH_DATA_DIR`, or a machine-local runtime config.

## Configuration

See [`references/configuration.md`](references/configuration.md) for profile fields and precedence. Machine-specific settings belong in an ignored `assets/local_runtime.yaml` copied from `assets/local_runtime.example.yaml`, or in a file referenced by `LIT_RESEARCH_RUNTIME_CONFIG`.

Secrets belong in environment variables or a user-owned `.env` file. They must never be added to profiles, runtime examples, logs, issues, or commits.

## Evidence and coverage semantics

- `included` means a valid local PDF was parsed and the full-text screen passed.
- `pending_pdf` and `pending_fulltext` are not final evidence.
- `peripheral` means relevant but outside the requested method or article-type boundary.
- WOS/SCI completeness must not be claimed without WOS credentials and a clean coverage audit.
- Automated keyword screening is triage; review borderline records manually.

## Institutional access

Institutional retrieval is optional and must use access the user is entitled to. TLS verification is enabled by default. Only set `LIT_WEBVPN_VERIFY_TLS=false` for a known institutional proxy after understanding the interception risk. See [`references/institutional_access.md`](references/institutional_access.md).

## Development

```bash
python -m unittest discover -s tests -v
python scripts/lit.py --help
python scripts/lit.py doctor
```

Before publishing, verify that `git status --ignored` shows local runtime files, databases, PDFs, browser profiles, caches, and `.env` files as ignored.

## License

MIT. Third-party APIs, publisher sites, metadata, and downloaded full text remain subject to their own terms and licenses.

