"""Digital-product fulfillment orchestrator.

Sister to ``backend.fulfill.fulfill_customer`` (the SEO chain). Same shape:

  1. Look up SKU in the registry; clear error if unknown.
  2. Find or create the customer (idempotent on email).
  3. Advance state to 'in_delivery' (skip if already past it).
  4. Render / copy the SKU's deliverable into
     ``<SAMUS_ARTIFACT_ROOT>/customers/<slug>/<sku>[.md|/]``.
  5. Email the customer with the artifact inlined (or attached for packs).
  6. Advance state to 'delivered'.

Failures halt the chain at the failing step — state is not rolled forward
so the operator can fix and re-run.

The function is callable-injectable for tests:
    - customer_store     : CustomerStore-like
    - send_email_fn      : callable(to=, subject=, body=, attachments=) -> dict
    - artifact_root_path : override SAMUS_ARTIFACT_ROOT for tests
"""
from __future__ import annotations

import logging
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .registry import (
    AddOnConfig,
    ProductConfig,
    UnknownSKUError,
    get_any,
    products_root,
)


_LOG = logging.getLogger("samus.products.fulfill")


DigitalStepName = Literal[
    "lookup_sku",
    "find_or_create_customer",
    "advance_to_in_delivery",
    "produce_artifact",
    "send_email",
    "advance_to_delivered",
]
StepStatus = Literal["ok", "skipped", "failed"]


class DigitalFulfillStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: DigitalStepName
    status: StepStatus
    detail: str = ""
    elapsed_ms: int = 0


class DigitalFulfillmentResult(BaseModel):
    """Audit trail of a fulfill_digital_product() invocation."""
    model_config = ConfigDict(extra="forbid")

    email: str
    sku_id: str
    customer_id: str | None = None
    prior_state: str | None = None
    final_state: str | None = None
    artifact_path: str | None = None
    email_message_id: str | None = None
    ok: bool
    steps: list[DigitalFulfillStep] = Field(default_factory=list)
    ts: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _resolve_artifact_root(override: Path | str | None) -> Path:
    if override is not None:
        return Path(override)
    import os
    return Path(os.getenv("SAMUS_ARTIFACT_ROOT", r"E:\Hustleforge\Samus\data\artifacts"))


def _email_body_inline(
    title: str,
    content: str,
    customer_name: str = "",
    download_note: str = "",
) -> str:
    """Cover note + inline markdown content (used for playbooks + add-ons)."""
    greeting = f"Hi {customer_name}," if customer_name else "Hi,"
    extra = f"\n{download_note}\n" if download_note else ""
    return (
        f"{greeting}\n\n"
        f"Your {title} is below. Save it, share it, run the steps.\n"
        f"{extra}\n"
        f"Reply to this email with any questions — every purchase comes\n"
        f"with one round of clarifying Q&A at no extra cost.\n\n"
        f"-- Hustleforge\n\n"
        f"{'=' * 72}\n\n"
        f"{content}\n"
    )


def _email_body_pack(title: str, file_list: list[str], customer_name: str = "") -> str:
    """Cover note for pack deliveries — attachment carries the bundle zip."""
    greeting = f"Hi {customer_name}," if customer_name else "Hi,"
    files = "\n".join(f"  - {f}" for f in file_list)
    return (
        f"{greeting}\n\n"
        f"Your {title} is attached as a zip. It contains:\n\n"
        f"{files}\n\n"
        f"Each template is fully written — no placeholders. Drop them into\n"
        f"your stack and tailor as needed.\n\n"
        f"Reply with any questions — every purchase comes with one round of\n"
        f"clarifying Q&A at no extra cost.\n\n"
        f"-- Hustleforge\n"
    )


# ---------------------------------------------------------------------------
# Artifact producers
# ---------------------------------------------------------------------------

def _produce_playbook(
    cfg: ProductConfig,
    customer_dir: Path,
) -> Path:
    """Copy the playbook markdown into the customer's artifact dir.

    Source: ``backend/products/<artifact_relpath>``.
    Target: ``<customer_dir>/<sku_id>.md``.
    """
    src = products_root() / cfg.artifact_relpath
    if not src.is_file():
        raise FileNotFoundError(
            f"playbook source missing for {cfg.sku_id!r}: {src}"
        )
    customer_dir.mkdir(parents=True, exist_ok=True)
    dest = customer_dir / f"{cfg.sku_id}.md"
    shutil.copyfile(src, dest)
    return dest


