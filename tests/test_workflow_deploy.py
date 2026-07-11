"""Tests for backend.workflow.deploy — dormant + dry-run posture, fail-closed.

The httpx call is monkeypatched at the thin _http_post wrapper; no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import backend.workflow.deploy as deploy_mod
from backend.services.scope_planner import TaskPlan
from backend.workflow.compiler import compile_workflow
from backend.workflow.deploy import deploy_workflow


def _wf():
    return compile_workflow(
        TaskPlan(
            triggers=["form_submission"],
            actions=["post_to_slack"],
            notifications=[],
            tools=["slack"],
        ),
        name="Deploy",
    )


def _settings(**over):
    base = dict(
        workflow_n8n_deploy_enabled=False,
        workflow_n8n_dry_run=True,
        n8n_base_url="",
        n8n_api_key="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_deploy_disabled_is_dormant(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        deploy_mod, "_http_post", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )
    res = deploy_workflow(_wf(), settings=_settings(workflow_n8n_deploy_enabled=False))
    assert res == {"status": "disabled", "deployed": False}
    assert called["n"] == 0


def test_deploy_dry_run_makes_no_call(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        deploy_mod, "_http_post", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )
    res = deploy_workflow(
        _wf(), settings=_settings(workflow_n8n_deploy_enabled=True, workflow_n8n_dry_run=True)
    )
    assert res["status"] == "dry_run"
    assert called["n"] == 0


def test_deploy_armed_without_creds_fails_closed():
    res = deploy_workflow(
        _wf(),
        settings=_settings(
            workflow_n8n_deploy_enabled=True,
            workflow_n8n_dry_run=False,
            n8n_base_url="",
            n8n_api_key="",
        ),
    )
    assert res["status"] == "no_credentials"
    assert res["deployed"] is False


def test_deploy_live_posts_and_returns_id(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 201

        def json(self):
            return {"id": "wf_42"}

    def fake_post(url, *, json, headers):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(deploy_mod, "_http_post", fake_post)
    res = deploy_workflow(
        _wf(),
        settings=_settings(
            workflow_n8n_deploy_enabled=True,
            workflow_n8n_dry_run=False,
            n8n_base_url="https://n8n.example.com/",
            n8n_api_key="k",
        ),
    )
    assert res == {"status": "ok", "deployed": True, "workflow_id": "wf_42"}
    assert captured["url"] == "https://n8n.example.com/api/v1/workflows"
    assert captured["headers"]["X-N8N-API-KEY"] == "k"
    # Read-only fields are not posted.
    assert "active" not in captured["payload"]
    assert set(captured["payload"]) == {"name", "nodes", "connections", "settings"}


def test_deploy_transport_error_is_caught(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(deploy_mod, "_http_post", boom)
    res = deploy_workflow(
        _wf(),
        settings=_settings(
            workflow_n8n_deploy_enabled=True,
            workflow_n8n_dry_run=False,
            n8n_base_url="https://n8n.example.com",
            n8n_api_key="k",
        ),
    )
    assert res["status"] == "transport_error"
    assert res["deployed"] is False
