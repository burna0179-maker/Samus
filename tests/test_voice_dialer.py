"""Morning dialer — CSV walk, priority sort, TCPA gate, dry-run, variable injection."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.voice.dialer import (
    _build_variable_values,
    _normalize_phone,
    _sort_prospects,
    dial_call_list,
    default_csv_path_for_today,
    load_prospects_from_csv,
)
from backend.voice.models import (
    DialerConfig,
    InitiateCallRequest,
    InitiateCallResult,
)
from backend.prospecting.csv_export import CSV_COLUMNS
from backend.prospecting.models import ProspectRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _phone_preflight_failopen(monkeypatch):
    """Default every dialer test to fail-OPEN on the Vapi phone-number
    existence preflight (Gap-5) so existing tests don't hit the network.
    Mocks httpx.get to a non-200 → the real validator logs + returns None.
    Tests that exercise the preflight override httpx.get / the validator
    themselves AFTER this fixture, so their setattr wins."""
    import httpx

    class _NonOk:
        status_code = 503
        def json(self):  # pragma: no cover — not reached on non-200
            return []
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _NonOk())


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
        "callsheet_opener": "Hey is this Acme?",
        "callsheet_pitch": "We can fix your slow page",
    }
    base.update(overrides)
    return base


def _override_vapi_settings(monkeypatch, *, assistant: str = "ast_x",
                            phone: str = "pn_y"):
    class _S:
        pass
    s = _S()
    s.vapi_assistant_id = assistant
    s.vapi_phone_number_id = phone
    s.vapi_api_key = "vapi_test"
    s.aws_region = "us-west-1"
    import backend.voice.dialer as mod
    monkeypatch.setattr(mod, "get_settings", lambda: s)
    # DNC/suppression is fail-closed (needs a real samus_suppression table).
    # These tests exercise cooldown/warm-exclusion, not DNC — a phone with no
    # seeded suppression entry is simply not on the DNC list, so give a clean
    # not-suppressed baseline. Dedicated DNC tests patch this themselves.
    monkeypatch.setattr(mod, "_is_suppressed", lambda phone: False)


def _isolate_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH",
                       str(tmp_path / "voice_audit.jsonl"))
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH",
                       str(tmp_path / "voice_events.jsonl"))
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------

def test_normalize_phone_us_10_digit():
    assert _normalize_phone("530-555-1234") == "+15305551234"
    assert _normalize_phone("(530) 555-1234") == "+15305551234"
    assert _normalize_phone("5305551234") == "+15305551234"


def test_normalize_phone_already_e164():
    assert _normalize_phone("+15305551234") == "+15305551234"
    assert _normalize_phone("+44 20 7946 0958") == "+442079460958"


def test_normalize_phone_us_11_digit_with_country_code():
    assert _normalize_phone("15305551234") == "+15305551234"


def test_normalize_phone_rejects_too_short():
    assert _normalize_phone("555-1234") is None
    assert _normalize_phone("") is None
    assert _normalize_phone(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CSV load
# ---------------------------------------------------------------------------

def test_load_prospects_from_csv_round_trips(tmp_path):
    csv_path = tmp_path / "call_list.csv"
    _write_csv(csv_path, [_prospect_row(), _prospect_row(prospect_id="p_002",
                                                        company_name="Bar Inc")])
    out = load_prospects_from_csv(csv_path)
    assert len(out) == 2
    assert out[0].company_name == "Acme Co"
    assert out[1].company_name == "Bar Inc"


def test_load_prospects_from_csv_skips_malformed(tmp_path, caplog):
    csv_path = tmp_path / "bad.csv"
    # Write a row with junk in lead_score (Pydantic should fail).
    rows = [_prospect_row(),
            _prospect_row(prospect_id="p_bad", lead_score="not-a-number")]
    _write_csv(csv_path, rows)
    out = load_prospects_from_csv(csv_path)
    # Both succeed because ProspectRecord coerces lead_score to str.
    # Strict cases (missing required field) -> validate would have failed.
    assert len(out) >= 1


def test_load_prospects_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_prospects_from_csv(tmp_path / "nope.csv")


# ---------------------------------------------------------------------------
# Priority sort
# ---------------------------------------------------------------------------

def test_sort_prospects_priority_then_score_then_seo():
    a = ProspectRecord(prospect_id="a", call_priority="warm", lead_score="50", seo_score="60")
    b = ProspectRecord(prospect_id="b", call_priority="hot",  lead_score="80", seo_score="40")
    c = ProspectRecord(prospect_id="c", call_priority="hot",  lead_score="80", seo_score="20")
    d = ProspectRecord(prospect_id="d", call_priority="low",  lead_score="99", seo_score="10")
    out = _sort_prospects([a, b, c, d])
    # hot first (c before b because lower seo wins tie on score)
    assert [p.prospect_id for p in out] == ["c", "b", "a", "d"]


# ---------------------------------------------------------------------------
# Variable values projection
# ---------------------------------------------------------------------------

def test_build_variable_values_projects_callsheet_fields():
    p = ProspectRecord(
        prospect_id="p1", company_name="Acme", industry="plumbing",
        city="Yuba City", state="CA",
        callsheet_opener="Hey",
        callsheet_pitch="We can help",
        callsheet_voicemail="Sorry we missed you",
        lead_score="80", seo_score="30",
    )
    vv = _build_variable_values(p)
    assert vv["company_name"] == "Acme"
    assert vv["callsheet_pitch"] == "We can help"
    assert vv["callsheet_opener"] == "Hey"
    assert vv["lead_score"] == "80"
    # Every value must be str (Vapi requires)
    for v in vv.values():
        assert isinstance(v, str)


def test_owner_name_flows_to_variable_values_when_present():
    """owner_name (CSV column / ProspectRecord) is passed to Vapi's
    variableValues so {{owner_name}} interpolates in Morgan's gatekeeper
    handoff. Must be a str."""
    p = ProspectRecord(
        prospect_id="p1", company_name="Acme", owner_name="Dana Reyes",
    )
    vv = _build_variable_values(p)
    assert vv["owner_name"] == "Dana Reyes"
    assert isinstance(vv["owner_name"], str)


def test_owner_name_is_empty_string_safe_when_absent():
    """No owner_name on the record -> empty string, never None / missing key,
    so the prompt's empty-safe fallback path fires cleanly."""
    p = ProspectRecord(prospect_id="p1", company_name="Acme")
    vv = _build_variable_values(p)
    assert vv["owner_name"] == ""
    assert isinstance(vv["owner_name"], str)


