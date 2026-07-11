# Docker Workcell Strategy — two-image hardening
Source: ChatGPT recovery chat 14

**Canonical relationship:** [EXPANDS §6 infrastructure] container runtime posture; [NEW] per-workcell isolation pattern

## Two-image strategy
1. **`samus-base`** — shared runtime, security posture, common Python deps, bootstrap tooling
2. **Per-workcell images** — inherit from base, add only cell-specific code + requirements

Benefits: fast rebuilds, low drift, centralized patching, single hardening point.

## Layout
```
samus/
├── docker/
│   ├── base/{Dockerfile, entrypoint.sh, constraints.txt}
│   ├── workcells/
│   │   ├── leadgen/{Dockerfile, requirements.txt}
│   │   ├── scaffold/{Dockerfile, requirements.txt}
│   │   ├── fulfillment/{Dockerfile, requirements.txt}
│   │   └── gateway/{Dockerfile, requirements.txt}
│   └── compose/compose.yml
├── backend/{common, leadgen, scaffold, fulfillment, gateway, shared}
└── pyproject.toml
```

## Base image pattern (key elements)
- `python:3.13-slim-bookworm` multi-stage builder + runtime
- Non-root user `samus` (uid 10001)
- `tini` as PID 1
- chown `/opt/samus` to non-root
- `chmod 0555 /entrypoint.sh`, `umask 027`

## Per-workcell hardening rules
- **Gateway only on edge network**, all workers on `samus-internal` network with `internal: true`
- `no-new-privileges: true`, `cap_drop: [ALL]`, `read_only: true`
- `tmpfs` for `/tmp` (`size=64m,noexec,nosuid,nodev`) and `/opt/samus/run`
- `pids_limit`, `mem_limit`, `cpus` ceilings per cell
- `HEALTHCHECK` required on every image
- One responsibility per workcell, single base image pinned

## Compose ports
- Gateway: 8100 (only published)
- Leadgen: 8200 internal
- Scaffold: 8201 internal
- Fulfillment: 8202 internal

## Frontier-grade roadmap
1. Distroless runtime (post-stabilization)
2. Internal request signing (HMAC: `X-Samus-Timestamp`, `X-Samus-Nonce`, `X-Samus-Signature`)
3. Image identity labels (OCI annotations)
4. Egress policy proxy
5. Build attestations (SBOM, image signing, vulnerability scan gate)

## What NOT to do
- Do NOT make one giant SAMUS image
- Do NOT let every workcell talk directly to Neo4j — put a memory gateway in front
- Do NOT expose Docker daemon remotely
