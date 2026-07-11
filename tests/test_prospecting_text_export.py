"""Morning-call-list text export tests."""

from __future__ import annotations

from datetime import date


def _prospect(**overrides):
    from backend.prospecting.models import ProspectRecord

    base = dict(
        prospect_id="pr_a",
        company_name="Acme Plumbing",
        phone="(555) 111-2222",
        website_url="https://acme.example/",
        city="Yuba City",
        state="CA",
        zipcode="95993",
        industry="finance",
        call_priority="low",
        lead_score=45,
        seo_score=39,
        callsheet_issues=(
            "Inconsistent name/address/phone across directories; "
            "No city or neighborhood landing pages; "
            "Mobile page speed below 50 — hurts local pack ranking"
        ),
        callsheet_offer="Local SEO Starter Audit + 30-day action plan",
        callsheet_pitch="A quick audit usually reveals 3–5 issues that are costing you search visibility.",
        callsheet_opener=(
            "Hi, this is [NAME] calling from HustleForge. We help local finance "
            "businesses in 95993 get more visibility on Google. Do you have a minute?"
        ),
        callsheet_voicemail=(
            "Hi, this is [NAME] from HustleForge. Give me a call at [PHONE] or "
            "visit hustleforge.tech for a free report."
        ),
        callsheet_objections=(
            "We already do SEO — 'Great, who manages it?' | "
            "Not interested — 'Totally fair.' | "
            "Too expensive — 'Our 48-Hour Rescue starts at $500 flat.'"
        ),
    )
    base.update(overrides)
    return ProspectRecord(**base)


def test_header_and_count():
    from backend.prospecting.text_export import render_morning_call_list

    out = render_morning_call_list([_prospect()], run_date=date(2026, 5, 7))
    assert out.startswith("MORNING CALL LIST — Thursday, May 07, 2026")
    assert "1 prospects ready | Sorted by priority + score" in out
    assert "=" * 60 in out


def test_block_layout_for_one_prospect():
    from backend.prospecting.text_export import render_morning_call_list

    out = render_morning_call_list([_prospect()], run_date=date(2026, 5, 7))

    assert "🟢 #1  Acme Plumbing" in out
    assert "   📞  (555) 111-2222" in out
    assert "   📍  Yuba City, CA  |  finance" in out
    assert "   🌐  https://acme.example/" in out
    assert "   📊  Lead: 45/100  |  SEO: 39/100" in out

    # WHY WE'RE CALLING — each issue rendered as its own ⚠ bullet
    assert "   WHY WE'RE CALLING (what we found):" in out
    assert "   ⚠  Inconsistent name/address/phone across directories" in out
    assert "   ⚠  No city or neighborhood landing pages" in out

    # HOW WE CAN HELP — pain hypothesis + offer + pitch (offer/pitch off the
    # record's callsheet fields, pain derived per prospect at render time).
    assert "   🧭  Likely pain:" in out
    assert "   💼  Offer:  Local SEO Starter Audit + 30-day action plan" in out
    assert "   🎯  Pitch:  A quick audit usually reveals" in out

    # WHAT TO LISTEN FOR — the Vapi-style qualifying questions
    assert "   WHAT TO LISTEN FOR (qualify — one question at a time):" in out
    assert out.count("   ❔  ") >= 2

    # CALL SCRIPT — opener / voicemail / objection handlers
    assert "   CALL SCRIPT:" in out
    assert "   OPENER:" in out
    assert "Hi, this is [NAME] calling from HustleForge" in out
    assert "   VOICEMAIL:" in out
    assert "   OBJECTION HANDLERS:" in out
    assert "   • We already do SEO" in out

    assert "-" * 60 in out