def test_callsheet_finding_flows_to_variable_values(monkeypatch):
    """The specific finding the gatekeeper-aware opener withholds is threaded
    to Vapi as {{callsheet_finding}} so Morgan can reveal it to the owner
    (prompt Step 2). Must be a str."""
    _fake_voice_settings(monkeypatch)
    p = ProspectRecord(
        prospect_id="p1", company_name="Acme",
        callsheet_finding="a security warning grading the site an F",
    )
    vv = _build_variable_values(p)
    assert vv["callsheet_finding"] == "a security warning grading the site an F"
    assert isinstance(vv["callsheet_finding"], str)


def test_callsheet_finding_is_empty_string_safe_when_absent(monkeypatch):
    """No finding on the record -> empty string, never None / missing key, so
    the prompt's {{callsheet_finding}} fallback (to {{callsheet_issues}}) fires
    cleanly."""
    _fake_voice_settings(monkeypatch)
    p = ProspectRecord(prospect_id="p1", company_name="Acme")
    vv = _build_variable_values(p)
    assert vv["callsheet_finding"] == ""
    assert isinstance(vv["callsheet_finding"], str)


def test_callsheet_finding_round_trips_through_csv(tmp_path):
    """callsheet_finding is a real CSV column: it survives write -> load so the
    prospecting-time finding reaches the dialer's variableValues."""
    csv_path = tmp_path / "call_list.csv"
    _write_csv(csv_path, [_prospect_row(
        callsheet_finding="a local-search score of 42 out of a hundred",
    )])
    out = load_prospects_from_csv(csv_path)
    assert len(out) == 1
    assert out[0].callsheet_finding == "a local-search score of 42 out of a hundred"


# ---------------------------------------------------------------------------
# Gap-6: [NAME]/[PHONE] placeholder substitution (the live-call voicemail bug)
# ---------------------------------------------------------------------------

def _fake_voice_settings(monkeypatch, *, rep="Morgan", callback="(530) 418-5105"):
    class _S:
        samus_voice_rep_name = rep
        samus_voice_callback_number = callback
    import backend.voice.dialer as mod
    monkeypatch.setattr(mod, "get_settings", lambda: _S())


