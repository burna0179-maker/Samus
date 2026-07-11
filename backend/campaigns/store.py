"""Durable persistence for :class:`CampaignRun` state.

Mirrors the ``backend.common.approvals`` storage idiom: a DDB table
(``samus_campaign_runs``, PK=campaign_id) attempted first when configured, with
an always-mirrored JSON-file fallback under the state root so a dev box / test
run needs no AWS. Writes never raise — a persistence fault degrades to the JSON
mirror and logs, it does not sink the orchestrator.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from backend.common.state_paths import state_path

from .models import CampaignRun

_LOG = logging.getLogger("samus.campaigns.store")
_LOCK = threading.Lock()

_PATH_ENV = "SAMUS_CAMPAIGN_RUNS_PATH"
_DDB_TABLE_ENV = "DDB_CAMPAIGN_RUNS_TABLE"


def _json_path() -> str:
    override = os.getenv(_PATH_ENV, "").strip()
    if override:
        return override
    return str(state_path("campaigns", "runs.json"))


def _load_file() -> dict[str, dict[str, Any]]:
    path = _json_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError) as exc:
        _LOG.warning("campaign runs load failed: %s", exc)
        return {}


def _save_file(data: dict[str, dict[str, Any]]) -> bool:
    path = _json_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, path)
        return True
    except (OSError, ValueError) as exc:
        _LOG.warning("campaign runs save failed: %s", exc)
        return False


def _ddb_table() -> Any | None:
    try:
        from backend.common import aws
        from backend.common.config import get_settings

        table_name = os.getenv(_DDB_TABLE_ENV, "").strip()
        if not table_name:
            return None
        return aws.table(table_name, get_settings().aws_region)
    except Exception as exc:  # noqa: BLE001 — fallback path
        _LOG.debug("campaign runs ddb table unavailable: %s", exc)
        return None


def _ddb_put(item: dict[str, Any]) -> bool:
    table = _ddb_table()
    if table is None:
        return False
    try:
        table.put_item(Item=item)
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("campaign runs ddb put failed: %s", exc)
        return False


class CampaignRunStore:
    """Read/write persistence for campaign runs."""

    def save(self, run: CampaignRun) -> None:
        """Write-through: DDB when reachable, ALWAYS mirrored to the JSON file."""
        run.touch()
        row = json.loads(run.model_dump_json())
        with _LOCK:
            data = _load_file()
            data[run.campaign_id] = row
            wrote_json = _save_file(data)
        wrote_ddb = _ddb_put(row)
        if not wrote_json and not wrote_ddb:
            _LOG.warning("campaign run %s not persisted anywhere", run.campaign_id)

    def get(self, campaign_id: str) -> CampaignRun | None:
        with _LOCK:
            row = _load_file().get(campaign_id)
        if row is None:
            return None
        try:
            return CampaignRun.model_validate(row)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("campaign run %s failed to deserialize: %s", campaign_id, exc)
            return None

    def list_runs(self) -> list[CampaignRun]:
        with _LOCK:
            rows = list(_load_file().values())
        out: list[CampaignRun] = []
        for row in rows:
            try:
                out.append(CampaignRun.model_validate(row))
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda r: r.created_at)
        return out
