# Security policy

Report vulnerabilities privately to the repository maintainers rather than opening a public issue with credentials, cookies, institutional URLs, or downloaded papers.

The project intentionally keeps secrets outside committed files. Use environment variables or a user-owned `.env` file, restrict its filesystem permissions, and rotate any credential that was exposed in logs or commits.

TLS certificate verification is enabled by default. `LIT_WEBVPN_VERIFY_TLS=false` is an explicit compatibility escape hatch for a known institutional proxy and weakens transport security.

Persistent browser profiles can contain authenticated session cookies. Store them outside the repository, do not share them, and delete them when the session is no longer needed.