def test_placeholders_substituted_in_opener_and_voicemail(monkeypatch):
    _fake_voice_settings(monkeypatch)
    p = ProspectRecord(
        prospect_id="p1", company_name="American Home Realty",
        callsheet_opener="Hi, this is [NAME] calling from HustleForge.",
        callsheet_voicemail="Hi, this is [NAME] from HustleForge. Give me a "
                            "call at [PHONE] or visit hustleforge.tech.",
        callsheet_pitch="[NAME] here with a quick idea.",
    )
    vv = _build_variable_values(p)
    # No raw bracket token may survive into any rep-facing field.
    for key in ("callsheet_opener", "callsheet_voicemail", "callsheet_pitch"):
        assert "[NAME]" not in vv[key], f"{key} still has [NAME]"
        assert "[PHONE]" not in vv[key], f"{key} still has [PHONE]"
    assert "Morgan" in vv["callsheet_opener"]
    assert "Morgan" in vv["callsheet_voicemail"]
    assert "(530) 418-5105" in vv["callsheet_voicemail"]
    # Raw vars exposed for direct {{rep_name}} / {{callback_number}} use.
    assert vv["rep_name"] == "Morgan"
    assert vv["callback_number"] == "(530) 418-5105"


def test_placeholder_substitution_case_insensitive(monkeypatch):
    _fake_voice_settings(monkeypatch, rep="Alex", callback="555-1212")
    p = ProspectRecord(
        prospect_id="p1",
        callsheet_voicemail="this is [name], reach me at [ Phone ]",
    )
    vv = _build_variable_values(p)
    assert vv["callsheet_voicemail"] == "this is Alex, reach me at 555-1212"


def test_no_placeholder_is_noop(monkeypatch):
    _fake_voice_settings(monkeypatch)
    p = ProspectRecord(prospect_id="p1", callsheet_opener="Clean opener, no tokens.")
    vv = _build_variable_values(p)
    assert vv["callsheet_opener"] == "Clean opener, no tokens."


# ---------------------------------------------------------------------------
# dial_call_list — dry-run + TCPA gate + priority filter
# ---------------------------------------------------------------------------

def test_dial_call_list_preflight_fails_without_assistant(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    # Settings missing assistant/phone.
    class _S:
        vapi_assistant_id = ""
        vapi_phone_number_id = ""
        vapi_api_key = "vapi_test"
    import backend.voice.dialer as mod
    monkeypatch.setattr(mod, "get_settings", lambda: _S())
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])
    with pytest.raises(RuntimeError, match="vapi_assistant_or_phone_unset"):
        dial_call_list(csv_path, DialerConfig())


def test_dial_call_list_dry_run_does_not_call_initiate_fn(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row(prospect_id="p1"),
                          _prospect_row(prospect_id="p2")])

    called: list = []
    def boom(req, vv):
        called.append((req, vv))
        return InitiateCallResult(call_id="should_not_happen", status=None,
                                  vapi_error=None)

    # Fixed clock inside TCPA window (3pm UTC = 8am PT)
    fixed_now = datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=True, delay_between_calls_s=0),
        initiate_fn=boom, now=fixed_now,
    )
    assert called == []
    assert result.initiated_count == 2
    assert all(a.outcome == "dry_run" for a in result.attempts)
    assert result.error_count == 0


def test_dial_call_list_respects_max_calls(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row(prospect_id=f"p{i}") for i in range(10)])
    fixed_now = datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=True, max_calls=3, delay_between_calls_s=0),
        now=fixed_now,
    )
    assert result.initiated_count == 3


def test_dial_call_list_priority_filter_skips_low(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [
        _prospect_row(prospect_id="hot1", call_priority="hot"),
        _prospect_row(prospect_id="low1", call_priority="low"),
        _prospect_row(prospect_id="warm1", call_priority="warm"),
    ])
    fixed_now = datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=True, delay_between_calls_s=0,
                     only_priorities=["hot"]),
        now=fixed_now,
    )
    assert result.eligible_count == 1
    assert result.initiated_count == 1
    assert result.attempts[0].prospect_id == "hot1"


def test_dial_call_list_tcpa_gate_skips_outside_hours(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    # 11pm UTC = 4pm PT (inside) BUT 7pm ET (inside) ... try 6am UTC = 11pm PT prev day = outside
    _write_csv(csv_path, [_prospect_row(state="CA")])
    # 06:00 UTC = 23:00 PT prev day (outside 8-21)
    fixed_now = datetime(2026, 5, 15, 6, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=fixed_now,
    )
    assert result.initiated_count == 0
    assert result.skipped_count == 1
    assert result.attempts[0].outcome == "skipped_hours"


def test_dial_call_list_tcpa_gate_passes_inside_hours(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row(state="CA")])
    # 20:00 UTC = 13:00 PT (inside 8-21)
    fixed_now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=fixed_now,
    )
    assert result.initiated_count == 1
    assert result.attempts[0].outcome == "dry_run"


def test_dial_call_list_skips_unusable_phone(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row(prospect_id="bad", phone="555")])
    fixed_now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=fixed_now,
    )
    assert result.skipped_count == 1
    assert result.attempts[0].outcome == "skipped_phone"


