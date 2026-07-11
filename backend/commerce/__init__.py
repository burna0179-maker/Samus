"""Internal commerce — Samus <-> Medusa, the `products` income stream.

`Medusa <https://github.com/medusajs/medusa>`_ (MIT, headless commerce on
Node+Postgres+Redis) is the internal store for selling products (e.g. sourced
TikTok-trending items). Samus does **not** embed Medusa — Medusa is an
operator-run service and Samus talks to its **Admin REST API** over HTTP. This
keeps Samus pure-Python and the commerce engine cleanly separated.

Income separation: Medusa is configured with its **own Stripe account**, which is
the ``products`` entry in :mod:`backend.finance.revenue_streams`. Orders pulled
back for the books are attributed to that stream/entity.

Everything here is **dormant + keyless-safe**: with ``commerce_medusa_enabled``
off (default) or no ``medusa_base_url`` / ``medusa_admin_token``, the client is a
no-op that returns structured empty results and never raises.
"""
