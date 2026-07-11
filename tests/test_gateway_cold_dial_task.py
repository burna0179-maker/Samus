"""Tests for backend.gateway.cold_dial_task — the in-container cold-dial driver.

Two layers, same split the module uses:

  * ``should_fire_now`` — the PURE gate reasoning. Each gate pinned so a future
    edit that drops the arm / attestation / window / cap check fails loudly.
  * ``run_once`` / ``_run_dial_pass`` — the impure tick. The gateway container
    holds NO Vapi credentials (per-workcell secret isolation), so the dial is
    DELEGATED to the samus-voice workcell over the signed mesh
    (``POST /voice/dial_call_list``). The headline test drives one tick with an
    injected executor (no running voice service) and asserts that, when armed +
    attested + in-window, the loop delegates ONE live (``dry_run=false``) pass
    and reports ``initiated>0``.
"""
from __future__ import annotations

import asyncio
import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from backend.gateway import cold_dial_task as cdt
from backend.prospecting.csv_export import CSV_COLUMNS


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def artifact_root(tmp_path, monkeypatch):
    """Point storage.root() (call list, attestation, dial-run ledgers) at tmp."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    return tmp_path


def _clear_env(monkeypatch):
    for k in (cdt.ENV_ENABLED, cdt.ENV_HOUR_START, cdt.ENV_HOUR_END,
              cdt.ENV_MAX_CALLS, cdt.ENV_DAILY_CAP, cdt.ENV_DELAY, cdt.ENV_PHONE_IDS):
        monkeypatch.delenv(k, raising=False)


def _weekday_in_window() -> datetime:
    """A Tuesday at 10:00 — a business day inside the 8-21 dial window."""
    dt = datetime(2026, 7, 7, 10, 0)
    assert dt.weekday() < 5  # self-check: 2026-07-07 IS a weekday (Tue)
    return dt


def _saturday_in_window() -> datetime:
    dt = datetime(2026, 7, 11, 10, 0)
    assert dt.weekday() >= 5  # self-check: 2026-07-11 IS a weekend (Sat)
    return dt


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        w.writeheader()
        for row in rows:
            w.writerow({col: row.get(col, "") for col in CSV_COLUMNS})


def _prospect_row(**overrides) -> dict:
    base = {
        "prospect_id": "p_001",
        "company_name": "Acme Co",
        "phone": "5305551234",
        "state": "CA",
        "call_priority": "hot",
        "lead_score": "80",
        "seo_score": "30",
        "industry": "plumbing",
    }
    base.update(overrides)
    return base


def _write_todays_call_list(root: Path, rows: list[dict]) -> None:
    """Write today's call list under BOTH the Pacific business date and the UTC
    date name, so cdt._resolve_call_list_path finds it whenever the test runs."""
    d = root / "daily_calls"
    names = {date.today().isoformat()}
    try:
        from backend.common.us_timezones import business_today
        names.add(business_today().isoformat())
    except Exception:  # noqa: BLE001
        pass
    for name in names:
        _write_csv(d / f"call_list_{name}.csv", rows)


def _base_kwargs(**over):
    kw = dict(
        armed=True, attested=True, now_local=_weekday_in_window(),
        dials_today=0, call_list_present=True,
        hour_start=8, hour_end=21, daily_cap=40,
    )
    kw.update(over)
    return kw


def _voice_result(*, dry_run: bool = False, initiated: int = 1) -> dict:
    """A DialRunResult-shaped dict, as samus-voice's /voice/dial_call_list returns."""
    return {
        "run_id": "dial_run_20260707T120000Z",
        "ts": "2026-07-07T12:00:00Z",
        "csv_path": "/opt/samus/data/artifacts/daily_calls/call_list_2026-07-07.csv",
        "config": {"dry_run": dry_run},
        "eligible_count": 1,
        "initiated_count": initiated,
        "skipped_count": 0,
        "error_count": 0,
        "attempts": [],
    }


# ---------------------------------------------------------------------------
# should_fire_now — pure gate reasoning
# ---------------------------------------------------------------------------

def test_should_fire_happy_path(monkeypatch):
    _clear_env(monkeypatch)
    fire, reason = cdt.should_fire_now(**_base_kwargs())
    assert fire is True, reason
    assert "in dial window" in reason


def test_should_fire_blocks_when_loop_disabled(monkeypatch):
    monkeypatch.setenv(cdt.ENV_ENABLED, "0")
    fire, reason = cdt.should_fire_now(**_base_kwargs())
    assert fire is False
    assert "disabled" in reason


def test_should_fire_blocks_when_disarmed(monkeypatch):
    _clear_env(monkeypatch)
    fire, reason = cdt.should_fire_now(**_base_kwargs(armed=False))
    assert fire is False
    assert "disarmed" in reason


def test_should_fire_blocks_when_not_attested(monkeypatch):
    _clear_env(monkeypatch)
    fire, reason = cdt.should_fire_now(**_base_kwargs(attested=False))
    assert fire is False
    assert "attested" in reason


