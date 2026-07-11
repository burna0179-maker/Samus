"""Morning outreach batch: filtering, suppression, cap, dry-run, CAN-SPAM, send.

Exercises backend.outreach.morning_batch, which reuses campaign.build_messages +
common.email_backend.send_email — so these tests assert the batch's OWN logic
(row filtering, stake framing, suppression wiring, cap, dry-run safety, and the
live send fan-out) without re-testing the reused compose/send internals.

The Codex registry is loaded session-wide by conftest, so compose_body clears
the VR-G8 / stake gate for the public_registry warmth signal the batch sets.
"""

from __future__ import annotations

from backend.outreach import morning_batch as mb
from backend.outreach.campaign import CampaignConfig


def _row(email: str, score: str, company: str = "Acme HVAC", **extra) -> dict:
    base = {
        "prospect_id": "pr_" + company.lower().replace(" ", "_"),
        "company_name": company,
        "owner_email": email,
        "owner_name": "",
        "owner_title": "Owner",
        "industry": "hvac contractor",
        "city": "Yuba City",
        "state": "CA",
        "phone": "(530) 555-0100",
        "lead_score": score,
        "seo_score": "80",
        "security_grade": "D",
        "review_count": "42",
    }
    base.update(extra)
    return base


def _cfg(max_send: int = 10) -> CampaignConfig:
    return CampaignConfig(
        template_id="morning_batch_places_v1",
        subject="Quick question about {company}",
        sender_postal_address="2290 Cheim Boulevard, Marysville, CA 95901-3560",
        unsubscribe_url="https://hustleforge.tech/unsubscribe",
        max_send=max_send,
        require_verified_email=True,
    )


def _write_csv(tmp_path, rows: list[dict]):
    import csv

    path = tmp_path / "call_list.csv"
    fields = sorted({k for r in rows for k in r})
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return str(path)


# --- filtering ---------------------------------------------------------------


def test_filter_keeps_only_score_ge_min_with_owner_email(tmp_path):
    rows = [
        _row("keep@a.test", "70", "A"),  # exactly at threshold -> keep
        _row("keep2@b.test", "95", "B"),  # above -> keep
        _row("drop-low@c.test", "69", "C"),  # below -> drop
        _row("", "88", "D"),  # no email -> drop
    ]
    csv_path = _write_csv(tmp_path, rows)
    kept = mb.load_hot_rows(csv_path, min_score=70)
    emails = {r["owner_email"] for r in kept}
    assert emails == {"keep@a.test", "keep2@b.test"}


def test_filter_handles_blank_and_nonnumeric_score(tmp_path):
    rows = [
        _row("blank@a.test", "", "A"),  # blank score -> drop
        _row("nan@b.test", "high", "B"),  # non-numeric -> drop
        _row("ok@c.test", "72", "C"),  # valid -> keep
    ]
    csv_path = _write_csv(tmp_path, rows)
    kept = mb.load_hot_rows(csv_path, min_score=70)
    assert [r["owner_email"] for r in kept] == ["ok@c.test"]


# --- suppression -------------------------------------------------------------


def test_suppression_skips_already_emailed():
    rows = [_row("new@a.test", "80", "A"), _row("old@b.test", "80", "B")]
    result = mb.build_batch(
        rows,
        _cfg(),
        already_sent={"old@b.test"},
    )
    tos = {m.to for m in result.messages}
    assert tos == {"new@a.test"}
    assert result.suppressed == 1
    assert result.built == 1


# --- cap ---------------------------------------------------------------------


def test_cap_respected():
    rows = [_row(f"p{i}@x.test", "80", f"Co{i}") for i in range(5)]
    result = mb.build_batch(rows, _cfg(max_send=2), already_sent=set())
    assert result.built == 2
    assert result.capped == 3
    assert len(result.messages) == 2


# --- CAN-SPAM footer ---------------------------------------------------------


