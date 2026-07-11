# Security Model

## Purpose

Document Samus trust boundaries, implemented controls, known limitations, and public-release requirements.

## Scope

Application, container, integration, operator, and repository-exposure security.

## Intended Audience

Security reviewers, platform engineers, maintainers, and interviewers.

## Source of Truth

Security claims must be verified against current middleware, capability registries, container configuration, tests, and deployment settings.

## Security Posture Statement

Samus contains layered security controls, but this repository should not be described as formally verified, fully zero-trust, or production hardened without environment-specific validation.

## Trust Boundaries

| Boundary | Primary risks |
|---|---|
| Public intake and webhooks | forgery, replay, abuse, injection, denial of service |
| Gateway to workcells | caller spoofing, privilege escalation, replay |
| SQS messages | poison messages, redelivery, unauthorized producer |
| External fetches | SSRF, redirect abuse, DNS rebinding |
| Provider APIs | credential leakage, rate limits, inconsistent side effects |
| Operator workstation | local credential theft, accidental exposure |
| Containers | privilege escalation, writable filesystem, secret leakage |
| Persistence | unauthorized reads, partial writes, stale fallback state |
| Generated artifacts | PII, recordings, transcripts, proprietary data |
| Repository | secrets, private topology, personal identifiers |

## Implemented Controls

### Service authentication

The shared middleware supports HMAC-signed requests, timestamps, nonces, and signed caller identity. Per-service keys are preferred; a shared-key fallback exists for compatibility.

### Authorization

Caller-to-callee grants and capability checks provide explicit authorization surfaces. Deployment must enable enforcement rather than assume the presence of code means enforcement is active.

### Public webhooks

Provider-specific signature validation exists for Vapi, Stripe, and SNS-oriented paths. Production deployments should fail closed when a required signing secret is absent.

### Intake abuse controls

Public intake includes validation, deduplication, rate limiting, trusted-proxy handling, and optional CAPTCHA. DynamoDB-backed counters provide cross-instance enforcement where configured.

### SSRF protection

Shared safe-fetch logic constrains protocols, rejects private and reserved destinations, and revalidates redirect hops.

### Idempotency

Provider event IDs, task envelopes, and storage claims are used to prevent duplicate side effects. Claiming must occur before external side effects.

### Container hardening

Container definitions include non-root execution and selected restrictions such as dropped capabilities, read-only filesystems, and process limits. Verify the effective Compose configuration.

### Secret handling

Runtime secrets are intended to come from external stores or process environment injection. Tracked example files should contain placeholders only.

### Immutable baseline

Protected files can be hashed and signature-checked at boot. This detects unauthorized drift but does not replace code review, dependency controls, or host security.

### Auditability

Correlation IDs, structured logs, task state, JSONL ledgers, and business-event records provide forensic context.

## Known Security Limitations

- Shared-key fallback weakens service identity separation.
- In-process nonce stores and rate limiters do not coordinate across instances.
- Fail-open persistence paths can preserve availability while weakening enforcement.
- Local JSONL data can expose sensitive content if host permissions are weak.
- Cloud and local environments have different ingress and identity properties.
- Customer recordings, transcripts, and contact data require explicit retention and access policy.
- Recovery artifacts may contain obsolete security assumptions.
- A large uncommitted working tree complicates review and provenance.
- Dependency locks reduce drift but do not replace vulnerability scanning or provenance attestations.

## Public Repository Exposure Assessment

Before making the repository public, search for and remediate:

- personal email addresses and phone numbers;
- private IP addresses;
- absolute workstation paths;
- cloud project IDs and account identifiers;
- service-account names;
- webhook URLs;
- customer names and records;
- recordings and transcripts;
- API keys, OAuth tokens, secret values, and signed cookies;
- internal pricing and finance records;
- generated `.env` files;
- local database dumps;
- private certificates or signing keys.

Recommended commands:

```bash
git grep -nE '(AKIA|AIza|sk_live_|sk_test_|xox[baprs]-|ghp_|-----BEGIN .*PRIVATE KEY-----)'
git grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}'
git grep -nE '[A-Za-z]:\\'
git log -p --all -- .env .env.local
```

Automated scanning should include a history-aware secret scanner, not only the current tree.

## Disclosure Boundaries

A public portfolio should retain:

- architecture;
- sanitized configuration examples;
- test evidence;
- generic deployment procedures;
- security design and limitations.

It should exclude or sanitize:

- operational endpoints;
- account-specific identifiers;
- customer artifacts;
- real credentials;
- private network topology;
- personal contact details not intentionally used for recruiting.

## Security Review Checklist

- [ ] Authz mode is explicitly configured
- [ ] No production webhook can disable signature verification
- [ ] Every external side effect has an idempotency strategy
- [ ] Safe fetch is used for user-controlled URLs
- [ ] Containers run non-root with minimum privileges
- [ ] Secrets are absent from image layers and logs
- [ ] PII retention and deletion policies are documented
- [ ] Audit storage has access and rotation controls
- [ ] Dependency and image scanning run in CI
- [ ] Public repository history has been scanned