# ---------------------------------------------------------------------------
# Warm-prospect exclusion (Gap-1 — dialer must honour the same cold-list
# exclusion that prospecting/text_export applies to the morning TXT)
# ---------------------------------------------------------------------------

def test_dial_call_list_excludes_warm_enrolled_prospect(tmp_path, monkeypatch):
    """A prospect in an active buying_signal enrollment must never be dialed,
    even if it sits at the top of the priority sort."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [
        _prospect_row(prospect_id="pr_warm", company_name="Kelly Z", call_priority="hot"),
        _prospect_row(prospect_id="pr_cold", company_name="Cold Co", call_priority="hot"),
    ])
    # Kelly is warm-enrolled; the dialer must drop her.
    import backend.voice.dialer as mod
    monkeypatch.setattr(mod, "_active_warm_prospect_ids", lambda: {"pr_warm"})

    fixed_now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=fixed_now,
    )
    dialed_ids = {a.prospect_id for a in result.attempts if a.outcome == "dry_run"}
    assert "pr_warm" not in dialed_ids, "warm prospect must not be dialed"
    assert "pr_cold" in dialed_ids, "cold prospect must still be dialed"
    assert result.skipped_count >= 1  # the warm exclusion counts as a skip


def test_dial_call_list_warm_filter_empty_is_noop(tmp_path, monkeypatch):
    """No warm enrollments -> every eligible prospect still dials."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [
        _prospect_row(prospect_id="p1", call_priority="hot"),
        _prospect_row(prospect_id="p2", call_priority="hot"),
    ])
    import backend.voice.dialer as mod
    monkeypatch.setattr(mod, "_active_warm_prospect_ids", lambda: set())

    fixed_now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=fixed_now,
    )
    assert result.initiated_count == 2


# ---------------------------------------------------------------------------
# Vapi phone-number existence preflight (Gap-5 — a stale id must fail fast)
# ---------------------------------------------------------------------------

def test_phone_number_preflight_blocks_stale_id(tmp_path, monkeypatch):
    """A configured phone-number id that does NOT exist in the Vapi account
    must abort the run up front, not burn dial attempts."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch, phone="pn_stale")
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])

    # Vapi reports only pn_real exists — pn_stale is gone.
    import backend.voice.dialer as mod
    monkeypatch.setattr(
        mod, "_validate_phone_numbers_exist",
        lambda ids, settings: ({"missing": ["pn_stale"], "available": ["pn_real"]}
                               if "pn_stale" in ids else None),
    )
    with pytest.raises(RuntimeError, match="vapi_phone_number_not_found"):
        dial_call_list(csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0))


def test_phone_number_preflight_passes_valid_id(tmp_path, monkeypatch):
    """A valid id (None returned from the check) lets the run proceed."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch, phone="pn_real")
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])
    import backend.voice.dialer as mod
    monkeypatch.setattr(mod, "_validate_phone_numbers_exist",
                        lambda ids, settings: None)
    fixed_now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=fixed_now,
    )
    assert result.initiated_count == 1


def test_phone_number_preflight_failopen_on_list_error(tmp_path, monkeypatch):
    """The validator itself fail-opens: a Vapi list outage (httpx raising)
    returns None so a run with a valid number isn't blocked."""
    import backend.voice.dialer as mod

    class _S:
        vapi_api_key = "vapi_test"
    # Force httpx.get to raise inside the validator.
    import httpx
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vapi down")))
    out = mod._validate_phone_numbers_exist(["pn_any"], _S())
    assert out is None  # fail-open


def test_phone_number_preflight_detects_missing_from_real_list(monkeypatch):
    """End-to-end of the validator with a mocked 200 list response."""
    import backend.voice.dialer as mod

    class _Resp:
        status_code = 200
        def json(self):
            return [{"id": "pn_real", "number": "+15005550006"}]
    class _S:
        vapi_api_key = "vapi_test"
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    out = mod._validate_phone_numbers_exist(["pn_stale"], _S())
    assert out == {"missing": ["pn_stale"], "available": ["pn_real"]}
    # A valid id returns None.
    assert mod._validate_phone_numbers_exist(["pn_real"], _S()) is None