def test_should_fire_blocks_on_weekend(monkeypatch):
    _clear_env(monkeypatch)
    fire, reason = cdt.should_fire_now(**_base_kwargs(now_local=_saturday_in_window()))
    assert fire is False
    assert "weekend" in reason


def test_should_fire_blocks_before_window(monkeypatch):
    _clear_env(monkeypatch)
    early = datetime(2026, 7, 7, 6, 0)  # Tue 06:00 < 8
    fire, reason = cdt.should_fire_now(**_base_kwargs(now_local=early))
    assert fire is False
    assert "before dial window" in reason


def test_should_fire_blocks_after_window(monkeypatch):
    _clear_env(monkeypatch)
    late = datetime(2026, 7, 7, 21, 0)  # Tue 21:00 >= 21 (end exclusive)
    fire, reason = cdt.should_fire_now(**_base_kwargs(now_local=late))
    assert fire is False
    assert "after dial window" in reason


def test_should_fire_blocks_at_daily_cap(monkeypatch):
    _clear_env(monkeypatch)
    fire, reason = cdt.should_fire_now(**_base_kwargs(dials_today=40, daily_cap=40))
    assert fire is False
    assert "daily dial cap" in reason


def test_should_fire_blocks_when_no_call_list(monkeypatch):
    _clear_env(monkeypatch)
    fire, reason = cdt.should_fire_now(**_base_kwargs(call_list_present=False))
    assert fire is False
    assert "call_list" in reason


def test_should_fire_custom_window_env(monkeypatch):
    """Window knobs override the 8-21 default when the explicit args are None."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(cdt.ENV_HOUR_START, "9")
    monkeypatch.setenv(cdt.ENV_HOUR_END, "17")
    at8 = datetime(2026, 7, 7, 8, 0)   # below the custom 9 floor
    fire, reason = cdt.should_fire_now(**_base_kwargs(now_local=at8, hour_start=None, hour_end=None))
    assert fire is False
    assert "before dial window" in reason


# ---------------------------------------------------------------------------
# Gate inputs — the impure reads
# ---------------------------------------------------------------------------

def test_production_armed_reads_idle_drive_flag(monkeypatch):
    """The cold-dial arm IS the idle-drive arm — one flag, no parallel system."""
    from backend.common.settings import reload_settings

    monkeypatch.setenv("SAMUS_IDLE_PRODUCTION_DRIVE_ENABLED", "1")
    reload_settings()
    assert cdt._production_armed() is True

    monkeypatch.setenv("SAMUS_IDLE_PRODUCTION_DRIVE_ENABLED", "0")
    reload_settings()
    assert cdt._production_armed() is False


def test_dials_today_counts_only_initiated(monkeypatch, artifact_root):
    from backend.common import storage
    d = storage.root() / "voice" / "dial_runs"
    d.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    (d / f"dial_run_{today}T120000Z.json").write_text(json.dumps({
        "attempts": [
            {"outcome": "initiated"},
            {"outcome": "initiated"},
            {"outcome": "dry_run"},       # not a live dial
            {"outcome": "skipped_hours"},  # not a live dial
        ],
    }), encoding="utf-8")
    assert cdt._dials_today() == 2


def test_dials_today_zero_on_empty(artifact_root):
    assert cdt._dials_today() == 0


def test_resolve_call_list_path(monkeypatch, artifact_root):
    assert cdt._resolve_call_list_path() is None
    _write_todays_call_list(artifact_root, [_prospect_row()])
    p = cdt._resolve_call_list_path()
    assert p is not None
    assert p.name.startswith("call_list_") and p.suffix == ".csv"


def test_build_config_is_live_and_clamps(monkeypatch):
    _clear_env(monkeypatch)
    cfg = cdt._build_config()
    assert cfg.dry_run is False               # the whole point of the loop
    assert cfg.only_priorities == ["hot", "warm"]
    assert cfg.call_hours_start == 8 and cfg.call_hours_end == 21
    monkeypatch.setenv(cdt.ENV_MAX_CALLS, "999")
    assert cdt._build_config().max_calls == 50  # clamped to the dialer's ceiling


# ---------------------------------------------------------------------------
# The dial pass — DELEGATE to samus-voice over the signed mesh
# ---------------------------------------------------------------------------

def test_dial_via_voice_posts_to_voice_endpoint(monkeypatch):
    """_dial_via_voice signs a POST to voice's /voice/dial_call_list carrying the
    config, and returns the DialRunResult dict the endpoint replies with."""
    from backend.voice.models import DialerConfig

    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return _voice_result(dry_run=False, initiated=3)

    def _fake_signed_post(base_url, path, payload, *, timeout=None, **kw):
        captured["base_url"] = base_url
        captured["path"] = path
        captured["payload"] = payload
        return _Resp()

    import backend.common.http_client as hc
    monkeypatch.setattr(hc, "signed_post_json_sync", _fake_signed_post)
    monkeypatch.setenv("VOICE_URL", "http://samus-voice:8080")

    out = cdt._dial_via_voice(
        Path("/opt/samus/data/artifacts/daily_calls/call_list_2026-07-07.csv"),
        DialerConfig(dry_run=False, max_calls=3, delay_between_calls_s=0.0),
    )
    assert captured["base_url"] == "http://samus-voice:8080"
    assert captured["path"] == "/voice/dial_call_list"
    assert captured["payload"]["config"]["dry_run"] is False
    assert captured["payload"]["csv_path"].endswith("call_list_2026-07-07.csv")
    assert out["initiated_count"] == 3


def test_dial_via_voice_raises_on_http_error(monkeypatch):
    """A non-2xx from voice raises so the caller can degrade the tick."""
    from backend.voice.models import DialerConfig

    class _Resp:
        status_code = 503
        text = "vapi down"

        def json(self):  # pragma: no cover
            return {}

    import backend.common.http_client as hc
    monkeypatch.setattr(hc, "signed_post_json_sync", lambda *a, **k: _Resp())
    monkeypatch.setenv("VOICE_URL", "http://samus-voice:8080")
    with pytest.raises(RuntimeError):
        cdt._dial_via_voice(Path("/x/call_list.csv"), DialerConfig(dry_run=False))


def test_voice_url_prefers_env_then_falls_back_to_convention(monkeypatch):
    """Regression: the gateway does NOT wire a voice URL (voice isn't in its
    gateway_urls), so _voice_url must FALL BACK to the compose-network convention
    http://samus-voice:8080 instead of raising — an explicit VOICE_URL still wins."""
    # explicit env wins
    monkeypatch.setenv("VOICE_URL", "http://custom-voice:9000")
    assert cdt._voice_url() == "http://custom-voice:9000"

    # no env + settings.gateway_urls has no 'voice' key -> the convention
    monkeypatch.delenv("VOICE_URL", raising=False)
    import backend.common.config as cfg

    class _S:
        gateway_urls = {"prospecting": "http://samus-prospecting:8080"}  # no 'voice'

    def _fake_get_settings():
        return _S()
    # keep the conftest's reload_settings() teardown happy (it calls .cache_clear())
    _fake_get_settings.cache_clear = lambda: None
    monkeypatch.setattr(cfg, "get_settings", _fake_get_settings)
    assert cdt._voice_url() == "http://samus-voice:8080"


