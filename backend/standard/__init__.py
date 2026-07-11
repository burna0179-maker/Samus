"""STANDARD tier root (Samus).

Vendor-neutral primitives shared across packs. Samus's CORE remains in
``backend/common/``; the workcell-specific code (crm, fulfillment, ...)
is intentionally NOT a STANDARD tier -- workcells own their own state
machines.
"""

__tier__ = "STANDARD"