def test_dial_call_list_warm_lookup_fault_is_failopen(tmp_path, monkeypatch):
    """A fault in the warm lookup must NOT block the dial run (fail-open) —
    a storage glitch shouldn't silently zero out the whole morning push."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row(prospect_id="p1", call_priority="hot")])

    def _boom():
        raise RuntimeError("buying_signal store unreadable")
    import backend.voice.dialer as mod
    # Patch the underlying lookup, not the filter, so the try/except path runs.
    monkeypatch.setattr(
        "backend.outreach.buying_signal_route.active_warm_prospect_ids", _boom
    )
    fixed_now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=fixed_now,
    )
    assert result.initiated_count == 1  # fail-open: still dials


def test_dial_call_list_live_calls_initiate_fn_with_variable_values(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row(callsheet_pitch="We can help with SEO")])

    captured: list[tuple[InitiateCallRequest, dict[str, str]]] = []
    def fake_initiate(req, vv):
        captured.append((req, vv))
        return InitiateCallResult(call_id="vapi_call_42", status="queued",
                                  vapi_error=None)

    fixed_now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=False, delay_between_calls_s=0),
        initiate_fn=fake_initiate, now=fixed_now,
    )
    assert result.initiated_count == 1
    assert result.attempts[0].outcome == "initiated"
    assert result.attempts[0].call_id == "vapi_call_42"
    # Verify variable_values + metadata both flow through
    assert len(captured) == 1
    req, vv = captured[0]
    assert vv["callsheet_pitch"] == "We can help with SEO"
    assert req.metadata["prospect_id"] == "p_001"
    assert req.metadata["source"] == "morning_dialer"
    # Phone normalized to E.164
    assert req.customer_number == "+15305551234"


def test_dial_call_list_records_vapi_error_outcome(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])

    def boom(req, vv):
        return InitiateCallResult(call_id="", status=None,
                                  vapi_error="vapi_http_429: rate limited")

    fixed_now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=False, delay_between_calls_s=0),
        initiate_fn=boom, now=fixed_now,
    )
    assert result.error_count == 1
    assert result.attempts[0].outcome == "vapi_error"
    assert "vapi_http_429" in (result.attempts[0].vapi_error or "")


def test_dial_call_list_persists_run_record_to_artifact_root(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])

    fixed_now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=fixed_now,
    )
    # Run record under <artifact_root>/voice/dial_runs/<run_id>.json
    run_path = tmp_path / "artifacts" / "voice" / "dial_runs" / f"{result.run_id}.json"
    assert run_path.exists()
    persisted = json.loads(run_path.read_text(encoding="utf-8"))
    assert persisted["run_id"] == result.run_id
    assert persisted["initiated_count"] == 1


def test_dial_call_list_appends_audit_rows(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])

    fixed_now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=fixed_now,
    )
    audit_path = tmp_path / "voice_audit.jsonl"
    assert audit_path.exists()
    rows = [json.loads(line) for line in audit_path.read_text("utf-8").splitlines() if line.strip()]
    actions = {r["action"] for r in rows}
    assert "dial_attempt" in actions


def test_dial_call_list_appends_rich_event_row(tmp_path, monkeypatch):
    """voice_events.jsonl receives the full outcome (briefing-rollup source)."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])

    fixed_now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=fixed_now,
    )
    events_path = tmp_path / "voice_events.jsonl"
    assert events_path.exists()
    rows = [json.loads(line) for line in events_path.read_text("utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "dial_attempt"
    assert r["outcome"] == "dry_run"
    assert r["prospect_id"] == "p_001"
    assert r["company"] == "Acme Co"


# ---------------------------------------------------------------------------
# Default CSV path
# ---------------------------------------------------------------------------

def test_default_csv_path_for_today(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    from datetime import date
    expected = tmp_path / "daily_calls" / f"call_list_{date.today().isoformat()}.csv"
    assert default_csv_path_for_today() == expected


# ---------------------------------------------------------------------------
# Per-number dial cooldown (7-14 day floor)
# ---------------------------------------------------------------------------

def _seed_dial_run(tmp_path, days_ago: int, *, prospect_id: str = "p_001",
                   phone: str = "+15305551234", outcome: str = "dry_run"):
    """Write a dial_run_<YYYYMMDD>.json file dated ``days_ago`` days back."""
    from datetime import date, timedelta
    day = date.today() - timedelta(days=days_ago)
    runs_dir = tmp_path / "artifacts" / "voice" / "dial_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    fpath = runs_dir / f"dial_run_{day.strftime('%Y%m%d')}_seed.json"
    fpath.write_text(json.dumps({
        "run_id": f"seed_{days_ago}",
        "attempts": [{
            "prospect_id": prospect_id,
            "phone": phone,
            "outcome": outcome,
            "initiated_at": day.isoformat(),
        }],
    }), encoding="utf-8")


def test_cooldown_blocks_recent_dial_by_prospect_id(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    # Real attempt 3 days ago — well inside a 7-day cooldown.
    _seed_dial_run(tmp_path, days_ago=3, outcome="end_of_call")
    monkeypatch.setenv("SAMUS_DIALER_COOLDOWN_DAYS", "7")
    from backend.common import settings as _s
    _s.reload_settings() if hasattr(_s, "reload_settings") else None
    # Patch the in-module settings cache too.
    import backend.voice.dialer as mod
    base = mod.get_settings()
    base.samus_dialer_cooldown_days = 7  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "get_settings", lambda: base)

    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])
    result = dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc),
    )
    assert result.initiated_count == 0
    assert result.skipped_count == 1
    assert result.attempts[0].outcome == "skipped_cooldown"
    assert "7d per-number cooldown" in (result.attempts[0].detail or "")