def test_priority_emoji_mapping():
    from backend.prospecting.text_export import render_morning_call_list

    out = render_morning_call_list(
        [
            _prospect(prospect_id="pr_low", call_priority="low", lead_score=40),
            _prospect(prospect_id="pr_warm", call_priority="warm", lead_score=60),
            _prospect(prospect_id="pr_hot", call_priority="hot", lead_score=88),
        ],
        run_date=date(2026, 5, 7),
    )
    # Sort puts hot first → 🔴 #1, warm → 🟡 #2, low → 🟢 #3
    lines = out.splitlines()
    idx_hot = next(i for i, line in enumerate(lines) if line.startswith("🔴 #1"))
    idx_warm = next(i for i, line in enumerate(lines) if line.startswith("🟡 #2"))
    idx_low = next(i for i, line in enumerate(lines) if line.startswith("🟢 #3"))
    assert idx_hot < idx_warm < idx_low


def test_sort_within_priority_lead_desc_then_seo_asc():
    from backend.prospecting.text_export import render_morning_call_list

    # Three low-priority prospects with same lead but ascending SEO score
    out = render_morning_call_list(
        [
            _prospect(prospect_id="pr_c", company_name="C Co", seo_score=70),
            _prospect(prospect_id="pr_a", company_name="A Co", seo_score=39),
            _prospect(prospect_id="pr_b", company_name="B Co", seo_score=49),
        ],
        run_date=date(2026, 5, 7),
    )
    # Worst SEO (39) should land first inside the low tier — most pitch material.
    pos_a = out.find("A Co")
    pos_b = out.find("B Co")
    pos_c = out.find("C Co")
    assert pos_a < pos_b < pos_c


def test_sort_security_grade_tiebreaker():
    """On a lead-score tie, the worse security grade sorts first (more trust
    problems to pitch); an ungraded prospect sorts last."""
    from backend.prospecting.text_export import render_morning_call_list

    out = render_morning_call_list(
        [
            _prospect(
                prospect_id="pr_a",
                company_name="A Co",
                lead_score=60,
                seo_score=50,
                security_grade="A",
            ),
            _prospect(
                prospect_id="pr_f",
                company_name="F Co",
                lead_score=60,
                seo_score=50,
                security_grade="F",
            ),
            _prospect(
                prospect_id="pr_n",
                company_name="N Co",
                lead_score=60,
                seo_score=50,
                security_grade="",
            ),
            _prospect(
                prospect_id="pr_c",
                company_name="C Co",
                lead_score=60,
                seo_score=50,
                security_grade="C",
            ),
        ],
        run_date=date(2026, 5, 7),
    )
    # Worse grade first (F < C < A), ungraded last.
    assert out.find("F Co") < out.find("C Co") < out.find("A Co") < out.find("N Co")


def test_security_grade_renders_in_score_line():
    """A graded prospect shows its grade on the score line; ungraded omits it."""
    from backend.prospecting.text_export import render_morning_call_list

    graded = render_morning_call_list([_prospect(security_grade="D")], run_date=date(2026, 5, 7))
    assert "Security: D" in graded
    ungraded = render_morning_call_list([_prospect(security_grade="")], run_date=date(2026, 5, 7))
    assert "Security:" not in ungraded


def test_write_morning_call_list_writes_file(tmp_path, monkeypatch):
    # Patch both the module attribute and the env var (origin used setattr,
    # HEAD used setenv; both are retained for full coverage).
    import backend.common.storage as storage

    monkeypatch.setattr(storage, "_ROOT", tmp_path)
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))

    from backend.prospecting.text_export import write_morning_call_list

    path = write_morning_call_list([_prospect()], run_date=date(2026, 5, 7))
    assert path.exists()
    assert path.name == "morning_call_list_2026-05-07.txt"
    assert path.parent.name == "daily_calls"
    body = path.read_text(encoding="utf-8")
    assert body.startswith("MORNING CALL LIST — Thursday, May 07, 2026")
    assert "Acme Plumbing" in body


def test_missing_callsheet_fields_render_placeholder():
    from backend.prospecting.text_export import render_morning_call_list

    out = render_morning_call_list(
        [_prospect(callsheet_issues="", callsheet_objections="")],
        run_date=date(2026, 5, 7),
    )
    assert "(no audit issues recorded)" in out
    assert "(none recorded)" in out