def test_canspam_footer_present_in_composed_body():
    rows = [_row("footer@a.test", "80", "Acme HVAC")]
    result = mb.build_batch(rows, _cfg(), already_sent=set())
    body = result.messages[0].body
    assert "2290 Cheim Boulevard, Marysville, CA 95901-3560" in body
    assert "Unsubscribe: https://hustleforge.tech/unsubscribe" in body


def test_stake_sentence_rendered_at_top_and_names_company():
    rows = [_row("stake@a.test", "80", "Acme HVAC")]
    result = mb.build_batch(rows, _cfg(), already_sent=set())
    body = result.messages[0].body
    first_line = body.lstrip().splitlines()[0]
    assert first_line.startswith("Alex flagged Acme HVAC because")


# --- dry-run sends nothing ---------------------------------------------------


def test_dry_run_sends_nothing_and_writes_nothing(tmp_path, monkeypatch):
    rows = [_row("dry@a.test", "80", "A"), _row("dry2@b.test", "80", "B")]
    csv_path = _write_csv(tmp_path, rows)
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(artifact_root))

    sent_calls = []
    monkeypatch.setattr(
        "backend.common.email_backend.send_email",
        lambda *a, **k: sent_calls.append((a, k)),
    )

    rc = mb.main(["--csv", csv_path, "--dry-run"])
    assert rc == 0
    assert sent_calls == []  # nothing sent
    supp = artifact_root / "outreach" / "emailed_emails.txt"
    # dry-run must not create/append the suppression file.
    assert not supp.exists() or supp.read_text(encoding="utf-8").strip() == ""


# --- live path calls send_email once per kept recipient ----------------------


def test_live_calls_send_email_once_per_recipient(tmp_path, monkeypatch):
    rows = [
        _row("live1@a.test", "80", "A"),
        _row("live2@b.test", "80", "B"),
        _row("live3@c.test", "80", "C"),
    ]
    csv_path = _write_csv(tmp_path, rows)
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(artifact_root))
    # Isolate the audit ledger to a tmp path so the live path doesn't touch
    # the real evidence tree.
    monkeypatch.setenv(
        "SAMUS_AUDIT_LEDGER_PATH",
        str(artifact_root / "audit_test.jsonl"),
    )
    from backend.common import audit_ledger

    audit_ledger.reset_default_ledger()

    calls = []

    def _fake_send(**kwargs):
        calls.append(kwargs["to"])
        return {"message_id": "mid-" + kwargs["to"], "ts": "2026-07-02T08:00:00Z"}

    monkeypatch.setattr(
        "backend.common.email_backend.send_email",
        _fake_send,
    )

    rc = mb.main(["--csv", csv_path])
    assert rc == 0
    assert sorted(calls) == ["live1@a.test", "live2@b.test", "live3@c.test"]

    # Every sent address is appended to the suppression file so a re-run skips it.
    supp = (artifact_root / "outreach" / "emailed_emails.txt").read_text(
        encoding="utf-8",
    )
    for email in calls:
        assert email in supp

    audit_ledger.reset_default_ledger()


def test_live_rerun_does_not_double_send(tmp_path, monkeypatch):
    rows = [_row("dup@a.test", "80", "A")]
    csv_path = _write_csv(tmp_path, rows)
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv(
        "SAMUS_AUDIT_LEDGER_PATH",
        str(artifact_root / "audit_test.jsonl"),
    )
    from backend.common import audit_ledger

    audit_ledger.reset_default_ledger()

    calls = []
    monkeypatch.setattr(
        "backend.common.email_backend.send_email",
        lambda **k: (calls.append(k["to"]), {"message_id": "m", "ts": "2026-07-02T08:00:00Z"})[1],
    )

    assert mb.main(["--csv", csv_path]) == 0
    assert mb.main(["--csv", csv_path]) == 0  # second run: already suppressed
    assert calls == ["dup@a.test"]  # sent exactly once across both runs

    audit_ledger.reset_default_ledger()