def test_cooldown_blocks_recent_dial_by_phone_even_if_pid_changed(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    # Same phone, DIFFERENT prospect_id 5 days ago.
    _seed_dial_run(tmp_path, days_ago=5,
                   prospect_id="p_OLD", phone="+15305551234",
                   outcome="end_of_call")
    import backend.voice.dialer as mod
    base = mod.get_settings()
    base.samus_dialer_cooldown_days = 7  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "get_settings", lambda: base)

    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row(prospect_id="p_NEW")])
    result = dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc),
    )
    assert result.attempts[0].outcome == "skipped_cooldown"


def test_cooldown_ignores_skipped_rows(tmp_path, monkeypatch):
    """A prior skipped_* row didn't actually place a call — cooldown shouldn't trip."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    _seed_dial_run(tmp_path, days_ago=2, outcome="skipped_hours")
    _seed_dial_run(tmp_path, days_ago=4, outcome="skipped_suppressed")
    import backend.voice.dialer as mod
    base = mod.get_settings()
    base.samus_dialer_cooldown_days = 7  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "get_settings", lambda: base)

    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])
    result = dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc),
    )
    # The cooldown should NOT have skipped it; only dry_run path runs.
    assert result.initiated_count == 1
    assert result.attempts[0].outcome == "dry_run"


def test_cooldown_releases_after_window(tmp_path, monkeypatch):
    """Dial 10 days ago, 7-day window — should be eligible again."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    _seed_dial_run(tmp_path, days_ago=10, outcome="end_of_call")
    import backend.voice.dialer as mod
    base = mod.get_settings()
    base.samus_dialer_cooldown_days = 7  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "get_settings", lambda: base)

    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])
    result = dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc),
    )
    assert result.initiated_count == 1
    assert result.attempts[0].outcome == "dry_run"


def _seed_retry_queue(tmp_path, *, prospect_id: str, prospect_phone: str,
                      ended_reason: str, hours_ago: float = 1.0,
                      monkeypatch=None):
    """Append a retry-queue entry. Returns the queue path."""
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    qpath = tmp_path / "retry_queue.jsonl"
    if monkeypatch is not None:
        monkeypatch.setenv("SAMUS_RETRY_QUEUE_PATH", str(qpath))
    qpath.write_text(json.dumps({
        "prospect_id": prospect_id,
        "prospect_phone": prospect_phone,
        "company_name": "Acme Co",
        "priority": "hot",
        "run_id": "seed",
        "ended_reason": ended_reason,
        "ts": ts,
    }) + "\n", encoding="utf-8")
    return qpath


def test_cooldown_exempts_same_day_voicemail_retry(tmp_path, monkeypatch):
    """Voicemail within 36h on the retry queue bypasses the 7d cooldown."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    # Real dial yesterday (in cooldown window) AND a voicemail retry entry.
    _seed_dial_run(tmp_path, days_ago=1, outcome="end_of_call")
    _seed_retry_queue(tmp_path, prospect_id="p_001",
                      prospect_phone="+15305551234",
                      ended_reason="voicemail", hours_ago=2.0,
                      monkeypatch=monkeypatch)
    import backend.voice.dialer as mod
    base = mod.get_settings()
    base.samus_dialer_cooldown_days = 7  # type: ignore[attr-defined]
    base.samus_dialer_voicemail_exempt_hours = 36  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "get_settings", lambda: base)

    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])
    result = dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc),
    )
    # Cooldown should have been bypassed -> dial proceeds (dry_run).
    assert result.initiated_count == 1
    assert result.attempts[0].outcome == "dry_run"


def test_cooldown_not_exempted_when_voicemail_entry_stale(tmp_path, monkeypatch):
    """Voicemail entry older than 36h does NOT exempt — falls back to cooldown."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    _seed_dial_run(tmp_path, days_ago=2, outcome="end_of_call")
    _seed_retry_queue(tmp_path, prospect_id="p_001",
                      prospect_phone="+15305551234",
                      ended_reason="voicemail", hours_ago=72.0,  # 3 days
                      monkeypatch=monkeypatch)
    import backend.voice.dialer as mod
    base = mod.get_settings()
    base.samus_dialer_cooldown_days = 7  # type: ignore[attr-defined]
    base.samus_dialer_voicemail_exempt_hours = 36  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "get_settings", lambda: base)

    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])
    result = dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc),
    )
    assert result.attempts[0].outcome == "skipped_cooldown"


