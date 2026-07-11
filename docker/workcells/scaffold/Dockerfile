# syntax=docker/dockerfile:1.7
# Samus scaffold workcell. Cloud Run service name: samus-scaffold-2026.
ARG BASE_IMAGE=us-west1-docker.pkg.dev/${GCP_PROJECT_ID}/samus/samus-base:latest
FROM ${BASE_IMAGE}

ARG SAMUS_SERVICE=scaffold
ARG APP_MODULE=backend.scaffold.app:app
ENV SAMUS_SERVICE=${SAMUS_SERVICE}

WORKDIR /opt/samus
USER root
COPY Samus/backend /opt/samus/backend
COPY Samus/axioms /opt/samus/axioms
COPY Samus/docs /opt/samus/docs
COPY Samus/docker/base/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && chown -R samus:samus /opt/samus
USER samus

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "backend.scaffold.app:app", "--host", "0.0.0.0", "--port", "8080"]
