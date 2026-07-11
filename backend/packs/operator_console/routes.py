"""FastAPI routes for the operator_console pack (Samus)."""
from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend.common.config import get_settings
from backend.standard.chat import (
    ChatEnrichmentBag,
    EnrichmentContext,
    PromptAssembler,
)
from backend.standard.persona import PersonaNotFound

from .history import StoredChat
from .pod import OperatorConsoleState

__tier__ = "PACKS"

_LOG = logging.getLogger("samus.operator_console.routes")


def _is_development() -> bool:
    """True when the runtime env is development.

    Mirrors the development-vs-production idiom in
    ``backend.common.app_factory`` (which `require_env`s the HMAC key outside
    development). Used by the console token gate to decide whether an unset
    ``SAMUS_OPERATOR_TOKEN`` is tolerated (development) or fail-closed
    (everywhere else).
    """
    return (get_settings().env or "").strip().lower() == "development"


_PACK_ROOT = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PACK_ROOT / "templates"
_STATIC_DIR = _PACK_ROOT / "static"


def _unsafe_draft_name(name: str) -> bool:
    """Reject any draft name that smells like a path-traversal attempt."""
    if not name or len(name) > 200:
        return True
    if "/" in name or "\\" in name or ".." in name:
        return True
    if not name.lower().endswith(".md"):
        return True
    return False


def _make_token_dep(state: OperatorConsoleState):
    def _token_dep(authorization: str | None = Header(default=None)) -> None:
        expected = state.api_token
        if not expected:
            # Finding M6 (2026-05-20) — fail CLOSED outside development.
            # The console routes are mounted on the gateway, which is itself
            # HMAC-exempt by name, so an empty ``SAMUS_OPERATOR_TOKEN`` used
            # to mean ZERO authentication on /api/console/* (including the
            # LLM-cost-incurring /respond endpoint). In development we keep
            # the open path so local work / the test suite aren't blocked by
            # an unset token; anywhere else an unset token now rejects every
            # request with 503 rather than silently disabling auth.
            if _is_development():
                return
            _LOG.error(
                "operator console auth fail-closed: SAMUS_OPERATOR_TOKEN "
                "unset in a non-development environment — rejecting request",
            )
            raise HTTPException(
                status_code=503, detail="operator_token_not_configured",
            )
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing_bearer")
        presented = authorization.split(" ", 1)[1].strip()
        if not hmac.compare_digest(
            presented.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(status_code=403, detail="invalid_bearer")

    return _token_dep


def build_console_router(state: OperatorConsoleState) -> APIRouter:
    router = APIRouter()
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    if _STATIC_DIR.exists():
        router.mount(
            "/console/static",
            StaticFiles(directory=str(_STATIC_DIR)),
            name="console_static",
        )

    @router.get("/console", response_class=HTMLResponse)
    async def console_shell(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "ai_name": state.ai_display_name,
                "user_name": state.operator_display_name,
                "api_version": "v1",
                "default_persona": state.default_persona,
            },
        )

    return router


