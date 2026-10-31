"""Automation and guardrail module tests (no external services required)."""

from __future__ import annotations

from unittest.mock import patch

import httpx

from app.automation.browser import BrowserAutomationEngine
from app.automation.scraper import LeadScraper
from app.security.guardrails import SandboxPolicy


def test_browser_reports_missing_playwright(monkeypatch):
    monkeypatch.setattr("app.automation.browser.BrowserAutomationEngine.available", lambda self: False)
    engine = BrowserAutomationEngine()
    result = engine.scrape_page("https://example.com")
    assert result["ok"] is False
    assert "Playwright" in result["error"]


def test_lead_scraper_extracts_contacts(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = "<title>Acme Cafe</title><p>contact@acme.test +212 6 12 34 56 78</p>"

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None, follow_redirects=True: FakeResponse())
    result = LeadScraper(timeout=5).fetch_leads_from_urls(["https://example.test"])
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["leads"][0]["emails"] == ["contact@acme.test"]
    assert result["leads"][0]["title"] == "Acme Cafe"


def test_lead_scraper_records_errors(monkeypatch):
    def fail(url, timeout=None, follow_redirects=True):
        raise RuntimeError("boom")

    monkeypatch.setattr(httpx, "get", fail)
    result = LeadScraper(timeout=5).fetch_leads_from_urls(["https://example.test"])
    assert result["ok"] is False
    assert result["errors"][0]["error"] == "boom"


def test_sandbox_blocks_protected_path():
    policy = SandboxPolicy(workspace_roots=[])
    decision = policy.check_path(r"C:\Windows\System32\drivers\etc\hosts", "write")
    assert decision.allowed is False
    assert decision.code == "protected"


def test_sandbox_blocks_dangerous_command():
    policy = SandboxPolicy(workspace_roots=[])
    decision = policy.check_command("format C:")
    assert decision.allowed is False
    assert decision.risk == "critical"