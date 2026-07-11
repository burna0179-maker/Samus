"""Finance workcell — Stripe balance/charges ingest + CODB registry + runway.

Read-only at Phase 1: pulls live data from the Stripe REST API using a
restricted-read key and combines it with a YAML-seeded Cost of Doing Business
(CODB) registry so Samus can answer:

  - "what's our current cash position?"           -> Stripe /v1/balance
  - "what's been earned in the last N days?"      -> Stripe /v1/charges
  - "what does it cost to keep the system up?"    -> codb_registry.yaml
  - "how many days of runway do we have?"         -> derived from both

All HTTP calls go through ``stripe_client.StripeClient`` which uses httpx
(already a project dep). No Stripe SDK is imported -- restricted-read scope
covers everything Phase 1 needs and the REST surface is small.

Capability slots reserved in :mod:`backend.common.capabilities`:
``snapshot``, ``codb_summary``, ``runway``.
"""