def build_api_router(state: OperatorConsoleState) -> APIRouter:
    router = APIRouter(prefix="/api/console", tags=["operator_console"])
    token_dep = _make_token_dep(state)

    @router.get("/state", dependencies=[Depends(token_dep)])
    async def get_state() -> dict[str, Any]:
        catalogue = state.catalogue()
        return {
            "ai_name": state.ai_display_name,
            "user_name": state.operator_display_name,
            "persona_count": len(state.personas.list()),
            "preset_ids": sorted(catalogue.presets.keys()),
            "spice_categories": sorted(catalogue.pool.categories.keys()),
            "default_persona": state.default_persona,
            "chat_count": state.history.chat_count(),
        }

    @router.get("/personas", dependencies=[Depends(token_dep)])
    async def list_personas() -> dict[str, Any]:
        items = [p.to_dict() for p in state.personas.list()]
        return {"count": len(items), "personas": items}

    @router.get("/personas/{persona_id}", dependencies=[Depends(token_dep)])
    async def get_persona(persona_id: str) -> dict[str, Any]:
        try:
            return state.personas.get(persona_id).to_dict()
        except PersonaNotFound as exc:
            raise HTTPException(status_code=404, detail=f"unknown persona: {exc}") from exc

    @router.get("/presets", dependencies=[Depends(token_dep)])
    async def list_presets() -> dict[str, Any]:
        catalogue = state.catalogue()
        return {
            "count": len(catalogue.presets),
            "presets": [p.model_dump() for p in catalogue.presets.values()],
        }

    @router.get("/presets/{preset_id}", dependencies=[Depends(token_dep)])
    async def get_preset(preset_id: str) -> dict[str, Any]:
        catalogue = state.catalogue()
        preset = catalogue.preset(preset_id)
        if preset is None:
            raise HTTPException(status_code=404, detail=f"unknown preset: {preset_id}")
        return preset.model_dump()

    @router.get("/pieces", dependencies=[Depends(token_dep)])
    async def list_pieces() -> dict[str, Any]:
        catalogue = state.catalogue()
        return {
            "pieces": {kind.value: dict(items) for kind, items in catalogue.library.pieces.items()},
        }

    @router.post("/chats", dependencies=[Depends(token_dep)])
    async def create_chat(body: dict) -> dict[str, Any]:
        persona_id = str(body.get("persona_id") or state.default_persona)
        name = str(body.get("name") or "Untitled chat")
        try:
            persona = state.personas.get(persona_id)
        except PersonaNotFound as exc:
            raise HTTPException(status_code=400, detail=f"unknown persona: {exc}") from exc
        bag_dict = persona.default_bag if isinstance(persona.default_bag, dict) else {}
        if "bag" in body and isinstance(body["bag"], dict):
            bag_dict = {**bag_dict, **body["bag"]}
        try:
            bag = ChatEnrichmentBag.model_validate(bag_dict)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid bag: {exc!r}") from exc
        preset = state.catalogue().preset(bag.preset_id)
        spice_category = preset.spice_category if preset is not None else "default"
        chat = state.history.create_chat(
            name=name, persona_id=persona_id, bag=bag, spice_category=spice_category,
        )
        return chat.to_dict()

    @router.get("/chats", dependencies=[Depends(token_dep)])
    async def list_chats() -> dict[str, Any]:
        items = [c.to_dict() for c in state.history.list_chats()]
        return {"count": len(items), "chats": items}

    @router.get("/chats/{chat_id}", dependencies=[Depends(token_dep)])
    async def get_chat(chat_id: str) -> dict[str, Any]:
        chat = state.history.get_chat(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="unknown chat")
        messages = [m.to_dict() for m in state.history.list_messages(chat_id)]
        body = chat.to_dict()
        body["messages"] = messages
        return body

    @router.patch("/chats/{chat_id}", dependencies=[Depends(token_dep)])
    async def update_chat(chat_id: str, body: dict) -> dict[str, Any]:
        chat = state.history.get_chat(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="unknown chat")
        bag = chat.bag
        if "bag" in body and isinstance(body["bag"], dict):
            try:
                bag = ChatEnrichmentBag.model_validate({**chat.bag.model_dump(), **body["bag"]})
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"invalid bag patch: {exc!r}") from exc
        name = str(body.get("name") or chat.name)
        persona_id = str(body.get("persona_id") or chat.persona_id)
        updated = state.history.update_chat(
            _patched(chat, name=name, persona_id=persona_id, bag=bag)
        )
        return updated.to_dict()

    @router.delete("/chats/{chat_id}", dependencies=[Depends(token_dep)])
    async def delete_chat(chat_id: str) -> Response:
        ok = state.history.delete_chat(chat_id)
        if not ok:
            raise HTTPException(status_code=404, detail="unknown chat")
        return Response(status_code=204)

    @router.post("/chats/{chat_id}/messages", dependencies=[Depends(token_dep)])
    async def append_message(chat_id: str, body: dict) -> dict[str, Any]:
        if state.history.get_chat(chat_id) is None:
            raise HTTPException(status_code=404, detail="unknown chat")
        role = str(body.get("role") or "user")
        if role not in {"user", "system", "assistant", "tool"}:
            raise HTTPException(status_code=400, detail="invalid role")
        content = str(body.get("content") or "")
        if not content.strip():
            raise HTTPException(status_code=400, detail="content must be non-empty")
        msg = state.history.append_message(
            chat_id=chat_id, role=role, content=content,
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
        )
        return msg.to_dict()

    @router.post("/chats/{chat_id}/assemble", dependencies=[Depends(token_dep)])
    async def assemble_prompt(chat_id: str) -> dict[str, Any]:
        chat = state.history.get_chat(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="unknown chat")
        catalogue = state.catalogue()
        preset = catalogue.preset(chat.bag.preset_id)
        if preset is None:
            raise HTTPException(
                status_code=400,
                detail=f"chat references unknown preset {chat.bag.preset_id!r}",
            )
        assembler = PromptAssembler(library=catalogue.library, pool=catalogue.pool)
        context = EnrichmentContext(chat_id=chat_id, turn=chat.spice_state.turn)
        result = assembler.assemble(
            preset=preset, bag=chat.bag, context=context,
            variables={
                "ai_name": state.ai_display_name,
                "user_name": state.operator_display_name,
            },
            spice_state=chat.spice_state,
        )
        state.history.update_chat(_patched(chat, spice_state=chat.spice_state))
        return {
            "text": result.text,
            "char_count": result.char_count,
            "sections": [{"label": label, "text": text} for label, text in result.sections],
        }

    @router.get("/opportunities/pending_stake", dependencies=[Depends(token_dep)])
    async def opportunities_pending_stake() -> dict[str, Any]:
        from backend.crm import service as crm_service
        pending = crm_service.list_opportunities_pending_stake(limit=50)
        return {
            "count": len(pending),
            "opportunities": [o.model_dump() for o in pending],
        }

    @router.post(
        "/opportunities/{opportunity_id}/stake",
        dependencies=[Depends(token_dep)],
    )
    async def stake_opportunity(opportunity_id: str, body: dict) -> dict[str, Any]:
        from backend.crm.stake_opportunity import (
            StakeOpportunityError,
            attach_stake_sentence,
        )
        sentence = str(body.get("stake_sentence") or "")
        operator_id = str(
            body.get("operator_id")
            or os.environ.get("SAMUS_OPERATOR")
            or "alex"
        )
        try:
            result = attach_stake_sentence(
                opportunity_id=opportunity_id,
                stake_sentence=sentence,
                operator_id=operator_id,
            )
        except StakeOpportunityError as exc:
            status_map = {
                "missing_opportunity_id": 400,
                "opportunity_not_found": 404,
                "already_staked": 409,
                "daily_cap_exhausted": 429,
                "budget_unavailable": 503,
                "guard_rejected": 422,
                "duplicate": 422,
                "persist_failed": 500,
                "budget_record_failed": 500,
            }
            raise HTTPException(
                status_code=status_map.get(exc.reason, 400),
                detail={"reason": exc.reason, "message": str(exc)},
            ) from exc
        from backend.crm import service as crm_service
        opp = crm_service.get_opportunity(opportunity_id)
        return {
            "stake_result": result,
            "opportunity": opp.model_dump() if opp is not None else None,
        }

    @router.get("/needs_warm_path", dependencies=[Depends(token_dep)])
    async def needs_warm_path_list(limit: int = 50) -> dict[str, Any]:
        """List prospects diverted by the G8 warmth pre-flight."""
        from backend.crm.needs_warm_path import list_pending
        try:
            limit_val = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit_val = 50
        items = list_pending(limit=limit_val)
        return {
            "count": len(items),
            "diverted": [r.model_dump() for r in items],
        }

    @router.post(
        "/needs_warm_path/{prospect_id}/promote",
        dependencies=[Depends(token_dep)],
    )
    async def needs_warm_path_promote(
        prospect_id: str, body: dict,
    ) -> dict[str, Any]:
        """Operator promotes a diverted prospect with a manual warmth signal."""
        from backend.crm.needs_warm_path import (
            NeedsWarmPathPersistError,
            promote,
        )
        signal_kind = str(body.get("signal_kind") or "").strip()
        signal_source = str(body.get("signal_source") or "").strip()
        operator_id = str(
            body.get("operator_id")
            or os.environ.get("SAMUS_OPERATOR")
            or "alex",
        )
        valid_kinds = {
            "rfp", "chamber_roster", "prior_inbound",
            "public_registry", "open_job_listing",
        }
        if signal_kind not in valid_kinds:
            raise HTTPException(
                status_code=400,
                detail={"reason": "invalid_signal_kind", "valid": sorted(valid_kinds)},
            )
        if not signal_source:
            raise HTTPException(
                status_code=400, detail={"reason": "missing_signal_source"},
            )
        try:
            record = promote(
                prospect_id,
                signal_kind=signal_kind,
                signal_source=signal_source,
                operator_id=operator_id,
            )
        except NeedsWarmPathPersistError as exc:
            msg = str(exc)
            if "not found" in msg:
                raise HTTPException(status_code=404, detail={"reason": "not_found"}) from exc
            raise HTTPException(
                status_code=500, detail={"reason": "persist_failed", "message": msg},
            ) from exc
        return {"promoted": record.model_dump()}

    @router.get("/codex/pending_adrs", dependencies=[Depends(token_dep)])
    async def codex_pending_adrs() -> dict[str, Any]:
        """List ADR drafts auto-generated by the Codex Validation Layer.

        Each draft represents a proposed action the validator blocked under
        a Codex rule (chapter 12). The operator must either author a real
        ADR (moving the file to docs/codex/_resolved/ and editing
        08_decisions_log.md) or modify the code to remove the violation.
        """
        from backend.common.codex.adr_drafter import _default_drafts_dir
        drafts_dir = _default_drafts_dir()
        if not drafts_dir.exists():
            return {"count": 0, "drafts": []}
        entries: list[dict[str, Any]] = []
        for path in sorted(drafts_dir.glob("ADR-*.draft.md")):
            try:
                stat = path.stat()
                entries.append({
                    "name": path.name,
                    "path": str(path),
                    "size_bytes": stat.st_size,
                    "mtime_iso": __import__("datetime").datetime.utcfromtimestamp(
                        stat.st_mtime,
                    ).isoformat() + "Z",
                })
            except OSError:
                continue
        return {"count": len(entries), "drafts": entries}

    @router.get("/apollo/budget", dependencies=[Depends(token_dep)])
    async def apollo_budget() -> dict[str, Any]:
        """G11 — today's Apollo daily-budget snapshot.

        Read-only view over :mod:`backend.common.apollo_budget`. Fail-OPEN
        on persistence (matches the budget module's own semantics): if
        the store is unreachable we still return a coherent payload with
        zeros + a ``store_status`` flag the console can show.
        """
        from backend.common.apollo_budget import get_daily_cap_usd, get_store
        store = get_store()
        cap = get_daily_cap_usd()
        try:
            snap = store.snapshot()
            today_spend = snap.dollars_today
            recent = [c.to_jsonable() for c in snap.recent_calls]
            store_status = "ok"
        except Exception as exc:  # noqa: BLE001 — fail-open on read
            _LOG.warning("apollo_budget snapshot unavailable: %s", exc)
            today_spend = 0.0
            recent = []
            store_status = f"unavailable: {exc!r}"
        return {
            "today_spend_usd": today_spend,
            "cap_usd": cap,
            "remaining_usd": max(0.0, cap - today_spend),
            "recent_calls": recent,
            "store_status": store_status,
        }

    @router.post(
        "/codex/drafts/{draft_name}/resolve",
        dependencies=[Depends(token_dep)],
    )
    async def codex_resolve_draft(draft_name: str, body: dict) -> dict[str, Any]:
        """Resolve an ADR draft as either ALLOW (move to _resolved/) or REJECT.

        Body: ``{"decision": "allow"|"reject", "rationale": "...",
        "operator": "alex"}``. On success the Codex registry is reloaded so
        any new banned-phrase / guardrail changes from the resolution take
        effect immediately.
        """
        from backend.common.codex import REGISTRY
        from backend.common.codex.exceptions import CodexUnavailable
        from backend.common.codex.resolution import _drafts_dir, resolve_draft
        decision = str(body.get("decision") or "").strip().lower()
        rationale = str(body.get("rationale") or "").strip()
        operator = body.get("operator")
        if decision not in ("allow", "reject"):
            raise HTTPException(
                status_code=400,
                detail="decision must be 'allow' or 'reject'",
            )
        if not rationale:
            raise HTTPException(status_code=400, detail="rationale required")
        if _unsafe_draft_name(draft_name):
            raise HTTPException(status_code=400, detail="invalid draft name")
        drafts_dir = _drafts_dir()
        draft_path = drafts_dir / draft_name
        if not draft_path.is_file():
            raise HTTPException(status_code=404, detail="draft not found")
        try:
            new_path = resolve_draft(
                draft_path,
                decision=decision,  # type: ignore[arg-type]
                rationale=rationale,
                operator=str(operator) if operator else None,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, FileExistsError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        try:
            REGISTRY.reload()
        except CodexUnavailable as exc:
            return {
                "resolved": True,
                "decision": decision,
                "new_path": str(new_path),
                "reload_warning": str(exc),
            }
        return {
            "resolved": True,
            "decision": decision,
            "new_path": str(new_path),
        }

    @router.post(
        "/codex/drafts/{draft_name}/promote",
        dependencies=[Depends(token_dep)],
    )
    async def codex_promote_draft(draft_name: str) -> dict[str, Any]:
        """Promote a resolved draft into ``08_decisions_log.md``.

        ``draft_name`` is expected to be the resolved filename inside
        ``docs/codex/_resolved/`` (e.g. ``ADR-012_<slug>.resolved.md``).
        Renumbers to the next sequential ADR id.
        """
        from backend.common.codex import REGISTRY
        from backend.common.codex.exceptions import CodexUnavailable
        from backend.common.codex.registry import _default_codex_dir
        from backend.common.codex.resolution import promote_to_decisions_log
        if _unsafe_draft_name(draft_name):
            raise HTTPException(status_code=400, detail="invalid draft name")
        resolved_path = _default_codex_dir() / "_resolved" / draft_name
        if not resolved_path.is_file():
            raise HTTPException(status_code=404, detail="resolved draft not found")
        try:
            new_id = promote_to_decisions_log(resolved_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        reload_warning: str | None = None
        try:
            REGISTRY.reload()
        except CodexUnavailable as exc:
            reload_warning = str(exc)
        result = {"promoted": True, "adr_id": new_id}
        if reload_warning is not None:
            result["reload_warning"] = reload_warning
        return result

    @router.post("/codex/reload", dependencies=[Depends(token_dep)])
    async def codex_reload() -> dict[str, Any]:
        """Re-parse docs/codex/ into the live registry (after edits).

        Use after editing a Codex chapter without restarting the workcell.
        Fail-CLOSED: if reload raises, the registry stays in its previous
        state (or, if previously failed, remains failed) — the response
        reports the error.
        """
        from backend.common.codex import REGISTRY
        from backend.common.codex.exceptions import CodexUnavailable
        try:
            REGISTRY.reload()
            return {
                "reloaded": True,
                "guardrails_loaded": len(REGISTRY.guardrails()),
                "adrs_loaded": len(REGISTRY.adrs()),
                "banned_phrases_loaded": len(REGISTRY.banned_phrases()),
            }
        except CodexUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": "codex_parse_failed", "message": str(exc)},
            ) from exc

    @router.post("/chats/{chat_id}/respond", dependencies=[Depends(token_dep)])
    async def respond(chat_id: str, body: dict) -> dict[str, Any]:
        chat = state.history.get_chat(chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="unknown chat")
        user_msg = str(body.get("content") or "").strip()
        if not user_msg:
            raise HTTPException(status_code=400, detail="content must be non-empty")
        state.history.append_message(chat_id=chat_id, role="user", content=user_msg)
        catalogue = state.catalogue()
        preset = catalogue.preset(chat.bag.preset_id)
        if preset is None:
            raise HTTPException(
                status_code=400,
                detail=f"chat references unknown preset {chat.bag.preset_id!r}",
            )
        assembler = PromptAssembler(library=catalogue.library, pool=catalogue.pool)
        ctx = EnrichmentContext(chat_id=chat_id, turn=chat.spice_state.turn)
        assembled = assembler.assemble(
            preset=preset, bag=chat.bag, context=ctx,
            variables={
                "ai_name": state.ai_display_name,
                "user_name": state.operator_display_name,
            },
            spice_state=chat.spice_state,
        )
        provider = state.model_backend
        try:
            response_text = provider.complete(user_msg) if provider is not None else f"[echo] {user_msg}"
        except Exception as exc:  # noqa: BLE001
            response_text = f"[echo error] {exc!r}"
        backend_name = getattr(provider, "name", "unknown") if provider is not None else "none"
        assistant_msg = state.history.append_message(
            chat_id=chat_id, role="assistant", content=response_text,
            metadata={
                "model_backend": backend_name,
                "assembled_char_count": assembled.char_count,
                "spice_turn": chat.spice_state.turn,
            },
        )
        state.history.update_chat(_patched(chat, spice_state=chat.spice_state))
        return {
            "assistant": assistant_msg.to_dict(),
            "assembled_prompt": {
                "text": assembled.text,
                "char_count": assembled.char_count,
            },
        }

    return router


def _patched(chat: StoredChat, **fields: Any) -> StoredChat:
    return StoredChat(
        chat_id=chat.chat_id,
        name=fields.get("name", chat.name),
        persona_id=fields.get("persona_id", chat.persona_id),
        bag=fields.get("bag", chat.bag),
        spice_state=fields.get("spice_state", chat.spice_state),
        created_at=chat.created_at,
        updated_at=chat.updated_at,
    )


__all__ = ["build_api_router", "build_console_router"]