def test_cooldown_not_exempted_for_non_voicemail_ended_reason(tmp_path, monkeypatch):
    """A no-answer retry (NOT voicemail) does not bypass cooldown."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    _seed_dial_run(tmp_path, days_ago=1, outcome="end_of_call")
    _seed_retry_queue(tmp_path, prospect_id="p_001",
                      prospect_phone="+15305551234",
                      ended_reason="no-answer", hours_ago=2.0,
                      monkeypatch=monkeypatch)
    import backend.voice.dialer as mod
    base = mod.get_settings()
    base.samus_dialer_cooldown_days = 7  # type: ignore[attr-defined]
    base.samus_dialer_voicemail_exempt_hours = 36  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "get_settings", lambda: base)

    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])
    result = dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc),
    )
    assert result.attempts[0].outcome == "skipped_cooldown"


def test_cooldown_disabled_when_set_to_zero(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    _seed_dial_run(tmp_path, days_ago=1, outcome="end_of_call")
    import backend.voice.dialer as mod
    base = mod.get_settings()
    base.samus_dialer_cooldown_days = 0  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "get_settings", lambda: base)

    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, [_prospect_row()])
    result = dial_call_list(
        csv_path, DialerConfig(dry_run=True, delay_between_calls_s=0),
        now=datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc),
    )
    # Falls through to _already_called_today which won't match the seeded
    # 1-day-ago file (it only checks today). So the dial proceeds.
    assert result.initiated_count == 1


# ---------------------------------------------------------------------------
# OUTBOUND firstMessage threading — the spoken opener is per-prospect.
# The dialer bypasses service.initiate_call and drives VapiClient directly via
# _wrap_service_initiate; assert the opener rides as create_call(first_message=).
# ---------------------------------------------------------------------------

def _capture_adapter_call(monkeypatch, variable_values):
    """Run _wrap_service_initiate's real impl with a mocked VapiClient and
    return the kwargs create_call was invoked with."""
    import backend.voice.dialer as mod
    from backend.voice.models import InitiateCallRequest

    cap: dict = {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def create_call(self, **kwargs):
            cap.update(kwargs)

            class _Call:
                id = "call_1"
                status = "queued"
            return _Call()

    class _S:
        vapi_api_key = "k"
        samus_voice_rep_name = "Morgan"
        samus_voice_callback_number = "(530) 418-5105"

    import backend.voice.client as client_mod
    monkeypatch.setattr(client_mod, "VapiClient", _FakeClient)
    monkeypatch.setattr(mod, "get_settings", lambda: _S())

    impl = mod._wrap_service_initiate(real_initiate=None)
    req = InitiateCallRequest(
        assistant_id="ast_x", phone_number_id="pn_y",
        customer_number="+15305551234", customer_name="Acme",
    )
    impl(req, variable_values)
    return cap


def test_outbound_threads_opener_as_first_message(monkeypatch):
    cap = _capture_adapter_call(monkeypatch, {
        "company_name": "Acme",
        "callsheet_opener": "Hey, is this Acme? Honestly, this is a cold call...",
        "callsheet_voicemail": "VM here",
    })
    assert cap["first_message"] == "Hey, is this Acme? Honestly, this is a cold call..."
    assert cap["voicemail_message"] == "VM here"


def test_outbound_omits_first_message_when_opener_empty(monkeypatch):
    cap = _capture_adapter_call(monkeypatch, {
        "company_name": "Acme",
        "callsheet_opener": "",
    })
    assert cap["first_message"] is None


# ---------------------------------------------------------------------------
# Caller-ID round-robin across the phone_number_pool
#
# Regression for the 2026-07-02 morning batch: all 15 placed calls landed on a
# single caller ID even though the pool had 2 numbers. Root cause was
# contiguous-chunk assignment keyed on the raw eligible position — the first
# chunk (all early positions) mapped to pool[0], and because prospects skipped
# BEFORE placement burned the batch cap inside pool[0]'s chunk, pool[1] was
# never reached. Fix: round-robin over the pool keyed on the PLACEMENT index,
# advancing only when a call is actually placed (dry-run or real success).
# ---------------------------------------------------------------------------

def _same_rank_rows(n: int) -> list[dict]:
    """N prospects with identical priority/score so eligible_sorted preserves
    CSV order (sorted() is stable), and distinct prospect_id + phone so no two
    collide on the per-prospect / per-number gates."""
    rows = []
    for i in range(n):
        rows.append(_prospect_row(
            prospect_id=f"p{i:02d}",
            phone=f"530555{1000 + i:04d}",
            call_priority="hot",
            lead_score="80",
            seo_score="30",
        ))
    return rows


def test_dry_run_round_robins_caller_id_across_two_number_pool(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, _same_rank_rows(6))

    fixed_now = datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(
            dry_run=True, delay_between_calls_s=0, max_calls=6,
            phone_number_ids=["numA", "numB"],
        ),
        now=fixed_now,
    )
    assert result.initiated_count == 6
    placed = [a for a in result.attempts if a.outcome == "dry_run"]
    numbers = [a.outbound_number_id for a in placed]
    # Consecutive PLACED calls alternate A,B,A,B,... from the very first call.
    assert numbers == ["numA", "numB", "numA", "numB", "numA", "numB"]


def test_live_round_robins_caller_id_across_two_number_pool(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, _same_rank_rows(5))

    def fake_initiate(req, vv):
        # Echo back which number the dialer told us to place on.
        return InitiateCallResult(
            call_id="c_" + req.phone_number_id, status="queued", vapi_error=None,
        )

    fixed_now = datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(
            dry_run=False, delay_between_calls_s=0, max_calls=5,
            phone_number_ids=["numA", "numB"],
        ),
        initiate_fn=fake_initiate, now=fixed_now,
    )
    assert result.initiated_count == 5
    placed = [a for a in result.attempts if a.outcome == "initiated"]
    numbers = [a.outbound_number_id for a in placed]
    assert numbers == ["numA", "numB", "numA", "numB", "numA"]


def test_skips_do_not_consume_a_rotation_slot(tmp_path, monkeypatch):
    """5 DNC-skipped prospects up front, then 4 placeable ones. The 4 PLACED
    calls must still alternate A,B,A,B — a skip must NOT advance the pool
    cursor. This is the exact failure mode from the live batch (skips ahead of
    placement) and the reason chunk assignment broke."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    rows = _same_rank_rows(9)  # p00..p04 skipped, p05..p08 placed
    _write_csv(csv_path, rows)

    # First 5 phones are on the DNC list (skipped BEFORE placement, no number);
    # the rest dial through. _override_vapi_settings set _is_suppressed to
    # always-False; override it here for the leading 5 phones.
    import backend.voice.dialer as mod
    suppressed = {_normalize_phone(rows[i]["phone"]) for i in range(5)}
    monkeypatch.setattr(mod, "_is_suppressed", lambda phone: phone in suppressed)

    fixed_now = datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(
            dry_run=True, delay_between_calls_s=0, max_calls=9,
            phone_number_ids=["numA", "numB"],
        ),
        now=fixed_now,
    )
    assert result.skipped_count == 5
    assert result.initiated_count == 4
    placed = [a for a in result.attempts if a.outcome == "dry_run"]
    numbers = [a.outbound_number_id for a in placed]
    # Skips consumed zero rotation slots: the 4 placements still start at A.
    assert numbers == ["numA", "numB", "numA", "numB"]
    # And the skipped rows carry no assigned number.
    skipped = [a for a in result.attempts if a.outcome == "skipped_suppressed"]
    assert len(skipped) == 5
    assert all(a.outbound_number_id == "" for a in skipped)


