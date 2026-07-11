"""Thin httpx wrapper around the Wix REST API.

Why no ``wix`` SDK: Wix ships official SDKs for JavaScript/TypeScript only —
there is no maintained Python SDK. The documented server-to-server path for a
backend is to call the REST API directly with an account-generated **API key**
(https://dev.wix.com/docs/api-reference/articles/authentication/about-api-keys),
which is exactly what this client does. Keeping it httpx-only matches the
finance ``stripe_client`` / seo ``pagespeed_client`` convention and keeps the
image lean.

Auth model (REST API keys, NOT OAuth — we operate on our own account):
  * ``Authorization: <API_KEY>``  — the raw key value (Wix does NOT use a
    ``Bearer`` prefix for API keys, unlike Stripe).
  * ``wix-account-id: <ACCOUNT_ID>``  — required on **account-level** calls
    (e.g. provisioning a new site).
  * ``wix-site-id: <SITE_ID>``  — required on **site-level** calls (content,
    business info). Supplied per-call so one client instance serves any site.

What this client deliberately does and does NOT do:
  * It owns the transport: auth headers, JSON encode/decode, error mapping,
    and the documented ~200 req/min rate limit (HTTP 429 -> bounded backoff).
  * High-level helpers exist only for the endpoints whose request shape is
    stable and account-generic: provisioning a site and CMS data items.
  * Everything else (Site Properties, Media, Blog, Stores) is reached through
    the generic :meth:`call` passthrough, because the exact body is
    template/site-specific and is composed by the stage handler that owns it.
    This is a transport, not a schema oracle — it never guesses a payload.

The Wix REST API "is not intended for use in Wix site *development*": there is
no endpoint that lays out pages or styles elements. Design comes from a
template (or the Editor); this client *provisions* and *populates*.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

_LOG = logging.getLogger("samus.website.wix_client")

_BASE_URL = "https://www.wixapis.com"
_HTTP_TIMEOUT = 20.0
# Wix publishes a ~200 req/min limit; a 429 means back off ~a minute. We retry
# a bounded number of times with a short, capped sleep rather than blocking a
# worker for a full minute — the staged worker is idempotent and will resume.
_MAX_RETRIES = 2
_RETRY_BACKOFF_SEC = 2.0


class WixError(Exception):
    """Raised when Wix returns a non-2xx response or the network fails.

    Carries the HTTP status (or None for a transport error) so callers can
    distinguish a 401/403 (re-auth / wrong key) from a 429 (rate limit) from a
    5xx, and decide whether to park, escalate, or retry.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WixClient:
    """Minimal Wix REST client. One instance per service call.

    ``api_key`` + ``account_id`` are taken at construction time (not read from
    env) so tests inject fakes without touching ``get_settings()``. ``account_id``
    is optional at construction because site-level calls do not need it, but the
    account-level helpers (:meth:`create_site`) require it.
    """

    def __init__(
        self,
        api_key: str,
        *,
        account_id: str = "",
        base_url: str = _BASE_URL,
        timeout: float = _HTTP_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("WixClient requires a non-empty api_key")
        self._api_key = api_key
        self._account_id = account_id
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # Injectable transport: tests pass an httpx.MockTransport so no real
        # network is ever touched.
        self._transport = transport

    # --- low-level ---------------------------------------------------------

    def _headers(self, *, site_id: str = "", account_level: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }
        if account_level:
            if not self._account_id:
                raise WixError("account-level call requires a wix-account-id")
            headers["wix-account-id"] = self._account_id
        if site_id:
            headers["wix-site-id"] = site_id
        return headers

    def call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        site_id: str = "",
        account_level: bool = False,
    ) -> dict[str, Any]:
        """Issue one Wix REST call. ``path`` must start with ``/``.

        The caller owns the body (``json``) and chooses whether the call is
        account-level (``account_level=True``, sends ``wix-account-id``) or
        site-level (``site_id=...``, sends ``wix-site-id``). Returns the parsed
        JSON body (``{}`` for an empty 2xx). Raises :class:`WixError` on any
        non-2xx after the bounded 429 retry, or on transport/parse failure.
        """
        if not path.startswith("/"):
            raise ValueError(f"path must start with /, got {path!r}")
        url = f"{self._base_url}{path}"
        headers = self._headers(site_id=site_id, account_level=account_level)

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                    response = client.request(
                        method.upper(),
                        url,
                        headers=headers,
                        json=json,
                        params=params or {},
                    )
            except httpx.HTTPError as exc:
                # Transport failure — fail-closed (no silent success).
                raise WixError(f"wix_transport_error: {exc}", status_code=None) from exc

            if response.status_code == 429 and attempt < _MAX_RETRIES:
                sleep_for = _RETRY_BACKOFF_SEC * (attempt + 1)
                _LOG.warning(
                    "wix 429 rate-limited on %s %s; backoff %.1fs (attempt %d/%d)",
                    method.upper(),
                    path,
                    sleep_for,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(sleep_for)
                continue

            if response.status_code >= 400:
                # Wix error bodies are { message, details: {...} } — surface the
                # message, fall back to a clipped raw body.
                try:
                    body = response.json()
                    message = body.get("message") or str(body)[:200]
                except ValueError:
                    message = response.text[:200]
                raise WixError(
                    f"wix_http_{response.status_code}: {message}",
                    status_code=response.status_code,
                )

            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise WixError(
                    f"wix_invalid_json: {exc}", status_code=response.status_code
                ) from exc

        # Exhausted retries on repeated 429.
        raise WixError(
            "wix_rate_limited: exhausted retries on 429",
            status_code=429,
        ) from last_exc

    # --- site-level: Media Manager (upload generated assets) --------------

    def generate_upload_url(
        self,
        *,
        site_id: str,
        filename: str,
        mime_type: str,
        parent_folder_id: str = "",
        private: bool = False,
    ) -> str:
        """POST /site-media/v1/files/generate-upload-url — get a signed upload URL.

        Docs: dev.wix.com/docs/api-reference/assets/media/media-manager/files/generate-file-upload-url
        """
        if not site_id:
            raise WixError("generate_upload_url requires a site_id")
        body: dict[str, Any] = {"fileName": filename, "mimeType": mime_type, "private": private}
        if parent_folder_id:
            body["parentFolderId"] = parent_folder_id
        resp = self.call(
            "POST",
            "/site-media/v1/files/generate-upload-url",
            json=body,
            site_id=site_id,
        )
        url = resp.get("uploadUrl") or ""
        if not url:
            raise WixError("generate_upload_url: no uploadUrl in response")
        return str(url)

    def upload_file_bytes(
        self,
        upload_url: str,
        data: bytes,
        *,
        filename: str,
        mime_type: str,
    ) -> dict[str, Any]:
        """PUT raw bytes to a signed upload URL; return the file descriptor.

        ``upload_url`` is the full pre-signed URL from generate_upload_url (NOT
        under _base_url). The bytes go as the body with the file's Content-Type
        and a ``filename`` query param. The descriptor carries the durable
        ``wixUrl`` (``wix:image://…`` / ``wix:video://…``) used to bind the asset
        on a page. EXPERIMENTAL: verify live with a real key.
        """
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.put(
                    upload_url,
                    params={"filename": filename},
                    headers={"Content-Type": mime_type},
                    content=data,
                )
        except httpx.HTTPError as exc:
            raise WixError(f"wix_media_upload_transport: {exc}", status_code=None) from exc
        if response.status_code >= 400:
            raise WixError(
                f"wix_media_upload_{response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )
        try:
            body = response.json() if response.content else {}
        except ValueError as exc:
            raise WixError(
                f"wix_media_upload_invalid_json: {exc}", status_code=response.status_code
            ) from exc
        return body.get("file") or body.get("fileDescriptor") or body

    def upload_bytes(
        self,
        *,
        site_id: str,
        data: bytes,
        filename: str,
        mime_type: str,
        parent_folder_id: str = "",
        private: bool = False,
    ) -> dict[str, Any]:
        """Convenience: generate a signed URL then PUT the bytes. Returns descriptor."""
        url = self.generate_upload_url(
            site_id=site_id,
            filename=filename,
            mime_type=mime_type,
            parent_folder_id=parent_folder_id,
            private=private,
        )
        return self.upload_file_bytes(url, data, filename=filename, mime_type=mime_type)

    def import_file(
        self,
        *,
        site_id: str,
        url: str,
        mime_type: str,
        display_name: str = "",
    ) -> dict[str, Any]:
        """POST /site-media/v1/files/import — import a publicly reachable file (e.g. a Veo URI)."""
        if not site_id:
            raise WixError("import_file requires a site_id")
        body: dict[str, Any] = {"url": url, "mimeType": mime_type}
        if display_name:
            body["displayName"] = display_name
        resp = self.call("POST", "/site-media/v1/files/import", json=body, site_id=site_id)
        # Unwrap to the file descriptor (carries id / url / wixUrl), mirroring upload_file_bytes.
        return resp.get("file") or resp.get("fileDescriptor") or resp

    # --- account-level: provision a site ----------------------------------

    def create_site(
        self,
        display_name: str,
        *,
        template_id: str | None = None,
        project_type: str = "WIX",
    ) -> dict[str, Any]:
        """POST /funnel/projects/v1/create — provision a new site/project.

        Returns the raw Wix project dict; the durable ``metaSiteId`` (the site
        id every subsequent site-level call needs) is read by the caller from
        the response. ``template_id`` optionally bases the new site on a
        template; ``project_type`` is ``WIX`` (standard Editor site) by default
        (other documented values: ``VIBE``, ``HEADLESS``, ``BRANDED_APP``).

        Docs: https://dev.wix.com/docs/rest/account-level/sites/project-v1/create-project

        Note: provisioning yields a *container* (blank or a template copy). It
        does NOT design the site — that remains a template/Editor concern.
        """
        if not display_name.strip():
            raise ValueError("create_site requires a non-empty display_name")
        body: dict[str, Any] = {
            "displayName": display_name.strip(),
            "projectType": project_type,
        }
        if template_id:
            body["templateId"] = template_id
        return self.call(
            "POST",
            "/funnel/projects/v1/create",
            json=body,
            account_level=True,
        )

    # --- site-level: CMS data items (dynamic page content) ----------------

    def insert_data_item(
        self,
        *,
        site_id: str,
        collection_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /wix-data/v2/items — insert one item into a CMS collection.

        CMS (Wix Data) is how content reaches a template's dynamic pages: each
        item in a collection renders a page/section the template binds to. The
        collection must already exist on the site (created in the Editor or via
        the Create Data Collection API). Returns the inserted item envelope.

        Docs: https://dev.wix.com/docs/rest/business-solutions/cms/data-items/insert-data-item
        """
        if not site_id:
            raise WixError("insert_data_item requires a site_id")
        if not collection_id:
            raise ValueError("insert_data_item requires a collection_id")
        body = {"dataCollectionId": collection_id, "dataItem": {"data": data}}
        return self.call("POST", "/wix-data/v2/items", json=body, site_id=site_id)

    def update_data_item(
        self,
        *,
        site_id: str,
        collection_id: str,
        item_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """PUT /wix-data/v2/items/{id} — overwrite an existing CMS item.

        ``data`` is the full field set; ``_id`` is injected so the body's
        dataItem carries its own id (verified-required). Used by the content
        stage's idempotent upsert so a re-run refreshes the row in place rather
        than inserting a duplicate.
        """
        if not site_id:
            raise WixError("update_data_item requires a site_id")
        if not item_id:
            raise ValueError("update_data_item requires an item_id")
        payload = {**data, "_id": item_id}
        body = {"dataCollectionId": collection_id, "dataItem": {"id": item_id, "data": payload}}
        return self.call("PUT", f"/wix-data/v2/items/{item_id}", json=body, site_id=site_id)

    def query_data_items(
        self,
        *,
        site_id: str,
        collection_id: str,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /wix-data/v2/items/query — read items from a CMS collection.

        Used by the QA stage to confirm content actually landed. ``query`` is
        the documented query object (filter/sort/paging); an empty query returns
        the collection's items (paged).
        """
        if not site_id:
            raise WixError("query_data_items requires a site_id")
        if not collection_id:
            raise ValueError("query_data_items requires a collection_id")
        body: dict[str, Any] = {"dataCollectionId": collection_id, "query": query or {}}
        return self.call("POST", "/wix-data/v2/items/query", json=body, site_id=site_id)

    def create_data_collection(
        self,
        *,
        site_id: str,
        collection_id: str,
        display_name: str,
        field_keys: list[str],
    ) -> dict[str, Any]:
        """POST /wix-data/v2/collections — create a CMS collection (all TEXT).

        Requires the site's Dev Mode / Wix Code to be enabled (else
        ``WDE0110: Wix Code not enabled``). This is a one-time TEMPLATE-setup
        step; per-order content population is :meth:`insert_data_item`. The
        dynamic-page / element binding to this collection is Editor-only.
        Docs: dev.wix.com/.../cms/collection-management/data-collections/create-data-collection
        """
        if not site_id:
            raise WixError("create_data_collection requires a site_id")
        body = {
            "collection": {
                "id": collection_id,
                "displayName": display_name,
                "fields": [{"key": k, "displayName": k, "type": "TEXT"} for k in field_keys],
            }
        }
        return self.call("POST", "/wix-data/v2/collections", json=body, site_id=site_id)

    # --- site-level: business identity (Site Properties v4) ----------------
    # v4 splits updates into purpose-specific POST sub-actions (no PATCH, no
    # fieldMask): fields go under a ``properties`` wrapper; omitted fields are
    # left unchanged. Verified live against site-properties/v4/properties GET.

    def get_site_properties(self, *, site_id: str) -> dict[str, Any]:
        """GET /site-properties/v4/properties -> {version, properties:{...}}."""
        if not site_id:
            raise WixError("get_site_properties requires a site_id")
        return self.call("GET", "/site-properties/v4/properties", site_id=site_id)

    @staticmethod
    def _field_mask(data: dict[str, Any]) -> str:
        """Comma-separated TOP-LEVEL keys of ``data`` — the v4 update field mask.

        Site Properties v4 updates are masked PATCHes: the wire carries a
        ``fields`` STRING (not an array, not ``fieldMask``) naming the proto
        paths to write. A field in the body but not in ``fields`` is IGNORED —
        hence the server's "No updates on request body" when the mask is empty.
        Verified live: nested objects (e.g. ``address``) are masked at the
        top-level key, NOT by dotted sub-paths — ``fields:"address"`` is
        accepted while ``fields:"address.city"`` is rejected ("not allowed").
        """
        return ",".join(data.keys())

    def update_business_profile(
        self,
        *,
        site_id: str,
        business_profile: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /site-properties/v4/properties/business-profile.

        Wraps the data under ``businessProfile`` with the required ``fields``
        mask (verified against @wix SDK source — NOT the docs' ``properties``
        shape). Settable fields: siteDisplayName, businessName, description,
        logo, companyId. (paymentCurrency/locale/timeZone are NOT settable via
        v4 — dashboard only.)
        """
        if not site_id:
            raise WixError("update_business_profile requires a site_id")
        body = {"businessProfile": business_profile, "fields": self._field_mask(business_profile)}
        return self.call(
            "POST",
            "/site-properties/v4/properties/business-profile",
            json=body,
            site_id=site_id,
        )

    def update_business_contact(
        self,
        *,
        site_id: str,
        business_contact: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /site-properties/v4/properties/business-contact.

        Wraps under ``businessContact`` + ``fields`` mask. Settable: email,
        phone, fax, address {street, streetNumber, city, state, zip, country,
        ...}. NOTE businessName is on the *profile*, not here.
        """
        if not site_id:
            raise WixError("update_business_contact requires a site_id")
        body = {"businessContact": business_contact, "fields": self._field_mask(business_contact)}
        return self.call(
            "POST",
            "/site-properties/v4/properties/business-contact",
            json=body,
            site_id=site_id,
        )

    # --- publish ----------------------------------------------------------

    def publish_site(self, *, site_id: str) -> dict[str, Any]:
        """POST /site-publisher/v1/site/publish — publish the site live.

        No body; the target is the wix-site-id header. Needs the "Manage SEO
        Settings" permission on the API key.
        Docs: dev.wix.com/.../sites/site-actions/publish-site
        """
        if not site_id:
            raise WixError("publish_site requires a site_id")
        return self.call(
            "POST",
            "/site-publisher/v1/site/publish",
            json={},
            site_id=site_id,
            account_level=True,
        )


__all__ = ["WixClient", "WixError"]
