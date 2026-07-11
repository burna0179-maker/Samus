"""Per-client brand-asset store + cross-client isolation gate.

Operator mandate (verbatim intent): "creating per client files that associate
logos, icons and media would be a safe way to force separation, that way no
assets or marketing material that is associated with a company logo is
re-used". Brand assets (logos, icons, images, media, marketing material) are
OWNED by exactly one client, and a generated site can only ship/reference
assets owned by ITS client — cross-client reuse is impossible BY CONSTRUCTION
(the gate strips it), not by convention.

Mechanics:

  * Each client gets a directory ``<SAMUS_ARTIFACT_ROOT>/client_assets/
    <client_key>/`` holding the asset files plus a ``manifest.json`` recording
    {filename, kind, sha256, added_utc, source, original_name, notes} per
    asset. ``derive_client_key`` is the ONE deterministic identity function
    (prospect_id > account_id > slugified company name) so every surface —
    store, demo builder, CLI — resolves the same owner for the same client.
  * ``register_asset`` COPIES the file into the client dir (the store owns
    its bytes — the original can move/vanish) and appends to the manifest
    atomically (tmp + replace) so a crash never half-writes ownership records.
  * ``enforce_asset_isolation`` is the fail-closed gate run on every generated
    site before deploy: a local asset must be hash-present in THIS client's
    manifest or it is dropped + its references stripped; anything matching an
    asset hash registered to a DIFFERENT client is stripped (the core
    cross-reuse guard, including embedded base64 data-URIs); remote references
    outside a small infrastructure allowlist (+ the client's own domain) are
    warnings — or stripped too under ``website_asset_isolation_strict``.

Pure stdlib (hashlib/json/shutil/re); never raises on a missing store — an
empty manifest simply means NO local assets are allowed (fail-closed).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

_LOG = logging.getLogger("samus.website.client_assets")

ASSET_KINDS = ("logo", "icon", "image", "media", "marketing")

# Remote hosts every generated site legitimately uses (fonts + Tailwind CDN).
REMOTE_ALLOWLIST = frozenset({
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "cdn.tailwindcss.com",
})

# File extensions treated as ASSETS (ownership-gated). Pages/styles/scripts
# the builder itself generates (html/css/js) are NOT assets.
_ASSET_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
    ".bmp", ".tiff", ".mp4", ".webm", ".mov", ".mp3", ".wav", ".ogg",
    ".pdf", ".woff", ".woff2", ".ttf", ".otf", ".eot",
})

_KEY_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_FNAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")

# Reference extractor: src/href/poster attributes + CSS url(...) + srcset
# first-URL. Generated sites are our own deterministic HTML, so a regex scan
# is sound here (no adversarial-parser concerns — and the gate FAILS CLOSED
# on anything it does match).
_REF_RE = re.compile(
    r"""(?:\b(?:src|href|poster)\s*=\s*["']([^"']+)["'])|(?:url\(\s*['"]?([^'")]+)['"]?\s*\))""",
    re.IGNORECASE,
)

_IGNORED_SCHEMES = ("mailto:", "tel:", "javascript:", "#", "about:")


# ---------------------------------------------------------------------------
# client identity
# ---------------------------------------------------------------------------

def derive_client_key(
    prospect_id: str = "", account_id: str = "", company_name: str = ""
) -> str:
    """THE deterministic client identity: prospect_id > account_id > company
    slug. Sanitized to a safe directory name; empty everything degrades to
    ``"unknown-client"`` (a real key never collides with it by accident
    because real prospect ids are Places-derived)."""
    for candidate in (prospect_id, account_id):
        c = (candidate or "").strip()
        if c:
            return _KEY_SAFE_RE.sub("_", c)[:120].strip("._") or "unknown-client"
    slug = re.sub(r"[^a-z0-9]+", "-", (company_name or "").lower()).strip("-")
    return slug[:120] or "unknown-client"


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def assets_root() -> Path:
    """``<SAMUS_ARTIFACT_ROOT>/client_assets`` (created on demand)."""
    from backend.common import storage

    root = storage.root() / "client_assets"
    root.mkdir(parents=True, exist_ok=True)
    return root


class ClientAssetStore:
    """Per-client asset directory + manifest. All operations are scoped to
    ONE client_key — there is deliberately no cross-client write surface."""

    def __init__(self, client_key: str, *, root: Path | None = None):
        self.client_key = derive_client_key(prospect_id=client_key)
        self._root = root

    # -- paths ------------------------------------------------------------
    @property
    def root(self) -> Path:
        return (self._root or assets_root()) / self.client_key

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    # -- manifest ---------------------------------------------------------
    def manifest(self) -> list[dict[str, Any]]:
        """Read the manifest; missing/corrupt store == empty (fail-closed:
        no manifest -> no local assets allowed, never an exception)."""
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            entries = data.get("assets", []) if isinstance(data, dict) else []
            return [e for e in entries if isinstance(e, dict)]
        except (OSError, ValueError):
            return []

    def _write_manifest(self, entries: list[dict[str, Any]]) -> None:
        """Atomic manifest write (tmp + os.replace) — a crash mid-write must
        never corrupt ownership records."""
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"client_key": self.client_key, "assets": entries},
                             indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.manifest_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- registration -----------------------------------------------------
    def register(
        self,
        source: str | Path | bytes,
        kind: str,
        *,
        original_name: str = "",
        notes: str = "",
        source_label: str = "operator",
    ) -> dict[str, Any]:
        """Copy an asset INTO this client's dir, hash it, append to the
        manifest. ``source`` is a file path or raw bytes (bytes require
        ``original_name`` for the filename)."""
        if kind not in ASSET_KINDS:
            raise ValueError(f"kind must be one of {ASSET_KINDS}, got {kind!r}")
        if isinstance(source, (str, Path)):
            src_path = Path(source)
            data = src_path.read_bytes()
            original_name = original_name or src_path.name
        else:
            data = bytes(source)
            if not original_name:
                raise ValueError("bytes source requires original_name")

        digest = hashlib.sha256(data).hexdigest()
        filename = _FNAME_SAFE_RE.sub("_", original_name).strip("._") or "asset"
        self.root.mkdir(parents=True, exist_ok=True)

        entries = self.manifest()
        # Same bytes re-registered -> idempotent (return the existing entry).
        for e in entries:
            if e.get("sha256") == digest and e.get("kind") == kind:
                return e
        # Filename collision with DIFFERENT bytes -> uniquify, never overwrite.
        existing_names = {e.get("filename") for e in entries}
        if filename in existing_names or (self.root / filename).exists():
            stem, dot, ext = filename.partition(".")
            filename = f"{stem}-{digest[:8]}{dot}{ext}".rstrip(".")

        (self.root / filename).write_bytes(data)
        entry = {
            "filename": filename,
            "kind": kind,
            "sha256": digest,
            "added_utc": datetime.now(timezone.utc).isoformat(),
            "source": source_label,
            "original_name": original_name,
            "notes": notes,
        }
        entries.append(entry)
        self._write_manifest(entries)
        _LOG.info("registered %s asset %r for client %s (sha256=%s)",
                  kind, filename, self.client_key, digest[:12])
        return entry


# Module-level conveniences (the deliverable's functional surface).

def register_asset(
    client_key: str,
    source: str | Path | bytes,
    kind: str,
    *,
    original_name: str = "",
    notes: str = "",
    source_label: str = "operator",
) -> dict[str, Any]:
    return ClientAssetStore(client_key).register(
        source, kind, original_name=original_name, notes=notes,
        source_label=source_label,
    )


def list_assets(client_key: str) -> list[dict[str, Any]]:
    return ClientAssetStore(client_key).manifest()


def get_asset_path(client_key: str, filename: str) -> Optional[Path]:
    store = ClientAssetStore(client_key)
    for e in store.manifest():
        if e.get("filename") == filename:
            path = store.root / filename
            return path if path.exists() else None
    return None


# ---------------------------------------------------------------------------
# the isolation gate
# ---------------------------------------------------------------------------

def _all_client_hashes(*, root: Path | None = None) -> dict[str, str]:
    """sha256 -> owning client_key across EVERY client store. Used to detect
    a build referencing/shipping bytes that belong to someone else."""
    base = root or assets_root()
    owners: dict[str, str] = {}
    try:
        client_dirs = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return owners
    for d in client_dirs:
        for e in ClientAssetStore(d.name, root=base).manifest():
            h = e.get("sha256")
            if h:
                owners.setdefault(h, d.name)
    return owners


def _is_asset_name(name: str) -> bool:
    return Path(name.split("?", 1)[0].split("#", 1)[0]).suffix.lower() in _ASSET_EXTS


def _normalize_local(ref: str) -> str:
    """'./assets/x.png', '/assets/x.png', 'assets/x.png' -> 'assets/x.png'."""
    r = ref.split("?", 1)[0].split("#", 1)[0]
    while r.startswith("./"):
        r = r[2:]
    return r.lstrip("/")


def _data_uri_sha256(ref: str) -> str:
    """sha256 of a base64 data-URI payload, or "" when undecodable."""
    try:
        head, _, payload = ref.partition(",")
        if "base64" not in head.lower():
            return ""
        return hashlib.sha256(base64.b64decode(payload, validate=False)).hexdigest()
    except (ValueError, binascii.Error):
        return ""


def enforce_asset_isolation(
    site_files: dict[str, str],
    client_key: str,
    *,
    own_domains: tuple[str, ...] = (),
    strict: bool = False,
    root: Path | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Fail-closed brand-asset isolation gate. Returns (clean_files,
    violations). Rules:

      a. a local asset file SHIPPED in the build must be hash-present in THIS
         client's manifest — else dropped + references stripped;
      b. any reference (file, path, or embedded data-URI) matching an asset
         hash registered to a DIFFERENT client is stripped (core guard);
      c. remote refs pass only for REMOTE_ALLOWLIST + ``own_domains``; other
         remotes are recorded as warnings — stripped too when ``strict``.

    Never raises on a missing store: empty manifest == no local assets allowed.
    """
    client_key = derive_client_key(prospect_id=client_key)
    own_store = ClientAssetStore(client_key, root=root)
    own_hashes = {e.get("sha256") for e in own_store.manifest()}
    own_names = {e.get("filename") for e in own_store.manifest()}
    owners = _all_client_hashes(root=root)

    violations: list[str] = []
    clean: dict[str, str] = dict(site_files)
    stripped_refs: set[str] = set()
    dropped_files: set[str] = set()

    # --- pass 1: shipped local asset files must be owned by THIS client ----
    for name, content in site_files.items():
        if not _is_asset_name(name):
            continue
        digest = hashlib.sha256(content.encode("utf-8", "surrogateescape")).hexdigest()
        owner = owners.get(digest)
        if owner and owner != client_key:
            violations.append(
                f"shipped file {name!r} belongs to client {owner!r} - dropped")
            dropped_files.add(name)
        elif digest not in own_hashes:
            violations.append(
                f"shipped file {name!r} not in client {client_key!r} manifest - dropped")
            dropped_files.add(name)
    for name in dropped_files:
        clean.pop(name, None)

    # --- pass 2: scan references in HTML/CSS ------------------------------
    page_names = set(site_files)
    for name, content in list(clean.items()):
        if _is_asset_name(name):
            continue
        for m in _REF_RE.finditer(content):
            ref = (m.group(1) or m.group(2) or "").strip()
            if not ref or ref.lower().startswith(_IGNORED_SCHEMES):
                continue

            if ref.lower().startswith("data:"):
                digest = _data_uri_sha256(ref)
                owner = owners.get(digest) if digest else None
                if owner and owner != client_key:
                    violations.append(
                        f"{name}: embedded data-URI matches asset of client "
                        f"{owner!r} - stripped")
                    stripped_refs.add(ref)
                continue

            if ref.lower().startswith(("http://", "https://", "//")):
                host = (urlsplit(ref if "://" in ref else "https:" + ref).hostname
                        or "").lower()
                allowed = host in REMOTE_ALLOWLIST or any(
                    host == d.lower() or host.endswith("." + d.lower())
                    for d in own_domains if d
                )
                if not allowed:
                    if strict:
                        violations.append(
                            f"{name}: remote ref {ref!r} outside allowlist - "
                            "stripped (strict)")
                        stripped_refs.add(ref)
                    else:
                        violations.append(
                            f"warning: {name}: remote ref {ref!r} outside allowlist")
                continue

            # local reference
            if not _is_asset_name(ref):
                continue  # page-to-page links, generated css/js — not assets
            local = _normalize_local(ref)
            basename = Path(local).name
            if local in dropped_files:
                stripped_refs.add(ref)  # already recorded in pass 1
                continue
            if local in clean or local in page_names:
                continue  # shipped + survived pass 1 -> owned by this client
            if basename in own_names:
                continue  # referenced from this client's store (copied at deploy)
            # Not ours. Foreign or unmanifested -> strip, fail-closed.
            foreign = _foreign_owner_by_name(basename, client_key, root=root)
            if foreign:
                violations.append(
                    f"{name}: ref {ref!r} matches asset of client {foreign!r} - stripped")
            else:
                violations.append(
                    f"{name}: ref {ref!r} not in client {client_key!r} manifest - stripped")
            stripped_refs.add(ref)

    # --- strip recorded references from surviving files -------------------
    if stripped_refs:
        for name in list(clean):
            content = clean[name]
            for ref in stripped_refs:
                content = content.replace(ref, "")
            clean[name] = content

    for v in violations:
        if v.startswith("warning:"):
            _LOG.warning("asset-isolation %s", v)
        else:
            _LOG.error("ASSET-ISOLATION VIOLATION [%s]: %s", client_key, v)
    return clean, violations


