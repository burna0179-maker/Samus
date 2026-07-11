# syntax=docker/dockerfile:1.7
# Samus shared base image. Each workcell image extends this one.
# Layout assumes build context is the Hustleforge repo root (parent of Samus/).
FROM python:3.13-slim-bookworm

# tini for PID 1 + minimal runtime deps. ca-certificates and curl are kept
# small and serve healthchecks + outbound TLS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

ARG SAMUS_UID=10001
RUN groupadd --system --gid ${SAMUS_UID} samus && \
    useradd --system --uid ${SAMUS_UID} --gid samus --no-create-home --shell /usr/sbin/nologin samus

WORKDIR /opt/samus
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Copy constraint + requirement files. All three are now required (the lock is
# COPY'd unconditionally — the old optional `requirements.loc[k]` wildcard let a
# missing lock silently disable pinning; CWE-1357).
COPY Samus/docker/base/constraints.txt /tmp/constraints.txt
COPY Samus/requirements.txt /tmp/requirements.txt
COPY Samus/requirements.lock /tmp/requirements.lock

# Install pinned. constraints.txt caps every dependency to its major-version
# envelope so a rebuild can never float a package to an unvetted new release.
#
# The build resolves from requirements.txt under that constraint ceiling.
# requirements.lock is the fully-pinned target set and is layered on top with
# --no-deps so any package it DOES pin is forced to its exact version; the lock
# is intentionally applied after (not instead of) requirements.txt because the
# current lock is an incomplete snapshot — it omits a few runtime deps
# (beautifulsoup4, dnspython, jinja2, ngrok, tzdata, youtube-transcript-api)
# and would break the build if used as the sole install source.
#
# TODO: regenerate a COMPLETE hashed lock and switch to a single
#   `pip install --require-hashes -r requirements.lock`. Until then the lock is
#   COPY'd + enforced for the packages it covers; constraints.txt is the
#   guarantee for the rest. Re-generate with:
#     pip-compile --generate-hashes --output-file=Samus/requirements.lock Samus/requirements.txt
RUN pip install --upgrade pip && \
    pip install -c /tmp/constraints.txt -r /tmp/requirements.txt && \
    pip install --no-deps -c /tmp/constraints.txt -r /tmp/requirements.lock

USER samus
ENTRYPOINT ["/usr/bin/tini","--"]