# ---------------------------------------------------------------------------
# run_once — the acceptance criterion + fail-closed holds
# ---------------------------------------------------------------------------

def test_run_once_delegates_live_dial_to_voice(monkeypatch, artifact_root):
    """ACCEPTANCE: a control-tick in the window, armed + attested, with today's
    call list, delegates ONE live (dry_run=false) dial pass to voice and reports
    initiated>0. The executor is injected so no voice service is needed."""
    _clear_env(monkeypatch)
    monkeypatch.setattr(cdt, "_production_armed", lambda: True)
    from backend.common.preshift_attestation import write_attestation
    assert write_attestation(briefing_id="test-cold-dial") is not None
    _write_todays_call_list(artifact_root, [_prospect_row()])

    captured = {}

    def _executor(csv_path, config):
        captured["csv_path"] = csv_path
        captured["dry_run"] = config.dry_run
        return _voice_result(dry_run=config.dry_run, initiated=2)

    out = cdt.run_once(now_local=_weekday_in_window(), executor=_executor)

    assert out["fired"] is True, out
    assert out["dry_run"] is False, out          # the loop dials LIVE
    assert out["initiated"] == 2, out
    assert captured["dry_run"] is False           # the config handed to voice is live
    assert str(captured["csv_path"]).endswith(".csv")


def test_run_once_holds_when_disarmed(monkeypatch, artifact_root):
    _clear_env(monkeypatch)
    monkeypatch.setattr(cdt, "_production_armed", lambda: False)
    from backend.common.preshift_attestation import write_attestation
    write_attestation(briefing_id="t")
    _write_todays_call_list(artifact_root, [_prospect_row()])
    out = cdt.run_once(now_local=_weekday_in_window())
    assert out["fired"] is False
    assert "disarmed" in out["reason"]


def test_run_once_holds_when_not_attested(monkeypatch, artifact_root):
    _clear_env(monkeypatch)
    monkeypatch.setattr(cdt, "_production_armed", lambda: True)
    # no attestation written for today
    _write_todays_call_list(artifact_root, [_prospect_row()])
    out = cdt.run_once(now_local=_weekday_in_window())
    assert out["fired"] is False
    assert "attested" in out["reason"]


def test_run_once_holds_when_no_call_list(monkeypatch, artifact_root):
    _clear_env(monkeypatch)
    monkeypatch.setattr(cdt, "_production_armed", lambda: True)
    from backend.common.preshift_attestation import write_attestation
    write_attestation(briefing_id="t")
    # no call list written
    out = cdt.run_once(now_local=_weekday_in_window())
    assert out["fired"] is False
    assert "call_list" in out["reason"]


# ---------------------------------------------------------------------------
# Lifespan hook — respects the master switch at start
# ---------------------------------------------------------------------------

def test_start_loop_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv(cdt.ENV_ENABLED, "0")

    class _State:
        pass

    class _App:
        state = _State()

    task = asyncio.run(cdt.start_cold_dial_loop(_App()))
    assert task is None
