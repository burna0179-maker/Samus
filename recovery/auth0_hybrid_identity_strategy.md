# Auth0 Hybrid Identity Strategy
Source: ChatGPT recovery chat 19

**Canonical relationship:**
- [EXPANDS §10 inter_agent.identity] external identity plane
- [EXPANDS §6 security] edge auth boundary
- [NEW] hybrid pattern: Auth0 edge + mTLS internal workload identity

## Core decision rule
- **Human / browser / mobile / customer / 3rd-party integration** → Auth0
- **Agent-to-agent inside our estate (especially critical/local-only)** → internal workload identity

## Why split
- Auth0 has rate limits on M2M auth calls — dense agent meshes can hit them
- Central token minting becomes a shared-dependency failure amplifier
- East-west traffic needs offline-survivable trust (local-first principle)

## Layered architecture
```
Layer 1 — IDENTITY CONTROL PLANE (Auth0)
  - Operator login (HUD/admin)
  - External API gateway auth
  - Partner/customer access
  - Bootstrap identity for higher-trust agents crossing major trust boundaries

Layer 2 — RUNTIME TRUST PLANE (internal)
  - mTLS or signed service identity (Ed25519 thumbprints per §10)
  - Short-lived internal credentials DERIVED from Auth0 trust event
  - Local policy enforcement per agent/pod
  - Cached authorization decisions
  - Circuit breakers
  - Break-glass degraded mode (works when Auth0 unreachable)
```

## Implementation rules
1. **Token cadence**: never request a fresh Auth0 token per internal call. Cache short-lived + refresh with jitter ahead of expiry.
2. **Audience separation**: distinct APIs/audiences for operator control / automation control / customer-facing. No roaming admin tokens.
3. **Scope minimization**: each agent gets `task.submit`, `memory.read_limited`, `ledger.append` — NOT broad "admin".
4. **Claims enrichment**: use Auth0 Actions (Hooks deprecated).
5. **IaC**: Terraform provider for tenants/apps/APIs/Actions — reproducible setup.
6. **Observability**: stream Auth0 logs into central telemetry → part of forensic chain.

## Practical decision tree
```
Request originates from:
  ├── Human/browser/mobile/customer/3rd-party → Auth0 (strong choice)
  └── Pod-to-pod inside own estate
        ├── Critical / local-only path → internal workload identity FIRST
        └── Non-critical → Auth0 acceptable as upstream authority
```

## Bottom line
- YES: implement Auth0 as the hardened external identity plane
- NO: do NOT make Auth0 the sole trust mechanism for the agent mesh

**For HustleForge-style architecture:** Auth0 = identity control plane; mTLS + signed workload identity + local policy = runtime trust plane.

## SPA setup notes (chat 21 debugging)
- `@auth0/auth0-spa-js` v2.1+
- `cacheLocation: 'memory'` (NOT localStorage — security)
- Allowed Callback URLs MUST match served path exactly (incl. filename)
- VM browser → 127.0.0.1 points to VM, NOT host → use host IP or run on host
- Page caching can mask config changes — rename file to bust cache