def _foreign_owner_by_name(
    basename: str, client_key: str, *, root: Path | None = None
) -> str:
    """Which OTHER client (if any) has an asset with this filename — catches
    by-path reuse even when the bytes aren't shipped in the build."""
    base = root or assets_root()
    try:
        dirs = [d for d in base.iterdir() if d.is_dir() and d.name != client_key]
    except OSError:
        return ""
    for d in dirs:
        for e in ClientAssetStore(d.name, root=base).manifest():
            if e.get("filename") == basename:
                return d.name
    return ""


# ---------------------------------------------------------------------------
# CLI — operator registers a client's real logo/icon/media
# ---------------------------------------------------------------------------

def _resolve_key_from_call_list(company: str) -> str:
    """Try to resolve a prospect_id from today's call list by company-name
    substring (the same identity the demo builder will derive); fall back to
    the company slug so registration always succeeds."""
    try:
        from datetime import date

        from backend.website.demo_sites import load_no_website_prospects

        needle = company.strip().lower()
        for rec in load_no_website_prospects(run_date=date.today()):
            if needle in (rec.company_name or "").lower():
                return derive_client_key(rec.prospect_id, rec.account_id,
                                         rec.company_name)
    except Exception:  # noqa: BLE001 — no call list today is fine
        pass
    return derive_client_key(company_name=company)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m backend.website.client_assets",
        description="Per-client brand-asset store (operator-mandated isolation).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    reg = sub.add_parser("register", help="register an asset to ONE client")
    reg.add_argument("--company", default="", help="company name (resolves via "
                     "today's call list when possible, else slug)")
    reg.add_argument("--prospect-id", default="", help="explicit prospect id")
    reg.add_argument("--kind", required=True, choices=ASSET_KINDS)
    reg.add_argument("--file", required=True, help="path to the asset file")
    reg.add_argument("--notes", default="")

    lst = sub.add_parser("list", help="list a client's registered assets")
    lst.add_argument("--company", default="")
    lst.add_argument("--prospect-id", default="")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.prospect_id:
        key = derive_client_key(prospect_id=args.prospect_id)
    elif args.company:
        key = _resolve_key_from_call_list(args.company)
    else:
        parser.error("provide --company or --prospect-id")
        return 2

    if args.cmd == "register":
        entry = register_asset(key, args.file, args.kind, notes=args.notes)
        print(f"registered to client {key}: {json.dumps(entry, indent=2)}")
        return 0
    entries = list_assets(key)
    print(f"client {key}: {len(entries)} asset(s)")
    for e in entries:
        print(f"  {e.get('kind'):10} {e.get('filename')}  sha256={e.get('sha256', '')[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ASSET_KINDS",
    "REMOTE_ALLOWLIST",
    "ClientAssetStore",
    "derive_client_key",
    "register_asset",
    "list_assets",
    "get_asset_path",
    "enforce_asset_isolation",
]
