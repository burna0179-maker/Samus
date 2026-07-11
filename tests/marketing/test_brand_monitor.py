"""Tests for backend.marketing.brand_monitor."""

from __future__ import annotations

import json
from pathlib import Path


from backend.marketing.brand_monitor import (
    detect_ai_referrer,
    get_mention_stats,
    log_ai_referral,
)


# ---------------------------------------------------------------------------
# detect_ai_referrer
# ---------------------------------------------------------------------------


class TestDetectAiReferrer:
    def test_perplexity(self):
        assert detect_ai_referrer("https://www.perplexity.ai/search?q=foo") == "perplexity"

    def test_chatgpt_dot_com(self):
        assert detect_ai_referrer("https://chatgpt.com/c/abc") == "chatgpt"

    def test_chat_openai_com(self):
        assert detect_ai_referrer("https://chat.openai.com/c/abc") == "chatgpt"

    def test_claude(self):
        assert detect_ai_referrer("https://claude.ai/chat/xyz") == "claude"

    def test_bing_copilot(self):
        assert detect_ai_referrer("https://www.bing.com/chat?q=test") == "bing_copilot"

    def test_copilot_microsoft(self):
        assert detect_ai_referrer("https://copilot.microsoft.com/") == "bing_copilot"

    def test_you(self):
        assert detect_ai_referrer("https://you.com/search?q=ai") == "you"

    def test_gemini(self):
        assert detect_ai_referrer("https://gemini.google.com/app") == "gemini"

    def test_phind(self):
        assert detect_ai_referrer("https://www.phind.com/search") == "phind"

    def test_unknown_returns_none(self):
        assert detect_ai_referrer("https://google.com/search?q=foo") is None

    def test_empty_returns_none(self):
        assert detect_ai_referrer("") is None

    def test_none_input_returns_none(self):
        # Defensively handle None even though type hint says str.
        assert detect_ai_referrer(None) is None  # type: ignore[arg-type]

    def test_case_insensitive(self):
        assert detect_ai_referrer("https://PERPLEXITY.AI/search") == "perplexity"


# ---------------------------------------------------------------------------
# log_ai_referral + get_mention_stats
# ---------------------------------------------------------------------------


class TestLogAndStats:
    def test_round_trip(self, tmp_path: Path):
        ledger = tmp_path / "ai_mentions.jsonl"
        log_ai_referral(
            "perplexity",
            "AI tools for business",
            "https://hustleforge.tech/blog/ai",
            ledger_path=ledger,
        )
        log_ai_referral(
            "chatgpt", "automate small business", "https://hustleforge.tech/", ledger_path=ledger
        )

        lines = ledger.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        record = json.loads(lines[0])
        assert record["source"] == "perplexity"
        assert record["query"] == "AI tools for business"
        assert record["url"] == "https://hustleforge.tech/blog/ai"
        assert "ts" in record

    def test_stats_totals(self, tmp_path: Path):
        ledger = tmp_path / "mentions.jsonl"
        log_ai_referral("perplexity", "q1", "https://hustleforge.tech/a", ledger_path=ledger)
        log_ai_referral("perplexity", "q2", "https://hustleforge.tech/b", ledger_path=ledger)
        log_ai_referral("chatgpt", "q3", "https://hustleforge.tech/a", ledger_path=ledger)

        stats = get_mention_stats(days=90, ledger_path=ledger)
        assert stats["total"] == 3
        assert stats["by_platform"]["perplexity"] == 2
        assert stats["by_platform"]["chatgpt"] == 1
        assert stats["by_page"]["https://hustleforge.tech/a"] == 2

    def test_empty_ledger(self, tmp_path: Path):
        ledger = tmp_path / "empty.jsonl"
        # File does not exist yet.
        stats = get_mention_stats(days=30, ledger_path=ledger)
        assert stats["total"] == 0
        assert stats["by_platform"] == {}

    def test_missing_ledger_file(self, tmp_path: Path):
        ledger = tmp_path / "nonexistent.jsonl"
        stats = get_mention_stats(days=30, ledger_path=ledger)
        assert stats["total"] == 0

    def test_log_io_error_doesnt_raise(self, tmp_path: Path):
        # Point ledger at a path whose parent does not exist and cannot be
        # created — simulate write failure (use a file where a dir is expected).
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        ledger = blocker / "ai_mentions.jsonl"
        # Should log a warning but not raise.
        log_ai_referral("perplexity", "q", "https://hustleforge.tech", ledger_path=ledger)

    def test_top_queries(self, tmp_path: Path):
        ledger = tmp_path / "qs.jsonl"
        for _ in range(3):
            log_ai_referral("claude", "AI agent", "https://hustleforge.tech", ledger_path=ledger)
        log_ai_referral("claude", "other", "https://hustleforge.tech", ledger_path=ledger)

        stats = get_mention_stats(days=90, ledger_path=ledger)
        top = dict(stats["top_queries"])
        assert top["AI agent"] == 3
        assert top["other"] == 1

    def test_window_excludes_old_events(self, tmp_path: Path):
        """Events with timestamps older than the window should not be counted."""
        ledger = tmp_path / "old.jsonl"
        # Write a record with an ancient timestamp manually.
        old_record = {
            "source": "perplexity",
            "query": "q",
            "url": "https://hustleforge.tech",
            "ts": "2020-01-01T00:00:00Z",
        }
        ledger.write_text(json.dumps(old_record) + "\n", encoding="utf-8")
        stats = get_mention_stats(days=30, ledger_path=ledger)
        assert stats["total"] == 0
