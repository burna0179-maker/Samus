# syntax=docker/dockerfile:1.7
# Samus gateway workcell. Cloud Run service name: samus-2026.
# Build context: Hustleforge repo root.
ARG BASE_IMAGE=us-west1-docker.pkg.dev/${GCP_PROJECT_ID}/samus/samus-base:latest
FROM ${BASE_IMAGE}

ARG SAMUS_SERVICE=gateway
ARG APP_MODULE=backend.gateway.app:app
ENV SAMUS_SERVICE=${SAMUS_SERVICE}

WORKDIR /opt/samus
USER root
# operator_console pack imports fastapi Jinja2Templates; the samus-base image
# predates requirements.txt's jinja2>=3.1.4, so install it explicitly here.
# TODO: rebuild samus-base from current requirements.txt + drop this line.
RUN python -m pip install --no-cache-dir "jinja2>=3.1.4"
COPY Samus/backend /opt/samus/backend
COPY Samus/axioms /opt/samus/axioms
COPY Samus/docs /opt/samus/docs
# Shared cross-agent security_client (HMAC AgentEnvelope sign/verify). The
# inter-agent relief forwarder, quorum-vote verifier, and agora-contribute
# route lazy-import `security_client`, walking up from backend/ for a
# `_shared/security_client/agent_envelope.py`. Place it at /opt/samus/_shared
# (parents[2] of backend/common/*) so that shim resolves it; without this the
# cross-agent egress fail-closes with ModuleNotFoundError.
COPY _shared/security_client /opt/samus/_shared/security_client
# Shared operator-Ed25519 envelope primitive. backend/identity/boot.py runs
# the charter + immutable-baseline signature checks at gateway boot and
# imports `_shared.security.operator_signed_envelope` via shared_bootstrap.
# Without this COPY the verifier fails-closed (tamper=True) on a present
# signature because it cannot import the verify primitive. 2026-06-22.
COPY _shared/security /opt/samus/_shared/security
COPY Samus/docker/base/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && chown -R samus:samus /opt/samus
USER samus

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "backend.gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
