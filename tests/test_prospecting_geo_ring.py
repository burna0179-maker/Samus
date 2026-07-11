"""Geo-ring port + dormant-by-default in-stack cadence (ADR-014, DRAFT).

Covers:
  * Ring catalog + cumulative composition (dedup/order, ring 0 / mid / top).
  * Ring advancement (below top advances + resets day; at/over the top ring is
    a no-op).
  * State round-trip (save/load), missing-file + corrupt-file handling, and the
    record_run state-update tail (day increment, history append, 90-cap).
  * The cadence loop being DORMANT when the arming flag is OFF (the merge-time
    default): cadence_enabled() is False and start_cadence_loop schedules
    nothing. Plus: when armed it schedules a task, and run_prospecting_tick
    drives the in-container discover path with persist_prospects=True against
    the cumulative zipcode set, then records + persists state.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace


from backend.prospecting import cadence_task as ct
from backend.prospecting import geo_ring as gr


# ---------------------------------------------------------------------
# Ring catalog + cumulative composition
# ---------------------------------------------------------------------


def test_catalog_shape_and_top_index():
    assert len(gr.RING_CATALOG) == 6
    assert gr.RING_CATALOG[0].name == "yuba_city_core"
    assert gr.RING_CATALOG[-1].name == "sacramento_metro"
    assert gr.top_ring_index() == 5
    assert gr.ring_name(0) == "yuba_city_core"
    assert gr.ring_name(5) == "sacramento_metro"


def test_cumulative_ring_zero_is_just_first_ring():
    assert gr.cumulative_zipcodes(0) == list(gr.RING_CATALOG[0].zipcodes)


def test_cumulative_is_union_of_rings_zero_through_n_in_order():
    # Ring 2 = rings 0 + 1 + 2 concatenated, first-seen order, deduped.
    expected: list[str] = []
    seen: set[str] = set()
    for ring in gr.RING_CATALOG[:3]:
        for z in ring.zipcodes:
            if z not in seen:
                seen.add(z)
                expected.append(z)
    assert gr.cumulative_zipcodes(2) == expected
    # Cumulative is monotone non-decreasing in size.
    assert len(gr.cumulative_zipcodes(0)) <= len(gr.cumulative_zipcodes(2))
    assert len(gr.cumulative_zipcodes(2)) <= len(gr.cumulative_zipcodes(5))


def test_cumulative_dedupes_and_preserves_first_occurrence():
    zips = gr.cumulative_zipcodes(gr.top_ring_index())
    assert len(zips) == len(set(zips)), "cumulative set must not contain dupes"
    # Every configured zipcode across all rings is present at the top ring.
    all_configured = {z for ring in gr.RING_CATALOG for z in ring.zipcodes}
    assert set(zips) == all_configured


def test_cumulative_clamps_out_of_range_ring_index():
    # Above the top clamps to the top ring's cumulative set.
    assert gr.cumulative_zipcodes(99) == gr.cumulative_zipcodes(gr.top_ring_index())
    # Negative clamps to ring 0.
    assert gr.cumulative_zipcodes(-5) == gr.cumulative_zipcodes(0)


# ---------------------------------------------------------------------
# Ring advancement
# ---------------------------------------------------------------------


def test_advance_below_top_increments_and_resets_day():
    state = gr.GeoRingState(current_ring=0, days_at_ring=7)
    returned, advanced = gr.advance_ring(state)
    assert advanced is True
    assert returned is state  # mutates in place
    assert state.current_ring == 1
    assert state.days_at_ring == 0
    assert state.current_ring_name() == gr.RING_CATALOG[1].name


def test_advance_at_top_ring_is_noop():
    state = gr.GeoRingState(current_ring=gr.top_ring_index(), days_at_ring=3)
    _, advanced = gr.advance_ring(state)
    assert advanced is False
    # State untouched — same ring, same day counter.
    assert state.current_ring == gr.top_ring_index()
    assert state.days_at_ring == 3


def test_advance_over_top_ring_is_noop_after_clamp():
    # A state file that somehow holds an out-of-range ring is treated as the
    # top ring; advancing it is still a no-op (cannot exceed the catalog).
    state = gr.GeoRingState(current_ring=99, days_at_ring=1)
    _, advanced = gr.advance_ring(state)
    assert advanced is False
    assert state.at_top_ring() is True


def test_repeated_advance_walks_to_top_then_stops():
    state = gr.GeoRingState(current_ring=0)
    advanced_count = 0
    for _ in range(20):
        _, advanced = gr.advance_ring(state)
        if advanced:
            advanced_count += 1
    assert state.current_ring == gr.top_ring_index()
    # Exactly top_ring_index() successful advances from ring 0.
    assert advanced_count == gr.top_ring_index()


# ---------------------------------------------------------------------
# State persistence round-trip + record_run
# ---------------------------------------------------------------------


def test_load_missing_file_returns_ring_zero_defaults(tmp_path):
    state = gr.load_state(tmp_path / "nope.json")
    assert state.current_ring == 0
    assert state.days_at_ring == 0
    assert state.last_run_date is None
    assert state.history == []


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "geo.json"
    state = gr.GeoRingState(
        current_ring=2,
        days_at_ring=4,
        last_run_date="2026-06-10",
        history=[
            {
                "date": "2026-06-10",
                "ring_index": 2,
                "ring_name": "x",
                "zipcodes_count": 10,
                "day_at_ring": 4,
                "exit_code": 0,
            }
        ],
    )
    written = gr.save_state(state, path)
    assert written == path
    reloaded = gr.load_state(path)
    assert reloaded.current_ring == 2
    assert reloaded.days_at_ring == 4
    assert reloaded.last_run_date == "2026-06-10"
    assert len(reloaded.history) == 1
    assert reloaded.history[0]["ring_name"] == "x"


def test_corrupt_state_is_quarantined_and_reinitialised(tmp_path):
    path = tmp_path / "geo.json"
    path.write_text("{ this is not valid json", encoding="utf-8")
    state = gr.load_state(path)
    # Reinitialised at ring 0.
    assert state.current_ring == 0
    assert state.days_at_ring == 0
    # Original bad file moved aside to a .corrupt-* sibling; no valid file left
    # at the canonical path.
    assert not path.exists()
    siblings = list(tmp_path.glob("geo.json.corrupt-*"))
    assert len(siblings) == 1


def test_from_dict_defends_garbage_fields():
    state = gr.GeoRingState.from_dict(
        {
            "current_ring": "not-an-int",
            "days_at_ring": None,
            "last_run_date": 12345,  # wrong type -> dropped to None
            "history": "not-a-list",  # wrong type -> []
        }
    )
    assert state.current_ring == 0
    assert state.days_at_ring == 0
    assert state.last_run_date is None
    assert state.history == []


def test_from_dict_clamps_out_of_range_ring():
    state = gr.GeoRingState.from_dict({"current_ring": 999})
    assert state.current_ring == gr.top_ring_index()


def test_record_run_increments_day_and_appends_history():
    from datetime import date

    state = gr.GeoRingState(current_ring=1, days_at_ring=2)
    gr.record_run(state, run_date=date(2026, 6, 14), exit_code=0, zipcodes_count=12)
    assert state.days_at_ring == 3  # incremented
    assert state.last_run_date == "2026-06-14"
    assert len(state.history) == 1
    entry = state.history[0]
    assert entry["ring_index"] == 1
    assert entry["ring_name"] == gr.RING_CATALOG[1].name
    assert entry["zipcodes_count"] == 12
    assert entry["day_at_ring"] == 3  # the incremented value
    assert entry["exit_code"] == 0


def test_record_run_history_is_capped_at_90():
    state = gr.GeoRingState(current_ring=0)
    # Pre-fill with 95 stale placeholder entries, then record one real run.
    state.history = [{"n": i} for i in range(95)]
    gr.record_run(state, zipcodes_count=5)
    assert len(state.history) == 90
    # The newest record (the real run) is retained at the tail.
    assert state.history[-1]["day_at_ring"] == 1
    assert state.history[-1].get("ring_name") == gr.RING_CATALOG[0].name


def test_record_run_defaults_zip_count_to_active_set():
    state = gr.GeoRingState(current_ring=0)
    gr.record_run(state)  # no explicit zipcodes_count
    assert state.history[-1]["zipcodes_count"] == len(gr.cumulative_zipcodes(0))


# ---------------------------------------------------------------------
# Cadence: DORMANT by default (the merge-time guardrail)
# ---------------------------------------------------------------------


def test_cadence_disabled_by_default(monkeypatch):
    """The arming flag is OFF by default — the gate must read False.

    conftest's autouse _reset_flag_store leaves the flag store uninitialised,
    so cadence_enabled() falls back to the Settings field default (False).
    """
    monkeypatch.delenv("SAMUS_PROSPECTING_IN_STACK_CADENCE_ENABLED", raising=False)
    from backend.common.settings import reload_settings

    reload_settings()
    assert ct.cadence_enabled() is False


def test_start_cadence_loop_is_dormant_when_flag_off(monkeypatch):
    """With the flag OFF, start_cadence_loop schedules NOTHING and returns None.

    This is the core safety guardrail: merging changes nothing at runtime.
    """
    monkeypatch.delenv("SAMUS_PROSPECTING_IN_STACK_CADENCE_ENABLED", raising=False)
    from backend.common.settings import reload_settings

    reload_settings()

    app = SimpleNamespace(state=SimpleNamespace())

    async def _run():
        return await ct.start_cadence_loop(app)

    assert asyncio.run(_run()) is None
    # No task was ever attached to app.state.
    assert getattr(app.state, "prospecting_cadence_task", None) is None


def test_settings_field_defaults_off():
    """Belt-and-braces: the pydantic Settings field itself defaults False."""
    from backend.common.config import Settings

    assert Settings().prospecting_in_stack_cadence_enabled is False


# ---------------------------------------------------------------------
# Cadence: armed behaviour (flag ON)
# ---------------------------------------------------------------------


def _arm_flag(monkeypatch):
    """Arm the cadence via the env-bound Settings field.

    The flag store is left uninitialised (conftest resets it), so
    cadence_enabled() resolves through the settings fallback — setting the env
    + reloading settings flips it ON without needing the runtime flag store.
    """
    monkeypatch.setenv("SAMUS_PROSPECTING_IN_STACK_CADENCE_ENABLED", "1")
    from backend.common.settings import reload_settings

    reload_settings()


def test_cadence_enabled_when_flag_armed(monkeypatch):
    _arm_flag(monkeypatch)
    assert ct.cadence_enabled() is True


def test_start_cadence_loop_schedules_task_when_armed(monkeypatch):
    _arm_flag(monkeypatch)
    # Long initial delay so the loop never actually fires discovery in the test.
    monkeypatch.setenv(ct.ENV_INITIAL_DELAY, "999")
    monkeypatch.setenv(ct.ENV_INTERVAL, "999")

    async def _run():
        app = SimpleNamespace(state=SimpleNamespace())
        task = await ct.start_cadence_loop(app)
        assert task is not None and not task.done()
        # Idempotent: a second start returns the same task.
        task2 = await ct.start_cadence_loop(app)
        assert task2 is task
        await ct.stop_cadence_loop(app)
        assert getattr(app.state, "prospecting_cadence_task") is None
        return True

    assert asyncio.run(_run()) is True


def test_run_prospecting_tick_drives_discover_and_persists_state(monkeypatch, tmp_path):
    """One tick: cumulative zipcodes -> process_discovery(persist_prospects=True)
    -> state recorded + saved. process_discovery + state path are stubbed so the
    test is pure/offline.
    """
    from backend.prospecting.models import DiscoveryResult

    # Pin the geo-state file into tmp_path (start at ring 1).
    state_path = tmp_path / "prospecting_geo_state.json"
    initial = gr.GeoRingState(current_ring=1, days_at_ring=4)
    gr.save_state(initial, state_path)
    monkeypatch.setattr(gr, "default_state_path", lambda: state_path)

    captured = {}

    def _fake_process_discovery(req, *, task_id=None):
        captured["req"] = req
        return DiscoveryResult(
            campaign_name=req.campaign_name,
            prospect_count=7,
            csv_path="/opt/samus/data/artifacts/daily_calls/call_list_x.csv",
            txt_path="/opt/samus/data/artifacts/daily_calls/morning_call_list_x.txt",
            prospects=[],
            cache_hit=False,
            persisted_count=5,
            # Healthy fresh yield — this test covers the drive+persist path;
            # the exhaustion/advance behaviours have their own tests below.
            fresh_count=7,
        )

    import backend.prospecting.service as p_service

    monkeypatch.setattr(p_service, "process_discovery", _fake_process_discovery)

    summary = ct.run_prospecting_tick()

    # The discovery request used the cumulative zipcodes for ring 1 and armed
    # CRM persistence — the whole point of the in-stack path.
    req = captured["req"]
    assert req.zipcodes == gr.cumulative_zipcodes(1)
    assert req.persist_prospects is True
    assert req.must_have_website is False
    assert req.industries  # non-empty default industry set

    # The summary reflects the discovery result.
    assert summary["ok"] is True
    assert summary["prospect_count"] == 7
    assert summary["persisted_count"] == 5
    assert summary["ring_index"] == 1

    # State was recorded (day incremented 4 -> 5) and persisted to disk.
    reloaded = gr.load_state(state_path)
    assert reloaded.days_at_ring == 5
    assert reloaded.current_ring == 1  # healthy yield — ring held
    assert len(reloaded.history) == 1
    assert reloaded.history[-1]["exit_code"] == 0
    # The on-disk file is valid JSON in the expected shape.
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["current_ring"] == 1 and on_disk["days_at_ring"] == 5


def _stub_discovery(monkeypatch, *, fresh_count: int, held: int = 0):
    from backend.prospecting.models import DiscoveryResult

    import backend.prospecting.service as p_service

    monkeypatch.setattr(
        p_service,
        "process_discovery",
        lambda req, *, task_id=None: DiscoveryResult(
            campaign_name=req.campaign_name,
            prospect_count=fresh_count + held,
            csv_path="/x.csv",
            txt_path="/x.txt",
            prospects=[],
            cache_hit=False,
            persisted_count=0,
            fresh_count=fresh_count,
            recycled_held_count=held,
        ),
    )


def test_run_prospecting_tick_advances_ring_on_exhausted_fresh_yield(monkeypatch, tmp_path):
    """Ring lifecycle (operator-ratified 2026-07-03): a fire whose FRESH yield
    is below SAMUS_RING_MIN_NEW_YIELD auto-advances the ring — supersedes the
    original ADR-014 operator-only-advance stance (the cumulative ring was
    re-promoting the same businesses daily).

    Pinned to MAX_RETICKS=0 so the assertion covers the SINGLE-HOP advance
    semantic; the chained-re-tick behaviour has its own test below.
    """
    monkeypatch.setenv(ct.ENV_MAX_RETICKS, "0")
    state_path = tmp_path / "prospecting_geo_state.json"
    gr.save_state(gr.GeoRingState(current_ring=0, days_at_ring=3), state_path)
    monkeypatch.setattr(gr, "default_state_path", lambda: state_path)
    _stub_discovery(monkeypatch, fresh_count=0, held=40)

    summary = ct.run_prospecting_tick()
    reloaded = gr.load_state(state_path)
    assert reloaded.current_ring == 1
    assert reloaded.days_at_ring == 0
    assert summary["ring_action"].startswith("advanced_to_")
    assert summary["fresh_count"] == 0 and summary["recycled_held_count"] == 40
    assert summary["retick_count"] == 0


def test_run_prospecting_tick_holds_ring_on_empty_inconclusive_fire(monkeypatch, tmp_path):
    """A fire that returns ZERO businesses (prospect_count==0, fresh==0) — a
    Places quota exhaustion / transport failure / empty territory — is
    INCONCLUSIVE and must NOT be read as ring exhaustion. Otherwise a single
    bad-API tick advances (and, with reticks, multiplies) through productive
    rings. Guard: hold the ring, do not advance, do not re-fire."""
    monkeypatch.setenv(ct.ENV_MAX_RETICKS, "2")
    state_path = tmp_path / "prospecting_geo_state.json"
    gr.save_state(gr.GeoRingState(current_ring=0, days_at_ring=3), state_path)
    monkeypatch.setattr(gr, "default_state_path", lambda: state_path)
    _stub_discovery(monkeypatch, fresh_count=0, held=0)  # prospect_count == 0

    summary = ct.run_prospecting_tick()
    reloaded = gr.load_state(state_path)
    assert reloaded.current_ring == 0  # ring NOT burned
    assert summary["ring_action"] == "held_empty_fire_inconclusive"
    assert summary["retick_count"] == 0  # no retick amplification


def test_run_prospecting_tick_holds_ring_on_healthy_fresh_yield(monkeypatch, tmp_path):
    state_path = tmp_path / "prospecting_geo_state.json"
    gr.save_state(gr.GeoRingState(current_ring=0, days_at_ring=0), state_path)
    monkeypatch.setattr(gr, "default_state_path", lambda: state_path)
    _stub_discovery(monkeypatch, fresh_count=25)

    summary = ct.run_prospecting_tick()
    reloaded = gr.load_state(state_path)
    assert reloaded.current_ring == 0  # healthy yield — no advance
    assert reloaded.days_at_ring == 1  # but the day advanced
    assert summary["ring_action"] == "held"


def test_run_prospecting_tick_wraps_to_ring_0_at_top(monkeypatch, tmp_path):
    """Top ring exhausted -> wrap to ring 0: the recycle cooldown expiry then
    re-qualifies aged prospects, which IS the cycle-back pass. Pinned to
    MAX_RETICKS=0 to cover the single wrap semantic in isolation."""
    monkeypatch.setenv(ct.ENV_MAX_RETICKS, "0")
    top = gr.top_ring_index()
    state_path = tmp_path / "prospecting_geo_state.json"
    gr.save_state(gr.GeoRingState(current_ring=top, days_at_ring=9), state_path)
    monkeypatch.setattr(gr, "default_state_path", lambda: state_path)
    _stub_discovery(monkeypatch, fresh_count=1, held=80)

    summary = ct.run_prospecting_tick()
    reloaded = gr.load_state(state_path)
    assert reloaded.current_ring == 0
    assert reloaded.days_at_ring == 0
    assert summary["ring_action"] == "wrapped_to_ring_0_recycle_pass"


def test_run_prospecting_tick_min_yield_env_override(monkeypatch, tmp_path):
    state_path = tmp_path / "prospecting_geo_state.json"
    gr.save_state(gr.GeoRingState(current_ring=0, days_at_ring=0), state_path)
    monkeypatch.setattr(gr, "default_state_path", lambda: state_path)
    monkeypatch.setenv(ct.ENV_MIN_NEW_YIELD, "0")  # floor 0 -> never exhausted
    _stub_discovery(monkeypatch, fresh_count=0, held=40)

    summary = ct.run_prospecting_tick()
    assert gr.load_state(state_path).current_ring == 0
    assert summary["ring_action"] == "held"


def test_run_prospecting_tick_reticks_new_ring_on_exhaustion(monkeypatch, tmp_path):
    """The productivity fix (2026-07-08): when a fire exhausts its ring and
    auto-advances, the NEW ring is immediately re-fired instead of waiting the
    full 24h cadence interval. Without this, an exhaustion-day call list
    stayed near-empty until the next day.

    Simulates: ring 0 exhausted (fresh=0), ring 1 healthy (fresh=25). One
    ``run_prospecting_tick()`` must produce a summary reflecting the ring-1
    fire, so today's on-disk call list is populated the same day.
    """
    state_path = tmp_path / "prospecting_geo_state.json"
    gr.save_state(gr.GeoRingState(current_ring=0, days_at_ring=3), state_path)
    monkeypatch.setattr(gr, "default_state_path", lambda: state_path)

    from backend.prospecting.models import DiscoveryResult
    import backend.prospecting.service as p_service

    fires: list[int] = []

    def _fake_process_discovery(req, *, task_id=None):
        # Fire N records what ring was active. First fire (ring 0) returns an
        # exhausted result; the immediate re-fire (ring 1) returns healthy.
        current = gr.load_state(state_path).current_ring
        fires.append(current)
        fresh = 0 if current == 0 else 25
        return DiscoveryResult(
            campaign_name=req.campaign_name,
            prospect_count=fresh + 10,
            csv_path=f"/x_ring{current}.csv",
            txt_path=f"/x_ring{current}.txt",
            prospects=[],
            cache_hit=False,
            persisted_count=fresh,
            fresh_count=fresh,
            recycled_held_count=10,
        )

    monkeypatch.setattr(p_service, "process_discovery", _fake_process_discovery)

    summary = ct.run_prospecting_tick()

    # Two fires ran within the single tick invocation — the ring-0 exhaustion
    # triggered an immediate ring-1 re-fire, no 24h wait.
    assert fires == [0, 1]
    assert summary["retick_count"] == 1
    assert summary["retick_chain"][0].startswith("advanced_to_")
    assert summary["retick_chain"][-1] == "held"
    # The RETURNED summary reflects the FINAL (productive) fire, so callers
    # + the call-list file on disk both see the ring-1 result.
    assert summary["fresh_count"] == 25
    assert summary["csv_path"].endswith("_ring1.csv")


def test_run_prospecting_tick_retick_budget_caps_advances(monkeypatch, tmp_path):
    """Bound the chain so an all-rings-exhausted day still terminates. With
    every fire returning fresh=0, budget=2 => 1 initial fire + 2 re-ticks = 3
    total advances, then the tick returns even though the final ring is still
    exhausted."""
    monkeypatch.setenv(ct.ENV_MAX_RETICKS, "2")
    state_path = tmp_path / "prospecting_geo_state.json"
    gr.save_state(gr.GeoRingState(current_ring=0, days_at_ring=0), state_path)
    monkeypatch.setattr(gr, "default_state_path", lambda: state_path)
    _stub_discovery(monkeypatch, fresh_count=0, held=40)

    summary = ct.run_prospecting_tick()
    reloaded = gr.load_state(state_path)
    # 3 fires total, each advancing the ring (or wrapping at the top).
    assert summary["retick_count"] == 2
    assert len(summary["retick_chain"]) == 3
    # Final ring index is bounded by top_ring_index (wrap protection) — just
    # assert we didn't overshoot into an impossible state.
    assert 0 <= reloaded.current_ring <= gr.top_ring_index()
