"""TikTok Shop — trending-product research, content campaigns, dormant selling.

Three capabilities, each gated and compliance-aware:

* :mod:`backend.tiktok.product_research` — find trending products to sell, via a
  **pluggable provider**. IMPORTANT: TikTok's official *Research API* forbids
  commercial use (using it to drive selling / a paid tool risks permanent
  revocation), so we never wire it for sourcing decisions. Instead the provider
  is operator-chosen (a commercial-cleared third party, or the seller-side
  catalog once you're a seller). Default provider is ``none`` (returns nothing).

* :mod:`backend.tiktok.campaigns` — turn a sourced product into a 9:16 video by
  **reusing the reel engine** (:mod:`backend.social.video`) and post it via the
  TikTok **Content Posting API**. DRY-RUN by default; dormant without a token.

* :mod:`backend.tiktok.shop_seller` — the TikTok **Shop Seller API** (list/create
  listings, pull orders). **DORMANT**: requires an operator-approved TikTok Shop
  seller account + app review before it can transact. Orders attribute to the
  ``tiktok_shop`` revenue stream.

Nothing here posts or sells live unless an operator explicitly arms the relevant
flag AND seeds approved credentials. Every path fails closed.
"""
