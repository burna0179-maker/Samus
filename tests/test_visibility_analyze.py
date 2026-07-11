"""Tests for backend.visibility.analyze — pure mention/domain analysis."""

from __future__ import annotations

from backend.visibility.analyze import aggregate, analyze_answer, extract_domains


def test_extract_domains_dedup_and_www_strip():
    text = "See https://www.apollo.io/guide and http://clay.com, also https://apollo.io/x"
    assert extract_domains(text) == ["apollo.io", "clay.com"]


def test_extract_domains_empty():
    assert extract_domains("") == []
    assert extract_domains("no urls here") == []


def test_analyze_brand_cited_word_boundary():
    text = "Hustleforge is a strong option for autonomous prospecting."
    a = analyze_answer(text, ["Hustleforge", "Samus"], ["Apollo"])
    assert a["brand_cited"] is True
    assert a["competitor_hits"] == {}


def test_analyze_no_partial_match():
    # "Apollonian" must NOT count as a hit on "Apollo".
    text = "An Apollonian approach to growth."
    a = analyze_answer(text, ["Hustleforge"], ["Apollo"])
    assert a["brand_cited"] is False
    assert a["competitor_hits"] == {}


def test_analyze_competitor_counts():
    text = "Apollo and Clay are popular. Apollo leads. Sources: https://apollo.io"
    a = analyze_answer(text, ["Hustleforge"], ["Apollo", "Clay", "Outreach"])
    assert a["brand_cited"] is False
    assert a["competitor_hits"] == {"Apollo": 2, "Clay": 1}
    assert a["cited_domains"] == ["apollo.io"]


def test_aggregate_citation_rate_and_sov():
    analyses = [
        {
            "answered": True,
            "brand_cited": True,
            "competitor_hits": {"Apollo": 1},
            "cited_domains": ["apollo.io"],
        },
        {
            "answered": True,
            "brand_cited": False,
            "competitor_hits": {"Apollo": 2, "Clay": 1},
            "cited_domains": ["clay.com"],
        },
        {
            "answered": False,
            "brand_cited": False,
            "competitor_hits": {},
            "cited_domains": [],
        },  # unanswered, excluded
    ]
    agg = aggregate(analyses)
    assert agg["sample_n"] == 2  # only answered probes count
    assert agg["citation_rate"] == 0.5  # 1 of 2 answered cited the brand
    assert agg["brand_mentions"] == 1
    assert agg["competitor_mentions"] == 4  # 1 + 2 + 1
    assert agg["share_of_voice"] == round(1 / 5, 4)
    assert agg["competitor_breakdown"] == {"Apollo": 3, "Clay": 1}
    assert ("apollo.io", 1) in agg["top_cited_domains"]


def test_aggregate_empty():
    agg = aggregate([])
    assert agg["sample_n"] == 0
    assert agg["citation_rate"] == 0.0
    assert agg["share_of_voice"] == 0.0


def test_aggregate_no_voice_no_div_zero():
    analyses = [
        {"answered": True, "brand_cited": False, "competitor_hits": {}, "cited_domains": []}
    ]
    agg = aggregate(analyses)
    assert agg["share_of_voice"] == 0.0
    assert agg["citation_rate"] == 0.0