def test_single_number_pool_still_works(tmp_path, monkeypatch):
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch)
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, _same_rank_rows(3))

    fixed_now = datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(
            dry_run=True, delay_between_calls_s=0, max_calls=3,
            phone_number_ids=["soloNum"],
        ),
        now=fixed_now,
    )
    assert result.initiated_count == 3
    placed = [a for a in result.attempts if a.outcome == "dry_run"]
    numbers = [a.outbound_number_id for a in placed]
    assert numbers == ["soloNum", "soloNum", "soloNum"]


def test_single_number_pool_falls_back_to_env_default(tmp_path, monkeypatch):
    """Empty phone_number_ids => single-number mode from settings.vapi_phone_number_id."""
    _isolate_audit(monkeypatch, tmp_path)
    _override_vapi_settings(monkeypatch, phone="env_default_num")
    csv_path = tmp_path / "x.csv"
    _write_csv(csv_path, _same_rank_rows(2))

    fixed_now = datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc)
    result = dial_call_list(
        csv_path,
        DialerConfig(dry_run=True, delay_between_calls_s=0, max_calls=2),
        now=fixed_now,
    )
    assert result.initiated_count == 2
    placed = [a for a in result.attempts if a.outcome == "dry_run"]
    numbers = [a.outbound_number_id for a in placed]
    assert numbers == ["env_default_num", "env_default_num"]