def _produce_pack(
    cfg: ProductConfig,
    customer_dir: Path,
) -> tuple[Path, Path, list[str]]:
    """Copy the pack template bundle + zip it for email attachment.

    Returns (pack_dir, zip_path, relative_file_list).
    """
    src = products_root() / cfg.artifact_relpath
    if not src.is_dir():
        raise FileNotFoundError(
            f"pack source missing for {cfg.sku_id!r}: {src}"
        )
    customer_dir.mkdir(parents=True, exist_ok=True)
    pack_dir = customer_dir / cfg.sku_id
    if pack_dir.exists():
        shutil.rmtree(pack_dir)
    shutil.copytree(src, pack_dir)

    file_list: list[str] = []
    for path in sorted(pack_dir.rglob("*")):
        if path.is_file():
            file_list.append(path.relative_to(pack_dir).as_posix())

    zip_path = customer_dir / f"{cfg.sku_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in file_list:
            zf.write(pack_dir / rel, arcname=f"{cfg.sku_id}/{rel}")

    return pack_dir, zip_path, file_list


def _produce_addon(
    cfg: AddOnConfig,
    customer_dir: Path,
) -> Path:
    """Render the add-on's deliverable body to a markdown file."""
    customer_dir.mkdir(parents=True, exist_ok=True)
    dest = customer_dir / f"{cfg.sku_id}.md"
    dest.write_text(cfg.deliverable_body, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def fulfill_digital_product(
    *,
    sku_id: str,
    email: str,
    name: str = "",
    company: str = "",
    send_email: bool = True,
    customer_store: Any = None,
    send_email_fn: Any = None,
    artifact_root: Path | str | None = None,
) -> DigitalFulfillmentResult:
    """Drive a digital-product fulfillment to delivered + email-sent.

    Mirrors ``backend.fulfill.fulfill_customer`` so the operator's mental
    model is consistent across SKU families. The three injection points are
    for tests; production callers omit them.
    """
    started = _now_iso()
    steps: list[DigitalFulfillStep] = []
    customer_id: str | None = None
    prior_state: str | None = None
    final_state: str | None = None
    artifact_path: str | None = None
    email_message_id: str | None = None

    def _step(
        name: DigitalStepName,
        status: StepStatus,
        detail: str,
        t0: float,
    ) -> DigitalFulfillStep:
        s = DigitalFulfillStep(
            name=name, status=status, detail=detail, elapsed_ms=_ms_since(t0),
        )
        steps.append(s)
        _LOG.info(
            "digital_fulfill_step",
            extra={
                "step": name, "status": status, "detail": detail,
                "email": email, "sku_id": sku_id,
            },
        )
        return s

    def _fail_result() -> DigitalFulfillmentResult:
        return DigitalFulfillmentResult(
            email=email, sku_id=sku_id, customer_id=customer_id,
            prior_state=prior_state, final_state=final_state,
            artifact_path=artifact_path, email_message_id=email_message_id,
            ok=False, steps=steps, ts=started,
        )

    # ---- 1. Look up the SKU ---------------------------------------------
    t0 = time.monotonic()
    try:
        cfg = get_any(sku_id)
        _step("lookup_sku", "ok",
              f"{cfg.kind if isinstance(cfg, ProductConfig) else 'addon'} "
              f"-> {cfg.display_name}", t0)
    except UnknownSKUError as exc:
        _step("lookup_sku", "failed", str(exc), t0)
        return _fail_result()

    # ---- 2. Resolve injectables (after lookup so a bad SKU short-circuits) -
    if customer_store is None:
        from backend.memory.customers import CustomerStore
        customer_store = CustomerStore()
    if send_email_fn is None:
        from functools import partial
        from backend.common.email_backend import send_email as _real_send
        # Digital product delivery is CAN-SPAM transactional/relationship mail —
        # exempt from unsubscribe/postal rules (suppression still applies).
        send_email_fn = partial(_real_send, message_kind="transactional")

    # ---- 3. Find or create customer -------------------------------------
    t0 = time.monotonic()
    try:
        existing = customer_store.get_by_email(email)
        if existing is not None:
            customer = existing
            _step("find_or_create_customer", "ok",
                  f"found existing {customer.id} (state={customer.current_state})",
                  t0)
        else:
            customer = customer_store.create_customer(
                email=email, name=name, company=company,
                source=f"digital:{sku_id}",
            )
            _step("find_or_create_customer", "ok",
                  f"created {customer.id} in state={customer.current_state}", t0)
        customer_id = customer.id
        prior_state = customer.current_state
        final_state = customer.current_state
    except Exception as exc:
        _step("find_or_create_customer", "failed", str(exc), t0)
        return _fail_result()

    # ---- 4. Advance to in_delivery --------------------------------------
    t0 = time.monotonic()
    if customer.current_state in ("delivered", "renewed", "churned"):
        _step("advance_to_in_delivery", "skipped",
              f"current_state={customer.current_state} already past in_delivery",
              t0)
    elif customer.current_state == "in_delivery":
        _step("advance_to_in_delivery", "skipped", "already in_delivery", t0)
    else:
        try:
            event = customer_store.advance_state(
                customer_id=customer.id, to_state="in_delivery",
                reason=f"digital fulfill started ({sku_id})",
            )
            final_state = event.to_state
            _step("advance_to_in_delivery", "ok",
                  f"{event.from_state} -> in_delivery", t0)
        except Exception as exc:
            _step("advance_to_in_delivery", "failed", str(exc), t0)
            return _fail_result()

    # ---- 5. Produce the artifact ----------------------------------------
    t0 = time.monotonic()
    root = _resolve_artifact_root(artifact_root)
    customer_dir = root / "customers" / customer.id
    body_for_email: str
    attachments: list[dict[str, Any]] | None = None
    download_note = ""

    try:
        if isinstance(cfg, AddOnConfig):
            dest = _produce_addon(cfg, customer_dir)
            artifact_path = str(dest)
            body_for_email = _email_body_inline(
                title=cfg.display_name,
                content=cfg.deliverable_body,
                customer_name=customer.name,
            )
            _step("produce_artifact", "ok",
                  f"addon brief -> {dest}", t0)

        elif cfg.kind == "playbook":
            dest = _produce_playbook(cfg, customer_dir)
            artifact_path = str(dest)
            content = dest.read_text(encoding="utf-8")
            body_for_email = _email_body_inline(
                title=cfg.display_name,
                content=content,
                customer_name=customer.name,
            )
            _step("produce_artifact", "ok",
                  f"playbook -> {dest}", t0)

        elif cfg.kind == "pack":
            pack_dir, zip_path, file_list = _produce_pack(cfg, customer_dir)
            artifact_path = str(zip_path)
            body_for_email = _email_body_pack(
                title=cfg.display_name,
                file_list=file_list,
                customer_name=customer.name,
            )
            attachments = [{
                "filename": zip_path.name,
                "content": zip_path.read_bytes(),
                "mime_type": "application/zip",
            }]
            _step("produce_artifact", "ok",
                  f"pack -> {pack_dir} (zip {zip_path.name}, "
                  f"{len(file_list)} files)", t0)
        else:
            raise ValueError(f"unknown product kind: {cfg.kind!r}")
    except Exception as exc:
        _step("produce_artifact", "failed", str(exc), t0)
        return _fail_result()

    # ---- 6. Email the customer ------------------------------------------
    if send_email:
        t0 = time.monotonic()
        try:
            send_kwargs: dict[str, Any] = {
                "to": customer.email,
                "subject": cfg.email_subject,
                "body": body_for_email,
            }
            if attachments is not None:
                send_kwargs["attachments"] = attachments
            send_result = send_email_fn(**send_kwargs)
            email_message_id = send_result.get("message_id")
            channel = send_result.get("channel", "?")
            _step("send_email", "ok",
                  f"{channel} message_id={email_message_id}", t0)
        except Exception as exc:
            _step("send_email", "failed", str(exc), t0)
            return _fail_result()
    else:
        _step("send_email", "skipped", "send_email=False", 0.0)

    # ---- 7. Advance to delivered ----------------------------------------
    t0 = time.monotonic()
    try:
        reason = f"digital fulfill delivered {sku_id}"
        if send_email:
            reason += f" (email message_id={email_message_id})"
        event = customer_store.advance_state(
            customer_id=customer.id, to_state="delivered", reason=reason,
        )
        final_state = event.to_state
        _step("advance_to_delivered", "ok",
              f"{event.from_state} -> delivered", t0)
    except Exception as exc:
        _step("advance_to_delivered", "failed", str(exc), t0)
        return _fail_result()

    # ---- 8. Best-effort upsell enqueue (parity with backend.fulfill) ----
    # Pattern matches the SEO chain — a queue-write failure must not unwind
    # a successful delivery.
    try:
        from datetime import datetime, timezone
        from backend.finance.upsell_queue import enqueue_upsell
        enqueue_upsell(
            customer_id=customer.id,
            customer_email=email,
            source_offer_code=sku_id,
            delivered_at=datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("upsell enqueue failed for %s/%s: %s", email, sku_id, exc)

    return DigitalFulfillmentResult(
        email=email, sku_id=sku_id, customer_id=customer_id,
        prior_state=prior_state, final_state=final_state,
        artifact_path=artifact_path, email_message_id=email_message_id,
        ok=True, steps=steps, ts=started,
    )


__all__ = [
    "DigitalFulfillStep",
    "DigitalFulfillmentResult",
    "fulfill_digital_product",
]
