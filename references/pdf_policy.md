# PDF Policy

The final included set must be PDF-grounded. A paper can be in the final included list only when:

- a local PDF path exists,
- the PDF starts with `%PDF` or PyMuPDF can open it,
- page count is greater than zero,
- extracted text is available, and
- the full-text screen confirms the requested method is primary.

Runtime defaults:

- Machine-local runtime settings may live in the ignored `assets/local_runtime.yaml` or in the file named by `LIT_RESEARCH_RUNTIME_CONFIG`.
- Secrets remain outside committed skill files and are loaded from the environment or a user-owned `.env` file.
- New topic profiles inherit missing runtime fields such as `data_root`, `pdf_methods`, `institutional_access_mode`, `webvpn_user_data_dir`, timeout values, and the consecutive-failure stop rule.

Acquisition order:

1. Already existing local PDF.
2. Direct PDF URL from metadata.
3. Unpaywall OA locations.
4. Publisher DOI landing page and extracted PDF link.
5. EZproxy using `PROXY_URL` and `PROXY_TYPE`.
6. Wengine-style WebVPN using `WEBVPN_URL`, `WEBVPN_USERNAME`, and `WEBVPN_PASSWORD` when available.
7. MFA-assisted Chrome/WebVPN helper using the dedicated `webvpn_user_data_dir` browser profile when publisher access requires a logged-in institutional session.

For WebVPN access, read `references/institutional_access.md` and use `scripts/webvpn_browser_fetch.py` only with user-authorized access. It reuses a dedicated browser profile, lets the user complete MFA, and stops after `webvpn_max_consecutive_failures` consecutive missing PDFs so the strategy can be adjusted.

Reject supplementary information, mismatched DOI/title files, non-PDF responses, zero-page files, NUL-corrupted extraction, and substantively empty extracted text. Never use illegal sources. If a PDF cannot be fetched through open access or configured institutional access, keep the paper in `missing_pdf_candidates.csv` and do not count it as final included.
