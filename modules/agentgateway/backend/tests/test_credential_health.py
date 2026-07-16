"""Unit tests for app.core.credential_health (read-only, no network, no key leak)."""

from __future__ import annotations

import json

import pytest

from app.core.credential_health import (
    scan_credentials,
    unhealthy_summary,
    _looks_like_placeholder,
    _classify,
)


class TestPlaceholderHeuristic:
    @pytest.mark.parametrize("val", [
        "sk-your-openai-api-key",   # example sample
        "sk-xxx",                   # example sample
        "sk-lf-...",                # example sample
        "__set_via_env_only__",     # example sample
        "sk-1234567890",            # sk- and len<=16 (13-char observed stub)
        "sk-xxxxxx",                # all-x body
    ])
    def test_flagged_as_placeholder(self, val):
        assert _looks_like_placeholder(val) is True

    @pytest.mark.parametrize("val", [
        "sk-proj-" + "a1b2c3" * 7,             # long realistic openai key
        "sk-or-v1-" + "9f3b2c" * 8,            # long realistic key
        "a1b2c3d4e5" * 5 + "z",                # GLM-style 51-char mixed alnum
        "pk-lf-" + "abcdef012345" * 3,         # long public key
    ])
    def test_real_keys_not_placeholder(self, val):
        assert _looks_like_placeholder(val) is False

    def test_empty_is_not_placeholder(self):
        # empty is "missing", classified separately, not placeholder
        assert _looks_like_placeholder("") is False
        assert _classify("") == "missing"
        assert _classify("   ") == "missing"


class TestClassify:
    def test_three_states(self):
        assert _classify(None) == "missing"
        assert _classify("sk-1234567890") == "placeholder"
        assert _classify("sk-proj-" + "z" * 40) == "ok"


class TestScanCredentials:
    def test_covers_all_providers_and_langfuse(self, monkeypatch):
        names = {c["name"] for c in scan_credentials()}
        for p in ("openai", "glm", "anthropic", "deepseek", "custom"):
            assert p in names
        assert "langfuse_secret" in names and "langfuse_public" in names

    def test_missing_and_placeholder_and_ok(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-1234567890")        # placeholder (13)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "")                   # missing
        monkeypatch.setenv("GLM_API_KEY", "a1b2c3d4e5" * 5 + "z")    # ok (real-shaped 51)
        by_name = {c["name"]: c["status"] for c in scan_credentials()}
        assert by_name["openai"] == "placeholder"
        assert by_name["deepseek"] == "missing"
        assert by_name["glm"] == "ok"

    def test_result_never_contains_key_value(self, monkeypatch):
        secret = "sk-proj-supersecretvalue" + "q" * 30
        monkeypatch.setenv("OPENAI_API_KEY", secret)
        blob = json.dumps(scan_credentials())
        assert secret not in blob
        # every entry has exactly name+status, no extra fields
        for c in scan_credentials():
            assert set(c.keys()) == {"name", "status"}

    def test_unhealthy_summary_lists_bad_only(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-1234567890")
        s = unhealthy_summary()
        assert "openai=placeholder" in s
        assert "sk-" not in s  # no key value