def test_website_down_renders_alert_line():
    """A non-resolving domain gets a loud 🚨 line under the URL."""
    from backend.prospecting.text_export import render_morning_call_list

    out = render_morning_call_list(
        [_prospect(website_status="domain_unresolved")],
        run_date=date(2026, 5, 7),
    )
    assert "   🚨  WEBSITE DOWN" in out
    # The alert sits between the URL line and the score line.
    lines = out.splitlines()
    url_idx = next(i for i, ln in enumerate(lines) if ln.startswith("   🌐"))
    assert lines[url_idx + 1].startswith("   🚨")
    assert lines[url_idx + 2].startswith("   📊")


def test_live_website_renders_no_alert():
    from backend.prospecting.text_export import render_morning_call_list

    out = render_morning_call_list(
        [_prospect(website_status="live")],
        run_date=date(2026, 5, 7),
    )
    assert "🚨" not in out


def test_unchecked_website_status_renders_no_alert():
    """website_status="" (enrichment disabled / not run) → no alert line."""
    from backend.prospecting.text_export import render_morning_call_list

    out = render_morning_call_list(
        [_prospect(website_status="")],
        run_date=date(2026, 5, 7),
    )
    assert "🚨" not in out


def test_social_only_website_renders_alert():
    from backend.prospecting.text_export import render_morning_call_list

    out = render_morning_call_list(
        [_prospect(website_status="social_only")],
        run_date=date(2026, 5, 7),
    )
    assert "   🚨  NO REAL WEBSITE" in out


def test_gone_website_renders_alert():
    """An HTTP 410 site is a genuine hook — it gets the loud 🚨 line."""
    from backend.prospecting.text_export import render_morning_call_list

    out = render_morning_call_list([_prospect(website_status="gone")], run_date=date(2026, 5, 7))
    assert "   🚨  WEBSITE GONE" in out


def test_access_blocked_renders_info_note_not_alarm():
    """A WAF-blocked crawl is NOT a broken site — quiet ℹ note, never a 🚨."""
    from backend.prospecting.text_export import render_morning_call_list

    out = render_morning_call_list(
        [_prospect(website_status="access_blocked")], run_date=date(2026, 5, 7)
    )
    assert "🚨" not in out
    assert "   ℹ  Site blocks automated audits" in out
    assert "don't pitch this as a broken site" in out


def test_how_we_can_help_block_is_prospect_specific():
    """The HOW block renders a derived pain hypothesis + a pivot when the
    prospect has a secondary gap, and the copy varies with the signals."""
    from backend.prospecting.text_export import render_morning_call_list

    # A prospect with two real gaps: a failing security grade AND weak SEO.
    out = render_morning_call_list(
        [
            _prospect(
                website_status="live",
                seo_score=20,
                security_grade="F",
                callsheet_offer="",
                callsheet_pitch="",
            )
        ],
        run_date=date(2026, 5, 7),
    )
    assert "   🧭  Likely pain:" in out
    # Two gaps → a pivot line is rendered.
    assert "If that misses, pivot:" in out
    # The qualifying questions block is present.
    assert "   ❔  " in out


def test_qualify_prompts_differ_by_dominant_gap():
    """A no-website prospect and a thin-reviews prospect surface different
    qualifying questions — the Vapi heuristics adapt to the prospect."""
    from backend.prospecting.text_export import render_morning_call_list

    no_site = render_morning_call_list(
        [_prospect(prospect_id="pr_ns", website_status="no_website")],
        run_date=date(2026, 5, 7),
    )
    reviews = render_morning_call_list(
        [
            _prospect(
                prospect_id="pr_rv",
                website_status="live",
                seo_score=80,
                review_rating="3.1",
                review_count="4",
            )
        ],
        run_date=date(2026, 5, 7),
    )
    assert "what do they find today?" in no_site
    assert "happy customers for a review" in reviews
