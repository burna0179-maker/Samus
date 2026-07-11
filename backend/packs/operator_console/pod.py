"""Pod registration -- wires operator_console into a Samus FastAPI app."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.standard.chat import EnrichmentCatalogue
from backend.standard.persona import PersonaManager, load_persona_manager

from .history import ConsoleHistory

__tier__ = "PACKS"

DEFAULT_AI_DISPLAY_NAME = "Samus"
DEFAULT_OPERATOR_DISPLAY_NAME = "operator"
DEFAULT_PERSONA = "samus_console"
DEFAULT_DATA_ROOT = "/opt/samus/data"


def _data_root() -> Path:
    return Path(os.environ.get("SAMUS_DATA_ROOT", DEFAULT_DATA_ROOT))


@dataclass
class OperatorConsoleState:
    personas: PersonaManager
    history: ConsoleHistory
    model_backend: Any | None
    catalogue_root: Path
    ai_display_name: str
    operator_display_name: str
    default_persona: str
    api_token: str  # empty -> auth disabled

    def catalogue(self) -> EnrichmentCatalogue:
        return EnrichmentCatalogue.load(self.catalogue_root)


class _LocalEchoBackend:
    name = "local_echo"

    def complete(self, prompt: str) -> str:
        return f"[echo:{self.name}] {prompt}"


class LMStudioBackend:
    """OpenAI-compatible HTTP client targeting LM Studio /v1/chat/completions.

    Auto-selected by `register(app)` when SN_LM_STUDIO_BASE_URL / SS_LMSTUDIO_URL
    (or compatible) is set in the env. Falls back to LocalEcho when unreachable.
    """

    name = "lm_studio"

    def __init__(self, *, endpoint: str, model: str, timeout_s: int = 30) -> None:
        self._endpoint = endpoint
        self._model = model
        self._timeout = timeout_s

    def complete(self, prompt: str) -> str:
        import json
        import urllib.request
        import urllib.error

        body = json.dumps(
            {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 600,
                "stream": False,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            return f"[lm_studio unreachable: {exc!r}]"
        except (ValueError, KeyError) as exc:
            return f"[lm_studio parse error: {exc!r}]"
        choices = (data or {}).get("choices") or []
        if not choices:
            return "[lm_studio: empty response]"
        return choices[0].get("message", {}).get("content", "") or "[lm_studio: empty content]"


def register(
    app: Any,
    *,
    personas: PersonaManager | None = None,
    history: ConsoleHistory | None = None,
    enrichment_root: str | Path | None = None,
    history_db_path: str | Path | None = None,
    model_backend: Any | None = None,
    ai_display_name: str | None = None,
    operator_display_name: str | None = None,
    default_persona: str | None = None,
    api_token: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Mount the console UI + JSON API into a Samus FastAPI app."""
    data_root = _data_root()

    enrichment_root_path = Path(enrichment_root) if enrichment_root else data_root / "identity"
    prompts_root = enrichment_root_path / "prompts"
    history_path = (
        Path(history_db_path) if history_db_path else data_root / "operator_console" / "history.db"
    )

    if personas is None:
        personas = load_persona_manager(enrichment_root_path)
    if history is None:
        history = ConsoleHistory(history_path)
    if model_backend is None:
        backend = getattr(getattr(app, "state", None), "operator_console_backend", None)
        if backend is None:
            # Auto-select LM Studio when env-configured. Recognises every
            # naming convention used across the ecosystem: SN_LM_STUDIO_* /
            # SN_LMSTUDIO_* / SS_LMSTUDIO_* / SAMUS_LM_STUDIO_*.
            # Empty/unset -> LocalEcho.
            lm_endpoint = (
                os.environ.get("SN_LM_STUDIO_BASE_URL", "").rstrip("/")
                or os.environ.get("SN_LMSTUDIO_URL", "")
                or os.environ.get("SS_LMSTUDIO_URL", "")
                or os.environ.get("SAMUS_LM_STUDIO_URL", "")
            )
            lm_model = (
                os.environ.get("SN_LM_STUDIO_MODEL")
                or os.environ.get("SN_LMSTUDIO_MODEL")
                or os.environ.get("SS_LMSTUDIO_MODEL")
                or os.environ.get("SAMUS_LM_STUDIO_MODEL")
                or "local"
            )
            if lm_endpoint:
                # SN_LM_STUDIO_BASE_URL is the /v1 root (Darwin convention);
                # the other two are full chat/completions endpoints. Append
                # the rest of the path when needed.
                if lm_endpoint.endswith("/v1"):
                    lm_endpoint = lm_endpoint + "/chat/completions"
                backend = LMStudioBackend(endpoint=lm_endpoint, model=lm_model)
            else:
                backend = _LocalEchoBackend()
        model_backend = backend

    state = OperatorConsoleState(
        personas=personas,
        history=history,
        model_backend=model_backend,
        catalogue_root=prompts_root,
        ai_display_name=ai_display_name or DEFAULT_AI_DISPLAY_NAME,
        operator_display_name=operator_display_name or DEFAULT_OPERATOR_DISPLAY_NAME,
        default_persona=default_persona or DEFAULT_PERSONA,
        api_token=api_token
        if api_token is not None
        else os.environ.get("SAMUS_OPERATOR_TOKEN", ""),
    )

    from .routes import build_api_router, build_console_router  # noqa: PLC0415

    console_router = build_console_router(state)
    api_router = build_api_router(state)

    if enabled and hasattr(app, "include_router"):
        app.include_router(console_router)
        app.include_router(api_router)

    app_state = getattr(app, "state", None)
    if app_state is not None:
        app_state.operator_console = state

    return {
        "pods": {
            "operator_console": state,
            "operator_console_history": history,
        },
        "console_router": console_router,
        "api_router": api_router,
        "state": state,
    }


__all__ = [
    "OperatorConsoleState",
    "register",
    "DEFAULT_AI_DISPLAY_NAME",
    "DEFAULT_OPERATOR_DISPLAY_NAME",
    "DEFAULT_PERSONA",
]
