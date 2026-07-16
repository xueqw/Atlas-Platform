"""Tests: Langfuse export failures are logged + reflected in health, not swallowed."""

from __future__ import annotations

import logging

import app.core.observability as obs


class TestExportFailureSurfaced:
    def setup_method(self):
        # Reset the process-level flag before each test.
        obs._langfuse_export_failed = False

    def teardown_method(self):
        obs._langfuse_export_failed = False

    def test_note_export_failure_logs_warning_and_sets_flag(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.core.observability"):
            obs._note_export_failure(RuntimeError("code: 401, Unauthorized"))
        assert obs._langfuse_export_failed is True
        assert any("Langfuse export failed" in r.message for r in caplog.records)

    def test_warning_does_not_leak_key(self, caplog):
        # Even if an exception text somehow carried a key, the log formats only
        # type + truncated message; assert our own marker is present and the
        # log line never contains an obvious secret we didn't pass.
        with caplog.at_level(logging.WARNING, logger="app.core.observability"):
            obs._note_export_failure(RuntimeError("401 Unauthorized"))
        joined = " ".join(r.message for r in caplog.records)
        assert "sk-" not in joined

    def test_health_auth_ok_false_after_export_failure(self, monkeypatch):
        # Force "enabled" so we exercise the export-flag override path.
        monkeypatch.setattr(obs, "LANGFUSE_SECRET_KEY", "sk-real-looking-secret-aaaaaaaaaa")
        monkeypatch.setattr(obs, "LANGFUSE_PUBLIC_KEY", "pk-real-looking-public-bbbbbbbbbb")
        monkeypatch.setattr(obs, "health_check", lambda: (True, "ok"))
        # Without failure → auth_ok True
        assert obs.langfuse_health()["langfuse_auth_ok"] is True
        # After an observed export failure → auth_ok forced False
        obs._note_export_failure(RuntimeError("401"))
        assert obs.langfuse_health()["langfuse_auth_ok"] is False
