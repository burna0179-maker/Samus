"""PACKS tier root (Samus).

Per-pack subdirectory. Note: Samus's workcells (crm, fulfillment, ...) are
NOT packs -- they are independent FastAPI services with their own
state machines. Packs here are presentation/UI overlays an operator can
mount into ANY workcell app (or a dedicated console service).
"""
__tier__ = "PACKS"
