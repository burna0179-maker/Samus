"""Smoke tests for backend.prospecting.app."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.prospecting.models import DiscoveryResult


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.prospecting import app as p_app
    from backend.prospecting import service as p_service

    fake_result = DiscoveryResult(
        campaign_name="test_campaign",
        prospect_count=3,
        csv_path="/tmp/fake.csv",
        prospects=[],
        cache_hit=False,
    )
    fake = MagicMock(return_value=fake_result)
    monkeypatch.setattr(p_service, "process_discovery", fake)
    monkeypatch.setattr(p_app, "process_discovery", fake)

    return TestClient(p_app.app), fake


def test_post_work_routes_to_process_discovery(client):
    test_client, fake = client
    envelope = {
        "task_id": "t-100",
        "payload": {
            "campaign_name": "test_campaign",
            "zipcodes": ["95993"],
            "industries": ["finance"],
        },
        "metadata": {},
    }
    resp = test_client.post("/work", json=envelope)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_name"] == "test_campaign"
    assert body["prospect_count"] == 3
    fake.assert_called_once()
    args, kwargs = fake.call_args
    assert kwargs["task_id"] == "t-100"


def test_post_discover_accepts_direct_request(client):
    test_client, fake = client
    req = {
        "campaign_name": "test_campaign",
        "zipcodes": ["95993"],
        "industries": ["roofing"],
    }
    resp = test_client.post("/discover", json=req)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_name"] == "test_campaign"
    fake.assert_called_once()


def test_post_work_rejects_invalid_envelope(client):
    test_client, _ = client
    # missing task_id field
    resp = test_client.post("/work", json={"payload": {"zipcodes": ["95993"]}})
    assert resp.status_code == 400


def test_post_discover_rejects_invalid_payload(client):
    test_client, _ = client
    # zipcodes is required min_length=1
    resp = test_client.post("/discover", json={"zipcodes": []})
    assert resp.status_code == 400
