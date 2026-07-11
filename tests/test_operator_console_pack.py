"""Standalone smoke for Samus's operator_console pack."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.packs.operator_console.history import ConsoleHistory
from backend.packs.operator_console.pod import register
from backend.standard.chat import (
    PromptPieceKind,
    PromptPieceLibrary,
    ScenarioPreset,
    SpicePool,
)
from backend.standard.chat.catalogues import (
    save_prompt_pieces,
    save_scenario_presets,
    save_spice_pool,
)
from backend.standard.persona import Persona, PersonaManager


@pytest.fixture
def seeded_app(tmp_path: Path):
    prompts_root = tmp_path / "identity" / "prompts"
    prompts_root.mkdir(parents=True)

    library = PromptPieceLibrary(
        pieces={
            PromptPieceKind.CHARACTER: {"core": "You are {ai_name}, Samus's operator console."},
            PromptPieceKind.LOCATION: {"console": "Local console session."},
        }
    )
    preset = ScenarioPreset(
        preset_id="samus_console",
        character="core",
        location="console",
        spice_category="default",
    )
    pool = SpicePool(categories={"default": ["Stay aligned."]})

    save_prompt_pieces(prompts_root / "prompt_pieces.json", library)
    save_scenario_presets(prompts_root / "scenario_presets.json", {preset.preset_id: preset})
    save_spice_pool(prompts_root / "prompt_spices.json", pool)

    personas_path = tmp_path / "identity" / "personas" / "personas.json"
    persona_mgr = PersonaManager(personas_path)
    persona_mgr.upsert(
        Persona(
            persona_id="samus_console",
            display_name="Samus Console",
            tagline="Operator console for the commerce agent.",
            default_bag={"preset_id": "samus_console", "spice_enabled": True},
        )
    )

    history = ConsoleHistory(tmp_path / "history.db")

    app = FastAPI()
    register(
        app,
        personas=persona_mgr,
        history=history,
        enrichment_root=tmp_path / "identity",
        api_token="",  # auth disabled for tests
        enabled=True,
    )
    return app


def test_state_endpoint_returns_catalogue_summary(seeded_app):
    client = TestClient(seeded_app)
    r = client.get("/api/console/state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ai_name"] == "Samus"
    assert body["default_persona"] == "samus_console"
    assert body["persona_count"] == 1
    assert "samus_console" in body["preset_ids"]


def test_personas_list_includes_seed(seeded_app):
    client = TestClient(seeded_app)
    r = client.get("/api/console/personas")
    assert r.status_code == 200
    ids = [p["persona_id"] for p in r.json()["personas"]]
    assert "samus_console" in ids


def test_chat_lifecycle_end_to_end(seeded_app):
    client = TestClient(seeded_app)
    chat = client.post(
        "/api/console/chats", json={"name": "smoke", "persona_id": "samus_console"}
    ).json()
    chat_id = chat["chat_id"]

    r = client.post(f"/api/console/chats/{chat_id}/assemble")
    assert r.status_code == 200
    assert "Samus" in r.json()["text"]

    r = client.post(
        f"/api/console/chats/{chat_id}/respond",
        json={"content": "what is the current state?"},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["assistant"]["role"] == "assistant"
    assert "[echo" in out["assistant"]["content"]


def test_console_shell_renders_html(seeded_app):
    client = TestClient(seeded_app)
    r = client.get("/console")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "Samus Operator Console" in r.text


def test_unknown_persona_returns_400_on_create(seeded_app):
    client = TestClient(seeded_app)
    r = client.post("/api/console/chats", json={"name": "x", "persona_id": "ghost"})
    assert r.status_code == 400


def test_lm_studio_backend_auto_selected_from_env(tmp_path: Path, monkeypatch):
    """register() auto-picks LMStudioBackend when env vars set; LocalEcho otherwise.

    No network call -- only assert on the type of state.model_backend.
    """
    prompts_root = tmp_path / "identity" / "prompts"
    prompts_root.mkdir(parents=True)
    save_prompt_pieces(prompts_root / "prompt_pieces.json", PromptPieceLibrary())
    save_scenario_presets(prompts_root / "scenario_presets.json", {})
    save_spice_pool(prompts_root / "prompt_spices.json", SpicePool())
    pm = PersonaManager(tmp_path / "identity" / "personas" / "personas.json")
    history = ConsoleHistory(tmp_path / "history.db")

    # Case 1: env unset -> _LocalEchoBackend
    monkeypatch.delenv("SN_LM_STUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("SN_LM_STUDIO_MODEL", raising=False)
    monkeypatch.delenv("SN_LMSTUDIO_URL", raising=False)
    monkeypatch.delenv("SN_LMSTUDIO_MODEL", raising=False)
    monkeypatch.delenv("SS_LMSTUDIO_URL", raising=False)
    monkeypatch.delenv("SS_LMSTUDIO_MODEL", raising=False)
    app1 = FastAPI()
    register(
        app1, personas=pm, history=history, enrichment_root=tmp_path / "identity", api_token=""
    )
    assert type(app1.state.operator_console.model_backend).__name__ == "_LocalEchoBackend"

    # Case 2: SN_LM_STUDIO_BASE_URL + SN_LM_STUDIO_MODEL -> LMStudioBackend
    monkeypatch.setenv("SN_LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("SN_LM_STUDIO_MODEL", "google/gemma-4-e4b")
    app2 = FastAPI()
    register(
        app2, personas=pm, history=history, enrichment_root=tmp_path / "identity", api_token=""
    )
    assert type(app2.state.operator_console.model_backend).__name__ == "LMStudioBackend"

    # Case 3: SS_LMSTUDIO_URL + SS_LMSTUDIO_MODEL (Major-flavoured convention)
    monkeypatch.delenv("SN_LM_STUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("SN_LM_STUDIO_MODEL", raising=False)
    monkeypatch.setenv("SS_LMSTUDIO_URL", "http://127.0.0.1:1234/v1/chat/completions")
    monkeypatch.setenv("SS_LMSTUDIO_MODEL", "google/gemma-4-e4b")
    app3 = FastAPI()
    register(
        app3, personas=pm, history=history, enrichment_root=tmp_path / "identity", api_token=""
    )
    assert type(app3.state.operator_console.model_backend).__name__ == "LMStudioBackend"


def test_bearer_required_when_token_set(tmp_path: Path):
    """Sanity check that the auth gate flips on when api_token is set."""
    prompts_root = tmp_path / "identity" / "prompts"
    prompts_root.mkdir(parents=True)
    save_prompt_pieces(prompts_root / "prompt_pieces.json", PromptPieceLibrary())
    save_scenario_presets(prompts_root / "scenario_presets.json", {})
    save_spice_pool(prompts_root / "prompt_spices.json", SpicePool())
    pm = PersonaManager(tmp_path / "identity" / "personas" / "personas.json")
    history = ConsoleHistory(tmp_path / "history.db")
    app = FastAPI()
    register(
        app,
        personas=pm,
        history=history,
        enrichment_root=tmp_path / "identity",
        api_token="secret-token",
    )
    client = TestClient(app)
    # No bearer -- 401
    assert client.get("/api/console/state").status_code == 401
    # Wrong bearer -- 403
    assert (
        client.get("/api/console/state", headers={"Authorization": "Bearer wrong"}).status_code
        == 403
    )
    # Correct bearer -- 200
    assert (
        client.get(
            "/api/console/state", headers={"Authorization": "Bearer secret-token"}
        ).status_code
        == 200
    )
