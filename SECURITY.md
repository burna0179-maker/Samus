# Security Policy

Full security architecture, trust boundaries, controls, and known
limitations are documented in [`docs/SECURITY.md`](docs/SECURITY.md).

## Reporting a vulnerability

This repository is a portfolio snapshot of the Samus subsystem, not an
active service accepting external traffic. If you nonetheless identify a
security concern in this code, please open a GitHub issue with the
`security` label; do not include exploit details in a public issue.

For questions about the security posture (per-service HMAC identity,
SSRF-safe fetch, immutable baseline verification, Stripe webhook atomic
idempotency, LLM budget chain), see [`docs/SECURITY.md`](docs/SECURITY.md)
and the review-facing summary at
[`review/ENGINEERING_SUMMARY.md`](review/ENGINEERING_SUMMARY.md).
