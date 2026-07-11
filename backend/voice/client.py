"""Thin httpx wrapper around the Vapi REST API.

Endpoints used — the operator-initiated outbound flow, read paths the webhook
handler may use for enrichment, plus phone-number provisioning for the
AI Digital Receptionist's inbound DID:

  - ``POST  /call``            — initiate an outbound call
  - ``GET   /call/{id}``       — fetch a single call's status
  - ``GET   /call?limit=N``    — list recent calls
  - ``GET   /assistant``       — list assistants (operator UI helper)
  - ``PATCH /assistant/{id}``  — repoint an assistant's server (tunnel)
  - ``POST  /phone-number``    — buy + bind an inbound DID
  - ``GET   /phone-number``    — fetch / list provisioned DIDs
  - ``PATCH /phone-number/{id}`` — rebind a DID's assistant / server

Why no Vapi SDK: same reasoning as :mod:`backend.finance.stripe_client` —
the REST surface is tiny, the SDK pulls in extra deps the container doesn't
need, and httpx is already a project dependency. The api_key is injected at
construction time (not read from env) so tests can fake the client without
touching ``get_settings()``.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import VapiAssistant, VapiCall, VapiPhoneNumber


_LOG = logging.getLogger("samus.voice.client")

_BASE_URL = "https://api.vapi.ai"
_HTTP_TIMEOUT = 15.0


class VapiError(Exception):
    """Raised when Vapi returns a non-2xx response or the network fails."""


class VapiClient:
    """Minimal Vapi REST client. One instance per service call.

    Vapi uses Bearer auth — ``Authorization: Bearer $VAPI_API_KEY``. Both
    public and private keys are accepted at the wire level; the workcell
    expects a private key (full REST access). Restricted-public keys will
    return 401 on ``POST /call``.
    """

    def __init__(self, api_key: str, *, base_url: str = _BASE_URL,
                 timeout: float = _HTTP_TIMEOUT) -> None:
        if not api_key:
            raise ValueError("VapiClient requires a non-empty api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # --- low-level ---------------------------------------------------------

    def _request(self, method: str, path: str, *,
                 params: dict[str, Any] | None = None,
                 json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Issue an HTTP request. Raise VapiError on any failure."""
        if not path.startswith("/"):
            raise ValueError(f"path must start with /, got {path!r}")
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.request(
                    method, url, headers=headers,
                    params=params or None, json=json_body,
                )
        except httpx.HTTPError as exc:
            raise VapiError(f"vapi_transport_error: {exc}") from exc

        if response.status_code >= 400:
            # Vapi error bodies are usually { message: "...", error: "..." }
            # or a list of validation errors. Surface the first useful string.
            try:
                err_body = response.json()
            except ValueError:
                err_body = {}
            message: str
            if isinstance(err_body, dict):
                message = (
                    err_body.get("message")
                    or err_body.get("error")
                    or response.text[:200]
                )
            else:
                message = response.text[:200]
            raise VapiError(
                f"vapi_http_{response.status_code}: {message}"
            )
        # DELETE / 204 may have no body — return empty dict so callers don't
        # have to handle None.
        if response.status_code == 204 or not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise VapiError(f"vapi_invalid_json: {exc}") from exc
        # Vapi GET /call returns a bare list; wrap it so the caller always
        # sees a dict (mirrors how Stripe wraps lists in {"data": [...]}).
        if isinstance(data, list):
            return {"data": data}
        return data

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, json_body=body)

    def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", path, json_body=body)

    # --- high-level (typed) ------------------------------------------------

    def create_call(self, *, assistant_id: str, phone_number_id: str,
                    customer_number: str, customer_name: str | None = None,
                    metadata: dict[str, Any] | None = None,
                    variable_values: dict[str, str] | None = None,
                    voicemail_message: str | None = None,
                    first_message: str | None = None) -> VapiCall:
        """``POST /call`` — initiate an outbound call. Returns the created call.

        ``variable_values`` is forwarded as ``assistantOverrides.variableValues``
        so the assistant template can interpolate per-call context
        (``{{company_name}}``, ``{{callsheet_pitch}}``, etc.). Vapi silently
        ignores variables the assistant template doesn't reference, so this
        is backward-safe for templates that don't define any.

        ``voicemail_message`` is forwarded as ``assistantOverrides.voicemailMessage``
        — the message the assistant leaves when Vapi's voicemail detection
        fires. The dialer passes the prospect's ``callsheet_voicemail`` here so
        each prospect gets a voicemail enriched with their profile + sales
        angle; when omitted the assistant's own static ``voicemailMessage``
        applies (the console / no-prospect-data path).

        ``first_message`` is forwarded as ``assistantOverrides.firstMessage`` —
        the FIRST line the assistant speaks. OUTBOUND dials pass the prospect's
        hook-first ``callsheet_opener`` here so the spoken opener is
        personalized per prospect, WITHOUT mutating the shared assistant's
        static ``firstMessage`` (which the inbound receptionist still uses).
        When omitted the assistant's own ``firstMessage`` applies.
        """
        body: dict[str, Any] = {
            "assistantId": assistant_id,
            "phoneNumberId": phone_number_id,
            "customer": {"number": customer_number},
        }
        if customer_name:
            body["customer"]["name"] = customer_name[:40]
        if metadata:
            # Vapi accepts arbitrary metadata under `metadata` on the call;
            # values must be JSON-serializable scalars / dicts.
            body["metadata"] = metadata
        # variableValues + voicemailMessage both live under assistantOverrides
        # per the Vapi call schema — build it once, attach only if non-empty.
        overrides: dict[str, Any] = {}
        if variable_values:
            # Only string values are guaranteed to interpolate cleanly into
            # the assistant prompt — coerce here so callers can pass
            # ints/floats without surprise.
            overrides["variableValues"] = {
                k: str(v) for k, v in variable_values.items()
            }
        if voicemail_message:
            overrides["voicemailMessage"] = voicemail_message
        if first_message:
            overrides["firstMessage"] = first_message
        if overrides:
            body["assistantOverrides"] = overrides
        return VapiCall.model_validate(self._post("/call", body))

    def get_call(self, call_id: str) -> VapiCall:
        """``GET /call/{id}`` — fetch a single call by id."""
        if not call_id:
            raise ValueError("get_call requires a non-empty call_id")
        return VapiCall.model_validate(self._get(f"/call/{call_id}"))

    def list_calls(self, *, limit: int = 10) -> list[VapiCall]:
        """``GET /call?limit=N`` — list recent calls.

        Limit is clamped to the Vapi-documented range [1, 100].
        """
        clamped = max(1, min(100, int(limit)))
        data = self._get("/call", {"limit": clamped})
        rows = data.get("data", []) or []
        out: list[VapiCall] = []
        for row in rows:
            try:
                out.append(VapiCall.model_validate(row))
            except Exception as exc:  # noqa: BLE001 — log + skip malformed row
                _LOG.warning("skipping malformed call row: %s", exc)
        return out

    def update_assistant(self, *, assistant_id: str,
                         server_url: str | None = None,
                         server_url_secret: str | None = None) -> dict[str, Any]:
        """``PATCH /assistant/{id}`` — update the assistant's server config.

        Vapi nests serverUrl + secret under a single ``server`` object on
        the assistant body. Either field can be patched independently; we
        omit ``secret`` when caller passes None so existing secrets stay
        intact across tunnel rotations.
        """
        if not assistant_id:
            raise ValueError("update_assistant requires a non-empty assistant_id")
        server: dict[str, Any] = {}
        if server_url is not None:
            server["url"] = server_url
        if server_url_secret is not None:
            server["secret"] = server_url_secret
        if not server:
            raise ValueError(
                "update_assistant requires at least one of server_url / "
                "server_url_secret to be set",
            )
        return self._patch(f"/assistant/{assistant_id}", {"server": server})

    def get_assistant(self, assistant_id: str) -> dict[str, Any]:
        """``GET /assistant/{id}`` — fetch a single assistant's full config."""
        if not assistant_id:
            raise ValueError("get_assistant requires a non-empty assistant_id")
        return self._get(f"/assistant/{assistant_id}")

    def patch_assistant_config(
        self,
        assistant_id: str,
        *,
        system_prompt: str | None = None,
        first_message: str | None = None,
        voice_speed: float | None = None,
        voice_similarity_boost: float | None = None,
    ) -> dict[str, Any]:
        """``PATCH /assistant/{id}`` — update model system prompt, firstMessage, or voice params.

        Only the fields that are not None are included in the PATCH body so an
        unrelated field (e.g. server URL) is never disturbed.

        ``system_prompt`` needs care: Vapi validates the ``model`` object on
        PATCH and REQUIRES ``model.provider`` (and ``model.model``). A
        messages-only model patch — ``{"model": {"messages": [...]}}`` — 400s
        with "model.provider must be one of ...". So when a system prompt is
        supplied we FETCH the current assistant (``get_assistant``), take its
        existing ``model`` object, and replace ONLY ``model.messages`` with the
        new system message — preserving ``provider``, ``model``, ``toolIds``,
        ``knowledgeBase`` and any other model sub-field. That satisfies Vapi's
        schema while keeping the method's "only change what I was asked to
        change" contract, with no new params for callers. The GET happens ONLY
        when ``system_prompt`` is being patched — a firstMessage-only or
        voice-only patch issues no extra request.
        """
        if not assistant_id:
            raise ValueError("patch_assistant_config requires a non-empty assistant_id")
        body: dict[str, Any] = {}
        if system_prompt is not None:
            # Fetch-and-merge: preserve provider/model/toolIds/knowledgeBase and
            # only swap the system message, so Vapi's model-schema validation
            # passes. Fall back to a messages-only body only if the live model
            # object can't be fetched (still lets the caller see Vapi's error
            # rather than us swallowing it).
            current = self.get_assistant(assistant_id)
            model_obj = current.get("model")
            merged_model: dict[str, Any] = dict(model_obj) if isinstance(model_obj, dict) else {}
            merged_model["messages"] = [{"role": "system", "content": system_prompt}]
            body["model"] = merged_model
        if first_message is not None:
            body["firstMessage"] = first_message
        voice_patch: dict[str, Any] = {}
        if voice_speed is not None:
            voice_patch["speed"] = voice_speed
        if voice_similarity_boost is not None:
            voice_patch["similarityBoost"] = voice_similarity_boost
        if voice_patch:
            body["voice"] = voice_patch
        if not body:
            raise ValueError(
                "patch_assistant_config requires at least one field to patch",
            )
        return self._patch(f"/assistant/{assistant_id}", body)

    def list_assistants(self, *, limit: int = 100) -> list[VapiAssistant]:
        """``GET /assistant?limit=N`` — list configured assistants."""
        clamped = max(1, min(100, int(limit)))
        data = self._get("/assistant", {"limit": clamped})
        rows = data.get("data", []) or []
        out: list[VapiAssistant] = []
        for row in rows:
            try:
                out.append(VapiAssistant.model_validate(row))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("skipping malformed assistant row: %s", exc)
        return out

    # --- phone numbers (AI Digital Receptionist inbound DID) ---------------

    def create_phone_number(self, *, assistant_id: str, name: str = "",
                            provider: str = "vapi",
                            area_code: str | None = None,
                            number: str | None = None,
                            twilio_account_sid: str | None = None,
                            twilio_auth_token: str | None = None,
                            twilio_api_key: str | None = None,
                            twilio_api_secret: str | None = None,
                            sms_enabled: bool | None = None) -> VapiPhoneNumber:
        """``POST /phone-number`` — provision a DID and bind it to an assistant.

        Two providers are supported, both POSTing to the same endpoint:

        ``provider="vapi"`` (default) buys a fresh Vapi-managed US number;
        ``area_code`` requests a specific NANP area code (best-effort — Vapi
        falls back to any available number).

        ``provider="twilio"`` IMPORTS a number you already own in your own
        Twilio account (Vapi cannot port out numbers bought inside Vapi, so
        the migration path is: buy in Twilio → import here → cut over →
        release the old Vapi DID). It always requires the E.164 ``number`` and
        the **Account** SID ``twilio_account_sid`` (must start with ``AC`` —
        an ``SK`` value is an API *Key* SID, which Twilio/Vapi rejects as the
        account sid). Authentication is then EITHER:

          * ``twilio_api_key`` (``SK…``) + ``twilio_api_secret`` — a scoped,
            revocable API key (preferred: revoking it doesn't touch the
            account Auth Token), or
          * ``twilio_auth_token`` — the account-wide Auth Token.

        Vapi verifies the number against these and configures its
        voice/messaging webhooks; the number stays owned by Twilio. Per the
        Vapi API the field names are ``number``, ``twilioAccountSid``,
        ``twilioApiKey``, ``twilioApiSecret``, ``twilioAuthToken`` (camelCase).

        Either way the created number answers inbound calls with
        ``assistant_id`` and delivers events to that assistant's server URL.
        """
        if not assistant_id:
            raise ValueError("create_phone_number requires a non-empty assistant_id")
        body: dict[str, Any] = {"provider": provider, "assistantId": assistant_id}

        if provider == "twilio":
            num = (number or "").strip()
            sid = (twilio_account_sid or "").strip()
            tok = (twilio_auth_token or "").strip()
            api_key = (twilio_api_key or "").strip()
            api_secret = (twilio_api_secret or "").strip()
            # Fail closed: refuse locally rather than 400 at Vapi or — worse —
            # silently fall back to a different binding.
            if not num:
                raise ValueError(
                    "create_phone_number(provider='twilio') requires the "
                    "E.164 number to import",
                )
            if not sid:
                raise ValueError(
                    "create_phone_number(provider='twilio') requires "
                    "twilio_account_sid (the Account SID)",
                )
            # The single most common mis-seed: putting an API Key SID (SK…)
            # where the Account SID (AC…) belongs. Catch it here with a clear
            # message instead of Vapi's opaque 400.
            if not sid.startswith("AC"):
                raise ValueError(
                    f"twilio_account_sid must be the Account SID (starts with "
                    f"'AC'); got a value starting with '{sid[:2]}'. An 'SK' "
                    "value is an API Key SID — pass it as twilio_api_key "
                    "(with twilio_api_secret) and the AC account sid here.",
                )
            have_api_key = bool(api_key and api_secret)
            if not have_api_key and not tok:
                raise ValueError(
                    "create_phone_number(provider='twilio') requires either "
                    "twilio_api_key + twilio_api_secret, or twilio_auth_token",
                )
            body["number"] = num
            body["twilioAccountSid"] = sid
            if have_api_key:
                # API-key auth takes precedence — scoped + revocable.
                body["twilioApiKey"] = api_key
                body["twilioApiSecret"] = api_secret
            else:
                body["twilioAuthToken"] = tok
            if sms_enabled is not None:
                body["smsEnabled"] = bool(sms_enabled)
        elif area_code:
            body["numberDesiredAreaCode"] = area_code

        if name:
            body["name"] = name
        return VapiPhoneNumber.model_validate(self._post("/phone-number", body))

    def get_phone_number(self, phone_number_id: str) -> VapiPhoneNumber:
        """``GET /phone-number/{id}`` — fetch one provisioned DID."""
        if not phone_number_id:
            raise ValueError("get_phone_number requires a non-empty phone_number_id")
        return VapiPhoneNumber.model_validate(
            self._get(f"/phone-number/{phone_number_id}"),
        )

    def list_phone_numbers(self, *, limit: int = 100) -> list[VapiPhoneNumber]:
        """``GET /phone-number?limit=N`` — list provisioned DIDs."""
        clamped = max(1, min(100, int(limit)))
        data = self._get("/phone-number", {"limit": clamped})
        rows = data.get("data", []) or []
        out: list[VapiPhoneNumber] = []
        for row in rows:
            try:
                out.append(VapiPhoneNumber.model_validate(row))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("skipping malformed phone-number row: %s", exc)
        return out

    def update_phone_number(self, *, phone_number_id: str,
                            assistant_id: str | None = None,
                            server_url: str | None = None) -> VapiPhoneNumber:
        """``PATCH /phone-number/{id}`` — rebind a DID's assistant / server URL.

        At least one of ``assistant_id`` / ``server_url`` must be set. Fields
        left None are omitted so an existing binding survives the patch.
        """
        if not phone_number_id:
            raise ValueError("update_phone_number requires a non-empty phone_number_id")
        body: dict[str, Any] = {}
        if assistant_id is not None:
            body["assistantId"] = assistant_id
        if server_url is not None:
            body["server"] = {"url": server_url}
        if not body:
            raise ValueError(
                "update_phone_number requires assistant_id or server_url",
            )
        return VapiPhoneNumber.model_validate(
            self._patch(f"/phone-number/{phone_number_id}", body),
        )
