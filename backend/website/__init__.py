"""Website-build fulfillment capability — Samus delivers a customer's site.

This workcell is the *delivery* arm the Cash Engine never had: once a deal is
won, the cash engine's forward walk (audit -> proposal -> contact -> outreach)
ends at ``dormant`` with nothing built. This package takes a won deal (or a
standalone order) and walks it through the staged production of a real website
on Wix — brief -> provision -> business_info -> content -> seo -> qa ->
publish -> deliver -> settle.

Two design invariants make it safe to ship dormant *and* let it run
autonomously later WITHOUT a code change:

  1. **Double gate per stage.** Every stage re-validates through the Codex
     (:func:`backend.common.codex.validator.check_action`, fail-closed) AND an
     operator-approval gate. In *supervised* mode (the default) a stage runs
     only after the operator approves it — this is the "walk-through": show ->
     approve -> run -> record. In *autonomous* mode (a flag the operator flips
     once the sequence is proven) approval is auto-granted and the same
     handlers run unattended. Supervised-now and autonomous-later are the
     **same code path**.

  2. **Fail-closed dormancy.** The whole engine is gated by
     ``website_builder_enabled``; the outward, irreversible steps (publish,
     deliver) additionally require ``website_live_publish_enabled``; and any
     real Wix call requires ``wix_api_key`` + ``wix_account_id`` to be seeded.
     With none of those set the capability is fully present but fires nothing —
     stages that need an unseeded credential or an unchosen template *park*
     honestly (the established cash_engine idiom), they do not fake a result.

See :mod:`backend.website.wix_client` for the transport, :mod:`.state` for the
durable state machine, :mod:`.stages` for the leaf handlers, and
:mod:`.service` for the orchestrator (the supervised + autonomous entry).
"""
