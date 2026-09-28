# Institutional access

Use institutional retrieval only when the user is authorized and the institution permits automated or browser-assisted access. Respect publisher terms and conservative request rates.

## EZproxy

Configure `PROXY_URL` and `PROXY_TYPE` in the environment. Do not place proxy credentials or private URLs in a committed profile.

## WebVPN and MFA

The browser helper opens a visible Chromium-based browser, allows the user to complete MFA, and reuses the authorized session for pending PDFs:

```bash
python scripts/webvpn_browser_fetch.py --profile path/to/profile.yaml --env-file path/to/.env --limit 40
```

Install browser dependencies from `requirements-browser.txt` and run `python -m playwright install chromium`. A system Chrome/Edge binary is used when found; otherwise the Playwright-managed Chromium installation is used.

Use a dedicated persistent profile via `webvpn_user_data_dir`. It contains session cookies and must remain outside version control. The helper stops after a configurable number of consecutive failures rather than retrying indefinitely.

The non-browser WebVPN adapter can discover a CAS base URL from a redirect. If that is not possible, set `WEBVPN_CAS_URL` explicitly; no institution-specific identity-provider URL is assumed.

TLS verification is enabled by default. If a known institutional interception proxy requires disabling it, set `LIT_WEBVPN_VERIFY_TLS=false` only for that run and document the risk locally. Never make disabled verification a published default.

If retrieval fails, leave the record in `pending_pdf` or `missing_pdf_candidates.csv`. Do not add shadow-library or access-control-bypass methods.

