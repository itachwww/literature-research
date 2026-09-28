# Source Strategy

Use WOS as the authority for SCI/Core Collection coverage when `WOS_API_KEY` is present. Use OpenAlex and Crossref to improve recall, fill abstracts/DOIs, find open-access metadata, and discover recent papers that may not have high citations yet.

Default priority:

1. WOS search for SCI/Web of Science provenance.
2. OpenAlex search for abstracts, citation counts, OA URLs, and related works.
3. Crossref search for DOI and publisher metadata cleanup.
4. DOI lookup to merge duplicate records.
5. Unpaywall for OA PDF locations.

Do not claim WOS/SCI completeness if WOS is not configured. In that case, state that the run used OpenAlex/Crossref coverage and should be repeated with WOS credentials for SCI verification.

Keep API queries broad first, then screen locally. For new papers, do not filter only by citation count. Respect source terms, pagination, rate limits, and retry headers; do not treat a locally uncapped search as proof that an upstream source returned everything.

Before calling a search exhaustive, inspect every query/source/year partition in `query_coverage_report.csv`, then run `audit-search`. Report any truncated page, API error, missing credential, unsupported query syntax, or global cap as a coverage limitation.
